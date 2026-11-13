#!/usr/bin/env python3
"""Actual ADC/coarse/startup/final NCO/RRC -> shared4096 timing -> normal H.
All continuous samples, public metrics, interpolated training and both H
columns are compared to independent integer/Fraction references. Finite G0;
software calibration/notch, physical IO/CDC/header/packet/video still pending.
"""
import argparse,hashlib,json,os,shutil,subprocess
from pathlib import Path
from recovering_acquisition_probe import SOURCES as ACQUISITION_SOURCES
from fec_inverse_cost_probe import routed_measurements
from workspace_storage_guard import check as disk_check,GIB

SOURCES=ACQUISITION_SOURCES+['coherent_window_cost_probe.sv','coherent_peak_cost_probe.sv','coherent_timing_cost_probe.sv',
 'causal_sfo_cost_probe.sv','causal_tracking_timing_cost_probe.sv','tracking_job_dispatch_cost_probe.sv','tracking_acquiring_frontend_cost_probe.sv']

UNIT=r'''`timescale 1ns/1ps
module dispatch_unit;
reg clk=0;always #10 clk=~clk;
reg rst=1,cancel=0,ev=0,wr=1,ready=0;reg [31:0] id=32'hffffffff,ctx=32'h80000000,gen=32'h80000001,ec=32'h80000000,eg=32'h80000001;
reg [15:0] ep=16'hffff;reg [63:0] latest=64'hffff000000000010;reg [79:0] pos=80'hffffffffffff80000001;reg [34:0] step=35'd17179869184;
wire valid,fault,pending;wire [31:0] ji,jc,jg,expired;wire [15:0] je;wire [79:0] jp;wire [34:0] js;wire [2:0] reason;
tracking_job_dispatch_cost_probe #(.WAIT_LIMIT(16)) dut(clk,rst,cancel,ev,id,ec,eg,ep,pos,step,wr,ctx,gen,latest,valid,ready,ji,jc,jg,je,jp,js,fault,reason,expired,pending);
task pulse;begin ev=1;@(negedge clk);ev=0;end endtask
task nofault;begin if(fault)$fatal(1,"dispatch unexpected fault");end endtask
integer i;
initial begin
 repeat(3)@(negedge clk);rst=0;pulse();
 if(!valid||!pending||{ji,jc,jg,je,jp,js}!=={id,ec,eg,ep,pos,step})$fatal(1,"dispatch descriptor capture");
 repeat(15)begin @(negedge clk);if(!valid||!pending||jp!==pos) $fatal(1,"dispatch last-cycle hold");nofault();end
 ready=1;@(negedge clk);ready=0;if(pending||fault||expired)$fatal(1,"dispatch final-cycle admission");
 wr=0;pulse();repeat(16)@(negedge clk);if(pending||!fault||reason!=3||expired!=1)$fatal(1,"dispatch window wait not bounded");
 @(negedge clk);wr=1;pulse();ready=1;id=0;pos=0;ev=1;@(negedge clk);ev=0;
 if(!valid||ji!==0||jp!==0||fault)$fatal(1,"dispatch simultaneous pop/push");@(negedge clk);ready=0;
 for(i=0;i<3;i=i+1)begin
  ec=ctx;eg=gen;ep=latest[63:48];case(i)0:ec=ctx^32'h80000000;1:eg=gen^32'h80000000;2:ep=ep^16'h8000;endcase
  pulse();if(pending||!fault||reason!=1||expired!=2+i)$fatal(1,"dispatch malformed owner");@(negedge clk);
 end
 ec=ctx;eg=gen;ep=latest[63:48];step=35'd17176433209;pulse();if(pending||!fault||reason!=4||expired!=5)$fatal(1,"dispatch low step");
 step=35'd17183305159;pulse();if(pending||!fault||reason!=4||expired!=6)$fatal(1,"dispatch high step");
 step=35'd17179869184;pulse();id=17;pulse();if(pending||!fault||reason!=2||expired!=7)$fatal(1,"dispatch overflow");
 pulse();gen=gen^32'h80000000;#1;if(valid)$fatal(1,"dispatch stale owner transferred");@(negedge clk);if(pending||!fault||reason!=1||expired!=8)$fatal(1,"dispatch owner fence");
 gen=eg;pulse();cancel=1;#1;if(valid)$fatal(1,"dispatch cancel edge");@(negedge clk);cancel=0;
 repeat(32)begin @(negedge clk);if(pending||fault||expired||valid)$fatal(1,"dispatch cancelled revival");end
 $display("G0_TRACKING_DISPATCH_PASS final_cycle=1 expiries=1 malformed_owners=3 bad_steps=2 overruns=1 live_owner_fences=1 cancel_stopped_checks=32");$finish;
end
initial begin #100000;$fatal(1,"dispatch timeout");end
endmodule
'''

