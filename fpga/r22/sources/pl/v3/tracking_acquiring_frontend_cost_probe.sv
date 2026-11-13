`timescale 1ns/1ps
// Actual calibrated ADC -> acquisition/startup -> same final NCO/RRC -> ONE
// coherent4096 history, shared F=1 sampler -> autonomous two-column training.
// No physical CPACK/CDC, calibration/notch, header/payload/packet/DMA yet.
module tracking_acquiring_frontend_cost_probe #(parameter ENABLE_EXTERNAL_READER=0,EXTERNAL_RESERVATION=0,CHECK_INPUT_TAG=1)(
 input wire clk,rst,cancel,rearm,raw_valid,upstream_loss,input wire [63:0] raw_tag,raw_data,
 input wire search_valid,output wire search_ready,input wire [31:0] search_id,
 input wire [63:0] search_tag,input wire [47:0] phase_origin,
 output wire result_valid,input wire result_ready,output wire [31:0] result_id,result_generation,
 output wire [2:0] result_error,result_detail,output wire [31:0] result_step,result_phase,
 output wire [9:0] first_symbol,output wire [8:0] training_count,output wire [63:0] coarse_known,final_commit,
 output wire ingress_running,output wire [31:0] ingress_faults,output wire [2:0] ingress_reason,output wire [7:0] ingress_peak,
 output wire normal_valid,input wire normal_ready,output wire [31:0] normal_id,normal_context,normal_generation,
 output wire [15:0] normal_epoch,output wire [207:0] normal_h,output wire [63:0] normal_steps,
 output wire [1:0] normal_selected,output wire [199:0] normal_energies,output wire [2:0] normal_error,normal_detail,
 output wire tracking_locked,receiver_fence,output wire timing_update,
 output wire [31:0] timing_id,timing_generation,output wire [15:0] timing_epoch,
 output wire [79:0] timing_position,training_position,next_position,output wire [34:0] symbol_step,
 output wire [2:0] peaks_used,output wire [31:0] history_generation,output wire [63:0] latest_tag,output wire [12:0] history_count,
 output wire diagnostic_valid,input wire diagnostic_ready,output wire [121:0] diagnostic_data,output wire [31:0] diagnostic_replaced,
 output reg [31:0] local_reacquisitions,dispatch_failures,output reg [2:0] last_local_reason,
 output wire startup_locked,output wire [31:0] retry_count,recovery_count,output wire recovery_waiting,
 input wire external_reader_active,external_read_enable,input wire [11:0] external_read_address,
 output wire [79:0] external_read_word,output wire shared_input_valid,shared_input_start,shared_input_ready,shared_history_restart,
 output wire [63:0] shared_input_tag,output wire [79:0] shared_input_data,
 output wire frontend_window_ready,output wire [31:0] frontend_context,
 output reg [79:0] normal_training_position,output reg [34:0] normal_symbol_step,input wire external_read_reservation
);
 wire rrc_valid,rrc_start,pool_ready,pool_restart,read_enable,frontend_abort,search_applied,window_ready;
 wire [63:0] rrc_tag,startup_peak;wire [79:0] rrc_data,read_word;wire [11:0] read_address;
 wire [31:0] active_context,frontend_generation,commit_context,commit_id;
 wire startup_commit,base_search_ready,core_normal_valid,core_normal_ready;
 wire timing_active,timing_arm_ready,timing_request;wire [2:0] timing_reason;
 reg session_armed,reacquire_pending,arm_pending,tracking_locked_q;
 reg [31:0] bound_context,bound_id;reg [63:0] first_public;
 wire local_search=session_armed && reacquire_pending && !search_valid && raw_valid && ingress_running && base_search_ready;
 wire effective_search=search_valid || local_search;
 assign search_ready=base_search_ready;
 wire [31:0] effective_search_id=search_valid?search_id:bound_id+32'd1;
 wire [63:0] effective_search_tag=search_valid?search_tag:{raw_tag[63:48],raw_tag[47:0]+48'd1};
 wire [47:0] effective_origin=search_valid?phase_origin:raw_tag[47:0];
 wire arm_valid=arm_pending && window_ready && startup_locked && active_context==bound_context && frontend_generation==history_generation;
 wire job_valid,job_ready,acquisition_job_ready,dispatch_fault,job_pending;wire [2:0] dispatch_reason;wire [31:0] expired;
 wire [31:0] job_id,job_context,job_generation;wire [15:0] job_epoch;wire [79:0] job_position;wire [34:0] job_step;
 wire normal_failed=core_normal_valid && normal_error!=0;
 wire external_busy=ENABLE_EXTERNAL_READER && external_reader_active;
 wire reader_conflict=external_busy && read_enable;
 wire pool_read_enable=external_busy?external_read_enable:read_enable;
 wire pool_read_reservation=external_busy?external_read_reservation:read_enable;
 wire [11:0] pool_read_address=external_busy?external_read_address:read_address;
 assign external_read_word=read_word;
 assign shared_input_valid=rrc_valid;assign shared_input_start=rrc_start;assign shared_input_ready=pool_ready;
 assign shared_history_restart=pool_restart;
 assign shared_input_tag=rrc_tag;assign shared_input_data=rrc_data;
 assign frontend_window_ready=window_ready;assign frontend_context=active_context;
 assign job_ready=acquisition_job_ready && !reacquire_pending && !external_busy;
 assign receiver_fence=frontend_abort || pool_restart || reacquire_pending || reader_conflict || (tracking_locked_q && !tracking_locked);
 assign normal_valid=core_normal_valid && tracking_locked && !receiver_fence && normal_error==0 && !rst && !cancel;
 assign core_normal_ready=normal_ready || !tracking_locked || reacquire_pending || normal_failed || reader_conflict;
 always @(posedge clk)begin
  if(rst || cancel)begin
   session_armed<=0;reacquire_pending<=0;arm_pending<=0;tracking_locked_q<=0;
   bound_context<=0;bound_id<=0;first_public<=0;local_reacquisitions<=0;dispatch_failures<=0;last_local_reason<=0;
   normal_training_position<=0;normal_symbol_step<=0;
  end else begin
   tracking_locked_q<=tracking_locked;
   if(search_valid && search_ready)session_armed<=1;
   if(local_search)local_reacquisitions<=local_reacquisitions+1'b1;
   if(dispatch_fault)dispatch_failures<=dispatch_failures+1'b1;
   if(job_valid && job_ready)begin normal_training_position<=job_position;normal_symbol_step<=job_step;end
   if(frontend_abort)begin arm_pending<=0;reacquire_pending<=0;end
   else begin
    if(startup_commit)begin
     arm_pending<=1;bound_context<=commit_context;bound_id<=commit_id;
     first_public<={startup_peak[63:48],startup_peak[47:0]+48'd79968};
    end else if(arm_valid && timing_arm_ready)arm_pending<=0;
    if(session_armed && (timing_request || dispatch_fault || normal_failed || reader_conflict))begin
     reacquire_pending<=1;last_local_reason<=reader_conflict?3'd6:(dispatch_fault?dispatch_reason:(normal_failed?3'd5:timing_reason));
    end
   end
  end
 end
 tracking_job_dispatch_cost_probe dispatch(
  .clk(clk),.rst(rst),.cancel(frontend_abort || pool_restart),.event_valid(timing_update),
  .event_id(timing_id),.event_context(bound_context),.event_generation(timing_generation),.event_epoch(timing_epoch),
  .event_position(training_position),.event_step(symbol_step),.window_ready(window_ready),
  .current_context(active_context),.current_generation(history_generation),.latest_tag(latest_tag),
  .job_valid(job_valid),.job_ready(job_ready),.job_id(job_id),.job_context(job_context),.job_generation(job_generation),
  .job_epoch(job_epoch),.job_position(job_position),.job_step(job_step),
  .fault(dispatch_fault),.fault_reason(dispatch_reason),.expired_count(expired),.pending(job_pending));
 acquiring_continuous_frontend_cost_probe #(.AUTO_RETRY(1),.AUTO_RECOVER(1),.EXTERNAL_HISTORY(1),.CHECK_INPUT_TAG(CHECK_INPUT_TAG)) acquisition(
  .clk(clk),.rst(rst),.cancel(cancel),.rearm(rearm),.raw_valid(raw_valid),.raw_tag(raw_tag),.raw_data(raw_data),.upstream_loss(upstream_loss),
  .search_valid(effective_search),.search_ready(base_search_ready),.search_id(effective_search_id),.search_tag(effective_search_tag),.phase_origin(effective_origin),
  .result_valid(result_valid),.result_ready(result_ready),.result_id(result_id),.result_generation(result_generation),.result_error(result_error),.result_detail(result_detail),
  .result_step(result_step),.result_phase(result_phase),.first_symbol(first_symbol),.training_count(training_count),.coarse_known(coarse_known),.final_commit(final_commit),
  .locked(startup_locked),.ingress_running(ingress_running),.ingress_faults(ingress_faults),.ingress_reason(ingress_reason),.ingress_peak(ingress_peak),
  .normal_job_valid(job_valid && !reacquire_pending && !external_busy),.normal_job_ready(acquisition_job_ready),.normal_job_id(job_id),.normal_job_context(job_context),.normal_job_generation(job_generation),
  .normal_job_epoch(job_epoch),.normal_job_position(job_position),.normal_job_step(job_step),.normal_valid(core_normal_valid),.normal_ready(core_normal_ready),
  .normal_id(normal_id),.normal_context(normal_context),.normal_generation(normal_generation),.normal_epoch(normal_epoch),.normal_h(normal_h),.normal_steps(normal_steps),
  .normal_selected(normal_selected),.normal_energies(normal_energies),.normal_error(normal_error),.normal_detail(normal_detail),
  .retry_count(retry_count),.diagnostic_replaced(),.current_search_tag(),.recovery_count(recovery_count),.recovery_waiting(recovery_waiting),
  .shared_in_valid(rrc_valid),.shared_in_start(rrc_start),.shared_in_tag(rrc_tag),.shared_in_data(rrc_data),
  .shared_ready(pool_ready),.shared_generation(history_generation),.shared_latest(latest_tag),.shared_count(history_count),.shared_restart(pool_restart),
  .shared_read_enable(read_enable),.shared_read_address(read_address),.shared_read_word(read_word),
  .frontend_abort(frontend_abort),.search_applied(search_applied),.frontend_window_ready(window_ready),
  .frontend_context(active_context),.frontend_generation(frontend_generation),
  .startup_commit_valid(startup_commit),.startup_commit_context(commit_context),.startup_commit_id(commit_id),.startup_peak_tag(startup_peak));
 causal_tracking_timing_cost_probe #(.EXTERNAL_RESERVATION(EXTERNAL_RESERVATION)) timing(
  .clk(clk),.rst(rst),.cancel(frontend_abort),.in_valid(rrc_valid),.in_ready(pool_ready),.in_start(rrc_start),.in_tag(rrc_tag),.in_data(rrc_data),
  .external_read_enable(pool_read_enable),.external_read_address(pool_read_address),.read_word(read_word),.external_read_reservation(pool_read_reservation),
  .arm_valid(arm_valid),.arm_ready(timing_arm_ready),.arm_id(bound_id),.arm_generation(history_generation),.arm_first_tag(first_public),
  .timing_update(timing_update),.timing_id(timing_id),.timing_generation(timing_generation),.timing_epoch(timing_epoch),
  .timing_position(timing_position),.training_position(training_position),.next_position(next_position),.symbol_step(symbol_step),.peaks_used(peaks_used),
  .diagnostic_valid(diagnostic_valid),.diagnostic_ready(diagnostic_ready),.diagnostic_data(diagnostic_data),.diagnostic_replaced(diagnostic_replaced),
  .tracking_locked(tracking_locked),.reacquire_request(timing_request),.reacquire_reason(timing_reason),.windows_started(),.missed_windows(),
  .history_generation(history_generation),.latest_tag(latest_tag),.history_count(history_count),.history_restart(pool_restart),.active(timing_active));
endmodule
