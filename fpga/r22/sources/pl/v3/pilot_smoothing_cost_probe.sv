`timescale 1ns/1ps
// Bounded seven-observation, order1 Savitzky-Golay reference in phase32 turns.
// Two TX streams arrive in pair order. First3/last3 use the same seven-point
// edge fit; interior is the exact seven-point mean. Outputs identify the last
// observation used: first points0..3 are delayed until observation6 exists.
// RAM stores only7x2 unwrapped points, not an entire frame. No DSP required.
module pilot_smoothing_cost_probe #(parameter POINTS=274,FRAME_LIMIT=1000000)(
 input wire clk,rst,cancel,cfg_valid,output wire cfg_ready,
 input wire [31:0] cfg_id,cfg_context,cfg_generation,input wire [15:0] cfg_epoch,
 input wire in_valid,output wire in_ready,input wire [31:0] in_id,in_context,in_generation,
 input wire [15:0] in_epoch,input wire [8:0] in_point,input wire in_tx,in_error,input wire [31:0] in_phase,
 output wire out_valid,input wire out_ready,output reg [31:0] out_id,out_context,out_generation,
 output reg [15:0] out_epoch,output reg [8:0] out_point,out_known_point,output reg out_tx,
 output reg signed [40:0] out_phase,
 output wire frame_abort,output reg [1:0] abort_reason,output reg [8:0] abort_point,
 output wire active
);
 localparam IDLE=0,COLLECT=1,SUM_WINDOW=2,PREPARE=3,DIVIDE=4,WAIT_OUTPUT=5;
 localparam AGE_BITS=$clog2(FRAME_LIMIT+1)>0?$clog2(FRAME_LIMIT+1):1;
 reg [2:0] state;reg result_valid,abort_q;reg [AGE_BITS-1:0] age;
 reg [31:0] owner,owner_context,owner_generation;reg [15:0] owner_epoch;
 reg [8:0] expected_point,work_point,output_point;reg expected_tx,work_tx,work_last;
 reg [31:0] previous[0:1];reg signed [40:0] unwrapped[0:1];
 reg [2:0] write_pointer[0:1],read_pointer,sum_index;
 (* ram_style="distributed" *) reg signed [40:0] history[0:15];
 wire [3:0] write_address={in_tx,write_pointer[in_tx]};
 wire signed [40:0] read_value=history[{work_tx,read_pointer}];
 wire signed [48:0] extended_value={{8{read_value[40]}},read_value};
 reg signed [48:0] sum,weighted,weighted_value;
 reg signed [3:0] fit_offset,fit_end;
 reg signed [48:0] fit_product;
 always @*begin
  case(sum_index)
   0:weighted_value=-(extended_value+(extended_value<<<1));
   1:weighted_value=-(extended_value<<<1);2:weighted_value=-extended_value;
   3:weighted_value=0;4:weighted_value=extended_value;
   5:weighted_value=extended_value<<<1;
   default:weighted_value=extended_value+(extended_value<<<1);
  endcase
  case(fit_offset)
   -4'sd3:fit_product=-(weighted+(weighted<<<1));
   -4'sd2:fit_product=-(weighted<<<1);-4'sd1:fit_product=-weighted;
   4'sd0:fit_product=0;4'sd1:fit_product=weighted;
   4'sd2:fit_product=weighted<<<1;
   default:fit_product=weighted+(weighted<<<1);
  endcase
 end
 wire signed [48:0] numerator=(sum<<<2)+fit_product;
 reg [48:0] dividend,quotient;reg [5:0] remainder,iteration;reg negative;
 wire [6:0] shifted_remainder={remainder,dividend[48]};
 wire subtract=shifted_remainder>=7'd28;
 wire [6:0] next_remainder=subtract?shifted_remainder-7'd28:shifted_remainder;
 wire [48:0] next_quotient={quotient[47:0],subtract};
 wire signed [33:0] raw_delta=$signed({{2{in_phase[31]}},in_phase})-$signed({{2{previous[in_tx][31]}},previous[in_tx]});
 reg signed [33:0] delta;
 always @*begin
  delta=raw_delta;
  if(raw_delta>34'sd2147483648)delta=raw_delta-34'sd4294967296;
  else if(raw_delta< -34'sd2147483648)delta=raw_delta+34'sd4294967296;
 end
 wire ambiguous=expected_point!=0 && (delta>=34'sd2147483526 || delta<= -34'sd2147483526);
 wire signed [40:0] next_unwrapped=expected_point==0?$signed({{9{in_phase[31]}},in_phase}):unwrapped[in_tx]+delta;
 assign active=state!=IDLE;
 wire deadline=active && age==FRAME_LIMIT-1;
 assign cfg_ready=!rst && !cancel && !abort_q && state==IDLE && !result_valid;
 assign in_ready=!rst && !cancel && !abort_q && !deadline && state==COLLECT;
 assign out_valid=result_valid && !rst && !cancel && !abort_q;
 assign frame_abort=abort_q && !rst && !cancel;
 wire take=in_valid && in_ready;
 wire identity_bad=in_error || in_id!=owner || in_context!=owner_context || in_generation!=owner_generation || in_epoch!=owner_epoch || in_point!=expected_point || in_tx!=expected_tx;
 wire good_take=take && !identity_bad && !ambiguous;
 wire final_fire=out_valid && out_ready && state==WAIT_OUTPUT && work_last && work_tx && fit_offset==fit_end;
 always @(posedge clk)if(good_take)history[write_address]<=next_unwrapped;
 task abort_frame(input [1:0] reason,input [8:0] point);
  begin state<=IDLE;result_valid<=0;abort_q<=1;abort_reason<=reason;abort_point<=point;out_phase<=0;end
 endtask
 integer l;
 always @(posedge clk)begin
  if(rst || cancel)begin
   state<=IDLE;result_valid<=0;abort_q<=0;age<=0;owner<=0;owner_context<=0;owner_generation<=0;owner_epoch<=0;
   expected_point<=0;expected_tx<=0;work_point<=0;work_tx<=0;work_last<=0;output_point<=0;read_pointer<=0;sum_index<=0;
   sum<=0;weighted<=0;fit_offset<=0;fit_end<=0;dividend<=0;quotient<=0;remainder<=0;iteration<=0;negative<=0;
   out_id<=0;out_context<=0;out_generation<=0;out_epoch<=0;out_point<=0;out_known_point<=0;out_tx<=0;out_phase<=0;abort_reason<=0;abort_point<=0;
   for(l=0;l<2;l=l+1)begin previous[l]<=0;unwrapped[l]<=0;write_pointer[l]<=0;end
  end else begin
   abort_q<=0;
   if(active)age<=age+1'b1;
   if(out_valid && out_ready)result_valid<=0;
   if(deadline && !final_fire)abort_frame(3,expected_point);
   else begin
    if(cfg_valid && cfg_ready)begin
     state<=COLLECT;age<=0;owner<=cfg_id;owner_context<=cfg_context;owner_generation<=cfg_generation;owner_epoch<=cfg_epoch;
     out_id<=cfg_id;out_context<=cfg_context;out_generation<=cfg_generation;out_epoch<=cfg_epoch;expected_point<=0;expected_tx<=0;
     for(l=0;l<2;l=l+1)begin previous[l]<=0;unwrapped[l]<=0;write_pointer[l]<=0;end
    end
    if(take)begin
     if(identity_bad)abort_frame(1,in_point);
     else if(ambiguous)abort_frame(2,in_point);
     else begin
      previous[in_tx]<=in_phase;unwrapped[in_tx]<=next_unwrapped;
      write_pointer[in_tx]<=write_pointer[in_tx]==6?3'd0:write_pointer[in_tx]+1'b1;
      expected_tx<=!in_tx;if(in_tx)expected_point<=expected_point+1'b1;
      if(in_point>=6)begin
       work_point<=in_point;work_tx<=in_tx;work_last<=in_point==POINTS-1;
       output_point<=in_point==6?9'd0:in_point-9'd3;
       fit_offset<=in_point==6?-4'sd3:4'sd0;fit_end<=in_point==POINTS-1?4'sd3:4'sd0;
       read_pointer<=write_pointer[in_tx]==6?3'd0:write_pointer[in_tx]+1'b1;
       sum<=0;weighted<=0;sum_index<=0;state<=SUM_WINDOW;
      end
     end
    end
    if(state==SUM_WINDOW)begin
     sum<=sum+extended_value;weighted<=weighted+weighted_value;
     read_pointer<=read_pointer==6?3'd0:read_pointer+1'b1;
     if(sum_index==6)state<=PREPARE;else sum_index<=sum_index+1'b1;
    end
    if(state==PREPARE)begin
     negative<=numerator[48];dividend<=numerator[48]?-$signed(numerator)+49'd13:$unsigned(numerator)+49'd14;
     quotient<=0;remainder<=0;iteration<=0;state<=DIVIDE;
    end
    if(state==DIVIDE)begin
     dividend<={dividend[47:0],1'b0};quotient<=next_quotient;remainder<=next_remainder[5:0];
     if(iteration==48)begin
      out_phase<=negative?-$signed(next_quotient[40:0]):$signed(next_quotient[40:0]);
      out_point<=output_point;out_known_point<=work_point;out_tx<=work_tx;result_valid<=1;state<=WAIT_OUTPUT;
     end else iteration<=iteration+1'b1;
    end
    if(state==WAIT_OUTPUT && out_valid && out_ready)begin
     if(fit_offset==fit_end)state<=work_last && work_tx?IDLE:COLLECT;
     else begin fit_offset<=fit_offset+1'b1;output_point<=output_point+1'b1;state<=PREPARE;end
    end
   end
  end
 end
endmodule
