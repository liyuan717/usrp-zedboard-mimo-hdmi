#!/usr/bin/env python3
"""Actual calibrated-capture acquisition after FIFO overflow, tag/epoch loss and
held upstream loss; no second host rearm/search or diagnostic consumption.
Fresh phase origins and all NCO/RRC/training values use independent arithmetic.
Finite G0 only, not complete tracking, RF throughput or video acceptance.
"""
import argparse, hashlib, json, shutil, subprocess
from pathlib import Path
from acquiring_frontend_probe import prepare
from fec_inverse_cost_probe import routed_measurements
from workspace_storage_guard import check as disk_check, GIB

SOURCES = ['diagnostic_latest_mailbox_cost_probe.sv', 'ingress_recovery_control_cost_probe.sv',
 'compact_stream_fifo_cost_probe.sv', 'rf_rx_elastic_fifo.v', 'continuous_adc_ingress_cost_probe.sv',
 'differential_input_cost_probe.sv', 'differential_corr_cost_probe.sv', 'differential_frontend_cost_probe.sv',
 'differential_score_cost_probe.sv', 'differential_acquisition_cost_probe.sv', 'phase_angle_cost_probe.sv',
 'fine_nco_config_cost_probe.sv', 'nco_cost_probe.sv', 'rrc_cost_probe.sv', 'timing_request_cost_probe.sv',
 'interpolation_cost_probe.sv', 'frontend_chain_cost_probe.sv', 'training_channel_cost_probe.sv',
 'training_frontend_cost_probe.sv', 'coarse_startup_schedule_cost_probe.sv', 'acquiring_continuous_frontend_cost_probe.sv']

UNIT = r'''`timescale 1ns/1ps
module recovery_unit;
reg clk=0;always #10 clk=~clk;
reg rst=1,cancel=0,request=0,request_valid=0,fault=0,running=0,healthy=0,dispatch=1,rv=0;
reg [31:0] owner=32'hfffffffe;reg [63:0] tag=0;
wire rearm,fire,waiting;wire [31:0] id,count;wire [63:0] target;wire [47:0] origin;
ingress_recovery_control_cost_probe #(.ENABLE(1)) dut(clk,rst,cancel,request,request_valid,owner,fault,running,healthy,dispatch,rv,tag,rearm,fire,id,target,origin,waiting,count);
integer held=0;
initial begin
 repeat(3) @(negedge clk);rst=0;fault=1;rv=1;healthy=1;
 repeat(7) begin @(negedge clk);if(rearm || fire || waiting || count) $fatal(1,"unstarted session armed itself");end
 fault=0;rv=0;running=1;request=1;request_valid=1;@(negedge clk);request=0;request_valid=0;
 fault=1;@(negedge clk);fault=0;running=0;healthy=0;
 #1;if(!waiting || !rearm || id!==32'hffffffff) $fatal(1,"fault identity");
 // Repeated fault indications during one wait cannot consume another id.
 owner=0;fault=1;repeat(3) @(negedge clk);fault=0;
 repeat(80) begin @(negedge clk);if(fire || !waiting || !rearm || id!==32'hffffffff || count) $fatal(1,"held loss dispatch");held=held+1;end
 running=1;healthy=1;
 repeat(11) begin @(negedge clk);if(fire || !waiting) $fatal(1,"raw gap dispatch");end
 rv=1;dispatch=0;tag=64'hffff_ffff_ffff_ffff;@(negedge clk);if(fire) $fatal(1,"dispatch backpressure");
 dispatch=1;#1;if(!fire || target!==64'hffff_0000_0000_0000 || origin!==48'hffffffffffff || id!==32'hffffffff) $fatal(1,"fresh origin or wrap");
 @(negedge clk);rv=0;if(waiting || count!=1) $fatal(1,"single recovery");
 owner=32'hffffffff;fault=1;@(negedge clk);fault=0;running=0;healthy=0;
 #1;if(!waiting || id!==0 || !rearm) $fatal(1,"attempt id wrap");
 running=1;healthy=1;rv=1;request_valid=1;request=1;#1;if(fire) $fatal(1,"explicit search priority");
 @(negedge clk);request=0;request_valid=0;rv=0;if(waiting || count) $fatal(1,"explicit session replacement");
 fault=1;owner=123;@(negedge clk);fault=0;running=0;healthy=0;cancel=1;#1;if(rearm || fire) $fatal(1,"cancel boundary");
 @(negedge clk);cancel=0;running=1;healthy=1;rv=1;
 repeat(40) begin @(negedge clk);if(rearm || fire || waiting || count) $fatal(1,"cancelled session revived");end
 $display("G0_INGRESS_RECOVERY_CONTROL_PASS held_loss_cycles=%0d id_wrap=1 index_wrap=1 explicit_priority=1 cancel_stops=1",held);$finish;
end
initial begin #100000;$fatal(1,"unit timeout");end
endmodule
'''

