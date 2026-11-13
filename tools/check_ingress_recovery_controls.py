#!/usr/bin/env python3
"""Whole-chain negative witnesses for real rearm, fresh phase binding and diagnostic isolation."""
import argparse,json,shutil,subprocess
from pathlib import Path
from recovering_acquisition_probe import SOURCES
from workspace_storage_guard import check as disk_check,GIB

def main(a):
 out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);ref=a.reference.resolve();bp=a.vivado_bin.resolve();xpm=bp.parent/'data/ip/xpm'
 before=disk_check('before-recovery-negative-controls');(out/'disk-before.json').write_text(json.dumps(before,indent=2)+'\n')
 if before['disk']['available_bytes']<5*GIB:raise SystemExit('Under5GiB: resolve storage first.')
 try:
  assert json.loads((ref/'receipt.json').read_text())['simulation'].startswith('G0_INGRESS_RECOVERY_PASS ')
  results=[]
  cases=[('rearm','.rearm(rearm || automatic_rearm)','.rearm(rearm)','Fatal: automatic ingress recovery timed out'),
   ('origin','bound_origin<=effective_origin;locked','bound_origin<=phase_origin;locked','Fatal: recovery final configuration'),
   ('diagnostic','.dispatch_ready(base_search_ready)','.dispatch_ready(base_search_ready && result_ready)','Fatal: automatic acquisition did not lock')]
  for name,old,new,expected in cases:
   target=out/name;target.mkdir()
   for f in ref.iterdir():
    if f.suffix=='.hex' or f.name in SOURCES or f.name=='tb.sv':shutil.copyfile(f,target/f.name)
   f=target/'acquiring_continuous_frontend_cost_probe.sv';s=f.read_text();assert old in s;f.write_text(s.replace(old,new))
   for command,args in [('compile',[str(bp/'xvlog'),'--sv',str(xpm/'xpm_cdc/hdl/xpm_cdc.sv'),str(xpm/'xpm_memory/hdl/xpm_memory.sv'),*[str(target/n) for n in SOURCES],str(target/'tb.sv')]),('elaborate',[str(bp/'xelab'),'tb','-s',name+'_tb','-mt','off']),('simulate',[str(bp/'xsim'),name+'_tb','-runall'])]:
    with (target/(command+'.log')).open('w') as log:subprocess.run(args,cwd=target,stdout=log,stderr=subprocess.STDOUT,check=True)
   log=(target/'simulate.log').read_text();assert expected in log and 'G0_INGRESS_RECOVERY_PASS' not in log,log[-2000:];results.append({'name':name,'detected_by':expected})
  receipt={'scope':__doc__,'reference':str(ref),'negative_controls':results,'g0_passed':False}
  (out/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n');print(json.dumps(receipt))
 finally:(out/'disk-after.json').write_text(json.dumps(disk_check('after-recovery-negative-controls'),indent=2)+'\n')

if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);p.add_argument('--reference',type=Path,required=True);p.add_argument('--vivado-bin',type=Path,required=True);main(p.parse_args())
