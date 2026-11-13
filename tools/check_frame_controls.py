#!/usr/bin/env python3
"""Compiled numerical/ownership mutation witnesses on passed frame replay snapshots."""
import argparse,hashlib,json,os,shutil,subprocess
from pathlib import Path
from workspace_storage_guard import check as disk_check,GIB

def main(a):
 project=Path(__file__).resolve().parents[1];out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);ref=a.reference.resolve();bp=a.vivado_bin.resolve()
 before=disk_check('before-frame-'+a.mode+'-controls');(out/'disk-before.json').write_text(json.dumps(before,indent=2)+'\n')
 if before['disk']['available_bytes']<5*GIB:raise SystemExit('Under5GiB: handle storage first.')
 try:
  positive=json.loads((ref/'receipt.json').read_text());hashes=positive['source_sha256'];names=list(hashes)
  if a.mode=='unit':
   assert positive['simulation'].startswith('G0_FRAME_REPLAY_PASS')
   changes={
    'wrong_pair_gap':('frame_replay_cost_probe.sv','(wide_step<<6)+wide_step','(wide_step<<6)','Fatal: frame shared F1 oracle'),
    'wrong_TX_column':('frame_replay_cost_probe.sv','piloting && tx?h[207:104]:h[103:0]',"1'b0 ? h[207:104]:h[103:0]",'Fatal: frame shared full complex oracle'),
    'CRC_gate_removed':('frame_replay_cost_probe.sv','if(decoded_error!=0 || decoded_id!=owner)finish','if(decoded_id!=owner)finish','Fatal: frame rejected header admitted pilots'),
    'SG_abort_not_public':('frame_replay_cost_probe.sv','cancel || owner_lost || smooth_abort || (status_held','cancel || owner_lost || (status_held','Fatal: frame held header withdrawn without public abort')}
  else:
   assert positive['frame_simulation'].startswith('G0_LIVE_FRAME_ACQUISITION_PASS')
   changes={
    'read_owner_ignored':('tracking_acquiring_frontend_cost_probe.sv','pool_read_enable=external_busy?external_read_enable:read_enable;','pool_read_enable=read_enable;','Fatal: live header sampler oracle'),
    'pilot_center_twice':('frame_replay_cost_probe.sv','pilot_position<=cfg_training_position+(cfg_wide_step<<10)',"pilot_position<=cfg_training_position-80'd85899345920+(cfg_wide_step<<10)",'Fatal: live frame pilot sampler oracle')}
  snapshot_hashes={};changed=[]
  for n,h in hashes.items():
   assert hashlib.sha256((ref/n).read_bytes()).hexdigest()==h,n
   current=project/'sim/g0'/n;actual=hashlib.sha256(current.read_bytes()).hexdigest()
   if actual!=h:changed.append(n)
   if not a.current_rtl:assert actual==h,n
   shutil.copyfile(current if a.current_rtl else ref/n,out/n);snapshot_hashes[n]=hashlib.sha256((out/n).read_bytes()).hexdigest()
  if a.current_rtl:assert changed==['frame_replay_cost_probe.sv'],changed
  controls=[]
  for label,(target,old,new,witness) in changes.items():
   d=out/label;d.mkdir()
   for p in ref.glob('*.hex'):os.link(p,d/p.name)
   for n in names:shutil.copyfile(out/n,d/n)
   shutil.copyfile(ref/'tb.sv',d/'tb.sv');p=d/target;s=p.read_text();assert s.count(old)==1,(label,old);p.write_text(s.replace(old,new))
   def run(step,args):
    with (d/(step+'.log')).open('w') as f:subprocess.run(args,cwd=d,stdout=f,stderr=subprocess.STDOUT,check=True)
   xpm=bp.parent/'data/ip/xpm';run('compile',[str(bp/'xvlog'),'--sv',str(xpm/'xpm_cdc/hdl/xpm_cdc.sv'),str(xpm/'xpm_memory/hdl/xpm_memory.sv'),*[str(d/n) for n in names],str(d/'tb.sv')]);run('elaborate',[str(bp/'xelab'),'tb','-s','frame_control_tb','-mt','off'])
   try:run('simulate',[str(bp/'xsim'),'frame_control_tb','-runall'])
   except subprocess.CalledProcessError:pass
   log=(d/'simulate.log').read_text();assert witness in log and 'G0_FRAME_REPLAY_PASS' not in log and 'G0_LIVE_FRAME_ACQUISITION_PASS' not in log,(label,log[-3500:]);controls.append({'name':label,'detected_by':next(s for s in log.splitlines() if s.startswith(witness))});print(json.dumps(controls[-1]),flush=True)
  (out/'receipt.json').write_text(json.dumps({'scope':__doc__,'reference':str(ref.relative_to(project)),'mode':a.mode,'negative_controls':controls,'source_sha256':snapshot_hashes,'changes_from_reference':changed,'current_source_correction_tested':a.current_rtl,'g0_passed':False},indent=2)+'\n')
 finally:(out/'disk-after.json').write_text(json.dumps(disk_check('after-frame-'+a.mode+'-controls'),indent=2)+'\n')
if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);p.add_argument('--reference',type=Path,required=True);p.add_argument('--mode',choices=['unit','live'],required=True);p.add_argument('--vivado-bin',type=Path,required=True);p.add_argument('--current-rtl',action='store_true',help='Test the corrected current snapshot against unchanged prior numeric/protocol oracles');main(p.parse_args())
