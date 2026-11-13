#!/usr/bin/env python3
"""Route exactly the RTL/ROM snapshot of a passed autonomous ingress recovery run."""
import argparse,hashlib,json,shutil,subprocess
from pathlib import Path
from recovering_acquisition_probe import SOURCES
from fec_inverse_cost_probe import routed_measurements
from workspace_storage_guard import check as disk_check,GIB

def main(a):
 out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);ref=a.reference.resolve();project=Path(__file__).resolve().parents[1]
 before=disk_check('before-recovery-route');(out/'disk-before.json').write_text(json.dumps(before,indent=2)+'\n')
 if before['disk']['available_bytes']<5*GIB:raise SystemExit('Under5GiB: resolve storage first.')
 try:
  receipt=json.loads((ref/'receipt.json').read_text())
  assert receipt['simulation'].startswith('G0_INGRESS_RECOVERY_PASS ')
  assert receipt['controller_unit'].startswith('G0_INGRESS_RECOVERY_CONTROL_PASS ')
  assert receipt['g0_passed'] is False
  sources=[]
  for name in SOURCES:
   s=ref/name;assert hashlib.sha256(s.read_bytes()).hexdigest()==receipt['source_sha256'][name]
   assert s.read_bytes()==(project/'sim/g0'/name).read_bytes(),name
   d=out/name;shutil.copyfile(s,d);sources.append(d)
  roms={}
  for name in ['sine.hex','rrc.hex','training_signs.hex']:
   shutil.copyfile(ref/name,out/name);roms[name]=hashlib.sha256((out/name).read_bytes()).hexdigest()
  tcl='create_project -in_memory -part xc7z020clg484-1\nset_property XPM_LIBRARIES {XPM_MEMORY XPM_CDC} [current_project]\nset_param general.maxThreads 4\n'+''.join('read_verilog -sv {'+str(s)+'}\n' for s in sources)+'''synth_design -top acquiring_continuous_frontend_cost_probe -part xc7z020clg484-1 -mode out_of_context -generic {AUTO_RETRY=1 AUTO_RECOVER=1}
create_clock -name processing -period 20 [get_ports clk]
set_input_delay 2 -clock processing [get_ports -filter {DIRECTION == IN && NAME != clk}]
set_output_delay 2 -clock processing [get_ports -filter {DIRECTION == OUT}]
opt_design
place_design
route_design
report_utilization -file utilization.rpt
report_utilization -hierarchical -file hierarchy.rpt
report_timing_summary -file timing.rpt -report_unconstrained
report_drc -file drc.rpt
write_checkpoint -force recovery.dcp
'''
  (out/'ooc.tcl').write_text(tcl)
  with (out/'ooc.log').open('w') as log:subprocess.run([str(a.vivado_bin.resolve()/'vivado'),'-mode','batch','-source',str(out/'ooc.tcl'),'-nolog','-nojournal'],cwd=out,stdout=log,stderr=subprocess.STDOUT,check=True)
  receipt.update(reference_simulation=str(ref),ROM_sha256=roms,routed_OOC=routed_measurements(out))
  (out/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
  print(json.dumps({'simulation':receipt['simulation'],'routed_OOC':receipt['routed_OOC']}))
 finally:(out/'disk-after.json').write_text(json.dumps(disk_check('after-recovery-route'),indent=2)+'\n')

if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);p.add_argument('--reference',type=Path,required=True);p.add_argument('--vivado-bin',type=Path,required=True);main(p.parse_args())
