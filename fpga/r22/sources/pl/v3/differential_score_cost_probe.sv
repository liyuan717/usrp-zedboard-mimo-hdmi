`timescale 1ns/1ps
// One shared49x49 square, eight components per metric; exact rational scores.
// N_s=sum_RX(real(C_s)^2+imag(C_s)^2), denominator=1979168*E.
// No division/quantized score: threshold and peak comparisons retain integers.
module differential_score_cost_probe(
 input wire clk,rst,cancel,
 input wire in_valid,output wire in_ready,
 input wire [391:0] in_c,input wire [69:0] in_energy,
 input wire [63:0] in_tag,input wire [31:0] in_generation,
 output reg out_valid,input wire out_ready,
 output reg [99:0] out_n0,out_n1,out_n,
 output reg [69:0] out_energy,output reg [63:0] out_tag,
 output reg [31:0] out_generation,
 output reg out_sense,out_rx,out_hit,
 output reg signed [48:0] out_re,out_im
);
 reg [3:0] state;
 reg [391:0] words;reg [69:0] energy;
 reg [63:0] tag;reg [31:0] generation;
 reg [97:0] square;
 reg [99:0] accumulator,n0;
 reg [98:0] rx0_pair0,rx0_pair1;
 wire enable=!out_valid || out_ready;
 assign in_ready=!rst && !cancel && enable && (state==0 || state==8);
 wire fire=in_valid && in_ready;
 wire signed [48:0] operand=fire?$signed(in_c[48:0]):$signed(words[state*49+:49]);
 wire [99:0] total=accumulator+{{2{1'b0}},square};
 wire sense=n0<total; // Equal scores always prefer sense0.
 wire [99:0] chosen_n=sense?total:n0;
 wire [98:0] pair0=sense?rx0_pair1:rx0_pair0;
 wire rx=({1'b0,pair0}<(chosen_n-{1'b0,pair0}));
 wire [2:0] word_index={sense,rx,1'b0};
 wire [102:0] five_n=({3'b0,chosen_n}<<3)+({3'b0,chosen_n}<<1);
 wire [91:0] denominator=energy*22'd1979168;
 always @(posedge clk) begin
  if(rst || cancel) begin
   state<=0;words<=0;energy<=0;tag<=0;generation<=0;square<=0;
   accumulator<=0;n0<=0;rx0_pair0<=0;rx0_pair1<=0;
   out_valid<=0;out_n0<=0;out_n1<=0;out_n<=0;out_energy<=0;out_tag<=0;
   out_generation<=0;out_sense<=0;out_rx<=0;out_hit<=0;out_re<=0;out_im<=0;
  end else begin
   if(out_valid && out_ready) out_valid<=0;
   if(fire || (state>=1 && state<=7)) square<=operand*operand;
   if(state>=1 && state<=7) begin
    if(state==1 || state==5) accumulator<={{2{1'b0}},square};
    else accumulator<=total;
    if(state==2) rx0_pair0<=total[98:0];
    if(state==4) n0<=total;
    if(state==6) rx0_pair1<=total[98:0];
    state<=state+1'b1;
   end
   if(state==8 && enable) begin
    out_valid<=1;out_n0<=n0;out_n1<=total;out_n<=chosen_n;out_energy<=energy;
    out_tag<=tag;out_generation<=generation;out_sense<=sense;out_rx<=rx;
    out_hit<=energy!=0 && five_n>{{11{1'b0}},denominator};
    out_re<=words[word_index*49+:49];out_im<=words[(word_index+3'd1)*49+:49];
    state<=0;
   end
   if(fire) begin
    words<=in_c;energy<=in_energy;tag<=in_tag;generation<=in_generation;state<=1;
   end
  end
 end
endmodule
