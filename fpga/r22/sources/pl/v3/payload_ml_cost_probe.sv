`timescale 1ns/1ps
// Exact16-candidate ML: H27, y26, signed30 errors and unsigned62 metric.
// Three limb squares per error, four parallel errors:12DSP. No error truncation.
module payload_ml_cost_probe(
 input wire clk,rst,cancel,in_valid,output wire in_ready,
 input wire [31:0] in_id,in_context,in_generation,input wire [15:0] in_epoch,input wire [14:0] in_symbol,
 input wire [2:0] in_error,input wire [103:0] in_y,input wire [215:0] in_h,
 output wire out_valid,input wire out_ready,output reg [31:0] out_id,out_context,out_generation,
 output reg [15:0] out_epoch,output reg [14:0] out_symbol,output reg [2:0] out_error,
 output reg [3:0] out_decision,output reg [61:0] out_metric,output wire busy
);
 reg running,valid;reg [4:0] issued;reg [3:0] best_decision;reg [61:0] best_metric;
 reg signed [25:0] y[0:3];reg signed [26:0] h[0:7];reg signed [29:0] prediction[0:3],error_value[0:3],error_reg[0:3];
 wire [3:0] candidate=issued[3:0];
 function automatic signed [29:0] value(input integer i,input bit negative);
  reg signed [29:0] wide;begin wide={{3{h[i][26]}},h[i]};value=negative?-wide:wide;end
 endfunction
 always @*begin
  prediction[0]=value(0,candidate[0])-value(1,candidate[1])+value(2,candidate[2])-value(3,candidate[3]);
  prediction[1]=value(1,candidate[0])+value(0,candidate[1])+value(3,candidate[2])+value(2,candidate[3]);
  prediction[2]=value(4,candidate[0])-value(5,candidate[1])+value(6,candidate[2])-value(7,candidate[3]);
  prediction[3]=value(5,candidate[0])+value(4,candidate[1])+value(7,candidate[2])+value(6,candidate[3]);
  for(integer r=0;r<4;r=r+1)error_value[r]=$signed({{4{y[r][25]}},y[r]})-prediction[r];
 end
 reg error_valid,product_valid,square_valid,pair_valid,metric_valid;
 reg [3:0] error_candidate,product_candidate,square_candidate,pair_candidate,metric_candidate;
 reg [59:0] square[0:3];reg [60:0] pair0,pair1;reg [61:0] metric;
 generate for(genvar r=0;r<4;r=r+1)begin: lane
  wire signed [24:0] low={8'd0,error_reg[r][16:0]},high={{12{error_reg[r][29]}},error_reg[r][29:17]};
  wire signed [17:0] low18={1'b0,error_reg[r][16:0]},high18={{5{error_reg[r][29]}},error_reg[r][29:17]};
  (* use_dsp="yes" *) reg signed [42:0] ll,lh,hh;
  wire signed [61:0] full_square=$signed({{19{ll[42]}},ll})+($signed({{19{lh[42]}},lh})<<<18)+($signed({{19{hh[42]}},hh})<<<34);
  always @(posedge clk)begin
   if(rst || cancel)begin ll<=0;lh<=0;hh<=0;square[r]<=0;end
   else begin if(error_valid)begin ll<=low*low18;lh<=low*high18;hh<=high*high18;end if(product_valid)square[r]<=full_square[59:0];end
  end
 end endgenerate
 assign busy=running || valid;
 assign in_ready=!rst && !cancel && !running && (!valid || out_ready);
 assign out_valid=valid && !rst && !cancel;
 integer n;
 always @(posedge clk)begin
  if(rst || cancel)begin
   running<=0;valid<=0;issued<=0;best_decision<=0;best_metric<=0;error_valid<=0;product_valid<=0;square_valid<=0;pair_valid<=0;metric_valid<=0;
   error_candidate<=0;product_candidate<=0;square_candidate<=0;pair_candidate<=0;metric_candidate<=0;pair0<=0;pair1<=0;metric<=0;
   out_id<=0;out_context<=0;out_generation<=0;out_epoch<=0;out_symbol<=0;out_error<=0;out_decision<=0;out_metric<=0;
   for(n=0;n<4;n=n+1)begin y[n]<=0;error_reg[n]<=0;end for(n=0;n<8;n=n+1)h[n]<=0;
  end else begin
   error_valid<=running && issued<16;product_valid<=error_valid;square_valid<=product_valid;pair_valid<=square_valid;metric_valid<=pair_valid;
   if(out_valid && out_ready)valid<=0;
   if(in_valid && in_ready)begin
    out_id<=in_id;out_context<=in_context;out_generation<=in_generation;out_epoch<=in_epoch;out_symbol<=in_symbol;out_error<=in_error;
    issued<=0;best_decision<=0;best_metric<=0;running<=1;
    for(n=0;n<4;n=n+1)y[n]<=in_y[n*26+:26];for(n=0;n<8;n=n+1)h[n]<=in_h[n*27+:27];
    if(in_error!=0)begin running<=0;valid<=1;out_decision<=0;out_metric<=0;end
   end
   if(running && issued<16)begin for(n=0;n<4;n=n+1)error_reg[n]<=error_value[n];error_candidate<=issued[3:0];issued<=issued+1'b1;end
   if(error_valid)product_candidate<=error_candidate;
   if(product_valid)square_candidate<=product_candidate;
   if(square_valid)begin pair0<={1'b0,square[0]}+{1'b0,square[1]};pair1<={1'b0,square[2]}+{1'b0,square[3]};pair_candidate<=square_candidate;end
   if(pair_valid)begin metric<={1'b0,pair0}+{1'b0,pair1};metric_candidate<=pair_candidate;end
   if(metric_valid)begin
    if(metric_candidate==0 || metric<best_metric)begin best_metric<=metric;best_decision<=metric_candidate;end
    if(metric_candidate==15)begin running<=0;valid<=1;out_metric<=metric<best_metric?metric:best_metric;out_decision<=metric<best_metric?metric_candidate:best_decision;end
   end
  end
 end
endmodule
