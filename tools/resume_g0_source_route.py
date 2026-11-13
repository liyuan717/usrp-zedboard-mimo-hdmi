#!/usr/bin/env python3
"""Resume an actually stopped G0 implementation from saved opt/physopt checkpoints."""
import argparse,hashlib,json,shutil,subprocess,time,os,signal
from pathlib import Path
from workspace_storage_guard import check,GIB
from workspace_vivado_memory import memory,monitor,select_threads

def digest(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def main(a):
 v=Path(__file__).resolve().parents[1];platform=a.platform.resolve();reference=a.reference.resolve();out=platform/('route-resume-r'+str(a.attempt));out.mkdir(exist_ok=False)
 stopped=out/f'route-prior-{a.prior_termination}.log';log=platform/'route.log'
 # The caller must revalidate that the prior process is gone, not merely silent.
 assert a.confirmed_prior_process_absent,'Revalidate prior PID/handle before resuming'
 opt=platform/('project/receiver_v2.runs/impl_1/receiver_v2_wrapper_physopt.dcp' if a.mode=='placed-alternate-clb' else 'project/receiver_v2.runs/impl_1/receiver_v2_wrapper_opt.dcp');ref=reference/'project/g0_source_review_routed.dcp'
 for p in [opt,ref]:assert p.is_file()
 assert not (platform/'project/g0_source_review_routed.dcp').exists(),'Do not replace a completed implementation'
 assert not stopped.exists()
 if a.prior_termination=='terminal_failure':
  assert 'ERROR:' in log.read_text() and 'Exiting Vivado at' in log.read_text(),'Need an actual terminal implementation failure'
 proof=json.loads((reference/'route-review.json').read_text())
 assert proof.get('route_complete_no_errors',proof.get('route_all95575_nets_complete_no_errors',False))
 assert proof['typed_RF_HDMI_counter65_contracts_passed'] and proof['WNS_ns']>=0 and proof['WHS_ns']>=0
 before=check('before-resume-SOURCE-route');(out/'disk-before.json').write_text(json.dumps(before,indent=2)+'\n');assert before['disk']['available_bytes']>=5*GIB
 original=(v/'pl/g0/implement_g0_source.tcl').read_text()
 old='''if {$retry} {reset_run impl_1}
launch_runs impl_1 -to_step route_design -jobs 1
wait_on_run impl_1
set routed [file join [get_property DIRECTORY [get_runs impl_1]] receiver_v2_wrapper_routed.dcp]
if {![file isfile $routed]} {error "route checkpoint absent: [get_property STATUS [get_runs impl_1]]"}
open_checkpoint $routed
'''
 assert original.count(old)==1
 new='''# Saved checkpoint is the same candidate netlist; no resynthesis or RTL change.
open_checkpoint {'''+str(opt)+'''}
read_checkpoint -incremental {'''+str(ref)+'''}
report_incremental_reuse -file {'''+str(out/'reuse-before-place.rpt')+'''}
place_design
phys_opt_design
write_checkpoint -force {'''+str(out/'incremental-placed.dcp')+'''}
route_design
report_incremental_reuse -file {'''+str(out/'reuse-after-route.rpt')+'''}
'''
 if a.mode=='placed-alternate-clb':
  new='open_checkpoint {'+str(opt)+'}\nroute_design -directive AlternateCLBRouting\n'
 if a.mode in ('opt-control-set-merge','opt-control-set-merge-spread'):
  assert a.prior_termination=='terminal_failure'
  new='open_checkpoint {'+str(opt)+'}\n'
  new+='proc total_control_sets {path} {\n'
  new+=' set report [report_control_sets -return_string]\n'
  new+=' set output [open $path w]; puts $output $report; close $output\n'
  new+=r' if {![regexp {\|\s*Total control sets\s*\|\s*([0-9]+)\s*\|} $report unused count]} {error "missing control set census"}'+'\n'
  new+=' return $count\n}\n'
  new+='set before_merge [total_control_sets {'+str(out/'control-sets-before.rpt')+'}]\n'
  new+='opt_design -control_set_merge\n'
  new+='set after_merge [total_control_sets {'+str(out/'control-sets-after.rpt')+'}]\n'
  new+='puts "G0_CONTROL_SET_MERGE before=$before_merge after=$after_merge"\n'
  new+='if {$after_merge >= $before_merge} {error "control-set merge did not reduce the census"}\n'
  if a.mode=='opt-control-set-merge-spread':
   new+='place_design -directive AltSpreadLogic_medium\nphys_opt_design -directive Explore\n'
   new+='write_checkpoint -force {'+str(out/'merged-spread-placed.dcp')+'}\n'
   new+='report_design_analysis -congestion -file {'+str(out/'merged-spread-placed-congestion.rpt')+'}\n'
   new+='report_timing_summary -file {'+str(out/'merged-spread-placed-timing.rpt')+'}\n'
   new+='if {[catch {route_design -directive AlternateCLBRouting} route_error]} {\n'
   for command,name in [('write_checkpoint -force','failed-route.dcp'),('report_route_status -file','failed-route-status.rpt'),('report_design_analysis -congestion -file','failed-congestion.rpt')]:
    new+=' catch {'+command+' {'+str(out/name)+'}}\n'
   new+=' error $route_error\n}\n'
  else:new+='place_design\nphys_opt_design\nroute_design\n'
 if a.mode=='opt-spread-high':
  assert a.prior_termination=='terminal_failure','Re-placement requires recorded terminal failure, not a silent live run'
  new='open_checkpoint {'+str(opt)+'}\n'
  new+='report_design_analysis -complexity -file {'+str(out/'opt-complexity.rpt')+'}\n'
  new+='place_design -directive AltSpreadLogic_high\nphys_opt_design\n'
  new+='write_checkpoint -force {'+str(out/'spread-placed.dcp')+'}\n'
  new+='report_utilization -hierarchical -file {'+str(out/'spread-placed-utilization.rpt')+'}\n'
  new+='report_design_analysis -congestion -file {'+str(out/'spread-placed-congestion.rpt')+'}\n'
  new+='if {[catch {route_design -directive Explore} route_error]} {\n'
  for command,name in [('write_checkpoint -force','failed-route.dcp'),('report_route_status -file','failed-route-status.rpt'),('report_design_analysis -congestion -file','failed-congestion.rpt'),('report_design_analysis -complexity -file','failed-complexity.rpt')]:
   new+=' catch {'+command+' {'+str(out/name)+'}}\n'
  new+=' error $route_error\n}\n'
 script=out/'resume.tcl';script.write_text(original.replace(old,new))
 log.rename(stopped)
 result={'scope':__doc__,'started_unix':time.time(),'prior_termination':a.prior_termination,'mode':a.mode,'current_host_boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),'prior_process_absence_confirmed':True,'prior_route_log_sha256':digest(stopped),'saved_candidate_checkpoint':str(opt),'saved_candidate_checkpoint_sha256':digest(opt),'qualified_route_reference':str(ref) if a.mode=='incremental' else None,'qualified_route_reference_sha256':digest(ref) if a.mode=='incremental' else None,'canonical_route_TCL_sha256':digest(v/'pl/g0/implement_g0_source.tcl'),'generated_TCL_sha256':digest(script),'original_post_route_contracts_and_reports_unchanged':True,'RTL_or_synthesis_changed':False,'completed':False,'route_passed':False,'supervisor_PID':os.getpid()}
 try:
  limit=min(8,len(os.sched_getaffinity(0)))
  reference_threads=a.reference_threads
  peak=None
  if a.memory_reference:
   peak=max(json.loads(line)['peak_build_rss_plus_swap_mib'] for line in a.memory_reference.read_text().splitlines())
   assert peak>0,'Need a nonempty measurement from the previous implementation'
   result.update(memory_reference=str(a.memory_reference.resolve()),reference_peak_mib=peak)
  else:
   limit=min(limit,2)
  result['attempts']=[]
  attempt_out=out
  attempt_script=script
  while True:
   initial=memory()
   threads,estimate=(limit,None) if peak is None else select_threads(initial['ram_mib'],peak,reference_threads,limit)
   stats=dict(threads=threads,estimated_peak_mib=estimate,initial_memory=initial,
              output=str(attempt_out),memory_log=str(attempt_out/'memory.jsonl'),
              attempt_TCL_sha256=digest(attempt_script))
   result['attempts'].append(stats)
   with log.open('w') as f:
    proc=subprocess.Popen([str(a.vivado_bin.resolve()/'vivado'),'-mode','batch','-source',str(attempt_script),'-nolog','-nojournal','-tclargs',str(platform),str(threads),'0'],cwd=platform,stdout=f,stderr=subprocess.STDOUT,start_new_session=True)
    stats['launcher_PID']=proc.pid
    result.update(stats)
    (out/'receipt.json').write_text(json.dumps(result,indent=2)+'\n')
    reason,measured=monitor(proc.pid,lambda:proc.poll() is None,attempt_out/'memory.jsonl')
    try:rc=proc.wait(timeout=15 if reason else None)
    except subprocess.TimeoutExpired:
     os.killpg(proc.pid,signal.SIGKILL)
     rc=proc.wait()
   stats.update(stop_reason=reason,measured_peak_group_mib=measured,returncode=rc)
   result.update(stats)
   if attempt_out!=out or reason:
    after=check('after-resume-SOURCE-route-attempt')
    (attempt_out/'disk-after-attempt.json').write_text(json.dumps(after,indent=2)+'\n')
   if not reason or not rc or threads==1:break
   archived_log=attempt_out/'route-pressure-stop.log'
   shutil.copyfile(log,archived_log)
   stats['archived_route_log']=str(archived_log)
   (out/'receipt.json').write_text(json.dumps(result,indent=2)+'\n')
   time.sleep(10)
   peak=max(estimate or 0,measured)
   reference_threads=threads
   limit=threads-1
   attempt_out=out/f'pressure-retry-{len(result["attempts"])}'
   attempt_out.mkdir(exist_ok=False)
   before_retry=check('before-resume-SOURCE-route-pressure-retry')
   (attempt_out/'disk-before.json').write_text(json.dumps(before_retry,indent=2)+'\n')
   assert before_retry['disk']['available_bytes']>=5*GIB
   attempt_script=attempt_out/'resume.tcl'
   attempt_script.write_text(script.read_text().replace(f'{out}/',f'{attempt_out}/'))
  result['completed']=True
  result['route_passed']=result['returncode']==0 and 'G0_SOURCE_ROUTE_COMPLETE_REVIEW_FULL_REPORTS_BEFORE_BITSTREAM' in log.read_text()
  assert result['route_passed'],'Inspect the terminal route log/reports; no bitstream emitted'
 finally:
  result['ended_unix']=time.time();(out/'disk-after.json').write_text(json.dumps(check('after-resume-SOURCE-route'),indent=2)+'\n');(out/'receipt.json').write_text(json.dumps(result,indent=2)+'\n')
 print(json.dumps(result),flush=True)
if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__)
 for n in ['platform','reference','vivado-bin']:p.add_argument('--'+n,type=Path,required=True)
 p.add_argument('--confirmed-prior-process-absent',action='store_true')
 p.add_argument('--attempt',type=int,choices=(1,2),default=1)
 p.add_argument('--mode',choices=('incremental','placed-alternate-clb','opt-control-set-merge','opt-control-set-merge-spread','opt-spread-high'),default='incremental')
 p.add_argument('--memory-reference',type=Path)
 p.add_argument('--reference-threads',type=int,choices=range(1,9),default=8)
 p.add_argument('--prior-termination',choices=('host_reboot','terminal_failure'),default='host_reboot');main(p.parse_args())
