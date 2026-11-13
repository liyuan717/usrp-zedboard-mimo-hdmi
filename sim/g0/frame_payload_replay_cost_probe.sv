`timescale 1ns/1ps
// ONE shared pool/F1 sampler, protected header -> actual pilots/SG queues -> ML.
// All publication ownership is fenced by the frame controller's explicit abort.
module frame_payload_replay_cost_probe #(parameter WORK_LIMIT=1100000)(
 input wire clk,rst,cancel,cfg_valid,output wire cfg_ready,
 input wire [31:0] cfg_id,cfg_context,cfg_generation,input wire [15:0] cfg_epoch,
 input wire [79:0] cfg_training_position,input wire [34:0] cfg_symbol_step,input wire [207:0] cfg_h,input wire [63:0] cfg_phase_steps,
 input wire window_ready,input wire [31:0] current_context,
 input wire rrc_valid,rrc_start,shared_ready,input wire [63:0] rrc_tag,input wire [79:0] rrc_data,
 input wire [31:0] history_generation,input wire [63:0] latest_tag,input wire [12:0] history_count,input wire history_restart,
 output wire reader_active,read_enable,output wire [11:0] read_address,input wire [79:0] read_word,
 output wire header_valid,input wire header_ready,output wire [31:0] header_id,header_context,header_generation,output wire [15:0] header_epoch,
 output wire [2:0] header_error,header_detail,output wire [191:0] header_raw,output wire [31:0] header_session,header_superframe,output wire [15:0] header_stream0_bytes,header_stream1_bytes,
 output wire out_valid,input wire out_ready,output wire [31:0] out_id,out_context,out_generation,output wire [15:0] out_epoch,
 output wire [14:0] out_symbol,output wire [3:0] out_decision,output wire [61:0] out_metric,
 output wire frame_abort,status_valid,input wire status_ready,output wire [31:0] status_id,status_context,status_generation,output wire [15:0] status_epoch,
 output wire [2:0] status_error,status_detail,output wire [8:0] status_point,output wire read_reservation
);
 wire frame_ready,payload_ready,phase_valid,phase_ready,phase_tx;wire [31:0] phase_id,phase_context,phase_generation;wire [15:0] phase_epoch;
 wire [8:0] phase_point,phase_known;wire signed [40:0] phase_value;
 wire req_valid,req_ready,sample_valid,sample_ready,payload_done,payload_abort,payload_active;wire [31:0] req_generation,req_id,sample_generation,sample_id;
 wire [63:0] req_tag,sample_tag;wire [15:0] req_mu,sample_mu;wire [2:0] sample_error,payload_detail;wire [83:0] sample_data;
 assign cfg_ready=frame_ready && payload_ready;
 frame_replay_cost_probe #(.WORK_LIMIT(WORK_LIMIT),.BUSINESS_ENABLED(1)) frame(.clk(clk),.rst(rst),.cancel(cancel),.cfg_valid(cfg_valid && payload_ready),.cfg_ready(frame_ready),
  .cfg_id(cfg_id),.cfg_context(cfg_context),.cfg_generation(cfg_generation),.cfg_epoch(cfg_epoch),.cfg_training_position(cfg_training_position),.cfg_symbol_step(cfg_symbol_step),.cfg_h(cfg_h),.cfg_phase_steps(cfg_phase_steps),
  .window_ready(window_ready),.current_context(current_context),.rrc_valid(rrc_valid),.rrc_start(rrc_start),.shared_ready(shared_ready),.rrc_tag(rrc_tag),.rrc_data(rrc_data),
  .history_generation(history_generation),.latest_tag(latest_tag),.history_count(history_count),.history_restart(history_restart),.reader_active(reader_active),.read_enable(read_enable),.read_address(read_address),.read_word(read_word),.read_reservation(read_reservation),
  .header_valid(header_valid),.header_ready(header_ready),.header_id(header_id),.header_context(header_context),.header_generation(header_generation),.header_epoch(header_epoch),.header_error(header_error),.header_detail(header_detail),.header_raw(header_raw),.header_session(header_session),.header_superframe(header_superframe),.header_stream0_bytes(header_stream0_bytes),.header_stream1_bytes(header_stream1_bytes),
  .phase_valid(phase_valid),.phase_ready(phase_ready),.phase_id(phase_id),.phase_context(phase_context),.phase_generation(phase_generation),.phase_epoch(phase_epoch),.phase_point(phase_point),.phase_known_point(phase_known),.phase_tx(phase_tx),.phase_value(phase_value),
  .frame_abort(frame_abort),.status_valid(status_valid),.status_ready(status_ready),.status_id(status_id),.status_context(status_context),.status_generation(status_generation),.status_epoch(status_epoch),.status_error(status_error),.status_detail(status_detail),.status_point(status_point),
  .business_req_valid(req_valid),.business_req_ready(req_ready),.business_req_generation(req_generation),.business_req_id(req_id),.business_req_tag(req_tag),.business_req_mu(req_mu),
  .business_sample_valid(sample_valid),.business_sample_ready(sample_ready),.business_sample_generation(sample_generation),.business_sample_id(sample_id),.business_sample_tag(sample_tag),.business_sample_mu(sample_mu),.business_sample_error(sample_error),.business_sample_data(sample_data),
  .business_done(payload_done),.business_abort(payload_abort),.business_detail(payload_detail));
 payload_schedule_cost_probe payload(.clk(clk),.rst(rst),.cancel(cancel || frame_abort),.cfg_valid(cfg_valid && frame_ready),.cfg_ready(payload_ready),
  .cfg_id(cfg_id),.cfg_context(cfg_context),.cfg_generation(cfg_generation),.cfg_epoch(cfg_epoch),.cfg_training_position(cfg_training_position),.cfg_symbol_step(cfg_symbol_step),.cfg_h(cfg_h),.cfg_phase_steps(cfg_phase_steps),
  .trusted_header(header_valid && header_error==0),.node_valid(phase_valid),.node_ready(phase_ready),.node_id(phase_id),.node_context(phase_context),.node_generation(phase_generation),.node_epoch(phase_epoch),.node_point(phase_point),.node_known(phase_known),.node_tx(phase_tx),.node_phase(phase_value),
  .req_valid(req_valid),.req_ready(req_ready),.req_generation(req_generation),.req_id(req_id),.req_tag(req_tag),.req_mu(req_mu),
  .sample_valid(sample_valid),.sample_ready(sample_ready),.sample_generation(sample_generation),.sample_id(sample_id),.sample_tag(sample_tag),.sample_mu(sample_mu),.sample_error(sample_error),.sample_data(sample_data),
  .out_valid(out_valid),.out_ready(out_ready),.out_id(out_id),.out_context(out_context),.out_generation(out_generation),.out_epoch(out_epoch),.out_symbol(out_symbol),.out_decision(out_decision),.out_metric(out_metric),
  .done(payload_done),.abort(payload_abort),.detail(payload_detail),.active(payload_active));
endmodule
