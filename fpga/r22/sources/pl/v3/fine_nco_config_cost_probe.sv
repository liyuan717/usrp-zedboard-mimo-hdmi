`timescale 1ns/1ps
// Exact final NCO descriptor from the admitted startup slope num/D.
// The caller owns CFO/quality admission and actual sample-time dispatch.
// All time arithmetic uses low48 indices; epoch is immutable identity only.
module fine_nco_config_cost_probe(
 input wire clk,rst,cancel,
 input wire in_valid,output wire in_ready,
 input wire [31:0] in_id,in_generation,
 input wire signed [47:0] in_numerator,input wire [15:0] in_divisor,
 input wire [31:0] in_coarse_step,
 input wire [63:0] in_known_tag,input wire [47:0] in_phase_origin_index,
 output reg out_valid,input wire out_ready,
 output reg [31:0] out_id,out_generation,
 output reg [63:0] out_known_tag,
 output reg [31:0] out_step,out_phase,output reg [1:0] out_error
);
 localparam IDLE=0,DIVIDE_STEP=1,PRODUCT=2,DIVIDE_PHASE=3;
 reg [1:0] state;
 assign in_ready=!rst && !cancel && state==IDLE && !out_valid;
 wire take=in_valid && in_ready;
 // Desired correction is -num/(4D). Store magnitude and sign independently.
 wire [47:0] input_magnitude=in_numerator[47]?-in_numerator:in_numerator;
 wire input_negative=!in_numerator[47] && in_numerator!=0;
 wire [17:0] input_denominator={in_divisor,2'b00};
 wire [17:0] input_bias=(input_denominator>>1)-(input_negative?18'd1:18'd0);
 reg negative;
 reg [47:0] magnitude,offset_shift;
 reg [17:0] denominator,bias;
 reg [31:0] coarse_step,coarse_phase,coarse_multiplicand;
 reg [95:0] product_accumulator,multiplicand;
 wire [95:0] next_product=product_accumulator+(offset_shift[0]?multiplicand:96'd0);
 wire [31:0] next_coarse_phase=coarse_phase+(offset_shift[0]?coarse_multiplicand:32'd0);
 reg [95:0] dividend;
 reg [17:0] remainder;
 reg [31:0] quotient;
 reg [6:0] iteration;
 wire [18:0] shifted_remainder={remainder,dividend[95]};
 wire quotient_bit=shifted_remainder>={1'b0,denominator};
 wire [18:0] reduced_remainder=quotient_bit?shifted_remainder-{1'b0,denominator}:shifted_remainder;
 // The quotient is deliberately modulo32. Remainder decisions still use all
 // 96 dividend bits; discarded high quotient bits cannot affect rounding.
 wire [31:0] next_quotient={quotient[30:0],quotient_bit};
 wire [31:0] rounded_correction=negative?-next_quotient:next_quotient;
 always @(posedge clk) begin
  if(rst || cancel) begin
   state<=IDLE;negative<=0;magnitude<=0;offset_shift<=0;denominator<=0;bias<=0;
   coarse_step<=0;coarse_phase<=0;coarse_multiplicand<=0;
   product_accumulator<=0;multiplicand<=0;dividend<=0;remainder<=0;quotient<=0;iteration<=0;
   out_valid<=0;out_id<=0;out_generation<=0;out_known_tag<=0;out_step<=0;out_phase<=0;out_error<=0;
  end else begin
   if(out_valid && out_ready) out_valid<=0;
   if(state==DIVIDE_STEP || state==DIVIDE_PHASE) begin
    remainder<=reduced_remainder[17:0];dividend<={dividend[94:0],1'b0};quotient<=next_quotient;
    if(iteration==95) begin
     iteration<=0;
     if(state==DIVIDE_STEP) begin
      out_step<=coarse_step+rounded_correction;state<=PRODUCT;
      multiplicand<={48'd0,magnitude};product_accumulator<=0;
      coarse_phase<=0;coarse_multiplicand<=coarse_step;
     end else begin
      out_phase<=coarse_phase+rounded_correction;out_error<=0;out_valid<=1;state<=IDLE;
     end
    end else iteration<=iteration+1'b1;
   end
   if(state==PRODUCT) begin
    product_accumulator<=next_product;multiplicand<={multiplicand[94:0],1'b0};
    coarse_phase<=next_coarse_phase;coarse_multiplicand<={coarse_multiplicand[30:0],1'b0};
    offset_shift<={1'b0,offset_shift[47:1]};
    if(iteration==47) begin
     dividend<=next_product+{78'd0,bias};remainder<=0;quotient<=0;iteration<=0;state<=DIVIDE_PHASE;
    end else iteration<=iteration+1'b1;
   end
   if(take) begin
    out_id<=in_id;out_generation<=in_generation;out_known_tag<=in_known_tag;
    out_step<=0;out_phase<=0;out_error<=0;
    negative<=input_negative;magnitude<=input_magnitude;denominator<=input_denominator;bias<=input_bias;
    coarse_step<=in_coarse_step;offset_shift<=in_known_tag[47:0]-in_phase_origin_index;
    dividend<={48'd0,input_magnitude}+{78'd0,input_bias};remainder<=0;quotient<=0;iteration<=0;
    if(in_divisor==0) begin out_error<=1;out_valid<=1;state<=IDLE;end
    else state<=DIVIDE_STEP;
   end
  end
 end
endmodule
