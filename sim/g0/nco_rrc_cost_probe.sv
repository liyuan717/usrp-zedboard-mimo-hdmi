`timescale 1ns/1ps
// G0 connected numerical islands only: no estimator, notch or interpolation.
module nco_rrc_cost_probe #(
 parameter SINE_FILE="sine.hex", COEFFICIENT_FILE="rrc.hex"
)(
 input wire clk,rst,
 input wire cfg_valid,output wire cfg_ready,
 input wire [63:0] cfg_tag,input wire [31:0] cfg_phase,cfg_step,
 input wire in_valid,output wire in_ready,input wire [63:0] in_tag,input wire [63:0] in_data,
 output wire out_valid,input wire out_ready,output wire [63:0] out_tag,
 output wire [79:0] out_data,output wire armed,
 output wire [31:0] nco_fences,rrc_fences,output wire out_start,
 output wire [31:0] out_context
);
 wire valid,ready,start;
 wire [63:0] tag;
 wire [71:0] data;
 // NCO configuration is admitted only after its older outputs drain. RRC
 // work already admitted retains its own context through any later config.
 reg [31:0] cfg_context;
 always @(posedge clk) begin
  if(rst) cfg_context<=0;
  else if(cfg_valid && cfg_ready) cfg_context<=cfg_context+32'd1;
 end
 nco_cost_probe #(.SINE_FILE(SINE_FILE)) nco(
  clk,rst,cfg_valid,cfg_ready,cfg_tag,cfg_phase,cfg_step,
  in_valid,in_ready,in_tag,in_data,valid,ready,tag,start,data,armed,nco_fences,1'b0,,);
 rrc_cost_probe #(.COEFFICIENT_FILE(COEFFICIENT_FILE)) rrc(
  clk,rst,valid,ready,start,tag,data,out_valid,out_ready,out_tag,out_data,rrc_fences,out_start,cfg_context,out_context);
endmodule
