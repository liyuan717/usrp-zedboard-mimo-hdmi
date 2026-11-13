`timescale 1ns/1ps
// Actual no-ready CPACK ingress -> fail-stop elastic boundary -> shared frontend.
module continuous_training_frontend_cost_probe(
 input wire clk,rst,cancel,
 input wire cfg_valid,output wire cfg_ready,
 input wire [63:0] cfg_tag,input wire [31:0] cfg_phase,cfg_step,
 input wire in_valid,output wire raw_admitted,
 input wire [63:0] in_tag,input wire [63:0] in_data,
 input wire job_valid,output wire job_ready,input wire [31:0] job_id,
 input wire [31:0] job_context,job_generation,input wire [15:0] job_epoch,
 input wire [79:0] job_first_position,input wire [34:0] job_step,
 output wire out_valid,input wire out_ready,
 output wire [31:0] out_id,out_context,out_generation,
 output wire [15:0] out_epoch,
 output wire [207:0] out_h,output wire [63:0] out_phase_steps,
 output wire [1:0] out_selected_rx,output wire [199:0] out_energies,
 output wire [2:0] out_error,out_detail,
 output wire armed,window_ready,request_idle,
 output wire [31:0] active_context,history_generation,
 output wire [63:0] latest_tag,output wire [9:0] history_count,
 output wire history_restart,
 input wire job_startup,input wire [8:0] job_count,input wire [9:0] job_first_symbol,
 output wire out_startup,output wire signed [47:0] out_numerator,
 output wire [15:0] out_divisor,output wire [72:0] out_quality_sum,
 output wire [71:0] out_coherent,output wire out_admitted,
 input wire [63:0] job_commit_tag,input wire [47:0] job_phase_origin_index,
 output wire [63:0] out_commit_tag,output wire [31:0] out_final_step,out_final_phase,
 output wire [1:0] out_config_error,
 input wire cfg_conjugate,output wire cfg_applied,output wire [1:0] cfg_error,
 output wire out_committed,output wire [31:0] out_commit_context,
 input wire ingress_rearm,ingress_upstream_loss,
 output wire ingress_cancel,ingress_running,
 output wire [31:0] ingress_generation,ingress_faults,
 output wire [2:0] ingress_reason,output wire [7:0] ingress_occupancy,ingress_peak
);
 // One synchronous cancellation per ingress failure/rearm interval. Keeping
 // the guard halted must not advance receiver context on every clock.
 reg ingress_cancel_q;
 always @(posedge clk) begin
  if(rst) ingress_cancel_q<=0;
  else ingress_cancel_q<=ingress_cancel;
 end
 wire core_abort=cancel || (ingress_cancel && !ingress_cancel_q);
 wire core_out_valid,core_job_ready;
 assign out_valid=core_out_valid && !ingress_cancel && !rst && !cancel;
 assign job_ready=core_job_ready && !ingress_cancel && !rst && !cancel;
 wire buffer_valid,buffer_ready;wire [63:0] buffer_tag,buffer_data;
 continuous_adc_ingress_cost_probe ingress(
 .clk(clk),.rst(rst || cancel),.rearm(ingress_rearm),.raw_valid(in_valid),.raw_tag(in_tag),.raw_data(in_data),.upstream_loss(ingress_upstream_loss),
 .out_valid(buffer_valid),.out_ready(buffer_ready),.out_tag(buffer_tag),.out_data(buffer_data),.out_start(),.receiver_cancel(ingress_cancel),.running(ingress_running),
 .generation(ingress_generation),.fault_count(ingress_faults),.last_fault(ingress_reason),.occupancy(ingress_occupancy),.high_water(ingress_peak),.admitted(raw_admitted),.dropped());
 training_frontend_cost_probe #(.ENABLE_STARTUP(1),.ENABLE_FINAL_CONFIG(1),.ENABLE_TIMED_CONFIG(1),.ENABLE_CONJUGATE(1),.ENABLE_AUTOCOMMIT(1)) core(
.clk(clk),
.rst(rst),
.cancel(core_abort),
.cfg_valid(cfg_valid),
.cfg_ready(cfg_ready),
.cfg_tag(cfg_tag),
.cfg_phase(cfg_phase),
.cfg_step(cfg_step),
.in_valid(buffer_valid),
.in_ready(buffer_ready),
.in_tag(buffer_tag),
.in_data(buffer_data),
.job_valid(job_valid && !ingress_cancel),
.job_ready(core_job_ready),
.job_id(job_id),
.job_context(job_context),
.job_generation(job_generation),
.job_epoch(job_epoch),
.job_first_position(job_first_position),
.job_step(job_step),
.out_valid(core_out_valid),
.out_ready(out_ready),
.out_id(out_id),
.out_context(out_context),
.out_generation(out_generation),
.out_epoch(out_epoch),
.out_h(out_h),
.out_phase_steps(out_phase_steps),
.out_selected_rx(out_selected_rx),
.out_energies(out_energies),
.out_error(out_error),
.out_detail(out_detail),
.armed(armed),
.window_ready(window_ready),
.request_idle(request_idle),
.active_context(active_context),
.history_generation(history_generation),
.latest_tag(latest_tag),
.history_count(history_count),
.history_restart(history_restart),
.job_startup(job_startup),
.job_count(job_count),
.job_first_symbol(job_first_symbol),
.out_startup(out_startup),
.out_numerator(out_numerator),
.out_divisor(out_divisor),
.out_quality_sum(out_quality_sum),
.out_coherent(out_coherent),
.out_admitted(out_admitted),
.job_commit_tag(job_commit_tag),
.job_phase_origin_index(job_phase_origin_index),
.out_commit_tag(out_commit_tag),
.out_final_step(out_final_step),
.out_final_phase(out_final_phase),
.out_config_error(out_config_error),
.cfg_conjugate(cfg_conjugate),
.cfg_applied(cfg_applied),
.cfg_error(cfg_error),
.out_committed(out_committed),
.out_commit_context(out_commit_context));
endmodule
