#!/usr/bin/env python3
"""Whole-chain negative witnesses: coarse IQ direction and diagnostic isolation."""
import argparse,json,shutil,subprocess
from pathlib import Path

def main(a):
 out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);ref=a.reference.resolve();bp=a.vivado_bin.resolve();xpm=bp.parent/'data/ip/xpm';results=[]
 for name,filename,old,new,expected in [
  ('sense','coarse_startup_schedule_cost_probe.sv','cfg_sense<=candidate_sense','cfg_sense<=0','Fatal: mixed data'),
  ('diagnostic','acquiring_continuous_frontend_cost_probe.sv','wire allow_normal=locked && !search_valid','wire allow_normal=locked && schedule_idle && !search_valid','Fatal: diagnostic ACK dependency')]:
  target=out/name;target.mkdir()
  for p in ref.iterdir():
   if p.suffix in ('.sv','.v','.hex'):shutil.copyfile(p,target/p.name)
  p=target/filename;text=p.read_text();assert old in text;p.write_text(text.replace(old,new))
  sources=list(target.glob('*.v'))+list(target.glob('*.sv'))
  for command,args in [('compile',[str(bp/'xvlog'),'--sv',str(xpm/'xpm_cdc/hdl/xpm_cdc.sv'),str(xpm/'xpm_memory/hdl/xpm_memory.sv'),*map(str,sources)]),('elaborate',[str(bp/'xelab'),'tb','-s',name+'_tb','-mt','off']),('simulate',[str(bp/'xsim'),name+'_tb','-runall'])]:
   with (target/(command+'.log')).open('w') as log:subprocess.run(args,cwd=target,stdout=log,stderr=subprocess.STDOUT,check=True)
  log=(target/'simulate.log').read_text();assert expected in log and 'G0_ACQUIRING_FRONTEND_PASS' not in log,log[-2000:];results.append(dict(name=name,detected_by=expected))
 receipt={'scope':__doc__,'reference':str(ref),'negative_controls':results,'g0_passed':False};(out/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n');print(json.dumps(receipt))
if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);p.add_argument('--reference',type=Path,required=True);p.add_argument('--vivado-bin',type=Path,required=True);main(p.parse_args())
