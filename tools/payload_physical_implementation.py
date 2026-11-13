#!/usr/bin/env python3
"""Source-identical routed candidate: physical optimization under unchanged constraints.
This is a placement/routing attempt for an evidenced failure, never a functional
pass, a new clock target, a false-path waiver or a resource-budget acceptance.
"""
import argparse,hashlib,json,shutil,subprocess
from pathlib import Path
from payload_acquisition_probe import SOURCES
from fec_inverse_cost_probe import routed_measurements
from workspace_storage_guard import check as disk_check,GIB

def main(a):
 project=Path(__file__).resolve().parents[1];out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);ref=a.reference.resolve();bp=a.vivado_bin.resolve()
 before=disk_check('before-payload-physical-optimization');(out/'disk-before.json').write_text(json.dumps(before,indent=2)+'\n')
 if before['disk']['available_bytes']<5*GIB:raise SystemExit('Under5GiB: handle storage first.')
 try:
  hashes={}
  for n in SOURCES:
   h=hashlib.sha256((ref/n).read_bytes()).hexdigest();assert hashlib.sha256((project/'sim/g0'/n).read_bytes()).hexdigest()==h
   hashes[n]=h;shutil.copyfile(ref/n,out/n)
  assert (ref/'live_frame.dcp').is_file()
  tcl='open_checkpoint {'+str(ref/'live_frame.dcp')+'}\nset_param general.maxThreads 4\n'+'''phys_opt_design -directive AggressiveExplore
route_design
report_utilization -file utilization.rpt
report_utilization -hierarchical -file hierarchy.rpt
report_timing_summary -file timing.rpt -report_unconstrained
report_drc -file drc.rpt
set fp [open memory-ownership.rpt w]
puts $fp "shared4096 [llength [get_cells -hier -filter {REF_NAME =~ RAMB* && NAME =~ *timing/coherent/window/history*}]]"
puts $fp "private512 [llength [get_cells -hier -filter {REF_NAME =~ RAMB* && NAME =~ *private_history*}]]"
puts $fp "frame_math_DSP [llength [get_cells -hier -filter {REF_NAME =~ DSP* && NAME =~ frame/tracking/math/math_core/*}]]"
puts $fp "frame_sampler_DSP [llength [get_cells -hier -filter {REF_NAME =~ DSP* && NAME =~ frame/sampler/*}]]"
close $fp
write_checkpoint -force live_frame.dcp
'''
  (out/'ooc.tcl').write_text(tcl)
  with (out/'ooc.log').open('w') as f:subprocess.run([str(bp/'vivado'),'-mode','batch','-source',str(out/'ooc.tcl'),'-nolog','-nojournal'],cwd=out,stdout=f,stderr=subprocess.STDOUT,check=True)
  cost=routed_measurements(out);memory=(out/'memory-ownership.rpt').read_text();assert memory=='shared4096 9\nprivate512 0\nframe_math_DSP 1\nframe_sampler_DSP 1\n',memory
  result={'scope':__doc__,'source_sha256':hashes,'reference_DCP':str((ref/'live_frame.dcp').relative_to(project)),
   'RTL_changed':False,'clock_or_IO_constraints_changed':False,'false_path_or_multicycle_exception_added':False,
   'implementation_flow':'Open same routed candidate; phys_opt_design AggressiveExplore; route_design; all original timing/DRC checks.',
   'routed_OOC':cost,'current_full_numerical_and_recovery_regression_required':True,
   'memory_ownership':{'shared4096_BRAM_count':9,'private512_BRAM_count':0,'shared_header_pilot_math_DSP_count':1,'shared_header_pilot_business_sampler_DSP_count':1},'g0_passed':False}
  (out/'receipt.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps({k:v for k,v in result.items() if k!='source_sha256'}),flush=True)
 finally:(out/'disk-after.json').write_text(json.dumps(disk_check('after-payload-physical-optimization'),indent=2)+'\n')

if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__)
 for name in ('output','reference','vivado-bin'):p.add_argument('--'+name,type=Path,required=True)
 main(p.parse_args())
