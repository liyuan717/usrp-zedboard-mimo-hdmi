`timescale 1ns/1ps
// Finite G0 calibrated-input acquisition, scheduling and startup commit.
// One no-ready ingress FIFO feeds BOTH coarse acquisition and the frontend.
module acquiring_continuous_frontend_cost_probe #(parameter AUTO_RETRY=0,AUTO_RECOVER=0,EXTERNAL_HISTORY=0,CHECK_INPUT_TAG=1)(
 input wire clk,rst,cancel,rearm,raw_valid,upstream_loss,
 input wire [63:0] raw_tag,raw_data,
 input wire search_valid,output wire search_ready,input wire [31:0] search_id,
 input wire [63:0] search_tag,input wire [47:0] phase_origin,
 output wire result_valid,input wire result_ready,
 output wire [31:0] result_id,result_generation,
 output wire [2:0] result_error,result_detail,
 output wire [31:0] result_step,result_phase,
 output wire [9:0] first_symbol,output wire [8:0] training_count,
 output wire [63:0] coarse_known,final_commit,
 output reg locked,
 output wire ingress_running,output wire [31:0] ingress_faults,
 output wire [2:0] ingress_reason,output wire [7:0] ingress_peak,
 // Future PL coherent timing can submit normal two-column channel jobs.
 input wire normal_job_valid,output wire normal_job_ready,
 input wire [31:0] normal_job_id,normal_job_context,normal_job_generation,
 input wire [15:0] normal_job_epoch,input wire [79:0] normal_job_position,
 input wire [34:0] normal_job_step,
 output wire normal_valid,input wire normal_ready,
 output wire [31:0] normal_id,normal_context,normal_generation,
 output wire [15:0] normal_epoch,output wire [207:0] normal_h,
 output wire [63:0] normal_steps,output wire [1:0] normal_selected,
 output wire [199:0] normal_energies,output wire [2:0] normal_error,normal_detail,
 output reg [31:0] retry_count,output wire [31:0] diagnostic_replaced,
 output wire [63:0] current_search_tag,
 output wire [31:0] recovery_count,output wire recovery_waiting,
 output wire shared_in_valid,shared_in_start,output wire [63:0] shared_in_tag,output wire [79:0] shared_in_data,
 input wire shared_ready,input wire [31:0] shared_generation,input wire [63:0] shared_latest,
 input wire [12:0] shared_count,input wire shared_restart,
 output wire shared_read_enable,output wire [11:0] shared_read_address,input wire [79:0] shared_read_word,
 output wire frontend_abort,search_applied,frontend_window_ready,
 output wire [31:0] frontend_context,frontend_generation,
 output wire startup_commit_valid,output wire [31:0] startup_commit_context,startup_commit_id,output reg [63:0] startup_peak_tag
);
 wire ingress_cancel,buffer_valid,buffer_ready,buffer_start;
 wire [63:0] buffer_tag,buffer_data;wire [31:0] ingress_generation;wire [7:0] occupancy;
 reg ingress_cancel_q;wire abort=cancel || (ingress_cancel && !ingress_cancel_q);
 always @(posedge clk) begin if(rst) ingress_cancel_q<=0;else ingress_cancel_q<=ingress_cancel;end
 wire automatic_rearm,automatic_recovery_fire;wire [31:0] recovery_id;
 wire [63:0] recovery_tag;wire [47:0] recovery_origin;
 continuous_adc_ingress_cost_probe #(.CHECK_INPUT_TAG(CHECK_INPUT_TAG)) ingress(
 .clk(clk),.rst(rst || cancel),.rearm(rearm || automatic_rearm),.raw_valid(raw_valid),.raw_tag(raw_tag),.raw_data(raw_data),.upstream_loss(upstream_loss),
 .out_valid(buffer_valid),.out_ready(buffer_ready),.out_tag(buffer_tag),.out_data(buffer_data),.out_start(buffer_start),
 .receiver_cancel(ingress_cancel),.running(ingress_running),.generation(ingress_generation),.fault_count(ingress_faults),
 .last_fault(ingress_reason),.occupancy(occupancy),.high_water(ingress_peak),.admitted(),.dropped());
 wire coarse_ready,core_ready;
 assign buffer_ready=coarse_ready && core_ready;
 wire [31:0] coarse_generation;wire coarse_cfg_ready,coarse_valid,coarse_accept;
 wire [31:0] candidate_id,candidate_generation,candidate_step,candidate_phase;
 wire [63:0] candidate_first,candidate_peak,candidate_known;wire candidate_sense;wire [2:0] candidate_error;
 wire schedule_idle;
 wire base_search_ready=!rst && !ingress_cancel && coarse_cfg_ready && schedule_idle;
 assign search_ready=base_search_ready;
 wire requested_search_fire=search_valid && search_ready;
 wire external_search_fire=requested_search_fire || automatic_recovery_fire;
 reg retry_pending;reg [31:0] retry_id,attempt_id;reg [63:0] retry_tag,last_raw_tag;
 wire automatic_search_fire=AUTO_RETRY && retry_pending && !search_valid && !automatic_recovery_fire && base_search_ready;
 wire search_fire=external_search_fire || automatic_search_fire;
 assign frontend_abort=abort || search_fire;
 assign search_applied=search_fire;
 wire [31:0] effective_search_id=external_search_fire?(automatic_recovery_fire?recovery_id:search_id):retry_id;
 wire [63:0] effective_search_tag=external_search_fire?(automatic_recovery_fire?recovery_tag:search_tag):retry_tag;
 reg [47:0] bound_origin;
 wire [47:0] effective_origin=external_search_fire?(automatic_recovery_fire?recovery_origin:phase_origin):bound_origin;
 ingress_recovery_control_cost_probe #(.ENABLE(AUTO_RECOVER)) recovery(
 .clk(clk),.rst(rst),.cancel(cancel),.requested_search_fire(requested_search_fire),.requested_search_valid(search_valid),
 .active_attempt_id(attempt_id),.fault_event(ingress_running && ingress_cancel),.ingress_running(ingress_running),
 .ingress_healthy(!ingress_cancel),.dispatch_ready(base_search_ready),.raw_valid(raw_valid),.raw_tag(raw_tag),
 .rearm_request(automatic_rearm),.recovery_fire(automatic_recovery_fire),.recovery_id(recovery_id),.recovery_tag(recovery_tag),
 .recovery_origin(recovery_origin),.waiting(recovery_waiting),.recovery_count(recovery_count));
 wire [31:0] status_id,status_generation,status_step,status_phase;
 wire [2:0] status_error,status_detail;wire status_valid,status_ready;
 reg [63:0] bound_search_tag;
 assign current_search_tag=bound_search_tag;
 always @(posedge clk) begin
  if(rst || abort) begin
   bound_origin<=0;locked<=0;retry_pending<=0;retry_id<=0;retry_tag<=0;attempt_id<=0;retry_count<=0;last_raw_tag<=0;bound_search_tag<=0;
  end else begin
   if(raw_valid && ingress_running) last_raw_tag<=raw_tag;
   if(external_search_fire) begin
    bound_origin<=effective_origin;locked<=0;retry_pending<=0;retry_count<=0;attempt_id<=effective_search_id;bound_search_tag<=effective_search_tag;
   end else if(automatic_search_fire) begin
    locked<=0;retry_pending<=0;retry_count<=retry_count+1'b1;attempt_id<=retry_id;bound_search_tag<=retry_tag;
   end
   if(status_valid) begin
    if(status_error==0) begin locked<=1;retry_pending<=0;end
    else if(AUTO_RETRY) begin
     locked<=0;retry_pending<=1;retry_id<=attempt_id+32'd1;
     // Start after the newest physical input, never replay an old window.
     if(raw_valid) retry_tag<={raw_tag[63:48],raw_tag[47:0]+48'd1};
     else retry_tag<={last_raw_tag[63:48],last_raw_tag[47:0]+48'd1};
    end
   end
  end
 end
 differential_acquisition_cost_probe coarse(
 .clk(clk),.rst(rst),.cancel(abort),.in_valid(buffer_valid && core_ready),.in_ready(coarse_ready),
 .in_start(buffer_start),.in_tag(buffer_tag),.in_data(buffer_data),
 .cfg_valid(search_fire),.cfg_ready(coarse_cfg_ready),.cfg_id(effective_search_id),
 .cfg_generation(coarse_generation+(buffer_start?32'd1:32'd0)),.cfg_search_tag(effective_search_tag),.cfg_phase_origin_index(effective_origin),
 .out_valid(coarse_valid),.out_ready(coarse_accept),.out_id(candidate_id),.out_generation(candidate_generation),
 .out_first_tag(candidate_first),.out_peak_tag(candidate_peak),.out_known_tag(candidate_known),
 .out_sense(candidate_sense),.out_nco_step(candidate_step),.out_nco_phase(candidate_phase),.out_error(candidate_error),
 .active_generation(coarse_generation));
 wire cfg_valid,cfg_ready,cfg_applied,cfg_sense;wire [1:0] cfg_error;
 wire [63:0] cfg_tag;wire [31:0] cfg_step,cfg_phase;
 wire window_ready,history_restart;wire [31:0] active_context,history_generation;
 wire job_valid,job_ready;wire [31:0] job_id,job_context,job_generation;wire [15:0] job_epoch;
 wire [79:0] job_position;wire [34:0] job_step;wire [47:0] job_origin;
 wire core_valid,core_result_ready,core_startup,core_committed,schedule_result_ready;
 assign frontend_window_ready=window_ready;
 assign frontend_context=active_context;assign frontend_generation=history_generation;
 always @(posedge clk)begin
  if(rst || abort)startup_peak_tag<=0;
  else if(coarse_valid && coarse_accept)startup_peak_tag<=candidate_peak;
 end

 wire [31:0] core_id,core_context,core_generation,core_step,core_phase;
 wire [15:0] core_epoch;wire [2:0] core_error,core_detail;
 wire [63:0] core_commit;
 assign startup_commit_valid=core_valid && core_result_ready && core_startup && core_committed && core_error==0;
 assign startup_commit_id=core_id;
 coarse_startup_schedule_cost_probe schedule(
 .clk(clk),.rst(rst),.cancel(abort || search_fire),
 .candidate_valid(coarse_valid),.candidate_ready(coarse_accept),.candidate_id(candidate_id),.candidate_generation(candidate_generation),
 .candidate_first(candidate_first),.candidate_peak(candidate_peak),.candidate_known(candidate_known),
 .candidate_step(candidate_step),.candidate_phase(candidate_phase),.candidate_sense(candidate_sense),.candidate_error(candidate_error),.phase_origin(bound_origin),
 .cfg_valid(cfg_valid),.cfg_ready(cfg_ready),.cfg_applied(cfg_applied),.cfg_error(cfg_error),
 .cfg_tag(cfg_tag),.cfg_phase(cfg_phase),.cfg_step(cfg_step),.cfg_sense(cfg_sense),
 .window_ready(window_ready),.history_restart(history_restart),.active_context(active_context),.history_generation(history_generation),
 .job_valid(job_valid),.job_ready(job_ready),.job_id(job_id),.job_context(job_context),.job_generation(job_generation),.job_epoch(job_epoch),
 .job_first_position(job_position),.job_step(job_step),.job_count(training_count),.job_first_symbol(first_symbol),.job_commit_tag(final_commit),.job_origin(job_origin),
 .result_valid(core_valid && core_startup),.result_ready(schedule_result_ready),.result_id(core_id),.result_context(core_context),.result_generation(core_generation),
 .result_error(core_error),.result_detail(core_detail),.result_committed(core_committed),.result_final_step(core_step),.result_final_phase(core_phase),
 .out_valid(status_valid),.out_ready(status_ready),.out_id(status_id),.out_generation(status_generation),
 .out_error(status_error),.out_detail(status_detail),.out_final_step(status_step),.out_final_phase(status_phase),.idle(schedule_idle));
 generate if(AUTO_RETRY || AUTO_RECOVER) begin: autonomous_status
  wire mailbox_valid;wire [133:0] mailbox_data;
  diagnostic_latest_mailbox_cost_probe mailbox(
   .clk(clk),.rst(rst),.clear(abort || external_search_fire),.in_valid(status_valid && !ingress_cancel),
   .in_data({status_id,status_generation,status_error,status_detail,status_step,status_phase}),
   .out_valid(mailbox_valid),.out_ready(result_ready),.out_data(mailbox_data),.replaced_count(diagnostic_replaced));
  assign status_ready=1'b1;
  assign result_valid=mailbox_valid && !ingress_cancel && !external_search_fire && !rst;
  assign {result_id,result_generation,result_error,result_detail,result_step,result_phase}=mailbox_data;
 end else begin: requested_status
  assign status_ready=result_ready;assign diagnostic_replaced=0;
  assign result_valid=status_valid && !ingress_cancel && !external_search_fire && !rst;
  assign {result_id,result_generation,result_error,result_detail,result_step,result_phase}={status_id,status_generation,status_error,status_detail,status_step,status_phase};
 end endgenerate
 assign coarse_known=cfg_tag;
 wire allow_normal=locked && !search_valid && !ingress_cancel;
 wire core_job_ready;
 assign job_ready=core_job_ready;
 assign normal_job_ready=core_job_ready && allow_normal;
 assign core_result_ready=core_startup?schedule_result_ready:normal_ready;
 assign normal_valid=core_valid && !core_startup && locked && !ingress_cancel && !search_fire && !rst;
 assign normal_id=core_id;assign normal_context=core_context;assign normal_generation=core_generation;assign normal_epoch=core_epoch;
 assign normal_error=core_error;assign normal_detail=core_detail;
 training_frontend_cost_probe #(.ENABLE_STARTUP(1),.ENABLE_FINAL_CONFIG(1),.ENABLE_CONJUGATE(1),.ENABLE_TIMED_CONFIG(1),.ENABLE_AUTOCOMMIT(1),.EXTERNAL_HISTORY(EXTERNAL_HISTORY)) core(
 .clk(clk),.rst(rst),.cancel(abort || search_fire),
 .cfg_valid(cfg_valid),.cfg_ready(cfg_ready),.cfg_tag(cfg_tag),.cfg_phase(cfg_phase),.cfg_step(cfg_step),.cfg_conjugate(cfg_sense),.cfg_applied(cfg_applied),.cfg_error(cfg_error),
 .in_valid(buffer_valid && coarse_ready),.in_ready(core_ready),.in_tag(buffer_tag),.in_data(buffer_data),
 .job_valid(job_valid || (allow_normal && normal_job_valid)),.job_ready(core_job_ready),
 .job_id(job_valid?job_id:normal_job_id),.job_context(job_valid?job_context:normal_job_context),.job_generation(job_valid?job_generation:normal_job_generation),
 .job_epoch(job_valid?job_epoch:normal_job_epoch),.job_first_position(job_valid?job_position:normal_job_position),.job_step(job_valid?job_step:normal_job_step),
 .job_startup(job_valid),.job_count(training_count),.job_first_symbol(first_symbol),.job_commit_tag(final_commit),.job_phase_origin_index(job_origin),
 .out_valid(core_valid),.out_ready(core_result_ready),.out_id(core_id),.out_context(core_context),.out_generation(core_generation),.out_epoch(core_epoch),
 .out_h(normal_h),.out_phase_steps(normal_steps),.out_selected_rx(normal_selected),.out_energies(normal_energies),.out_error(core_error),.out_detail(core_detail),
 .out_startup(core_startup),.out_committed(core_committed),.out_commit_tag(core_commit),.out_final_step(core_step),.out_final_phase(core_phase),
 .window_ready(window_ready),.history_restart(history_restart),.active_context(active_context),.history_generation(history_generation),.out_commit_context(startup_commit_context),
 .shared_in_valid(shared_in_valid),.shared_in_start(shared_in_start),.shared_in_tag(shared_in_tag),.shared_in_data(shared_in_data),
 .shared_ready(shared_ready),.shared_generation(shared_generation),.shared_latest(shared_latest),.shared_count(shared_count),.shared_restart(shared_restart),
 .shared_read_enable(shared_read_enable),.shared_read_address(shared_read_address),.shared_read_word(shared_read_word));
endmodule