TB=r'''`timescale 1ns/1ps
module tb;
reg clk=0;always #10 clk=~clk;
reg rst=1,cancel=0,rearm=0,rv=0,loss=0,sv=0,nready=0;reg [63:0] rt=0,rd=0,st=0;reg [31:0] sid=0;reg [47:0] origin=0;
wire sr,ov,running,nv,lock,fence,update,startlocked,diagvalid,waiting;wire [31:0] oid,og,rs,rp,faults,nid,nctx,ng,tid,tg,hgen,drops,local_count,dispatch_count,retries,recoveries;
wire [15:0] ne,te;wire [2:0] re,detail,ireason,nerr,ndetail,used,local_reason;wire [9:0] fs;wire [8:0] nc;wire [7:0] fifo_peak;wire [63:0] known,commit,nsteps,latest;
wire [207:0] nh;wire [199:0] energy;wire [1:0] selected;wire [79:0] tp,trainpos,nextpos;wire [34:0] step;wire [12:0] hc;wire [121:0] diag;
tracking_acquiring_frontend_cost_probe dut(
 .clk(clk),.rst(rst),.cancel(cancel),.rearm(rearm),.raw_valid(rv),.upstream_loss(loss),.raw_tag(rt),.raw_data(rd),
 .search_valid(sv),.search_ready(sr),.search_id(sid),.search_tag(st),.phase_origin(origin),
 .result_valid(ov),.result_ready(1'b0),.result_id(oid),.result_generation(og),.result_error(re),.result_detail(detail),.result_step(rs),.result_phase(rp),
 .first_symbol(fs),.training_count(nc),.coarse_known(known),.final_commit(commit),
 .ingress_running(running),.ingress_faults(faults),.ingress_reason(ireason),.ingress_peak(fifo_peak),
 .normal_valid(nv),.normal_ready(nready),.normal_id(nid),.normal_context(nctx),.normal_generation(ng),.normal_epoch(ne),.normal_h(nh),.normal_steps(nsteps),
 .normal_selected(selected),.normal_energies(energy),.normal_error(nerr),.normal_detail(ndetail),
 .tracking_locked(lock),.receiver_fence(fence),.timing_update(update),.timing_id(tid),.timing_generation(tg),.timing_epoch(te),
 .timing_position(tp),.training_position(trainpos),.next_position(nextpos),.symbol_step(step),.peaks_used(used),
 .history_generation(hgen),.latest_tag(latest),.history_count(hc),
 .diagnostic_valid(diagvalid),.diagnostic_ready(1'b0),.diagnostic_data(diag),.diagnostic_replaced(drops),
 .local_reacquisitions(local_count),.dispatch_failures(dispatch_count),.last_local_reason(local_reason),
 .startup_locked(startlocked),.retry_count(retries),.recovery_count(recoveries),.recovery_waiting(waiting));
reg [63:0] adc[0:@ALAST@];reg [71:0] mixed_gold[0:@RLAST@];reg [79:0] filtered_gold[0:@RLAST@];
reg [227:0] metrics[0:@MLAST@];reg [83:0] startup_gold[0:@SLAST@],normal_gold[0:@NLAST@];reg [47:0] normal_tags[0:@NLAST@];reg [15:0] normal_mus[0:@NLAST@];
integer moffsets[0:6],mlengths[0:6],sourceno[0:6],startup_offset[0:6],origins[0:6],peaks[0:6],firsthits[0:6],knowns[0:6],commits[0:6],counts[0:6],first_symbols[0:6];
reg [31:0] coarse_steps[0:6],coarse_phases[0:6],final_steps[0:6],final_phases[0:6];integer adc_offsets[0:5],adc_lengths[0:5];
integer wfirst[0:34],wused[0:34],metric_offset[0:34],normal_offset[0:34],header_right[0:34],data_right[0:34];
reg [79:0] wpeak[0:34],wtrain[0:34],wnext[0:34];reg [34:0] wstep[0:34];reg [207:0] gold_h[0:34];reg [63:0] gold_phase[0:34];reg [199:0] gold_energy[0:34];reg [1:0] gold_selected[0:34];
integer j=0,stage=0,episode=0,model=0,k,cycle=0,window_no=0,active_window=0,idx,slot,endpoint,vector_index,w,q,arm_wait=0,nwait=0;
reg [47:0] base;reg [15:0] epoch;reg [31:0] initial_id;reg [31:0] bound_context,bound_generation;
integer starts=0,host_searches=0,local_searches=0,recovery_searches=0,configs=0,candidates=0,raw_inputs=0,mixed=0,filtered=0,startup_vectors=0,normal_vectors=0,updates=0,jobs=0,produced=0,consumed=0,revoked=0,metric_count=0;
integer e_starts,e_updates,e_jobs,e_produced,e_consumed,e_revoked,e_windows,ep_index,matrix_count[0:49],matrix_cycle[0:49],header_cycle[0:49],data_cycle[0:49],matrix_latest[0:49];
integer min_data_lead=100000000,max_header_delay=0,max_header_symbols=0,held_checks=0,header_symbols,h,delta,raw_pause_cycles=0;
reg consumer_valid_q=0,consumer_fence_q=0;reg norm_seen=0,held=0;reg [591:0] held_view;wire [591:0] norm_view={nid,nctx,ng,ne,nh,nsteps,selected,energy,nerr,ndetail};
reg [133:0] held_startup;reg startup_seen=0;reg [121:0] held_diag;reg diag_seen=0;
reg [79:0] relative_pos,header_pos;reg [47:0] actual_right;reg [31:0] offset_id;
always @(negedge clk)begin
 if(rst||cancel||consumer_fence_q||!consumer_valid_q)begin nwait=0;nready=0;end
 else if(j==7&&stage==0)nready=0;
 else if(nwait==33)nready=1;
 else begin nwait=nwait+1;nready=0;end
end
always @(posedge clk)begin
 cycle=cycle+1;
 // Consumers sample cancellation/valid at the active edge, as the hardware
 // does. Raw-valid may still be held until the falling-edge source driver.
 consumer_valid_q<=nv;consumer_fence_q<=fence;
 if(!rst&&!cancel&&!fence&&nv&&!norm_seen)begin
  q=model*5+active_window;idx=episode*5+active_window;
  if(nerr||ndetail||nid!=initial_id+32'(active_window)||nctx!=bound_context||ng!=bound_generation||ne!=epoch||nh!==gold_h[q]||nsteps!==gold_phase[q]||energy!==gold_energy[q]||selected!==gold_selected[q])$fatal(1,"live normal H oracle case%0d stage%0d window%0d",j,stage,active_window);
  norm_seen=1;produced=produced+1;matrix_count[idx]=matrix_count[idx]+1;matrix_cycle[idx]=cycle;matrix_latest[idx]=48'(latest[47:0]-base);
  header_symbols=0;
  for(h=0;h<384;h=h+1)begin header_pos=wpeak[q]+80'(1152+h)*wstep[q];actual_right=48'((header_pos+80'd32768)>>32)+48'd1;if(matrix_latest[idx]>=actual_right)header_symbols=header_symbols+1;end
  if(header_symbols>max_header_symbols)max_header_symbols=header_symbols;
  $display("LIVE_MATRIX case=%0d stage=%0d window=%0d header_vectors_ready=%0d",j,stage,active_window,header_symbols);
 end
 if(rst||cancel||fence)begin
  if(norm_seen)revoked=revoked+1;norm_seen=0;held=0;
 end else begin
  if(held&&(!nv||norm_view!==held_view))$fatal(1,"live held normal matrix changed");
  if(nv&&nready)begin consumed=consumed+1;norm_seen=0;$display("LIVE_CONSUME case=%0d stage=%0d id=%h",j,stage,nid);end
  held=nv&&!nready;if(held)begin held_view=norm_view;held_checks=held_checks+1;end
 end
 if(rst||cancel)begin startup_seen=0;diag_seen=0;arm_wait=0;end
 else begin
  if(rv)raw_inputs=raw_inputs+1;
  if(dut.frontend_abort)begin startup_seen=0;diag_seen=0;arm_wait=0;end
  if(dut.acquisition.requested_search_fire)begin
   if(dut.local_search)begin
    if(j!=7||stage!=1||dut.effective_origin!==rt[47:0]||dut.effective_search_tag!=={rt[63:48],rt[47:0]+48'd1})$fatal(1,"live local search origin");local_searches=local_searches+1;
   end else begin if(stage!=0)$fatal(1,"live second host search");host_searches=host_searches+1;end
   $display("LIVE_SEARCH case=%0d stage=%0d local=%0d",j,stage,dut.local_search);
  end
  if(dut.acquisition.automatic_recovery_fire)begin
   if(j!=6||stage!=1||dut.acquisition.effective_origin!==rt[47:0])$fatal(1,"live ingress recovery origin");recovery_searches=recovery_searches+1;
  end
  if(dut.acquisition.coarse_valid&&dut.acquisition.coarse_accept)begin
   if(dut.acquisition.candidate_error || dut.acquisition.candidate_peak!=={epoch,base+48'(peaks[model])} || dut.acquisition.candidate_first!=={epoch,base+48'(firsthits[model])} || dut.acquisition.candidate_sense!==1'(sourceno[model]%2))$fatal(1,"live coarse candidate");candidates=candidates+1;
  end
  if(dut.acquisition.cfg_applied)begin
   configs=configs+1;
   if(!dut.acquisition.core.automatic_config)begin
    if(dut.acquisition.cfg_tag!=={epoch,base+48'(knowns[model])} || dut.acquisition.cfg_phase!==coarse_phases[model] || dut.acquisition.cfg_step!==coarse_steps[model])$fatal(1,"live coarse configuration");
   end else if(dut.acquisition.core.effective_tag!=={epoch,base+48'(commits[model])} || dut.acquisition.core.effective_phase!==final_phases[model] || dut.acquisition.core.effective_step!==final_steps[model])$fatal(1,"live final configuration");
  end
  if(dut.startup_commit)begin starts=starts+1;$display("LIVE_COMMIT case=%0d stage=%0d context=%0d",j,stage,dut.commit_context);end
  if(dut.arm_pending&&dut.window_ready&&startlocked)begin arm_wait=arm_wait+1;if(arm_wait>32)$fatal(1,"live timing arm blocked");end
  if(dut.timing.arm_valid&&dut.timing_arm_ready)begin
   if(dut.first_public!=={epoch,base+48'(peaks[model])+48'd79968} || dut.active_context!=dut.bound_context || dut.frontend_generation!=hgen)$fatal(1,"live final context arm");
   bound_context=dut.bound_context;bound_generation=hgen;arm_wait=0;
  end
  if(dut.acquisition.core.frontend.mixed_valid&&dut.acquisition.core.frontend.mixed_ready)begin
   slot=48'(dut.acquisition.core.frontend.mixed_tag[47:0]-base);
   if(slot>=mlengths[model] || dut.acquisition.core.frontend.mixed_data!==mixed_gold[moffsets[model]+slot])$fatal(1,"live NCO data case%0d stage%0d slot%0d",j,stage,slot);mixed=mixed+1;
  end
  if(dut.rrc_valid&&dut.pool_ready)begin
   endpoint=48'(dut.rrc_tag[47:0]-base+48'd20);
   if(endpoint>=mlengths[model] || dut.rrc_data!==filtered_gold[moffsets[model]+endpoint])$fatal(1,"live RRC data case%0d stage%0d endpoint%0d",j,stage,endpoint);filtered=filtered+1;
   slot=48'(dut.rrc_tag[47:0]-base);
   for(w=1;w<5;w=w+1)begin q=model*5+w;idx=episode*5+w;if(slot==header_right[q])header_cycle[idx]=cycle;if(slot==data_right[q])data_cycle[idx]=cycle;end
  end
  if(dut.timing.coherent.cfg_valid&&dut.timing.cfg_ready)begin
   window_no=32'(dut.timing.window_id-initial_id);
   if(window_no>5 || dut.timing.owner_generation!=bound_generation)$fatal(1,"live public window owner");
   if(window_no<5)begin if(dut.timing.window_first_tag!=={epoch,base+48'(wfirst[model*5+window_no])})$fatal(1,"live public window oracle");end
   else if(dut.timing.window_first_tag!=={epoch,base+wnext[model*5+4][79:32]-48'd32})$fatal(1,"live terminal window oracle");
  end
  if(dut.timing.coherent.metric_valid&&dut.timing.coherent.metric_ready)begin
   q=model*5+window_no;
   if(window_no>4 || dut.timing.coherent.metric_error || {dut.timing.coherent.metric_e,dut.timing.coherent.metric_n,dut.timing.coherent.metric_c}!==metrics[metric_offset[q]+dut.timing.coherent.metric_point])$fatal(1,"live coherent metric oracle");metric_count=metric_count+1;
  end
  if(update)begin
   offset_id=tid-initial_id;q=model*5+offset_id;relative_pos=tp-{base,32'd0};
   if(offset_id<1 || offset_id>4 || tg!=bound_generation || te!=epoch || relative_pos!==wpeak[q] || trainpos-{base,32'd0}!==wtrain[q] || nextpos-{base,32'd0}!==wnext[q] || step!==wstep[q] || used!=wused[q])$fatal(1,"live SFO position oracle");updates=updates+1;
   $display("LIVE_TIMING case=%0d stage=%0d window=%0d pending=%0d core_ready=%0d normal_valid=%0d normal_ready=%0d",j,stage,offset_id,dut.job_pending,dut.acquisition_job_ready,nv,nready);
  end
  if(dut.job_valid&&dut.job_ready)begin
   active_window=32'(dut.job_id-initial_id);q=model*5+active_window;
   if(active_window<1||active_window>4||dut.job_context!=bound_context||dut.job_generation!=bound_generation||dut.job_epoch!=epoch||dut.job_position-{base,32'd0}!==wtrain[q]||dut.job_step!==wstep[q])$fatal(1,"live normal job oracle");jobs=jobs+1;
   $display("LIVE_JOB case=%0d stage=%0d window=%0d",j,stage,active_window);
  end
  if(dut.acquisition.core.train_input_valid&&dut.acquisition.core.train_ready)begin
   if(dut.acquisition.core.startup)begin
    if(dut.acquisition.core.sample_data!==startup_gold[startup_offset[model]+dut.acquisition.core.received])$fatal(1,"live startup training oracle");startup_vectors=startup_vectors+1;
   end else begin
    q=model*5+active_window;vector_index=normal_offset[q]+(dut.acquisition.core.column?448:0)+dut.acquisition.core.received;
    if(dut.acquisition.core.sample_data!==normal_gold[vector_index] || dut.acquisition.core.sample_tag!=={epoch,base+normal_tags[vector_index]} || dut.acquisition.core.sample_mu!==normal_mus[vector_index])$fatal(1,"live normal training oracle case%0d stage%0d w%0d tx%0d idx%0d",j,stage,active_window,dut.acquisition.core.column,dut.acquisition.core.received);normal_vectors=normal_vectors+1;
   end
  end
  if(dut.core_normal_valid&&nerr)$fatal(1,"live normal task error %0d:%0d",nerr,ndetail);
  if(dut.dispatch_fault && !(j==7&&stage==0))begin
   $display("LIVE_DISPATCH_DEBUG reason=%0d nwait=%0d ready=%0d nv=%0d core_valid=%0d core_ready=%0d core_state=%0d context=%0d/%0d gen=%0d/%0d expired=%0d",dut.dispatch_reason,nwait,nready,nv,dut.core_normal_valid,dut.acquisition_job_ready,dut.acquisition.core.state,dut.job_context,dut.active_context,dut.job_generation,hgen,dut.expired);
   $fatal(1,"live unexpected dispatch fault");
  end
  if(local_count && j!=7)$fatal(1,"live unexpected local reacquisition");
  if(ov)begin if(startup_seen&&{oid,og,re,detail,rs,rp}!==held_startup)$fatal(1,"live held startup diagnostic changed");held_startup={oid,og,re,detail,rs,rp};startup_seen=1;end
  if(diagvalid)begin if(diag_seen&&diag!==held_diag)$fatal(1,"live held timing diagnostic changed");held_diag=diag;diag_seen=1;end
 end

end
task run_samples(input bit pause_on_pending);
 begin
  for(k=0;k<mlengths[model];k=k+1)begin
   rt={epoch,base+48'(k)};rd=adc[adc_offsets[sourceno[model]]+k];rv=1;
   @(negedge clk);rv=0;repeat(j%2==0?7:11+k%2)@(negedge clk);
   if(pause_on_pending&&dut.job_pending&&dut.core_normal_valid)break;
  end
 end
endtask
task verify_episode(input integer expected_updates,input integer expected_jobs,input integer expected_produced,input integer expected_consumed,input integer expected_revoked);
 begin
  repeat(100)@(negedge clk);
  if(starts-e_starts!=1||updates-e_updates!=expected_updates||jobs-e_jobs!=expected_jobs||produced-e_produced!=expected_produced||consumed-e_consumed!=expected_consumed||revoked-e_revoked!=expected_revoked)$fatal(1,"live episode coverage case%0d stage%0d start%0d upd%0d jobs%0d matrix%0d consumed%0d revoked%0d",j,stage,starts-e_starts,updates-e_updates,jobs-e_jobs,produced-e_produced,consumed-e_consumed,revoked-e_revoked);
  for(w=1;w<5;w=w+1)begin
   idx=episode*5+w;
   if(matrix_count[idx])begin
    if(matrix_count[idx]!=1||header_cycle[idx]<0||data_cycle[idx]<matrix_cycle[idx])$fatal(1,"live matrix/data deadline");
    delta=data_cycle[idx]-matrix_cycle[idx];if(delta<min_data_lead)min_data_lead=delta;
    delta=matrix_cycle[idx]-header_cycle[idx];if(delta>max_header_delay)max_header_delay=delta;
   end
  end
  $display("G0_LIVE_TRACKING_EPISODE_PASS case=%0d stage=%0d updates=%0d matrices=%0d",j,stage,updates-e_updates,produced-e_produced);
 end
endtask
task begin_episode;
 begin e_starts=starts;e_updates=updates;e_jobs=jobs;e_produced=produced;e_consumed=consumed;e_revoked=revoked;window_no=0;active_window=0;bound_context=0;bound_generation=0;end
endtask
initial begin
 $readmemh("adc.hex",adc);$readmemh("mixed.hex",mixed_gold);$readmemh("filtered.hex",filtered_gold);$readmemh("metrics.hex",metrics);
 $readmemh("startup.hex",startup_gold);$readmemh("normal.hex",normal_gold);$readmemh("normal_tags.hex",normal_tags);$readmemh("normal_mus.hex",normal_mus);
 @INIT@
 for(idx=0;idx<50;idx=idx+1)begin matrix_count[idx]=0;matrix_cycle[idx]=-1;header_cycle[idx]=-1;data_cycle[idx]=-1;matrix_latest[idx]=-1;end
 for(j=0;j<8;j=j+1)begin
  stage=0;episode=j<6?j:(j==6?6:8);model=j<6?j:0;base=48'hfffffffeEAA8+48'(j*1234);epoch=j==6?16'hffff:16'hff00+16'(j);initial_id=32'hfffffffe+32'(j*16);
  rst=1;rv=0;sv=0;loss=0;cancel=0;rearm=0;repeat(5)@(negedge clk);rst=0;begin_episode();
  rearm=1;@(negedge clk);rearm=0;while(!running)@(negedge clk);
  sid=initial_id;st={epoch,base+48'd4};origin=base-48'(origins[model]);sv=1;@(posedge clk);while(!sr)@(posedge clk);@(negedge clk);sv=0;origin=48'hdeadbeefbad0;sid=0;st=0;
  run_samples(j==7);
  if(j==7)begin
   while(!dut.reacquire_pending)begin @(negedge clk);raw_pause_cycles=raw_pause_cycles+1;if(raw_pause_cycles>2200)$fatal(1,"live job timeout unbounded");end
   repeat(32)begin @(negedge clk);if(local_count||dut.local_search)$fatal(1,"live reacquisition reused stale raw tag");end
   verify_episode(2,1,1,0,1);
  end else verify_episode(4,4,4,4,0);
  if(j==6||j==7)begin
   if(j==6)begin
    loss=1;repeat(37)begin rt={rt[63:48],rt[47:0]+48'd1};rv=1;@(negedge clk);if(update||nv||dut.startup_commit)$fatal(1,"live held source loss leaked work");end
    rv=0;loss=0;while(!running)@(negedge clk);repeat(20)begin @(negedge clk);if(dut.acquisition.automatic_recovery_fire)$fatal(1,"live ingress stale raw search");end
    epoch=0;base=48'hffffffff1234;
   end else base=rt[47:0]+48'd1;
   stage=1;episode=j==6?7:9;model=6;initial_id=initial_id+1'b1;begin_episode();run_samples(0);verify_episode(4,4,4,4,0);
   if(j==6 && (recoveries!=1 || local_count || faults!=1))$fatal(1,"live ingress recovery census");
   if(j==7 && (local_count!=1||dispatch_count!=1||local_reason!=3||faults))$fatal(1,"live local recovery census");
  end
  if(!lock||!startlocked||dut.reacquire_pending)$fatal(1,"live final lock");
  cancel=1;#1;if(nv||update||sr)$fatal(1,"live cancel boundary");repeat(2)@(negedge clk);cancel=0;
  repeat(32)begin @(negedge clk);if(nv||update||dut.local_search||startlocked||lock)$fatal(1,"live cancelled session revived");end
 end
 if(starts!=10||host_searches!=8||local_searches!=1||recovery_searches!=1||configs!=20||candidates!=10||updates!=38||jobs!=37||produced!=37||consumed!=36||revoked!=1||normal_vectors!=37*896||startup_vectors!=10*400||metric_count!=48*65||min_data_lead<=0)$fatal(1,"live global coverage starts%0d host%0d local%0d recovery%0d cfg%0d cand%0d upd%0d jobs%0d matrix%0d/%0d/%0d vectors%0d/%0d metrics%0d",starts,host_searches,local_searches,recovery_searches,configs,candidates,updates,jobs,produced,consumed,revoked,normal_vectors,startup_vectors,metric_count);
 $display("G0_LIVE_TRACKING_ACQUISITION_PASS starts=%0d host_searches=%0d local_reacquisitions=%0d ingress_recoveries=%0d configurations=%0d raw_inputs=%0d NCO=%0d RRC=%0d coherent_metrics=%0d timing_updates=%0d normal_jobs=%0d matrices=%0d consumed=%0d revoked=%0d normal_vectors=%0d startup_vectors=%0d minimum_matrix_data_lead_cycles=%0d maximum_header_wait_cycles=%0d maximum_header_ready_vectors=%0d held_matrix_checks=%0d",starts,host_searches,local_searches,recovery_searches,configs,raw_inputs,mixed,filtered,metric_count,updates,jobs,produced,consumed,revoked,normal_vectors,startup_vectors,min_data_lead,max_header_delay,max_header_symbols,held_checks);$finish;
end
initial begin #2000000000;$fatal(1,"live tracking timeout");end
endmodule
'''

