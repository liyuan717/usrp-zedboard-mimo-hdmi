#!/usr/bin/env python3
"""Negative witnesses for full-window energy, shared reads, ratio ranking and signed ties."""
import argparse,json,shutil,subprocess
from pathlib import Path
from workspace_storage_guard import check as disk_check,GIB

def main(a):
 out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);bp=a.vivado_bin.resolve()
 before=disk_check('before-coherent-negative-controls');(out/'disk-before.json').write_text(json.dumps(before,indent=2)+'\n')
 if before['disk']['available_bytes']<5*GIB:raise SystemExit('Under5GiB: handle storage first.')
 try:
  cases=[('energy',a.window_reference,'coherent_window_cost_probe.sv','energy<=energy-{10\'d0,power_sum}','energy<=energy+{10\'d0,power_sum}','Fatal: coherent metric mismatch'),
   ('priority',a.window_reference,'coherent_window_cost_probe.sv','own_request && !external_read_enable && !owner_lost','own_request && !owner_lost','Fatal: coherent metric mismatch'),
   ('ratio',a.peak_reference,'coherent_peak_cost_probe.sv','!best_valid || new_product>product','!best_valid || pending_n>best_n','Fatal: coherent peak oracle j4'),
   ('negative_tie',a.peak_reference,'coherent_peak_cost_probe.sv',"(twice_fraction_remainder>{1'b0,fraction_divisor})","(twice_fraction_remainder>={1'b0,fraction_divisor})",'Fatal: coherent peak oracle j11')]
  results=[]
  for name,reference,filename,old,new,expected in cases:
   ref=reference.resolve();assert json.loads((ref/'receipt.json').read_text())['simulation'].startswith('G0_COHERENT_')
   target=out/name;target.mkdir()
   for f in ref.iterdir():
    if f.suffix in ('.hex','.sv'):shutil.copyfile(f,target/f.name)
   f=target/filename;s=f.read_text();assert old in s;s=s.replace(old,new);f.write_text(s)
   xpm=bp.parent/'data/ip/xpm';libs=[str(xpm/'xpm_cdc/hdl/xpm_cdc.sv'),str(xpm/'xpm_memory/hdl/xpm_memory.sv')] if filename=='coherent_window_cost_probe.sv' else []
   for step,args in [('compile',[str(bp/'xvlog'),'--sv',*libs,str(f),str(target/'tb.sv')]),('elaborate',[str(bp/'xelab'),'tb','-s',name+'_tb','-mt','off']),('simulate',[str(bp/'xsim'),name+'_tb','-runall'])]:
    with (target/(step+'.log')).open('w') as log:subprocess.run(args,cwd=target,stdout=log,stderr=subprocess.STDOUT,check=True)
   log=(target/'simulate.log').read_text();assert expected in log and '_PASS' not in log,log[-2000:]
   results.append({'name':name,'detected_by':expected})
  r={'scope':__doc__,'negative_controls':results,'g0_passed':False};(out/'receipt.json').write_text(json.dumps(r,indent=2)+'\n');print(json.dumps(r))
 finally:(out/'disk-after.json').write_text(json.dumps(disk_check('after-coherent-negative-controls'),indent=2)+'\n')

if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);p.add_argument('--window-reference',type=Path,required=True);p.add_argument('--peak-reference',type=Path,required=True);p.add_argument('--vivado-bin',type=Path,required=True);main(p.parse_args())
