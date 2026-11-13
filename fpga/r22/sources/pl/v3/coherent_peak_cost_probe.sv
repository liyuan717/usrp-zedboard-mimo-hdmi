`timescale 1ns/1ps
// Exact N/E peak ranking, three Q24 ratios and nearest-ties-up Q16 parabola.
// The first maximum wins. Only public-window metrics enter this interface.
module coherent_peak_cost_probe(
 input wire clk,rst,cancel,
 input wire in_valid,output wire in_ready,
 input wire [31:0] in_id,in_generation,input wire [63:0] in_tag,
 input wire [6:0] in_point,input wire in_last,input wire [2:0] in_error,
 input wire [59:0] in_n,input wire [51:0] in_e,
 output wire out_valid,input wire out_ready,
 output reg [31:0] out_id,out_generation,output reg [63:0] out_tag,
 output reg signed [17:0] out_fraction,output reg [79:0] out_position,
 output reg [2:0] out_error,output reg [6:0] out_point
);
 localparam IDLE=0,MUL_NEW=1,MUL_OLD=2,RANK=3,SCORE_START=4,SCORE_DIV=5,
  SCORE_FINISH=6,PARABOLA=7,FRACTION_DIV=8,FRACTION_FINISH=9;
 reg [3:0] state;
 reg response_valid,active,best_valid;
 reg [31:0] owner,owner_generation;
 reg [63:0] previous_tag,best_tag,pending_tag;
 reg [6:0] expected_point,best_point,pending_point;
 reg pending_last;
 reg [59:0] pending_n,previous_n,best_n,left_n,right_n;
 reg [51:0] pending_e,previous_e,best_e,left_e,right_e;
 (* use_dsp="yes" *) reg [111:0] product;
 reg [111:0] new_product;
 wire replace_best=pending_e!=0 && (!best_valid || new_product>product);
 wire [59:0] final_n=replace_best?pending_n:best_n;
 wire [51:0] final_e=replace_best?pending_e:best_e;
 wire [6:0] final_point=replace_best?pending_point:best_point;
 wire [57:0] threshold=({6'd0,final_e}<<5)+({6'd0,final_e}<<4);
 wire [59:0] multiply_n=state==MUL_NEW?pending_n:best_n;
 wire [51:0] multiply_e=state==MUL_NEW?best_e:pending_e;
 reg [1:0] score_index;
 reg [39:0] scores[0:2];
 reg [83:0] dividend,quotient;
 reg [51:0] divisor;
 reg [52:0] remainder;
 reg [6:0] count;
 wire [59:0] selected_n=score_index==0?left_n:score_index==1?best_n:right_n;
 wire [51:0] selected_e=score_index==0?left_e:score_index==1?best_e:right_e;
 wire [52:0] shifted_remainder={remainder[51:0],dividend[83]};
 wire subtract=shifted_remainder>={1'b0,divisor};
 wire [52:0] next_remainder=subtract?shifted_remainder-{1'b0,divisor}:shifted_remainder;
 reg fraction_negative;
 reg [57:0] fraction_dividend,fraction_quotient;
 reg [42:0] fraction_divisor;
 reg [43:0] fraction_remainder;
 wire [43:0] shifted_fraction={fraction_remainder[42:0],fraction_dividend[57]};
 wire fraction_subtract=shifted_fraction>={1'b0,fraction_divisor};
 wire [43:0] next_fraction=fraction_subtract?shifted_fraction-{1'b0,fraction_divisor}:shifted_fraction;
 wire signed [41:0] curve=$signed({2'd0,scores[0]})-($signed({2'd0,scores[1]})<<<1)+$signed({2'd0,scores[2]});
 wire signed [57:0] numerator=($signed({18'd0,scores[0]})-$signed({18'd0,scores[2]}))<<<15;
 wire [43:0] twice_fraction_remainder=fraction_remainder<<1;
 wire round_fraction=fraction_negative?(twice_fraction_remainder>{1'b0,fraction_divisor}):(twice_fraction_remainder>={1'b0,fraction_divisor});
 wire [58:0] rounded_fraction={1'b0,fraction_quotient}+{58'd0,round_fraction};
 wire signed [17:0] final_fraction=rounded_fraction>59'd65536 ? 18'sd0 :
    fraction_negative?-$signed(rounded_fraction[17:0]):$signed(rounded_fraction[17:0]);
 wire [79:0] final_position={best_tag[47:0],32'd0}+($signed({{62{final_fraction[17]}},final_fraction})<<<16);
 assign in_ready=!rst && !cancel && state==IDLE && !response_valid;
 assign out_valid=response_valid && !rst && !cancel;
 task fail(input [2:0] reason,input [31:0] id,generation);
  begin
   state<=IDLE;active<=0;response_valid<=1;out_id<=id;out_generation<=generation;
   out_tag<=0;out_fraction<=0;out_position<=0;out_error<=reason;out_point<=0;
  end
 endtask
 always @(posedge clk) begin
  if(rst || cancel) begin
   state<=IDLE;response_valid<=0;active<=0;best_valid<=0;
   owner<=0;owner_generation<=0;previous_tag<=0;best_tag<=0;pending_tag<=0;
   expected_point<=0;best_point<=0;pending_point<=0;pending_last<=0;
   pending_n<=0;previous_n<=0;best_n<=0;left_n<=0;right_n<=0;
   pending_e<=0;previous_e<=0;best_e<=0;left_e<=0;right_e<=0;
   product<=0;new_product<=0;score_index<=0;dividend<=0;quotient<=0;divisor<=0;remainder<=0;count<=0;
   fraction_negative<=0;fraction_dividend<=0;fraction_quotient<=0;fraction_divisor<=0;fraction_remainder<=0;
   out_id<=0;out_generation<=0;out_tag<=0;out_fraction<=0;out_position<=0;out_error<=0;out_point<=0;
   for(integer k=0;k<3;k=k+1) scores[k]<=0;
  end else begin
   if(out_valid && out_ready) response_valid<=0;
   // One expression structurally shares the exact60x52 cross-product unit.
   if(state==MUL_NEW || state==MUL_OLD) product<=multiply_n*multiply_e;
   case(state)
    IDLE:if(in_valid && in_ready) begin
     if(in_error!=0) fail(1,in_id,in_generation);
     else if(in_point>64 || (!active && in_point!=0) || (active &&
       (in_id!=owner || in_generation!=owner_generation || in_point!=expected_point || in_tag!={previous_tag[63:48],previous_tag[47:0]+48'd1}))) fail(2,in_id,in_generation);
     else begin
      if(!active) begin
       owner<=in_id;owner_generation<=in_generation;best_valid<=0;
       best_n<=0;best_e<=0;left_n<=0;left_e<=0;right_n<=0;right_e<=0;
      end
      active<=1;pending_n<=in_n;pending_e<=in_e;pending_tag<=in_tag;
      pending_point<=in_point;pending_last<=in_last;state<=MUL_NEW;
     end
    end
    MUL_NEW:state<=MUL_OLD;
    MUL_OLD:begin new_product<=product;state<=RANK;end
    RANK:begin
     previous_n<=pending_n;previous_e<=pending_e;previous_tag<=pending_tag;expected_point<=pending_point+1'b1;
     if(replace_best) begin
      best_valid<=1;best_n<=pending_n;best_e<=pending_e;best_tag<=pending_tag;best_point<=pending_point;
      left_n<=pending_point==0?60'd0:previous_n;left_e<=pending_point==0?52'd0:previous_e;
      right_n<=0;right_e<=0;
     end else if(best_valid && pending_point==best_point+1'b1) begin right_n<=pending_n;right_e<=pending_e;end
     if(!pending_last) state<=IDLE;
     else if((!best_valid && !replace_best) || final_n<={2'd0,threshold}) fail(4,owner,owner_generation);
     else if(final_point==0 || final_point==pending_point) fail(3,owner,owner_generation);
     else begin active<=0;score_index<=0;state<=SCORE_START;end
    end
    SCORE_START:begin
     if(selected_e==0) begin scores[score_index]<=0;state<=SCORE_FINISH;end
     else begin
      dividend<={selected_n,24'd0}+{{32{1'b0}},selected_e[51:1]};
      divisor<=selected_e;remainder<=0;quotient<=0;count<=0;state<=SCORE_DIV;
     end
    end
    SCORE_DIV:begin
     remainder<=next_remainder;dividend<=dividend<<1;quotient<={quotient[82:0],subtract};
     if(count==83) begin scores[score_index]<={quotient[38:0],subtract};state<=SCORE_FINISH;end
     else count<=count+1'b1;
    end
    SCORE_FINISH:if(score_index==2) state<=PARABOLA;
     else begin score_index<=score_index+1'b1;state<=SCORE_START;end
    PARABOLA:begin
     fraction_negative<=numerator[57]^curve[41];
     fraction_dividend<=numerator[57]?-numerator:numerator;
     fraction_divisor<=curve==0?43'd1:curve[41]?-$signed({curve[41],curve}):$signed({1'b0,curve});
     fraction_remainder<=0;fraction_quotient<=0;count<=0;
     if(curve==0) begin fraction_quotient<=0;fraction_negative<=0;state<=FRACTION_FINISH;end
     else state<=FRACTION_DIV;
    end
    FRACTION_DIV:begin
     fraction_remainder<=next_fraction;fraction_dividend<=fraction_dividend<<1;
     fraction_quotient<={fraction_quotient[56:0],fraction_subtract};
     if(count==57) state<=FRACTION_FINISH;else count<=count+1'b1;
    end
    FRACTION_FINISH:begin
     response_valid<=1;state<=IDLE;out_id<=owner;out_generation<=owner_generation;
     out_tag<=best_tag;out_point<=best_point;out_error<=0;
     out_fraction<=final_fraction;out_position<=final_position;
    end
   endcase
  end
 end
endmodule
