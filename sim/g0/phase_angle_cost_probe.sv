`timescale 1ns/1ps
// G0 reusable configured numerical island: signed27 atan2, phase32 turns.
// No training detector, CFO estimator, RF measurement or complete PHY.
// signed61 coordinates preserve32 fractional bits and2 guard bits.
// 32 iterations, nearest-integer angle table, arithmetic-floor coordinate
// shifts. y==0 holds the recurrence; zero vector has error1 and phase0.
// Mathematical +pi and -pi both map to0x80000000 (signed -pi).
module phase_angle_cost_probe(
 input wire clk,rst,cancel,
 input wire in_valid,output wire in_ready,input wire [31:0] in_id,
 input wire signed [26:0] in_x,in_y,
 output reg out_valid,input wire out_ready,
 output reg [31:0] out_id,out_phase,output reg out_error
);
 reg busy,work_zero;
 reg [4:0] iteration;
 reg signed [60:0] x,y;
 reg [31:0] phase,work_id;
 wire signed [60:0] extended_x={{34{in_x[26]}},in_x};
 wire signed [60:0] extended_y={{34{in_y[26]}},in_y};
 wire signed [60:0] scaled_x=extended_x<<<32;
 wire signed [60:0] scaled_y=extended_y<<<32;
 function [31:0] atan_turns(input [4:0] index);
 begin case(index)
 5'd0:atan_turns=32'h20000000;
 5'd1:atan_turns=32'h12e4051e;
 5'd2:atan_turns=32'h09fb385b;
 5'd3:atan_turns=32'h051111d4;
 5'd4:atan_turns=32'h028b0d43;
 5'd5:atan_turns=32'h0145d7e1;
 5'd6:atan_turns=32'h00a2f61e;
 5'd7:atan_turns=32'h00517c55;
 5'd8:atan_turns=32'h0028be53;
 5'd9:atan_turns=32'h00145f2f;
 5'd10:atan_turns=32'h000a2f98;
 5'd11:atan_turns=32'h000517cc;
 5'd12:atan_turns=32'h00028be6;
 5'd13:atan_turns=32'h000145f3;
 5'd14:atan_turns=32'h0000a2fa;
 5'd15:atan_turns=32'h0000517d;
 5'd16:atan_turns=32'h000028be;
 5'd17:atan_turns=32'h0000145f;
 5'd18:atan_turns=32'h00000a30;
 5'd19:atan_turns=32'h00000518;
 5'd20:atan_turns=32'h0000028c;
 5'd21:atan_turns=32'h00000146;
 5'd22:atan_turns=32'h000000a3;
 5'd23:atan_turns=32'h00000051;
 5'd24:atan_turns=32'h00000029;
 5'd25:atan_turns=32'h00000014;
 5'd26:atan_turns=32'h0000000a;
 5'd27:atan_turns=32'h00000005;
 5'd28:atan_turns=32'h00000003;
 5'd29:atan_turns=32'h00000001;
 5'd30:atan_turns=32'h00000001;
 5'd31:atan_turns=32'h00000000;
 default:atan_turns=0;
 endcase end
 endfunction
 wire signed [60:0] shifted_x=x>>>iteration,shifted_y=y>>>iteration;
 wire signed [60:0] next_x=y>0?x+shifted_y:(y<0?x-shifted_y:x);
 wire signed [60:0] next_y=y>0?y-shifted_x:(y<0?y+shifted_x:y);
 wire [31:0] angle=atan_turns(iteration);
 wire [31:0] next_phase=y>0?phase+angle:(y<0?phase-angle:phase);
 assign in_ready=!rst && !cancel && !busy && (!out_valid || out_ready);
 always @(posedge clk) begin
  if(rst) begin
   busy<=0;work_zero<=0;iteration<=0;x<=0;y<=0;phase<=0;work_id<=0;
   out_valid<=0;out_id<=0;out_phase<=0;out_error<=0;
  end else if(cancel) begin
   busy<=0;out_valid<=0;iteration<=0;work_zero<=0;
  end else begin
   if(out_valid && out_ready) out_valid<=0;
   if(busy) begin
    x<=next_x;y<=next_y;phase<=next_phase;
    if(iteration==31) begin
     busy<=0;out_valid<=1;out_id<=work_id;
     out_error<=work_zero;out_phase<=work_zero?32'd0:next_phase;
    end else iteration<=iteration+1'b1;
   end
   if(in_valid && in_ready) begin
    busy<=1;iteration<=0;work_id<=in_id;work_zero<=(in_x==0 && in_y==0);
    if(in_x<0) begin x<=-scaled_x;y<=-scaled_y;phase<=32'h80000000;end
    else begin x<=scaled_x;y<=scaled_y;phase<=0;end
   end
  end
 end
endmodule
