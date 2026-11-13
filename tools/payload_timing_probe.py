#!/usr/bin/env python3
"""Routed current payload main after evidenced scheduling/ownership timing fixes.
No functional proof is inherited for changed RTL: current full simulation,
boundaries and compiled controls are required separately.
"""
import argparse,hashlib,json,os,shutil,subprocess
from pathlib import Path
from payload_acquisition_probe import SOURCES
from fec_inverse_cost_probe import routed_measurements
from workspace_storage_guard import check as disk_check,GIB

def main(a):
 project=Path(__file__).resolve().parents[1];out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);ref=a.reference.resolve();bp=a.vivado_bin.resolve()
 before=disk_check('before-payload-job-launch-timing-fix');(out/'disk-before.json').write_text(json.dumps(before,indent=2)+'\n')
 if before['disk']['available_bytes']<5*GIB:raise SystemExit('Under5GiB: handle storage first.')
 try:
  old=json.loads((ref/'receipt.json').read_text());assert old['payload_simulation'].startswith('G0_LIVE_PAYLOAD_ACQUISITION_PASS');changed=[]
  for n in SOURCES:
   p=project/'sim/g0'/n;h=hashlib.sha256(p.read_bytes()).hexdigest()
   if n in old['source_sha256']:
    assert hashlib.sha256((ref/n).read_bytes()).hexdigest()==old['source_sha256'][n]
    if h!=old['source_sha256'][n]:changed.append(n)
   else:changed.append(n)
   shutil.copyfile(p,out/n)
  expected=['frame_replay_cost_probe.sv']
  if a.reservation_change:expected+=['interpolation_cost_probe.sv','coherent_window_cost_probe.sv','coherent_timing_cost_probe.sv','causal_tracking_timing_cost_probe.sv','tracking_acquiring_frontend_cost_probe.sv','frame_acquiring_frontend_cost_probe.sv']
  if a.domain_handoff:expected+=['frame_domain_cost_probe.sv']
  assert sorted(changed)==sorted(expected),changed
  for p in ref.glob('*.hex'):os.link(p,out/p.name)
  tcl=(ref/'ooc.tcl').read_text().replace(str(ref),str(out));assert '-generic ENABLE_PAYLOAD=1' in tcl
  for n in SOURCES:
   if n not in old['source_sha256']:tcl=tcl.replace('synth_design -top','read_verilog -sv {'+str(out/n)+'}\nsynth_design -top')
  (out/'ooc.tcl').write_text(tcl)
  with (out/'ooc.log').open('w') as f:subprocess.run([str(bp/'vivado'),'-mode','batch','-source',str(out/'ooc.tcl'),'-nolog','-nojournal'],cwd=out,stdout=f,stderr=subprocess.STDOUT,check=True)
  cost=routed_measurements(out);memory=(out/'memory-ownership.rpt').read_text();assert memory=='shared4096 9\nprivate512 0\nframe_math_DSP 1\nframe_sampler_DSP 1\n',memory
  result={'scope':__doc__,'source_sha256':{n:hashlib.sha256((out/n).read_bytes()).hexdigest() for n in SOURCES},'changed_from_functional_reference':changed,
   'functional_reference_before_timing_fix':str(ref.relative_to(project)),'current_full_functional_regression_required':True,
   'routed_OOC':cost,'memory_ownership':{'shared4096_BRAM_count':9,'private512_BRAM_count':0,'shared_header_pilot_math_DSP_count':1,'shared_header_pilot_business_sampler_DSP_count':1},'g0_passed':False}
  (out/'receipt.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps({k:v for k,v in result.items() if k!='source_sha256'}),flush=True)
 finally:(out/'disk-after.json').write_text(json.dumps(disk_check('after-payload-job-launch-timing-fix'),indent=2)+'\n')

if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__)
 for name in ('output','reference','vivado-bin'):p.add_argument('--'+name,type=Path,required=True)
 p.add_argument('--reservation-change',action='store_true',help='Measure the bounded7-file read-slot reservation/enable separation on the same41RTL main')
 p.add_argument('--domain-handoff',action='store_true',help='Include actual H-descriptor register/publication fence domain in the42RTL candidate')
 main(p.parse_args())
