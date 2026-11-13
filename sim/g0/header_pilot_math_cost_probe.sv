`timescale 1ns/1ps
// Actual header and pilot coherent operations share one DSP/quarter ROM.
// Pilot config is one observation with its causal nominal phase correction.
module header_pilot_math_cost_probe(
 input wire clk,rst,cancel,cfg_valid,output wire cfg_ready,
 input wire cfg_pilot,input wire [31:0] cfg_id,input wire [103:0] cfg_h,
 input wire [31:0] cfg_phase_step,cfg_pilot_phase,
 input wire in_valid,output wire in_ready,input wire [31:0] in_id,
 input wire [8:0] in_index,input wire in_error,input wire [83:0] in_data,
 output wire header_valid,input wire header_ready,output wire [31:0] header_id,
 output wire [2:0] header_error,output wire [191:0] header_raw,
 output wire [31:0] header_session,header_superframe,output wire [15:0] header_stream0_bytes,header_stream1_bytes,
 output wire pilot_valid,input wire pilot_ready,output wire [31:0] pilot_id,pilot_phase,
 output wire [2:0] pilot_error,output wire [5:0] pilot_shift,
 output wire signed [26:0] pilot_x,pilot_y
);
 wire math_ready,math_valid,math_pilot,math_out_ready;wire [31:0] math_id;
 wire [2:0] math_error;wire signed [67:0] math_real,math_imag;
 wire angle_ready,angle_valid,angle_error;wire [31:0] angle_id,angle_phase;wire [5:0] angle_shift;wire signed [26:0] angle_x,angle_y;
 wire raw_error=math_valid && math_pilot && math_error!=0;
 // One pilot may be computing/held while a complete header uses the same DSP.
 // A second pilot waits for the angle slot; this does not gate header work.
 assign cfg_ready=math_ready && (!cfg_pilot || angle_ready);
 assign math_out_ready=math_pilot?(math_error!=0?pilot_ready:angle_ready):header_ready;
 assign header_valid=math_valid && !math_pilot && !rst && !cancel;
 assign header_id=math_id;assign header_error=math_error;
 assign pilot_valid=(angle_valid || raw_error) && !rst && !cancel;
 assign pilot_id=raw_error?math_id:angle_id;
 assign pilot_phase=raw_error?32'd0:angle_phase;
 assign pilot_error=raw_error?(math_error==5?3'd2:3'd3):(angle_error?3'd1:3'd0);
 assign pilot_shift=raw_error?6'd0:angle_shift;
 assign pilot_x=raw_error?27'sd0:angle_x;assign pilot_y=raw_error?27'sd0:angle_y;
 bpsk_header_cost_probe #(.ENABLE_PILOT(1)) math_core(
  .clk(clk),.rst(rst),.cancel(cancel),.cfg_valid(cfg_valid && (!cfg_pilot || angle_ready)),.cfg_ready(math_ready),
  .cfg_id(cfg_id),.cfg_h(cfg_h),.cfg_phase_step(cfg_phase_step),.cfg_pilot(cfg_pilot),.cfg_pilot_phase(cfg_pilot_phase),
  .in_valid(in_valid),.in_ready(in_ready),.in_id(in_id),.in_index(in_index),.in_error(in_error),.in_data(in_data),
  .out_valid(math_valid),.out_ready(math_out_ready),.out_id(math_id),.out_error(math_error),.out_raw_header(header_raw),
  .out_session(header_session),.out_superframe(header_superframe),.out_stream0_bytes(header_stream0_bytes),.out_stream1_bytes(header_stream1_bytes),
  .out_pilot(math_pilot),.out_pilot_real(math_real),.out_pilot_imag(math_imag));
 pilot_angle_cost_probe observation(.clk(clk),.rst(rst),.cancel(cancel),
  .in_valid(math_valid && math_pilot && math_error==0),.in_ready(angle_ready),.in_id(math_id),.in_real(math_real),.in_imag(math_imag),
  .out_valid(angle_valid),.out_ready(pilot_ready),.out_id(angle_id),.out_phase(angle_phase),.out_error(angle_error),
  .out_shift(angle_shift),.out_x(angle_x),.out_y(angle_y));
endmodule
