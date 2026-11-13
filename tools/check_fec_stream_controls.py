#!/usr/bin/env python3
"""Fixed8 FEC compiled checks for commit/early output/dedup/queue/deadline.
Specific functional assertions must detect each mutant; timeout is not evidence.
"""
import argparse,hashlib,json,os,shutil,subprocess
from pathlib import Path
from workspace_storage_guard import check as disk_check,GIB

def main(a):
 project=Path(__file__).resolve().parents[1];out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);ref=a.reference.resolve();bp=a.vivado_bin.resolve()
 before=disk_check('before-fixed8-fec-controls');(out/'disk-before.json').write_text(json.dumps(before,indent=2)+'\n')
 if before['disk']['available_bytes']<5*GIB:raise SystemExit('Under5GiB: handle storage first.')
 try:
  good=json.loads((ref/'receipt.json').read_text());assert good['banks']==8 and good['simulation'].startswith('G0_FEC_STREAM_PASS banks=8');hashes=good['source_sha256']
  for n,h in hashes.items():assert hashlib.sha256((project/'sim/g0'/n).read_bytes()).hexdigest()==h
  target='fec_stream_recovery_cost_probe.sv';changes=[
   ('no_automatic_k_queue','AUTO_RECOVERY=1','AUTO_RECOVERY=0','Fatal: stream automatic k queue absent'),
   ('no_early_source','EARLY_SOURCE=1','EARLY_SOURCE=0','Fatal: stream early delivery not before k'),
   ('restore_already_delivered_source',"deliver_mask<=~bank_delivered[scan_bank]","deliver_mask<=20'hfffff",'Fatal: stream source byte/dedup oracle'),
   ('ignore_current_packet_abort','packet_active && !packet_abort','packet_active','Fatal: stream packet abort fence late'),
   ('mark_source_complete_on_first_byte','if(offset==511)begin\n      bank_delivered[active_bank][row[4:0]]<=1;','bank_delivered[active_bank][row[4:0]]<=1;\n     if(offset==511)begin','Fatal: stream delivered bitmap before whole source'),
   ('no_queued_bank_deadline','assign expiring[q]=(bank_state[q]==LOADING || bank_state[q]==QUEUED || bank_state[q]==WORKING)','assign expiring[q]=(bank_state[q]==LOADING || bank_state[q]==WORKING)','Fatal: stream bank deadline late'),
  ];rows=[]
  for label,old,new,witness in changes:
   d=out/label;d.mkdir()
   for p in ref.glob('*.hex'):os.link(p,d/p.name)
   for n in hashes:shutil.copyfile(ref/n,d/n)
   shutil.copyfile(ref/'tb.sv',d/'tb.sv');p=d/target;s=p.read_text();assert s.count(old)==1,(label,s.count(old));p.write_text(s.replace(old,new))
   def run(step,args):
    with (d/(step+'.log')).open('w') as f:subprocess.run(args,cwd=d,stdout=f,stderr=subprocess.STDOUT,check=True)
   run('compile',[str(bp/'xvlog'),'--sv',*hashes,'tb.sv']);run('elaborate',[str(bp/'xelab'),'tb','-s','fec_stream_control_tb','-mt','off'])
   try:run('simulate',[str(bp/'xsim'),'fec_stream_control_tb','-runall'])
   except subprocess.CalledProcessError:pass
   log=(d/'simulate.log').read_text();line=next((s for s in log.splitlines() if s.startswith(witness)),None)
   assert line and 'timeout' not in line.lower() and 'G0_FEC_STREAM_PASS' not in log,(label,log[-3000:])
   row={'name':label,'detected_by':line};rows.append(row);print(json.dumps(row),flush=True)
  (out/'receipt.json').write_text(json.dumps({'scope':__doc__,'banks':8,'source_sha256':hashes,'negative_controls':rows,'g0_passed':False},indent=2)+'\n')
 finally:(out/'disk-after.json').write_text(json.dumps(disk_check('after-fixed8-fec-controls'),indent=2)+'\n')

if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__)
 for n in ('output','reference','vivado-bin'):p.add_argument('--'+n,type=Path,required=True)
 main(p.parse_args())
