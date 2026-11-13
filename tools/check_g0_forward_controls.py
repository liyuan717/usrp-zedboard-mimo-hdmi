#!/usr/bin/env python3
"""Compile real forward-control RTL faults; require functional witnesses.
No RF, board, native speed or production source-clock acceptance.
"""
import argparse,hashlib,json,shutil,subprocess
from pathlib import Path
from dma_batch_stream_probe import decode_batch
from workspace_storage_guard import check,GIB

def main(a):
 out=a.output.resolve();out.mkdir(exist_ok=False);ref=a.reference.resolve()
 before=check('before-forward-control-faults');(out/'disk-before.json').write_text(json.dumps(before,indent=2)+'\n')
 assert before['disk']['available_bytes']>=5*GIB
 proof=json.loads((ref/'receipt.json').read_text());assert proof['completed']
 names=list(proof['source_sha256']);bp=a.vivado_bin.resolve()
 for n,h in proof['source_sha256'].items():assert hashlib.sha256((ref/n).read_bytes()).hexdigest()==h
 faults=[
 ('CRC_bypass','link_packet_cost_probe.sv','if({in_data,received_crc[31:8]}!=~crc)result_error<=1;',"if(1'b0)result_error<=1;",'Fatal: untrusted timebase promoted'),
 ('foreign_SESSION','link_fec_ingress_cost_probe.sv','same_session && !retired;',"1'b1 && !retired;",'Fatal: untrusted timebase promoted'),
 ('ignore_reference_valid','link_fec_ingress_cost_probe.sv','if(timebase_ok && meta_reference_valid)','if(timebase_ok)','Fatal: untrusted timebase promoted'),
 ('consume_both_SOURCE_slots','source_record_bridge_cost_probe.sv','&& !control_live)',')','Fatal: metadata consumed reserved SOURCE slot'),
 ('SESSION_aliases_SOURCE_duplicate','source_record_bridge_cost_probe.sv','!slot_control[0] &&','', 'Fatal: metadata consumed reserved SOURCE slot'),
 ('VRT_resets_FEC','link_fec_ingress_cost_probe.sv','wire core_rst=rst || session_switch;','wire core_rst=rst || session_switch || (state==APPLY && timebase_packet && timebase_ok);','Fatal: metadata consumed reserved SOURCE slot'),
 ('live_reference_in_record','source_record_bridge_cost_probe.sv','slot_position[first_slot][95:32]','control_position[95:32]','independent_SESSION_sample_coordinate_mismatch')]
 rows=[]
 try:
  for label,target,old,new,witness in faults:
   d=out/label;d.mkdir()
   for n in [*names,'tb.sv','generator.hex','inverse.hex','bytes.hex','offsets.hex']:shutil.copyfile(ref/n,d/n)
   source=(d/target).read_text()
   if label=='live_reference_in_record' and 'control_position_saved[95:32]' in source:
    old='control_position_saved[95:32]'
   assert source.count(old)==1,(label,source.count(old))
   (d/target).write_text(source.replace(old,new,1))
   def run(step,args):
    with (d/(step+'.log')).open('w') as f:subprocess.run(args,cwd=d,stdout=f,stderr=subprocess.STDOUT,check=True)
   run('compile',[str(bp/'xvlog'),'--sv',*names,'tb.sv'])
   run('elaborate',[str(bp/'xelab'),'tb','-s','forward_control_fault_tb','-mt','off'])
   # xsim may return0 on $fatal, so inspect the actual functional witness.
   run('simulate',[str(bp/'xsim'),'forward_control_fault_tb','-runall'])
   text=(d/'simulate.log').read_text()
   if label=='live_reference_in_record':
    assert 'G0_FORWARD_CONTROL_PASS' in text and 'Fatal:' not in text
    raw=b''.join(int(s.split()[0],16).to_bytes(8,'little') for s in (d/'batches.txt').read_text().splitlines())
    records,_=decode_batch(raw,0)
    import struct
    coords=[struct.unpack_from('<Q',r,32)[0] for r in records if struct.unpack_from('<H',r,12)[0]==3]
    assert coords!=[0x0007123456789abc]*3,(label,coords)
    actual=witness+': '+repr(coords)
   else:
    actual=next((s for s in text.splitlines() if s.startswith(witness)),None)
    assert actual and 'G0_FORWARD_CONTROL_PASS' not in text,(label,text[-1600:])
   rows.append(dict(name=label,compiled=True,functional_witness=actual))
   print(json.dumps(rows[-1]),flush=True)
  (out/'receipt.json').write_text(json.dumps(dict(completed=True,reference=str(ref),
   compiled_functional_faults=rows,source_sha256=proof['source_sha256'],
   RF_started=False,full_G0_passed=False),indent=2)+'\n')
 finally:(out/'disk-after.json').write_text(json.dumps(check('after-forward-control-faults'),indent=2)+'\n')

if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__)
 for n in ['reference','output','vivado-bin']:p.add_argument('--'+n,type=Path,required=True)
 main(p.parse_args())
