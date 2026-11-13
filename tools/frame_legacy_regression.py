#!/usr/bin/env python3
"""Run the unchanged42case frame bench/golds against current default-disabled RTL.
All original assertions and result counts remain required. Held-cycle counts are
reported separately because the measured job-launch register adds latency.
"""
import argparse,hashlib,json,os,shutil,subprocess
from pathlib import Path
from frame_replay_probe import SOURCES
from workspace_storage_guard import check as disk_check,GIB

def main(a):
 project=Path(__file__).resolve().parents[1];out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);ref=a.reference.resolve();bp=a.vivado_bin.resolve()
 before=disk_check('before-frame-legacy');(out/'disk-before.json').write_text(json.dumps(before,indent=2)+'\n')
 if before['disk']['available_bytes']<5*GIB:raise SystemExit('Under5GiB: handle storage first.')
 try:
  old=json.loads((ref/'receipt.json').read_text());assert old['simulation'].startswith('G0_FRAME_REPLAY_PASS')
  for p in ref.glob('*.hex'):os.link(p,out/p.name)
  shutil.copyfile(ref/'tb.sv',out/'tb.sv')
  for n in SOURCES:shutil.copyfile(project/'sim/g0'/n,out/n)
  xpm=bp.parent/'data/ip/xpm'
  for step,args in [('compile',[str(bp/'xvlog'),'--sv',str(xpm/'xpm_cdc/hdl/xpm_cdc.sv'),str(xpm/'xpm_memory/hdl/xpm_memory.sv'),*[str(out/n) for n in SOURCES],str(out/'tb.sv')]),('elaborate',[str(bp/'xelab'),'tb','-s','frame_legacy_tb','-mt','off']),('simulate',[str(bp/'xsim'),'frame_legacy_tb','-runall'])]:
   with (out/(step+'.log')).open('w') as f:subprocess.run(args,cwd=out,stdout=f,stderr=subprocess.STDOUT,check=True)
  log=(out/'simulate.log').read_text();assert 'G0_FRAME_REPLAY_PASS' in log and 'Fatal:' not in log,log[-4000:]
  line=next(s for s in log.splitlines() if s.startswith('G0_FRAME_REPLAY_PASS'))
  assert line.split(' held_checks=')[0]==old['simulation'].split(' held_checks=')[0],(line,old['simulation'])
  r={'scope':__doc__,'simulation':line,'reference_simulation':old['simulation'],'reference':str(ref.relative_to(project)),
   'unchanged_TB_sha256':hashlib.sha256((out/'tb.sv').read_bytes()).hexdigest(),
   'source_sha256':{n:hashlib.sha256((out/n).read_bytes()).hexdigest() for n in SOURCES},'g0_passed':False}
  (out/'receipt.json').write_text(json.dumps(r,indent=2)+'\n');print(line,flush=True)
 finally:(out/'disk-after.json').write_text(json.dumps(disk_check('after-frame-legacy'),indent=2)+'\n')

if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__)
 for name in ('output','reference','vivado-bin'):p.add_argument('--'+name,type=Path,required=True)
 main(p.parse_args())
