`timescale 1ns/1ps
// Actual group interpolation -> identity-checked sample join -> H/y rotation -> ML.
// SG node availability and actual shared-history sample scheduling belong to
// the frame coordinator; this arithmetic domain never invents missing inputs.
module payload_datapath_cost_probe(
 input wire clk,rst,cancel,frame_valid,output wire frame_ready,
 input wire [31:0] frame_id,frame_context,frame_generation,input wire [15:0] frame_epoch,input wire [207:0] frame_h,
 input wire group_valid,output wire group_ready,input wire [31:0] group_id,group_context,group_generation,input wire [15:0] group_epoch,
 input wire [8:0] group_index,input wire [63:0] group_nominal,group_steps,
 input wire signed [40:0] group_left0,group_right0,group_left1,group_right1,
 input wire in_valid,output wire in_ready,input wire [31:0] in_id,in_context,in_generation,
 input wire [15:0] in_epoch,input wire [14:0] in_symbol,input wire [2:0] in_error,input wire [83:0] in_y,
 output wire out_valid,input wire out_ready,output wire [31:0] out_id,out_context,out_generation,
 output wire [15:0] out_epoch,output wire [14:0] out_symbol,output wire [2:0] out_error,
 output wire [3:0] out_decision,output wire [61:0] out_metric,output wire busy
);
 reg configured;wire phase_cfg_ready,phase_valid,phase_ready,phase_busy,math_ready,math_cfg_ready,math_busy;
 wire [31:0] id,context_id,generation;wire [15:0] epoch;wire [14:0] symbol;wire [63:0] phases;wire phase_error;
 assign frame_ready=math_cfg_ready && phase_cfg_ready;
 assign group_ready=phase_cfg_ready && configured && !frame_valid;
 assign in_ready=phase_valid && math_ready;
 assign phase_ready=in_valid && math_ready;
 assign busy=phase_busy || math_busy;
 wire identity_bad=in_id!=id || in_context!=context_id || in_generation!=generation || in_epoch!=epoch || in_symbol!=symbol;
 wire [2:0] error=phase_error?3'd1:(identity_bad?3'd3:in_error);
 always @(posedge clk)begin if(rst || cancel)configured<=0;else if(frame_valid && frame_ready)configured<=1;end
 payload_phase_cost_probe phasor(.clk(clk),.rst(rst),.cancel(cancel),.cfg_valid(group_valid && configured && !frame_valid),.cfg_ready(phase_cfg_ready),
  .cfg_id(group_id),.cfg_context(group_context),.cfg_generation(group_generation),.cfg_epoch(group_epoch),.cfg_group(group_index),.cfg_nominal(group_nominal),.cfg_steps(group_steps),
  .cfg_left0(group_left0),.cfg_right0(group_right0),.cfg_left1(group_left1),.cfg_right1(group_right1),
  .out_valid(phase_valid),.out_ready(phase_ready),.out_id(id),.out_context(context_id),.out_generation(generation),.out_epoch(epoch),.out_group(),.out_index(),.out_symbol(symbol),
  .out_phases(phases),.out_residual0(),.out_residual1(),.out_error(phase_error),.out_last(),.busy(phase_busy));
 payload_math_cost_probe math(.clk(clk),.rst(rst),.cancel(cancel),.cfg_valid(frame_valid && phase_cfg_ready),.cfg_ready(math_cfg_ready),
  .cfg_id(frame_id),.cfg_context(frame_context),.cfg_generation(frame_generation),.cfg_epoch(frame_epoch),.cfg_h(frame_h),
  .in_valid(in_valid && phase_valid),.in_ready(math_ready),.in_id(id),.in_context(context_id),.in_generation(generation),.in_epoch(epoch),.in_symbol(symbol),.in_error(error),.in_y(in_y),.in_phases(phases),
  .out_valid(out_valid),.out_ready(out_ready),.out_id(out_id),.out_context(out_context),.out_generation(out_generation),.out_epoch(out_epoch),.out_symbol(out_symbol),.out_error(out_error),.out_decision(out_decision),.out_metric(out_metric),.busy(math_busy));
endmodule
