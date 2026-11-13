`timescale 1ns/1ps
// G0 configured chain, not a timing/CFO estimator or complete PHY.
// NCO->RRC carries an explicit context; history binds descriptors only after
// the FIRST VALID filtered sample of that context is actually written.
module frontend_chain_cost_probe #(
 parameter SINE_FILE="sine.hex",COEFFICIENT_FILE="rrc.hex",WAIT_LIMIT=4096,ENABLE_CONJUGATE=0,ENABLE_TIMED_CONFIG=0,EXTERNAL_HISTORY=0
)(
 input wire clk,rst,cancel,
 input wire cfg_valid,output wire cfg_ready,
 input wire [63:0] cfg_tag,input wire [31:0] cfg_phase,cfg_step,
 input wire in_valid,output wire in_ready,
 input wire [63:0] in_tag,input wire [63:0] in_data,
 input wire timing_cfg_valid,output wire timing_cfg_ready,
 input wire [15:0] timing_cfg_epoch,
 input wire [31:0] timing_cfg_context,
 input wire [79:0] timing_cfg_first_position,
 input wire [34:0] timing_cfg_step,
 input wire [15:0] timing_cfg_count,input wire [31:0] timing_cfg_first_id,
 output wire timing_done,output wire [1:0] timing_cfg_error,
 output wire [15:0] timing_emitted_count,
 output wire out_valid,input wire out_ready,
 output wire [31:0] out_generation,out_id,output wire [63:0] out_tag,
 output wire [15:0] out_mu,output wire [2:0] out_error,
 output wire [83:0] out_data,
 output wire armed,output wire window_ready,
 output reg [31:0] active_context,bound_generation,
 output wire [31:0] history_generation,output wire [63:0] latest_tag,
 output wire [9:0] history_count,
 output wire [31:0] nco_fences,rrc_fences,history_fences,
 input wire request_cancel,output wire request_idle,output wire history_restart,
 input wire cfg_conjugate,output wire cfg_applied,output wire [1:0] cfg_error,
 output wire shared_in_valid,shared_in_start,output wire [63:0] shared_in_tag,output wire [79:0] shared_in_data,
 input wire shared_ready,input wire [31:0] shared_generation,input wire [63:0] shared_latest,
 input wire [12:0] shared_count,input wire shared_restart,
 output wire shared_read_enable,output wire [11:0] shared_read_address,input wire [79:0] shared_read_word
);
 wire core_reset=rst || cancel;
 wire cfg_fire=cfg_applied;
 reg window_established;
 wire mixed_valid,mixed_ready,mixed_start;
 wire [63:0] mixed_tag;
 wire [71:0] mixed_data;
 wire filtered_valid,filtered_ready,filtered_start;
 wire [63:0] filtered_tag;
 wire [79:0] filtered_data;
 wire [31:0] filtered_context;
 assign shared_in_valid=filtered_valid;
 assign shared_in_start=filtered_start;
 assign shared_in_tag=filtered_tag;assign shared_in_data=filtered_data;
 wire req_valid,req_ready;
 wire [31:0] req_generation,req_id;
 wire [63:0] req_tag;
 wire [15:0] req_mu;
 wire generator_cfg_ready;
 wire interpolation_request_idle;
 wire [1:0] generator_cfg_error;
 reg stale_timing_error;
 wire timing_context_matches=timing_cfg_context==active_context;

 // A new NCO descriptor withdraws unaccepted timing requests, but DOES NOT
 // retract accepted/held interpolation responses. They retain their identity.
 wire generator_cancel=cancel || cfg_fire || request_cancel;
 // Empty request path does not imply a usable history window. Keep this
 // separate from window_ready; cancellation never resets NCO/RRC/history.
 assign request_idle=!core_reset && !request_cancel && generator_cfg_ready && interpolation_request_idle;
 assign window_ready=!core_reset && !cfg_valid && armed && window_established;
 assign timing_cfg_ready=window_ready && generator_cfg_ready;
 // A descriptor held across a frequency reconfiguration must not silently
 // acquire the new context. Accept it as an explicit control error, no data.
 assign timing_cfg_error=stale_timing_error?2'd3:generator_cfg_error;
 always @(posedge clk) begin
  if(core_reset || cfg_fire || request_cancel) stale_timing_error<=0;
  else stale_timing_error<=timing_cfg_valid && timing_cfg_ready && !timing_context_matches;
 end
 always @(posedge clk) begin
  if(rst) begin
   active_context<=0;bound_generation<=0;window_established<=0;
  end else if(cancel) begin
   // Preserve context separation across cancellation; global reset is the
   // boundary at which every producer/consumer must discard old ownership.
   active_context<=active_context+32'd1;bound_generation<=0;window_established<=0;
  end else if(cfg_fire) begin
   active_context<=active_context+32'd1;window_established<=0;
  end else if(filtered_valid && filtered_ready && filtered_start &&
              filtered_context==active_context) begin
   // in_start forces exactly this history-generation increment on this edge.
   bound_generation<=history_generation+32'd1;window_established<=1;
  end
 end

 nco_cost_probe #(.SINE_FILE(SINE_FILE),.ENABLE_CONJUGATE(ENABLE_CONJUGATE),.ENABLE_TIMED_CONFIG(ENABLE_TIMED_CONFIG)) nco(
  .clk(clk),.rst(core_reset),.cfg_valid(cfg_valid),.cfg_ready(cfg_ready),
  .cfg_tag(cfg_tag),.cfg_phase(cfg_phase),.cfg_step(cfg_step),.cfg_conjugate(cfg_conjugate),.cfg_applied(cfg_applied),.cfg_error(cfg_error),
  .in_valid(in_valid),.in_ready(in_ready),.in_tag(in_tag),.in_data(in_data),
  .out_valid(mixed_valid),.out_ready(mixed_ready),.out_tag(mixed_tag),
  .out_start(mixed_start),.out_data(mixed_data),.armed(armed),.fence_count(nco_fences));
 rrc_cost_probe #(.COEFFICIENT_FILE(COEFFICIENT_FILE)) rrc(
  .clk(clk),.rst(core_reset),.in_valid(mixed_valid),.in_ready(mixed_ready),
  .in_start(mixed_start),.in_tag(mixed_tag),.in_data(mixed_data),
  .out_valid(filtered_valid),.out_ready(filtered_ready),.out_tag(filtered_tag),
  .out_data(filtered_data),.fence_count(rrc_fences),.out_start(filtered_start),
  .in_context(active_context),.out_context(filtered_context));
 timing_request_cost_probe timing(
  .clk(clk),.rst(rst),.cancel(generator_cancel),
  .cfg_valid(timing_cfg_valid && window_ready),.cfg_ready(generator_cfg_ready),
  .cfg_generation(bound_generation),.cfg_epoch(timing_cfg_epoch),
  .cfg_first_position(timing_cfg_first_position),.cfg_step(timing_cfg_step),
  .cfg_count(timing_context_matches?timing_cfg_count:16'd0),.cfg_first_id(timing_cfg_first_id),
  .req_valid(req_valid),.req_ready(req_ready),.req_generation(req_generation),
  .req_id(req_id),.req_tag(req_tag),.req_mu(req_mu),.done(timing_done),
  .cfg_error(generator_cfg_error),.emitted_count(timing_emitted_count));
 interpolation_cost_probe #(.WAIT_LIMIT(WAIT_LIMIT),.OUT_FRACTION_BITS(1),.EXTERNAL_HISTORY(EXTERNAL_HISTORY)) interp(
  .clk(clk),.rst(rst),.cancel(cancel),
  .in_valid(filtered_valid),.in_ready(filtered_ready),.in_start(filtered_start),
  .in_tag(filtered_tag),.in_data(filtered_data),
  .req_valid(req_valid),.req_ready(req_ready),.req_generation(req_generation),
  .req_id(req_id),.req_tag(req_tag),.req_mu(req_mu),
  .out_valid(out_valid),.out_ready(out_ready),.out_generation(out_generation),
  .out_id(out_id),.out_tag(out_tag),.out_mu(out_mu),.out_error(out_error),.out_data(out_data),
  .history_generation(history_generation),.latest_tag(latest_tag),
  .history_count(history_count),.fence_count(history_fences),
  .request_cancel(request_cancel),.request_idle(interpolation_request_idle),.history_restart(history_restart),
  .shared_ready(shared_ready),.shared_generation(shared_generation),.shared_latest(shared_latest),.shared_count(shared_count),.shared_restart(shared_restart),
  .shared_read_enable(shared_read_enable),.shared_read_address(shared_read_address),.shared_read_word(shared_read_word));
endmodule
