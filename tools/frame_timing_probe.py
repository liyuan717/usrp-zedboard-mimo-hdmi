#!/usr/bin/env python3
"""Routed35RTL frame candidate after bounded admission timing correction.
This probe measures timing/resources only, never reuses old functional truth as
proof for changed RTL. A separate full numerical/recovery regression is required.
"""
import argparse,hashlib,json,os,shutil,subprocess
from pathlib import Path
from frame_acquisition_probe import SOURCES
from fec_inverse_cost_probe import routed_measurements
from workspace_storage_guard import check as disk_check,GIB

def main(a):
 project=Path(__file__).resolve().parents[1];out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);ref=a.reference.resolve();bp=a.vivado_bin.resolve()
 before=disk_check('before-frame-timing-correction');(out/'disk-before.json').write_text(json.dumps(before,indent=2)+'\n')
 if before['disk']['available_bytes']<5*GIB:raise SystemExit('Under5GiB: handle storage first.')
 try:
  old=json.loads((ref/'receipt.json').read_text());assert old['frame_simulation'].startswith('G0_LIVE_FRAME_ACQUISITION_PASS')
  changed=[]
  for n in SOURCES:
   p=project/'sim/g0'/n;shutil.copyfile(p,out/n)
   if hashlib.sha256(p.read_bytes()).hexdigest()!=old['source_sha256'][n]:changed.append(n)
  assert changed==['frame_replay_cost_probe.sv'],changed
  for p in ref.glob('*.hex'):os.link(p,out/p.name)
  (out/'ooc.tcl').write_text((ref/'ooc.tcl').read_text().replace(str(ref),str(out)))
  with (out/'ooc.log').open('w') as f:subprocess.run([str(bp/'vivado'),'-mode','batch','-source',str(out/'ooc.tcl'),'-nolog','-nojournal'],cwd=out,stdout=f,stderr=subprocess.STDOUT,check=True)
  cost=routed_measurements(out);memory=(out/'memory-ownership.rpt').read_text();assert memory=='shared4096 9\nprivate512 0\nframe_math_DSP 1\nframe_sampler_DSP 1\n',memory
  result={'scope':__doc__,'source_sha256':{n:hashlib.sha256((out/n).read_bytes()).hexdigest() for n in SOURCES},'changed_from_functional_reference':changed,'functional_reference_before_timing_fix':str(ref.relative_to(project)),'current_full_functional_regression_required':True,'routed_OOC':cost,'memory_ownership':{'shared4096_BRAM_count':9,'private512_BRAM_count':0,'shared_header_pilot_DSP_count':1,'shared_header_pilot_sampler_DSP_count':1},'g0_passed':False}
  (out/'receipt.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps({k:v for k,v in result.items() if k!='source_sha256'}),flush=True)
 finally:(out/'disk-after.json').write_text(json.dumps(disk_check('after-frame-timing-correction'),indent=2)+'\n')
if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);p.add_argument('--reference',type=Path,required=True);p.add_argument('--vivado-bin',type=Path,required=True);main(p.parse_args())
