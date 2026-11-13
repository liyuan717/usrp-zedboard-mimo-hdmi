`timescale 1ns/1ps
// G0 common-phase dual-RX mixer. Config supplies a causal future boundary;
// this block does not estimate CFO. No retroactive correction or gap hiding.
// Input signed16; coefficients Q16; output signed18 represents 2*input units.
module nco_cost_probe #(parameter SINE_FILE="@R22_ROOT@/rom/sine.hex",parameter ENABLE_CONJUGATE=0,parameter ENABLE_TIMED_CONFIG=0)(
 input wire clk,rst,
 input wire cfg_valid,output wire cfg_ready,
 input wire [63:0] cfg_tag,input wire [31:0] cfg_phase,cfg_step,
 input wire in_valid,output wire in_ready,input wire [63:0] in_tag,input wire [63:0] in_data,
 output reg out_valid,input wire out_ready,output reg [63:0] out_tag,
 output reg out_start,output reg [71:0] out_data,
 output reg armed,output reg [31:0] fence_count,
 input wire cfg_conjugate,output wire cfg_applied,output reg [1:0] cfg_error
);
 (* rom_style="block" *) reg [16:0] sine_rom[0:1023];
 initial $readmemh(SINE_FILE,sine_rom);
 reg [1:0] state;
 reg [31:0] phase,step;
 reg [63:0] expected_tag,work_tag;
 reg first_pending,work_start;
 reg conjugate;
 reg signed [15:0] x[0:3];
 reg [16:0] sine_q,cosine_q;
 reg sin_negative,cos_negative;
 (* use_dsp="yes" *) reg signed [33:0] product[0:3];
 reg signed [17:0] first_re,first_im;
 wire available=!out_valid || out_ready;
 // Ordering assumes the target is less than2^47 samples ahead. Epoch is
 // identity, never part of modulo index distance. Producer holds descriptor
 // and boundary sample until handshake; downstream stalls may delay commit.
 wire signed [47:0] cfg_distance=cfg_tag[47:0]-in_tag[47:0];
 wire cfg_wrong_epoch=cfg_tag[63:48]!=in_tag[63:48];
 wire cfg_rejected=ENABLE_TIMED_CONFIG && in_valid && (cfg_wrong_epoch || cfg_distance<0);
 wire cfg_due=!ENABLE_TIMED_CONFIG || (in_valid && !cfg_wrong_epoch && cfg_distance==0);
 assign cfg_ready=!rst && (cfg_rejected || (state==0 && !out_valid && cfg_due));
 assign cfg_applied=cfg_valid && cfg_ready && !cfg_rejected;
 assign in_ready=!rst && !(cfg_valid && cfg_due) && (armed || ENABLE_TIMED_CONFIG) &&
                 (state==0 || (state==3 && available));
 wire accept=in_valid && in_ready;
 wire matching=in_tag==expected_tag;
 wire [31:0] rounded_phase=phase+32'h00080000;
 wire [11:0] bin=rounded_phase[31:20];
 wire [10:0] sin_index=bin[10]?11'd1024-{1'b0,bin[9:0]}:{1'b0,bin[9:0]};
 wire [10:0] cos_index=bin[10]?{1'b0,bin[9:0]}:11'd1024-{1'b0,bin[9:0]};
 wire signed [17:0] sine_value=sin_negative?-$signed({1'b0,sine_q}):$signed({1'b0,sine_q});
 wire signed [17:0] cosine_value=cos_negative?-$signed({1'b0,cosine_q}):$signed({1'b0,cosine_q});
 // Conjugate in the wide sums: signed16 negation would overflow at -32768.
 wire signed [34:0] re_sum=(ENABLE_CONJUGATE && conjugate)?
  $signed({product[0][33],product[0]})+$signed({product[1][33],product[1]}):
  $signed({product[0][33],product[0]})-$signed({product[1][33],product[1]});
 wire signed [34:0] im_sum=(ENABLE_CONJUGATE && conjugate)?
  $signed({product[2][33],product[2]})-$signed({product[3][33],product[3]}):
  $signed({product[2][33],product[2]})+$signed({product[3][33],product[3]});
 wire signed [17:0] re_round=(re_sum+35'sd16384)>>>15;
 wire signed [17:0] im_round=(im_sum+35'sd16384)>>>15;
 wire signed [15:0] a=state==1?x[0]:x[2];
 wire signed [15:0] b=state==1?x[1]:x[3];
 always @(posedge clk) begin
  if(!rst && accept && armed && matching) begin
   sine_q<=sin_index==1024?17'd65536:sine_rom[sin_index[9:0]];
   cosine_q<=cos_index==1024?17'd65536:sine_rom[cos_index[9:0]];
  end
  if(!rst && (state==1 || state==2)) begin
   product[0]<=a*cosine_value;product[1]<=b*sine_value;
   product[2]<=a*sine_value;product[3]<=b*cosine_value;
  end
 end
 integer l;
 always @(posedge clk) begin
  if(rst) begin
   state<=0;phase<=0;step<=0;expected_tag<=0;work_tag<=0;first_pending<=0;work_start<=0;conjugate<=0;
   sin_negative<=0;cos_negative<=0;first_re<=0;first_im<=0;
   cfg_error<=0;out_valid<=0;out_tag<=0;out_start<=0;out_data<=0;armed<=0;fence_count<=0;
  end else begin
   if(out_valid && out_ready) out_valid<=0;
   if(state==1) state<=2;
   if(state==2) begin first_re<=re_round;first_im<=im_round;state<=3;end
   if(state==3 && available) begin
    out_data<={im_round,re_round,first_im,first_re};out_tag<=work_tag;out_start<=work_start;
    out_valid<=1;state<=0;
   end
   if(cfg_valid && cfg_ready) cfg_error<=cfg_rejected?(cfg_wrong_epoch?2'd2:2'd1):2'd0;
   if(cfg_applied) begin
    phase<=cfg_phase;step<=cfg_step;expected_tag<=cfg_tag;first_pending<=1;armed<=1;
    conjugate<=ENABLE_CONJUGATE && cfg_conjugate;
   end
   if(accept && armed) begin
    if(!matching) begin armed<=0;fence_count<=fence_count+1;end
    else begin
     for(l=0;l<4;l=l+1) x[l]<=in_data[l*16+:16];
     sin_negative<=bin[11];cos_negative<=bin[11]^bin[10];
     work_tag<=in_tag;work_start<=first_pending;first_pending<=0;
     phase<=phase+step;expected_tag<={expected_tag[63:48],(expected_tag[47:0]+48'd1)};
     state<=1;
    end
   end
  end
 end
endmodule
