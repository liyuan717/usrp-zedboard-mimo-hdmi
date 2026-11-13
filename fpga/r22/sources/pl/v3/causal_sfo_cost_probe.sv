`timescale 1ns/1ps
// Up to four CURRENT/PAST public centers. Infer bounded missing periods,
// exact OLS/rational +/-200ppm admission, nearest-ties-up Q32 symbol step.
// No source sequence, future peak, payload or session input.
module causal_sfo_cost_probe(
 input wire clk,rst,cancel,
 input wire in_valid,output wire in_ready,
 input wire [31:0] in_id,in_generation,input wire [15:0] in_epoch,
 input wire [79:0] in_position,input wire [2:0] in_error,
 output wire out_valid,input wire out_ready,
 output reg [31:0] out_id,out_generation,output reg [15:0] out_epoch,
 output reg [79:0] out_position,out_training_position,out_next_position,
 output reg [34:0] out_step,output reg out_locked,
 output reg [2:0] out_used,out_error
);
 localparam IDLE=0,GAP=1,SUM_X=2,WEIGHT_START=3,WEIGHT_BITS=4,
  CHECK_START=5,CHECK_SCALE=6,CHECK_BOUND=7,DIVIDE=8,PREDICT_START=9,
  PREDICT_BITS=10,PUBLISH=11;
 localparam [53:0] PERIOD=54'd80000<<32,HALF_PERIOD=54'd40000<<32;
 localparam [79:0] GAP_LIMIT=80'd680000<<32;
 localparam [34:0] NOMINAL_STEP=35'd17179869184;
 reg [3:0] state;
 reg response_valid;
 reg [79:0] positions[0:3];reg [4:0] frames[0:3];reg [2:0] used;
 reg [31:0] generation;reg [15:0] epoch;
 reg [31:0] owner;reg [79:0] current_position;
 reg [53:0] gap_remainder;reg [3:0] gap_count;
 reg [2:0] item;reg [6:0] sum_x;
 wire [8:0] count_times_frame={6'd0,used}*{4'd0,frames[item[1:0]]};
 wire signed [8:0] weight=$signed(count_times_frame)-$signed({2'b0,sum_x});
 wire [79:0] relative_position=positions[item[1:0]]-positions[0];
 reg [7:0] weight_magnitude;reg weight_negative;reg [3:0] bit_count;
 reg [63:0] time_shift;reg [15:0] frame_shift;
 reg signed [63:0] numerator;reg signed [15:0] frame_denominator;
 (* use_dsp="no" *) wire [31:0] symbol_denominator=$unsigned(frame_denominator)*32'd20000;
 wire [63:0] nominal_numerator={32'd0,symbol_denominator}<<34;
 wire signed [64:0] step_error=$signed({numerator[63],numerator})-$signed({1'b0,nominal_numerator});
 reg [64:0] absolute_error;reg [63:0] nominal_bound;
 reg [77:0] scaled_error;
 reg [31:0] divisor;reg [63:0] dividend,quotient;reg [32:0] remainder;
 reg [6:0] division_bit;
 wire [32:0] shifted_remainder={remainder[31:0],dividend[63]};
 wire subtract=shifted_remainder>={1'b0,divisor};
 wire [32:0] next_remainder=subtract?shifted_remainder-{1'b0,divisor}:shifted_remainder;
 reg [34:0] step;reg locked;
 reg [49:0] predict_shift,period_increment;reg [42:0] training_increment;
 reg [14:0] period_bits;reg [7:0] training_bits;
 assign in_ready=!rst && !cancel && state==IDLE && !response_valid;
 assign out_valid=response_valid && !rst && !cancel;
 wire [79:0] incoming_delta=in_position-positions[used==0?0:used-1'b1];
 task fail(input [2:0] reason,input keep_current);
  begin
   response_valid<=1;state<=IDLE;out_id<=owner;out_generation<=generation;out_epoch<=epoch;
   out_position<=0;out_training_position<=0;out_next_position<=0;out_step<=0;
   out_locked<=0;out_used<=0;out_error<=reason;
   used<=keep_current?3'd1:3'd0;
   if(keep_current) begin positions[0]<=current_position;frames[0]<=0;end
  end
 endtask
 always @(posedge clk) begin
  if(rst || cancel) begin
   state<=IDLE;response_valid<=0;used<=0;generation<=0;epoch<=0;owner<=0;current_position<=0;
   gap_remainder<=0;gap_count<=0;item<=0;sum_x<=0;weight_magnitude<=0;weight_negative<=0;bit_count<=0;
   time_shift<=0;frame_shift<=0;numerator<=0;frame_denominator<=0;absolute_error<=0;nominal_bound<=0;scaled_error<=0;
   divisor<=0;dividend<=0;quotient<=0;remainder<=0;division_bit<=0;step<=0;locked<=0;
   predict_shift<=0;period_increment<=0;training_increment<=0;period_bits<=0;training_bits<=0;
   out_id<=0;out_generation<=0;out_epoch<=0;out_position<=0;out_training_position<=0;out_next_position<=0;
   out_step<=0;out_locked<=0;out_used<=0;out_error<=0;
   for(integer i=0;i<4;i=i+1) begin positions[i]<=0;frames[i]<=0;end
  end else begin
   if(out_valid && out_ready) response_valid<=0;
   case(state)
    IDLE:if(in_valid && in_ready) begin
     owner<=in_id;current_position<=in_position;generation<=in_generation;epoch<=in_epoch;
     if(in_error!=0 || (used!=0 && (in_generation!=generation || in_epoch!=epoch))) begin
      response_valid<=1;out_id<=in_id;out_generation<=in_generation;out_epoch<=in_epoch;
      out_position<=0;out_training_position<=0;out_next_position<=0;out_step<=0;
      out_locked<=0;out_used<=0;out_error<=1;used<=0;
     end else if(used==0) begin
      positions[0]<=in_position;frames[0]<=0;used<=1;step<=NOMINAL_STEP;locked<=0;state<=PREDICT_START;
     end else if(incoming_delta==0 || incoming_delta>=GAP_LIMIT) begin
      response_valid<=1;out_id<=in_id;out_generation<=in_generation;out_epoch<=in_epoch;
      out_position<=0;out_training_position<=0;out_next_position<=0;out_step<=0;
      out_locked<=0;out_used<=0;out_error<=2;used<=0;
     end else begin gap_remainder<=incoming_delta[53:0]+HALF_PERIOD;gap_count<=0;state<=GAP;end
    end
    GAP:if(gap_remainder>=PERIOD) begin gap_remainder<=gap_remainder-PERIOD;gap_count<=gap_count+1'b1;end
     else if(gap_count==0 || gap_count>8) fail(2,0);
     else begin
      if(used<4) begin positions[used]<=current_position;frames[used]<=frames[used-1'b1]+gap_count;used<=used+1'b1;end
      else begin
       for(integer i=0;i<3;i=i+1) positions[i]<=positions[i+1];
       positions[3]<=current_position;frames[0]<=0;
       frames[1]<=frames[2]-frames[1];frames[2]<=frames[3]-frames[1];frames[3]<=frames[3]+gap_count-frames[1];
      end
      item<=0;sum_x<=0;numerator<=0;frame_denominator<=0;state<=SUM_X;
     end
    SUM_X:begin
     sum_x<=sum_x+{2'd0,frames[item[1:0]]};
     if(item+1'b1==used) begin item<=0;state<=WEIGHT_START;end else item<=item+1'b1;
    end
    WEIGHT_START:begin
     weight_negative<=weight[8];weight_magnitude<=weight[8]?-weight:weight;
     time_shift<={10'd0,relative_position[53:0]};frame_shift<={11'd0,frames[item[1:0]]};bit_count<=0;
     if(relative_position[79:54]!=0) fail(2,0);else state<=WEIGHT_BITS;
    end
    WEIGHT_BITS:begin
     if(weight_magnitude[0]) begin
      numerator<=weight_negative?numerator-$signed(time_shift):numerator+$signed(time_shift);
      frame_denominator<=weight_negative?frame_denominator-$signed(frame_shift):frame_denominator+$signed(frame_shift);
     end
     weight_magnitude<=weight_magnitude>>1;time_shift<=time_shift<<1;frame_shift<=frame_shift<<1;
     if(bit_count==7) begin
      if(item+1'b1==used) state<=CHECK_START;else begin item<=item+1'b1;state<=WEIGHT_START;end
     end else bit_count<=bit_count+1'b1;
    end
    CHECK_START:begin
     absolute_error<=step_error[64]?-step_error:step_error;nominal_bound<=nominal_numerator;
     if(numerator<=0 || frame_denominator<=0) fail(3,1);else begin divisor<=symbol_denominator;state<=CHECK_SCALE;end
    end
    CHECK_SCALE:begin
     // 5000=4096+1024-128+8. No rounded step is used for admission.
     scaled_error<=({13'd0,absolute_error}<<12)+({13'd0,absolute_error}<<10)-({13'd0,absolute_error}<<7)+({13'd0,absolute_error}<<3);
     state<=CHECK_BOUND;
    end
    CHECK_BOUND:if(scaled_error>{14'd0,nominal_bound}) fail(3,1);
     else begin dividend<=$unsigned(numerator)+{32'd0,divisor[31:1]};quotient<=0;remainder<=0;division_bit<=0;state<=DIVIDE;end
    DIVIDE:begin
     dividend<=dividend<<1;quotient<={quotient[62:0],subtract};remainder<=next_remainder;
     if(division_bit==63) begin step<={quotient[33:0],subtract};locked<=1;state<=PREDICT_START;end
     else division_bit<=division_bit+1'b1;
    end
    PREDICT_START:begin
     predict_shift<={15'd0,step};period_increment<=0;training_increment<=0;
     period_bits<=15'd20000;training_bits<=8'd160;bit_count<=0;state<=PREDICT_BITS;
    end
    PREDICT_BITS:begin
     if(period_bits[0]) period_increment<=period_increment+predict_shift;
     if(training_bits[0]) training_increment<=training_increment+predict_shift[42:0];
     predict_shift<=predict_shift<<1;period_bits<=period_bits>>1;training_bits<=training_bits>>1;
     if(bit_count==14) state<=PUBLISH;else bit_count<=bit_count+1'b1;
    end
    PUBLISH:begin
     response_valid<=1;state<=IDLE;out_id<=owner;out_generation<=generation;out_epoch<=epoch;
     out_position<=current_position;out_training_position<=current_position+{37'd0,training_increment};
     out_next_position<=current_position+{30'd0,period_increment};out_step<=step;
     out_locked<=locked;out_used<=used;out_error<=0;
    end
   endcase
  end
 end
endmodule
