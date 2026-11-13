`timescale 1ns/1ps
// Actual full precision rotation -> exact16-way ML, with one elastic result each.
// Configuration only when this whole arithmetic ownership domain is quiescent.
module payload_math_cost_probe(
 input wire clk,rst,cancel,cfg_valid,output wire cfg_ready,
 input wire [31:0] cfg_id,cfg_context,cfg_generation,input wire [15:0] cfg_epoch,input wire [207:0] cfg_h,
 input wire in_valid,output wire in_ready,input wire [31:0] in_id,in_context,in_generation,
 input wire [15:0] in_epoch,input wire [14:0] in_symbol,input wire [2:0] in_error,input wire [83:0] in_y,input wire [63:0] in_phases,
 output wire out_valid,input wire out_ready,output wire [31:0] out_id,out_context,out_generation,
 output wire [15:0] out_epoch,output wire [14:0] out_symbol,output wire [2:0] out_error,
 output wire [3:0] out_decision,output wire [61:0] out_metric,output wire busy
);
 wire rotate_ready,rotate_valid,rotate_pop,rotate_busy,ml_busy;wire [31:0] id,context_id,generation;wire [15:0] epoch;wire [14:0] symbol;
 wire [2:0] error;wire [103:0] y;wire [215:0] h;
 assign cfg_ready=rotate_ready && !ml_busy;
 assign busy=rotate_busy || ml_busy;
 payload_rotate_cost_probe rotation(.clk(clk),.rst(rst),.cancel(cancel),.cfg_valid(cfg_valid && !ml_busy),.cfg_ready(rotate_ready),
  .cfg_id(cfg_id),.cfg_context(cfg_context),.cfg_generation(cfg_generation),.cfg_epoch(cfg_epoch),.cfg_h(cfg_h),
  .in_valid(in_valid),.in_ready(in_ready),.in_id(in_id),.in_context(in_context),.in_generation(in_generation),.in_epoch(in_epoch),.in_symbol(in_symbol),.in_error(in_error),.in_y(in_y),.in_phases(in_phases),
  .out_valid(rotate_valid),.out_ready(rotate_pop),.out_id(id),.out_context(context_id),.out_generation(generation),.out_epoch(epoch),.out_symbol(symbol),.out_error(error),.out_y(y),.out_h(h),.busy(rotate_busy));
 payload_ml_cost_probe detector(.clk(clk),.rst(rst),.cancel(cancel),.in_valid(rotate_valid),.in_ready(rotate_pop),
  .in_id(id),.in_context(context_id),.in_generation(generation),.in_epoch(epoch),.in_symbol(symbol),.in_error(error),.in_y(y),.in_h(h),
  .out_valid(out_valid),.out_ready(out_ready),.out_id(out_id),.out_context(out_context),.out_generation(out_generation),.out_epoch(out_epoch),.out_symbol(out_symbol),.out_error(out_error),.out_decision(out_decision),.out_metric(out_metric),.busy(ml_busy));
endmodule
