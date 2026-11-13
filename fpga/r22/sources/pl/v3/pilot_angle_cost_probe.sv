`timescale 1ns/1ps
// Full68bit coherent pilot statistic -> common rounded normalization -> atan2.
// No supplied phase/channel truth. Coordinates retain25 magnitude bits plus
// sign guard; current proven27bit CORDIC is reused, not approximated by LUT sign.
module pilot_angle_cost_probe(
 input wire clk,rst,cancel,in_valid,output wire in_ready,input wire [31:0] in_id,
 input wire signed [67:0] in_real,in_imag,
 output wire out_valid,input wire out_ready,output reg [31:0] out_id,out_phase,
 output reg out_error,output reg [5:0] out_shift,
 output reg signed [26:0] out_x,out_y
);
 localparam IDLE=0,PREPARE=1,NORMALIZE=2,START=3,WAIT_ANGLE=4;
 reg [2:0] state;reg valid;reg [31:0] owner;reg signed [67:0] real_value,imag_value;
 reg [5:0] shift;
 wire signed [68:0] wide_real={real_value[67],real_value},wide_imag={imag_value[67],imag_value};
 wire [68:0] abs_real=wide_real<0?-wide_real:wide_real,abs_imag=wide_imag<0?-wide_imag:wide_imag;
 wire [68:0] magnitude=abs_real>abs_imag?abs_real:abs_imag;
 integer length,j;reg [5:0] selected_shift;
 always @*begin
  length=0;for(j=0;j<69;j=j+1)if(magnitude[j])length=j+1;
  selected_shift=length>25?length-25:0;
 end
 wire signed [69:0] bias=shift==0?70'sd0:(70'sd1<<<(shift-1));
 wire signed [69:0] rounded_real=($signed({{2{real_value[67]}},real_value})+bias)>>>shift;
 wire signed [69:0] rounded_imag=($signed({{2{imag_value[67]}},imag_value})+bias)>>>shift;
 reg signed [26:0] x,y;wire angle_ready,angle_valid,angle_error;wire [31:0] angle_id,angle_phase;
 assign in_ready=!rst && !cancel && state==IDLE && !valid && angle_ready;
 assign out_valid=valid && !rst && !cancel;
 phase_angle_cost_probe angle(.clk(clk),.rst(rst),.cancel(cancel),
  .in_valid(state==START),.in_ready(angle_ready),.in_id(owner),.in_x(x),.in_y(y),
  .out_valid(angle_valid),.out_ready(1'b1),.out_id(angle_id),.out_phase(angle_phase),.out_error(angle_error));
 always @(posedge clk)begin
  if(rst || cancel)begin
   state<=IDLE;valid<=0;owner<=0;real_value<=0;imag_value<=0;shift<=0;x<=0;y<=0;
   out_id<=0;out_phase<=0;out_error<=0;out_shift<=0;out_x<=0;out_y<=0;
  end else begin
   if(out_valid && out_ready)valid<=0;
   if(in_valid && in_ready)begin owner<=in_id;real_value<=in_real;imag_value<=in_imag;state<=PREPARE;end
   if(state==PREPARE)begin shift<=selected_shift;state<=NORMALIZE;end
   if(state==NORMALIZE)begin x<=rounded_real[26:0];y<=rounded_imag[26:0];state<=START;end
   if(state==START && angle_ready)state<=WAIT_ANGLE;
   if(state==WAIT_ANGLE && angle_valid)begin
    valid<=1;state<=IDLE;out_id<=owner;out_error<=angle_error || angle_id!=owner;
    out_phase<=angle_error || angle_id!=owner?32'd0:angle_phase;out_shift<=shift;out_x<=x;out_y<=y;
   end
  end
 end
endmodule