def main(a):
 out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);bp=a.vivado_bin.resolve();project=Path(__file__).resolve().parents[1];ref=a.fixture_directory.resolve()
 before=disk_check('before-live-tracking-acquisition');(out/'disk-before.json').write_text(json.dumps(before,indent=2)+'\n')
 if before['disk']['available_bytes']<5*GIB:raise SystemExit('Under5GiB: handle storage first.')
 try:
  meta=json.loads((ref/'fixture_metadata.json').read_text());assert meta['generator_sha256']==hashlib.sha256((project/'tools/tracking_acquisition_fixtures.py').read_bytes()).hexdigest()
  for p in ref.glob('*.hex'):os.link(p,out/p.name)
  shutil.copyfile(ref/'fixture_metadata.json',out/'fixture_metadata.json');init=(ref/'initialization.sv').read_text()
  sources=[]
  for name in SOURCES:
   target=out/name;shutil.copyfile(project/'sim/g0'/name,target);sources.append(target)
  def run(step,args):
   with (out/(step+'.log')).open('w') as f:subprocess.run(args,cwd=out,stdout=f,stderr=subprocess.STDOUT,check=True)
  (out/'unit.sv').write_text(UNIT);run('unit-compile',[str(bp/'xvlog'),'--sv',str(out/'tracking_job_dispatch_cost_probe.sv'),str(out/'unit.sv')]);run('unit-elaborate',[str(bp/'xelab'),'dispatch_unit','-s','dispatch_unit','-mt','off']);run('unit-simulate',[str(bp/'xsim'),'dispatch_unit','-runall'])
  unit=(out/'unit-simulate.log').read_text();assert 'G0_TRACKING_DISPATCH_PASS' in unit and 'Fatal:' not in unit,unit[-2500:]
  text=TB.replace('@INIT@',init)
  for key,count in [('A',meta['total_ADC_vectors']),('R',meta['total_mixed_filtered_vectors']),('M',meta['metric_vectors']),('S',meta['startup_vectors']),('N',meta['normal_vectors'])]:text=text.replace('@'+key+'LAST@',str(count-1))
  (out/'tb.sv').write_text(text);xpm=bp.parent/'data/ip/xpm'
  run('compile',[str(bp/'xvlog'),'--sv',str(xpm/'xpm_cdc/hdl/xpm_cdc.sv'),str(xpm/'xpm_memory/hdl/xpm_memory.sv'),*map(str,sources),str(out/'tb.sv')]);run('elaborate',[str(bp/'xelab'),'tb','-s','live_tracking_tb','-mt','off']);run('simulate',[str(bp/'xsim'),'live_tracking_tb','-runall'])
  log=(out/'simulate.log').read_text();assert 'G0_LIVE_TRACKING_ACQUISITION_PASS' in log and 'Fatal:' not in log,log[-4000:]
  result={'scope':__doc__,'simulation':next(s for s in log.splitlines() if s.startswith('G0_LIVE_TRACKING_ACQUISITION_PASS')),'dispatcher_unit':next(s for s in unit.splitlines() if s.startswith('G0_TRACKING_DISPATCH_PASS')),
   'fixtures':str(ref.relative_to(project)),'source_sha256':{s.name:hashlib.sha256(s.read_bytes()).hexdigest() for s in sources},'routed_OOC':None,'g0_passed':False}
  (out/'receipt.json').write_text(json.dumps(result,indent=2)+'\n')
  if not a.sim_only:
   tcl='create_project -in_memory -part xc7z020clg484-1\nset_property XPM_LIBRARIES {XPM_MEMORY XPM_CDC} [current_project]\nset_param general.maxThreads 4\n'+''.join('read_verilog -sv {'+str(s)+'}\n' for s in sources)+'''synth_design -top tracking_acquiring_frontend_cost_probe -part xc7z020clg484-1 -mode out_of_context
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
set fp [open memory-ownership.rpt w]
puts $fp "shared4096 [llength [get_cells -hier -filter {REF_NAME =~ RAMB* && NAME =~ *timing/coherent/window/history*}]]"
puts $fp "private512 [llength [get_cells -hier -filter {REF_NAME =~ RAMB* && NAME =~ *private_history*}]]"
close $fp
write_checkpoint -force live_tracking.dcp
'''
   (out/'ooc.tcl').write_text(tcl);run('ooc',[str(bp/'vivado'),'-mode','batch','-source',str(out/'ooc.tcl'),'-nolog','-nojournal']);result['routed_OOC']=routed_measurements(out)
   memory=(out/'memory-ownership.rpt').read_text();assert memory=='shared4096 9\nprivate512 0\n',memory;result['memory_ownership']={'shared_history_BRAM_count':9,'private512_BRAM_count':0}
  (out/'receipt.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps({k:v for k,v in result.items() if k!='source_sha256'}),flush=True)
 finally:(out/'disk-after.json').write_text(json.dumps(disk_check('after-live-tracking-acquisition'),indent=2)+'\n')

if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);p.add_argument('--fixture-directory',type=Path,required=True);p.add_argument('--vivado-bin',type=Path,required=True);p.add_argument('--sim-only',action='store_true');main(p.parse_args())
