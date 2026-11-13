#!/usr/bin/env python3
"""Negative witnesses for autonomous retry and use of current physical time."""
import argparse,json,shutil,subprocess
from pathlib import Path

def main(a):
 out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);ref=a.reference.resolve();bp=a.vivado_bin.resolve();xpm=bp.parent/'data/ip/xpm';results=[]
 cases=[('disabled',"locked<=0;retry_pending<=1;retry_id", "locked<=0;retry_pending<=0;retry_id",'Fatal: retry mixed'),
 ('past',"if(raw_valid) retry_tag<={raw_tag[63:48],raw_tag[47:0]+48'd1};\n     else retry_tag<={last_raw_tag[63:48],last_raw_tag[47:0]+48'd1};",'retry_tag<=bound_search_tag;','Fatal: retry replayed old samples')]
 for name,old,new,expected in cases:
  target=out/name;target.mkdir()
  for p in ref.iterdir():
   if p.suffix in ('.sv','.v','.hex'):shutil.copyfile(p,target/p.name)
  p=target/'acquiring_continuous_frontend_cost_probe.sv';s=p.read_text();assert old in s;p.write_text(s.replace(old,new))
  files=list(target.glob('*.v'))+list(target.glob('*.sv'))
  for command,args in [('compile',[str(bp/'xvlog'),'--sv',str(xpm/'xpm_cdc/hdl/xpm_cdc.sv'),str(xpm/'xpm_memory/hdl/xpm_memory.sv'),*map(str,files)]),('elaborate',[str(bp/'xelab'),'tb','-s',name+'_tb','-mt','off']),('simulate',[str(bp/'xsim'),name+'_tb','-runall'])]:
   with (target/(command+'.log')).open('w') as log:subprocess.run(args,cwd=target,stdout=log,stderr=subprocess.STDOUT,check=True)
  log=(target/'simulate.log').read_text();assert expected in log and 'G0_AUTONOMOUS_RETRY_PASS' not in log,log[-2000:];results.append(dict(name=name,detected_by=expected))
 receipt={'scope':__doc__,'reference':str(ref),'negative_controls':results,'g0_passed':False};(out/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n');print(json.dumps(receipt))
if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);p.add_argument('--reference',type=Path,required=True);p.add_argument('--vivado-bin',type=Path,required=True);main(p.parse_args())
