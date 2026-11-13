#!/usr/bin/env python3
"""Compiled packet/CRC/stream-ownership mutations need specific positive oracles.
Compilation errors and global timeouts do not count as detection.
"""
import argparse,hashlib,json,os,shutil,subprocess
from pathlib import Path
from workspace_storage_guard import check as disk_check,GIB

def main(a):
 project=Path(__file__).resolve().parents[1];out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);bp=a.vivado_bin.resolve()
 before=disk_check('before-link-controls');(out/'disk-before.json').write_text(json.dumps(before,indent=2)+'\n')
 if before['disk']['available_bytes']<5*GIB:raise SystemExit('Under5GiB: handle storage first.')
 try:
  refs={'packet':a.packet_reference.resolve(),'frame':a.frame_reference.resolve()};proof={k:json.loads((v/'receipt.json').read_text()) for k,v in refs.items()}
  for k,r in proof.items():
   assert r['simulation'].startswith('G0_LINK_PACKET_PASS' if k=='packet' else 'G0_PAYLOAD_LINK_PASS')
   for n,h in r['source_sha256'].items():assert hashlib.sha256((project/'sim/g0'/n).read_bytes()).hexdigest()==h
  changes=[
   ('CRC_bypass','frame','link_packet_cost_probe.sv',[("if({in_data,received_crc[31:8]}!=~crc)","if(1'b0)",1)],'Fatal: payload link metadata/CRC oracle'),
   ('wrong_CRC_init','packet','link_packet_cost_probe.sv',[("crc<=32'hffffffff;","crc<=32'd0;",2)],'Fatal: link CRC/bounds oracle'),
   ('ignore_declared_payload_length','packet','link_packet_cost_probe.sv',[(' || header[255:240]!=payload_size','',1)],'Fatal: link CRC/bounds oracle'),
   ('payload_before_CRC','packet','link_packet_cost_probe.sv',[('assign out_valid=!rst && !cancel && state==SEND;','assign out_valid=!rst && !cancel && (state==SEND || state==COLLECT);',1)],'Fatal: link payload oracle'),
   ('wrong_QPSK_bit_order','frame','payload_link_cost_probe.sv',[('in_decision[0],in_decision[1]','in_decision[1],in_decision[0]',2)],'Fatal: payload link metadata/CRC oracle'),
   ('TX_streams_swapped','frame','payload_link_cost_probe.sv',[('.in_data(t==0?pair0:pair1)','.in_data(t==0?pair1:pair0)',1)],'Fatal: payload link metadata/CRC oracle'),
   ('packet_lock_overwritten','frame','payload_link_cost_probe.sv',[('if(!arb_locked && |rv)','if(|rv)',1)],'Fatal: payload link'),
   ('success_before_packet_drain','frame','payload_link_cost_probe.sv',[('symbols==17536 && bytes_sent==4384 && !pair_pending && slot0==8 && slot1==8 && !arb_locked && engine_active==0','symbols==17536',1)],'Fatal: payload link full frame census/service'),
   ('generation_high_bits_unchecked','frame','payload_link_cost_probe.sv',[('in_generation!=generation || ','',1)],'Fatal: payload link framing identity fault'),
  ];rows=[]
  for label,kind,target,edits,witness in changes:
   ref=refs[kind];d=out/label;d.mkdir();sources=proof[kind]['source_sha256']
   for p in [*ref.glob('*.hex'),*ref.glob('*.txt')]:os.link(p,d/p.name)
   for n in sources:shutil.copyfile(ref/n,d/n)
   shutil.copyfile(ref/'tb.sv',d/'tb.sv');p=d/target;s=p.read_text()
   for old,new,count in edits:assert s.count(old)==count,(label,old,s.count(old));s=s.replace(old,new)
   p.write_text(s)
   def run(step,args):
    with (d/(step+'.log')).open('w') as f:subprocess.run(args,cwd=d,stdout=f,stderr=subprocess.STDOUT,check=True)
   run('compile',[str(bp/'xvlog'),'--sv',*sources,'tb.sv']);run('elaborate',[str(bp/'xelab'),'tb','-s','link_control_tb','-mt','off'])
   try:run('simulate',[str(bp/'xsim'),'link_control_tb','-runall'])
   except subprocess.CalledProcessError:pass
   log=(d/'simulate.log').read_text();line=next((s for s in log.splitlines() if s.startswith(witness)),None)
   assert line and 'timeout' not in line.lower() and 'G0_PAYLOAD_LINK_PASS' not in log and 'G0_LINK_PACKET_PASS' not in log,(label,log[-4000:])
   row={'name':label,'reference':kind,'detected_by':line};rows.append(row);print(json.dumps(row),flush=True)
  (out/'receipt.json').write_text(json.dumps({'scope':__doc__,'negative_controls':rows,'source_sha256':proof['frame']['source_sha256'],'g0_passed':False},indent=2)+'\n')
 finally:(out/'disk-after.json').write_text(json.dumps(disk_check('after-link-controls'),indent=2)+'\n')

if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__)
 for name in ('output','packet-reference','frame-reference','vivado-bin'):p.add_argument('--'+name,type=Path,required=True)
 main(p.parse_args())