TB = r'''`timescale 1ns/1ps
module tb;
reg clk=0;always #10 clk=~clk;
reg rst=1,cancel=0,rearm=0,rv=0,sv=0,loss=0;reg [63:0] rt=0,rd=0,search_tag=0;reg [31:0] sid=0;reg [47:0] origin=0;
wire sr,ov,locked,running,njready,nv,waiting;wire [31:0] oid,ogen,step,phase,faults,retries,drops,recoveries;
wire [2:0] error,detail,reason;wire [9:0] first_symbol;wire [8:0] count;wire [7:0] peak;wire [63:0] known,commit,current_search;
acquiring_continuous_frontend_cost_probe #(.AUTO_RETRY(1),.AUTO_RECOVER(1)) dut(.clk(clk),.rst(rst),.cancel(cancel),.rearm(rearm),.raw_valid(rv),.raw_tag(rt),.raw_data(rd),.upstream_loss(loss),
 .search_valid(sv),.search_ready(sr),.search_id(sid),.search_tag(search_tag),.phase_origin(origin),
 .result_valid(ov),.result_ready(1'b0),.result_id(oid),.result_generation(ogen),.result_error(error),.result_detail(detail),.result_step(step),.result_phase(phase),
 .first_symbol(first_symbol),.training_count(count),.coarse_known(known),.final_commit(commit),.locked(locked),.ingress_running(running),.ingress_faults(faults),.ingress_reason(reason),.ingress_peak(peak[7:0]),
 .retry_count(retries),.diagnostic_replaced(drops),.current_search_tag(current_search),.recovery_count(recoveries),.recovery_waiting(waiting),
 .normal_job_valid(1'b0),.normal_job_ready(njready),.normal_job_id(32'd0),.normal_job_context(32'd0),.normal_job_generation(32'd0),.normal_job_epoch(16'd0),.normal_job_position(80'd0),.normal_job_step(35'd17179869184),.normal_ready(1'b1),.normal_valid(nv));
reg [63:0] adc[0:@LAST@];reg [71:0] mixed_gold[0:@LAST@];reg [79:0] filtered_gold[0:@LAST@];reg [83:0] train_gold[0:@TRAINLAST@];
integer offsets[0:5],lengths[0:5],train_offsets[0:5],counts[0:5],symbols[0:5];reg [63:0] bases[0:5],firsts[0:5],peaks[0:5],knowns[0:5],commits[0:5];reg [47:0] origins[0:5];reg [31:0] coarse_steps[0:5],coarse_phases[0:5],final_steps[0:5],final_phases[0:5];
integer j,k,stage,slot,endpoint,n,waited,expected_generation,previous_generation;
integer starts=0,candidates=0,configs=0,training=0,received=0,filtered=0,raw_inputs=0,recovery_arms=0,host_searches=0,held_revoked=0,loss_checks=0;
reg verify_data=0;reg [47:0] first_delta,peak_delta,known_delta,commit_delta,base_index;reg [15:0] epoch;
reg [133:0] held_data;reg [31:0] old_faults;reg [2:0] expected_reason;
wire [133:0] view={oid,ogen,error,detail,step,phase};
always @(posedge clk) begin
 if(!rst) begin
  if(dut.ingress_cancel && (ov || nv || njready || dut.cfg_applied)) $fatal(1,"old generation escaped fault");
  if(dut.requested_search_fire) begin if(stage!=0) $fatal(1,"second host search");host_searches=host_searches+1;end
  if(dut.automatic_recovery_fire) begin
   if(stage==0 || rearm || sv || dut.effective_search_tag!=={rt[63:48],rt[47:0]+48'd1} || dut.effective_origin!==rt[47:0] || dut.effective_search_id!=300+j+stage) $fatal(1,"recovery reused old identity or phase origin");
   expected_generation=dut.coarse_generation+(dut.buffer_start?1:0);recovery_arms=recovery_arms+1;
  end
  if(verify_data && !dut.ingress_cancel) begin
   if(rv) raw_inputs=raw_inputs+1;
   if(dut.coarse_valid && dut.coarse_accept) begin
    if(dut.candidate_error || dut.candidate_first!==firsts[j] || dut.candidate_peak!==peaks[j] || dut.candidate_known!==knowns[j] || dut.candidate_sense!==1'(j%2)) $fatal(1,"recovery coarse j%0d stage%0d",j,stage);candidates=candidates+1;
   end
   if(dut.cfg_applied) begin
    configs=configs+1;
    if(!dut.core.automatic_config) begin
     if(dut.cfg_tag!==knowns[j] || dut.cfg_step!==coarse_steps[j] || dut.cfg_phase!==coarse_phases[j]) $fatal(1,"recovery coarse configuration");
    end else if(dut.buffer_tag!==commits[j] || dut.core.effective_step!==final_steps[j] || dut.core.effective_phase!==final_phases[j]) $fatal(1,"recovery final configuration");
   end
   if(dut.job_valid && dut.job_ready && (count!=counts[j] || first_symbol!=symbols[j])) $fatal(1,"recovery crop");
   if(dut.core.frontend.mixed_valid && dut.core.frontend.mixed_ready) begin
    slot=48'(dut.core.frontend.mixed_tag[47:0]-bases[j][47:0]);
    if(slot>=lengths[j] || dut.core.frontend.mixed_data!==mixed_gold[offsets[j]+slot]) $fatal(1,"recovery mixed j%0d stage%0d slot%0d",j,stage,slot);received=received+1;
   end
   if(dut.core.frontend.filtered_valid && dut.core.frontend.filtered_ready) begin
    endpoint=48'(dut.core.frontend.filtered_tag[47:0]-bases[j][47:0]+48'd20);
    if(endpoint>=lengths[j] || dut.core.frontend.filtered_data!==filtered_gold[offsets[j]+endpoint]) $fatal(1,"recovery RRC");filtered=filtered+1;
   end
   if(dut.core.train_input_valid && dut.core.train_ready) begin
    if(dut.core.sample_data!==train_gold[train_offsets[j]+dut.core.received]) $fatal(1,"recovery training");training=training+1;
   end
  end
 end
end
task fault_and_clear;
 begin
  verify_data=0;old_faults=faults;held_data=view;if(!locked || !ov) $fatal(1,"fault must revoke held successful result");
  if(stage==1) begin
   expected_reason=2;rv=1;n=0;
   // Observe the registered fault count. Between edges, the previous valid
   // word and newly advanced expected_tag need not yet form the next beat.
   while(faults==old_faults && n<512) begin rt={rt[63:48],rt[47:0]+48'd1};rd=64'h1234abcd5678ef90;@(negedge clk);n=n+1;end
   rv=0;if(n==512) $fatal(1,"real FIFO overflow not reached");
  end else if(stage==2) begin
   expected_reason=4;rt={rt[63:48]+16'd1,rt[47:0]+48'd2};rv=1;@(negedge clk);rv=0;
  end else begin
   expected_reason=1;loss=1;rv=1;
   repeat(37) begin
    rt={rt[63:48],rt[47:0]+48'd1};@(negedge clk);
    if(ov || nv || dut.automatic_recovery_fire || dut.cfg_applied) $fatal(1,"held upstream loss accepted work");loss_checks=loss_checks+1;
   end
   rv=0;loss=0;
  end
  waited=0;
  while((faults!=old_faults+1 || !running) && waited<500) begin @(negedge clk);waited=waited+1;end
  if(waited==500) $fatal(1,"automatic ingress recovery timed out stage%0d faults%0d/%0d state%0d pending%0d rearm%0d",stage,faults,old_faults,dut.ingress.state,waiting,dut.automatic_rearm);
  if(reason!==expected_reason || !waiting || ov || locked || nv || njready) $fatal(1,"fault generation revocation reason%0d expected%0d",reason,expected_reason);
  held_revoked=held_revoked+1;
  // Healthy but no new physical sample: no search may use a stale raw tag.
  repeat(20) begin @(negedge clk);if(ov || dut.automatic_recovery_fire || !waiting) $fatal(1,"recovery used stale sample during gap");end
 end
endtask
initial begin
$readmemh("adc.hex",adc);$readmemh("mixed.hex",mixed_gold);$readmemh("filtered.hex",filtered_gold);$readmemh("training.hex",train_gold);
@INIT@
for(j=0;j<6;j=j+1) begin
 rst=1;rv=0;loss=0;verify_data=0;stage=0;repeat(5) @(negedge clk);rst=0;rearm=1;@(negedge clk);rearm=0;while(!running) @(negedge clk);
 first_delta=firsts[j][47:0]-bases[j][47:0];peak_delta=peaks[j][47:0]-bases[j][47:0];known_delta=knowns[j][47:0]-bases[j][47:0];commit_delta=commits[j][47:0]-bases[j][47:0];
 sid=300+j;search_tag={bases[j][63:48],bases[j][47:0]+48'd4};origin=origins[j];sv=1;
 @(posedge clk);while(!sr) @(posedge clk);expected_generation=dut.coarse_generation+(dut.buffer_start?1:0);@(negedge clk);sv=0;sid=~sid;search_tag=0;origin=0;previous_generation=-1;
 for(stage=0;stage<4;stage=stage+1) begin
  if(stage!=0) begin
   fault_and_clear;
   // Exercise changed index bases, low48 wrap and epoch ffff -> 0000.
   base_index=stage==2?48'hfffffffffc18:48'(stage*16777216+17);
   epoch=stage==1?16'hffff:stage==2?16'h0000:16'(100+j);
   bases[j]={epoch,base_index};firsts[j]={epoch,base_index+first_delta};peaks[j]={epoch,base_index+peak_delta};knowns[j]={epoch,base_index+known_delta};commits[j]={epoch,base_index+commit_delta};
  end
  verify_data=1;
  for(k=0;k<lengths[j];k=k+1) begin rt={bases[j][63:48],bases[j][47:0]+48'(k)};rd=adc[offsets[j]+k];rv=1;@(negedge clk);rv=0;repeat(11+k%2) @(negedge clk);end
  waited=0;while((!locked || !ov) && waited<5000) begin @(negedge clk);waited=waited+1;end
  if(waited==5000) $fatal(1,"automatic acquisition did not lock");
  if(error || detail || oid!=300+j+stage || ogen!=expected_generation || ogen==previous_generation || step!==final_steps[j] || phase!==final_phases[j]) $fatal(1,"recovered result j%0d stage%0d err%0d:%0d gen%0d/%0d",j,stage,error,detail,ogen,expected_generation);
  previous_generation=ogen;held_data=view;
  repeat(80) begin @(negedge clk);if(!ov || view!==held_data || !njready || !locked) $fatal(1,"diagnostic consumption required");end
  if(recoveries!=stage || faults!=stage || retries || drops || waiting || (stage>0 && peak!=128)) $fatal(1,"recovery endpoint state");
  starts=starts+1;verify_data=0;
 end
 // Explicit stop must defeat automatic rearm even with a healthy live source.
 cancel=1;@(negedge clk);cancel=0;rv=1;
 repeat(32) begin rt={rt[63:48],rt[47:0]+48'd1};@(negedge clk);if(running || ov || waiting || dut.automatic_rearm || dut.automatic_recovery_fire) $fatal(1,"cancelled receiver restarted");end
 rv=0;
end
if(starts!=24 || recovery_arms!=18 || host_searches!=6 || held_revoked!=18 || candidates!=24 || configs!=48 || training!=9600 || raw_inputs!=4*(@LAST@+1)) $fatal(1,"recovery coverage %0d/%0d/%0d/%0d/%0d/%0d/%0d/%0d",starts,recovery_arms,host_searches,held_revoked,candidates,configs,training,raw_inputs);
$display("G0_INGRESS_RECOVERY_PASS successful_starts=%0d automatic_recoveries=%0d host_searches=%0d revoked_held_results=%0d coarse_candidates=%0d configurations=%0d raw_inputs=%0d nco_outputs=%0d rrc_outputs=%0d training_vectors=%0d held_loss_checks=%0d",starts,recovery_arms,host_searches,held_revoked,candidates,configs,raw_inputs,received,filtered,training,loss_checks);$finish;
end
initial begin #800000000;$fatal(1,"recovery timeout");end
endmodule
'''

