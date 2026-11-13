#!/usr/bin/env python3
"""Negative witness: held frontend result must be hidden on ingress abort edge."""
import argparse,json,shutil,subprocess
from pathlib import Path

def main(a):
 out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);ref=a.reference.resolve();bp=a.vivado_bin.resolve()
 for p in ref.iterdir():
  if p.suffix=='.hex' or p.name=='tb.sv' or p.name in ['fine_nco_config_cost_probe.sv','nco_cost_probe.sv','rrc_cost_probe.sv','timing_request_cost_probe.sv','interpolation_cost_probe.sv','frontend_chain_cost_probe.sv','phase_angle_cost_probe.sv','training_channel_cost_probe.sv','training_frontend_cost_probe.sv','continuous_adc_ingress_cost_probe.sv','compact_stream_fifo_cost_probe.sv','rf_rx_elastic_fifo.v','continuous_training_frontend_cost_probe.sv']:shutil.copyfile(p,out/p.name)
 p=out/'continuous_training_frontend_cost_probe.sv';text=p.read_text();assert 'assign out_valid=core_out_valid && !ingress_cancel' in text
 p.write_text(text.replace('assign out_valid=core_out_valid && !ingress_cancel','assign out_valid=core_out_valid'))
 xpm=bp.parent/'data/ip/xpm';files=[str(xpm/'xpm_cdc/hdl/xpm_cdc.sv'),str(xpm/'xpm_memory/hdl/xpm_memory.sv'),*map(str,list(out.glob('*.sv'))+list(out.glob('*.v')))]
 for name,args in [('compile',[str(bp/'xvlog'),'--sv',*files]),('elaborate',[str(bp/'xelab'),'tb','-s','publication_tb','-mt','off']),('simulate',[str(bp/'xsim'),'publication_tb','-runall'])]:
  with (out/(name+'.log')).open('w') as log:subprocess.run(args,cwd=out,stdout=log,stderr=subprocess.STDOUT,check=True)
 log=(out/'simulate.log').read_text();assert 'Fatal: held result escaped fault' in log and 'G0_AUTOCOMMIT_PASS' not in log,log[-2000:]
 r={'scope':__doc__,'reference':str(ref),'publication_gate_removed_fault_detected':True,'g0_passed':False};(out/'receipt.json').write_text(json.dumps(r,indent=2)+'\n');print(json.dumps(r))
if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);p.add_argument('--reference',type=Path,required=True);p.add_argument('--vivado-bin',type=Path,required=True);main(p.parse_args())
