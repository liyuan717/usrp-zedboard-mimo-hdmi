`timescale 1ns/1ps
// Shared header/pilot arithmetic -> bounded two-TX seven-point phase tracking.
// Input jobs are real coherent sample vectors, never precomputed phase truth.
// One pending pilot owns the sideband identity until the smoother accepts it.
// A smoothing frame abort is an explicit fence for this whole ownership domain,
// including any simultaneous header work. Upstream must restart after the fence.
module pilot_tracking_cost_probe #(parameter FRAME_LIMIT=1000000)(
 input wire clk,rst,cancel,frame_valid,output wire frame_ready,
 input wire [31:0] frame_id,frame_context,frame_generation,input wire [15:0] frame_epoch,
 input wire cfg_valid,output wire cfg_ready,input wire cfg_pilot,
 input wire [31:0] cfg_id,cfg_context,cfg_generation,input wire [15:0] cfg_epoch,
 input wire [8:0] cfg_point,input wire cfg_tx,input wire [103:0] cfg_h,
 input wire [31:0] cfg_phase_step,cfg_pilot_phase,
 input wire in_valid,output wire in_ready,input wire [31:0] in_id,
 input wire [8:0] in_index,input wire in_error,input wire [83:0] in_data,
 output wire header_valid,input wire header_ready,output wire [31:0] header_id,
 output wire [2:0] header_error,output wire [191:0] header_raw,
 output wire [31:0] header_session,header_superframe,
 output wire [15:0] header_stream0_bytes,header_stream1_bytes,
 output wire out_valid,input wire out_ready,
 output wire [31:0] out_id,out_context,out_generation,
 output wire [15:0] out_epoch,output wire [8:0] out_point,out_known_point,
 output wire out_tx,output wire signed [40:0] out_phase,
 output wire frame_abort,output wire [1:0] abort_reason,
 output wire [8:0] abort_point,output wire active,observation_accepted
);
 wire math_ready,pilot_valid,pilot_ready,smooth_ready,smooth_cfg_ready;
 wire [31:0] pilot_id,pilot_phase;wire [2:0] pilot_error;
 reg pending;reg [31:0] owner_context,owner_generation;
 reg [15:0] owner_epoch;reg [8:0] owner_point;reg owner_tx;
 wire fence=cancel || frame_abort;
 wire admissible=!cfg_pilot || (active && !pending);
 assign cfg_ready=math_ready && admissible && !fence;
 assign frame_ready=smooth_cfg_ready && !pending && !fence;
 assign pilot_ready=smooth_ready && pending;
 assign observation_accepted=pilot_valid && pilot_ready;
 always @(posedge clk)begin
  if(rst || fence)begin
   pending<=0;owner_context<=0;owner_generation<=0;owner_epoch<=0;owner_point<=0;owner_tx<=0;
  end else begin
   if(pilot_valid && pilot_ready)pending<=0;
   if(cfg_valid && cfg_ready && cfg_pilot)begin
    pending<=1;owner_context<=cfg_context;owner_generation<=cfg_generation;
    owner_epoch<=cfg_epoch;owner_point<=cfg_point;owner_tx<=cfg_tx;
   end
  end
 end
 header_pilot_math_cost_probe math(.clk(clk),.rst(rst),.cancel(fence),
  .cfg_valid(cfg_valid && admissible && !fence),.cfg_ready(math_ready),.cfg_pilot(cfg_pilot),
  .cfg_id(cfg_id),.cfg_h(cfg_h),.cfg_phase_step(cfg_phase_step),.cfg_pilot_phase(cfg_pilot_phase),
  .in_valid(in_valid),.in_ready(in_ready),.in_id(in_id),.in_index(in_index),.in_error(in_error),.in_data(in_data),
  .header_valid(header_valid),.header_ready(header_ready),.header_id(header_id),.header_error(header_error),
  .header_raw(header_raw),.header_session(header_session),.header_superframe(header_superframe),
  .header_stream0_bytes(header_stream0_bytes),.header_stream1_bytes(header_stream1_bytes),
  .pilot_valid(pilot_valid),.pilot_ready(pilot_ready),.pilot_id(pilot_id),.pilot_phase(pilot_phase),
  .pilot_error(pilot_error),.pilot_shift(),.pilot_x(),.pilot_y());
 pilot_smoothing_cost_probe #(.FRAME_LIMIT(FRAME_LIMIT)) smoothing(.clk(clk),.rst(rst),.cancel(cancel),
  .cfg_valid(frame_valid && !pending && !fence),.cfg_ready(smooth_cfg_ready),
  .cfg_id(frame_id),.cfg_context(frame_context),.cfg_generation(frame_generation),.cfg_epoch(frame_epoch),
  .in_valid(pilot_valid && pending),.in_ready(smooth_ready),.in_id(pilot_id),
  .in_context(owner_context),.in_generation(owner_generation),.in_epoch(owner_epoch),
  .in_point(owner_point),.in_tx(owner_tx),.in_error(pilot_error!=0),.in_phase(pilot_phase),
  .out_valid(out_valid),.out_ready(out_ready),.out_id(out_id),.out_context(out_context),
  .out_generation(out_generation),.out_epoch(out_epoch),.out_point(out_point),.out_known_point(out_known_point),
  .out_tx(out_tx),.out_phase(out_phase),.frame_abort(frame_abort),.abort_reason(abort_reason),
  .abort_point(abort_point),.active(active));
endmodule
