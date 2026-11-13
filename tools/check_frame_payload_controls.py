#!/usr/bin/env python3
"""Compiled coordinate, queue, F1-owner and completion witnesses for frame payload."""
import argparse,hashlib,json,os,shutil,subprocess
from pathlib import Path
from workspace_storage_guard import check as disk_check,GIB

def main(a):
 project=Path(__file__).resolve().parents[1];out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);ref=a.reference.resolve();bp=a.vivado_bin.resolve()
 before=disk_check('before-frame-payload-controls');(out/'disk-before.json').write_text(json.dumps(before,indent=2)+'\n')
 if before['disk']['available_bytes']<5*GIB:raise SystemExit('Under5GiB: handle storage first.')
 try:
  positive=json.loads((ref/'receipt.json').read_text());assert positive['simulation'].startswith('G0_FRAME_PAYLOAD_PASS');hashes=positive['source_sha256']
  for n,h in hashes.items():
   assert hashlib.sha256((ref/n).read_bytes()).hexdigest()==h
   assert hashlib.sha256((project/'sim/g0'/n).read_bytes()).hexdigest()==h
  changes=[
   ('first_business_at_pilot', 'payload_schedule_cost_probe.sv', [('+(initial_step<<1);',';')], ['Fatal: business actual shared F1 oracle']),
   ('group_gap_one_symbol_short', 'payload_schedule_cost_probe.sv', [('sent==63?wide_step+(wide_step<<1):wide_step','sent==63?(wide_step<<1):wide_step')], ['Fatal: business actual shared F1 oracle']),
   ('four_nodes_tail_deadlock', 'payload_schedule_cost_probe.sv', [('count1<8','count1<4'),('count0<8','count0<4')], ['Fatal: business complete census']),
   ('F1_business_owner_lost', 'frame_replay_cost_probe.sv', [('business_owner<=choose_business;','business_owner<=0;')], ['Fatal: business shared header/pilot F1 oracle','Fatal: business complete census']),
   ('frame_success_before_business_done', 'frame_replay_cost_probe.sv', [(' && (!BUSINESS_ENABLED || business_done)','')], ['Fatal: business complete census']),
   ('timing_launch_without_paired_slot', 'frame_replay_cost_probe.sv', [('wire timing_cfg=launching && job_launch_ready;','wire timing_cfg=launching;')], ['Fatal: business job launched into busy configuration slot']),
  ]
  rows=[]
  for label,target,edits,witnesses in changes:
   d=out/label;d.mkdir()
   for p in ref.glob('*.hex'):os.link(p,d/p.name)
   for n in hashes:shutil.copyfile(ref/n,d/n)
   shutil.copyfile(ref/'tb.sv',d/'tb.sv');p=d/target;s=p.read_text()
   for old,new in edits:assert s.count(old)==1,(label,old);s=s.replace(old,new)
   p.write_text(s);xpm=bp.parent/'data/ip/xpm'
   def run(step,args):
    with (d/(step+'.log')).open('w') as f:subprocess.run(args,cwd=d,stdout=f,stderr=subprocess.STDOUT,check=True)
   run('compile',[str(bp/'xvlog'),'--sv',str(xpm/'xpm_cdc/hdl/xpm_cdc.sv'),str(xpm/'xpm_memory/hdl/xpm_memory.sv'),*[str(d/n) for n in hashes],str(d/'tb.sv')])
   run('elaborate',[str(bp/'xelab'),'tb','-s','frame_payload_control_tb','-mt','off'])
   try:run('simulate',[str(bp/'xsim'),'frame_payload_control_tb','-runall'])
   except subprocess.CalledProcessError:pass
   log=(d/'simulate.log').read_text();witness=next((w for w in witnesses if w in log),None)
   assert witness is not None and 'G0_FRAME_PAYLOAD_PASS' not in log,(label,log[-4000:])
   rows.append({'name':label,'detected_by':next(s for s in log.splitlines() if s.startswith(witness))});print(json.dumps(rows[-1]),flush=True)
  (out/'receipt.json').write_text(json.dumps({'scope':__doc__,'reference':str(ref.relative_to(project)),'source_sha256':hashes,'negative_controls':rows,'g0_passed':False},indent=2)+'\n')
 finally:(out/'disk-after.json').write_text(json.dumps(disk_check('after-frame-payload-controls'),indent=2)+'\n')

if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__)
 for name in ('output','reference','vivado-bin'):p.add_argument('--'+name,type=Path,required=True)
 main(p.parse_args())