def run_probe(a):
 out=a.output.resolve();out.mkdir(parents=True,exist_ok=False)
 project=Path(__file__).resolve().parents[1];bp=a.vivado_bin.resolve();xpm=bp.parent/'data/ip/xpm';commands=[]
 def run(name,args):
  commands.append(args);(out/'commands.json').write_text(json.dumps(commands,indent=2)+'\n')
  with (out/(name+'.log')).open('w') as log:subprocess.run(args,cwd=out,stdout=log,stderr=subprocess.STDOUT,check=True)
 sources=[]
 for name in SOURCES:
  target=out/name;shutil.copyfile(project/'sim/g0'/name,target);sources.append(target)
 (out/'unit.sv').write_text(UNIT)
 run('unit-compile',[str(bp/'xvlog'),'--sv',str(out/'ingress_recovery_control_cost_probe.sv'),str(out/'unit.sv')])
 run('unit-elaborate',[str(bp/'xelab'),'recovery_unit','-s','recovery_unit','-mt','off'])
 run('unit-simulate',[str(bp/'xsim'),'recovery_unit','-runall'])
 unitlog=(out/'unit-simulate.log').read_text();assert 'G0_INGRESS_RECOVERY_CONTROL_PASS' in unitlog and 'Fatal:' not in unitlog
 n,nt,init,reports=prepare(out,a.fixture_directory.resolve(),phase_origin_at_slice=True)
 (out/'tb.sv').write_text(TB.replace('@LAST@',str(n-1)).replace('@TRAINLAST@',str(nt-1)).replace('@INIT@',init))
 run('compile',[str(bp/'xvlog'),'--sv',str(xpm/'xpm_cdc/hdl/xpm_cdc.sv'),str(xpm/'xpm_memory/hdl/xpm_memory.sv'),*map(str,sources),str(out/'tb.sv')])
 run('elaborate',[str(bp/'xelab'),'tb','-s','recovery_tb','-mt','off'])
 run('simulate',[str(bp/'xsim'),'recovery_tb','-runall'])
 log=(out/'simulate.log').read_text();assert 'G0_INGRESS_RECOVERY_PASS' in log and 'Fatal:' not in log,log[-2000:]
 receipt={'scope':__doc__,'simulation':next(s for s in log.splitlines() if s.startswith('G0_INGRESS_RECOVERY_PASS')),
  'controller_unit':next(s for s in unitlog.splitlines() if s.startswith('G0_INGRESS_RECOVERY_CONTROL_PASS')),
  'fixtures':reports,'source_sha256':{s.name:hashlib.sha256(s.read_bytes()).hexdigest() for s in sources},'routed_OOC':None,'g0_passed':False}
 (out/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
 if not a.sim_only:
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
  (out/'ooc.tcl').write_text(tcl);run('ooc',[str(bp/'vivado'),'-mode','batch','-source',str(out/'ooc.tcl'),'-nolog','-nojournal'])
  receipt['routed_OOC']=routed_measurements(out);(out/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
 print(json.dumps({k:v for k,v in receipt.items() if k not in ('fixtures','source_sha256')}))

def main(a):
 out=a.output.resolve();out.parent.mkdir(parents=True,exist_ok=True)
 before=disk_check('before-'+out.name);(out.parent/(out.name+'-disk-before.json')).write_text(json.dumps(before,indent=2)+'\n')
 if before['disk']['available_bytes']<5*GIB:raise SystemExit('Under5GiB: resolve storage before build.')
 try:run_probe(a)
 finally:(out.parent/(out.name+'-disk-after.json')).write_text(json.dumps(disk_check('after-'+out.name),indent=2)+'\n')

if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);p.add_argument('--fixture-directory',type=Path,required=True);p.add_argument('--vivado-bin',type=Path,required=True);p.add_argument('--sim-only',action='store_true');main(p.parse_args())
