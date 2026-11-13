#!/usr/bin/env python3
"""Mutate passed autonomous timing snapshots; require exact live-data witnesses."""
import argparse,hashlib,json,os,shutil,subprocess
from pathlib import Path
from causal_tracking_timing_probe import SOURCES
from workspace_storage_guard import check as disk_check,GIB

def main(a):
 out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);ref=a.reference.resolve();bp=a.vivado_bin.resolve();project=Path(__file__).resolve().parents[1]
 before=disk_check('before-causal-tracking-controls');(out/'disk-before.json').write_text(json.dumps(before,indent=2)+'\n')
 if before['disk']['available_bytes']<5*GIB:raise SystemExit('Under5GiB: handle storage first.')
 try:
  receipt=json.loads((ref/'receipt.json').read_text());assert receipt['simulation'].startswith('G0_CAUSAL_TRACKING_TIMING_PASS')
  for name in SOURCES:assert hashlib.sha256((project/'sim/g0'/name).read_bytes()).hexdigest()==receipt['source_sha256'][name]==hashlib.sha256((ref/name).read_bytes()).hexdigest()
  changes={
   'diagnostic_blocks_search':('.cfg_valid(state==ISSUE && identity_valid && !history_restart)', '.cfg_valid(state==ISSUE && identity_valid && !history_restart && diagnostic_ready)','Fatal: tracking case coverage case0 cfg0 peak0 sfo0 updates0 misses0'),
   'diagnostic_blocks_SFO':(".out_valid(sfo_valid),.out_ready(1'b1)",'.out_valid(sfo_valid),.out_ready(diagnostic_ready)','Fatal: tracking internal SFO receipt not drained'),
   'next_window_offset':("next_position[79:32]-48'd32","next_position[79:32]-48'd31",'Fatal: tracking predicted window oracle')}
  detected=[];xpm=bp.parent/'data/ip/xpm'
  for name,(old,new,witness) in changes.items():
   d=out/name;d.mkdir()
   for file in ['words.hex','metrics.hex']:os.link(ref/file,d/file)
   for file in ['tb.sv','coherent_signs.hex',*SOURCES]:shutil.copyfile(ref/file,d/file)
   source=d/'causal_tracking_timing_cost_probe.sv';text=source.read_text();assert text.count(old)==1;source.write_text(text.replace(old,new))
   def run(step,args):
    with (d/(step+'.log')).open('w') as f:subprocess.run(args,cwd=d,stdout=f,stderr=subprocess.STDOUT,check=True)
   run('compile',[str(bp/'xvlog'),'--sv',str(xpm/'xpm_cdc/hdl/xpm_cdc.sv'),str(xpm/'xpm_memory/hdl/xpm_memory.sv'),*[str(d/s) for s in SOURCES],str(d/'tb.sv')]);run('elaborate',[str(bp/'xelab'),'tb','-s','tracking_control_tb','-mt','off'])
   try:run('simulate',[str(bp/'xsim'),'tracking_control_tb','-runall'])
   except subprocess.CalledProcessError:pass
   log=(d/'simulate.log').read_text();assert witness in log and 'G0_CAUSAL_TRACKING_TIMING_PASS' not in log,(name,log[-2000:])
   detected.append({'name':name,'detected_by':witness})
  result={'scope':__doc__,'reference':str(ref.relative_to(project)),'negative_controls':detected,'source_sha256':receipt['source_sha256'],'g0_passed':False}
  (out/'receipt.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result),flush=True)
 finally:(out/'disk-after.json').write_text(json.dumps(disk_check('after-causal-tracking-controls'),indent=2)+'\n')

if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);p.add_argument('--reference',type=Path,required=True);p.add_argument('--vivado-bin',type=Path,required=True);main(p.parse_args())
