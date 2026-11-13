`timescale 1ns/1ps
// Full H26/scale64 -> actual phase rotation -> H27/scale64; y21/scale4 ->
// y26/scale64*sqrt2. No common lossy exponent or 14bit H truncation.
// Two25x18 DSPs share exact low17/high9 limbs and four y constant products.
module payload_rotate_cost_probe #(parameter SINE_FILE="sine.hex")(
 input wire clk,rst,cancel,cfg_valid,output wire cfg_ready,
 input wire [31:0] cfg_id,cfg_context,cfg_generation,input wire [15:0] cfg_epoch,input wire [207:0] cfg_h,
 input wire in_valid,output wire in_ready,input wire [31:0] in_id,in_context,in_generation,
 input wire [15:0] in_epoch,input wire [14:0] in_symbol,input wire [2:0] in_error,
 input wire [83:0] in_y,input wire [63:0] in_phases,
 output wire out_valid,input wire out_ready,output reg [31:0] out_id,out_context,out_generation,
 output reg [15:0] out_epoch,output reg [14:0] out_symbol,output reg [2:0] out_error,
 output reg [103:0] out_y,output reg [215:0] out_h,output wire busy
);
 localparam IDLE=0,READ0=1,READ1=2,SCALE=3,ROTATE=4,FLUSH=5;
 reg [2:0] state;reg configured,valid;reg [31:0] owner,context_id,generation;reg [15:0] epoch;
 reg signed [25:0] h[0:7];reg signed [20:0] y[0:3];reg [31:0] phase[0:1];reg signed [17:0] cosine[0:1],sine[0:1];
 (* rom_style="block" *) reg [16:0] sine_rom[0:1023];initial $readmemh(SINE_FILE,sine_rom);
 wire [31:0] rom_phase=state==READ1?phase[1]:phase[0];wire [31:0] rounded_phase=rom_phase+32'h80000;wire [11:0] bin=rounded_phase[31:20];
 wire [10:0] sin_index=bin[10]?11'd1024-{1'b0,bin[9:0]}:{1'b0,bin[9:0]};
 wire [10:0] cos_index=bin[10]?{1'b0,bin[9:0]}:11'd1024-{1'b0,bin[9:0]};
 wire [16:0] sin_value=sin_index==1024?17'd65536:sine_rom[sin_index[9:0]],cos_value=cos_index==1024?17'd65536:sine_rom[cos_index[9:0]];
 reg [4:0] issued;wire issue=state==SCALE || (state==ROTATE && issued<16);
 reg signed [24:0] a0,a1;reg signed [17:0] b0,b1;reg signed [25:0] selected_h;reg signed [17:0] coefficient;
 wire [2:0] component=issued[3:1];wire tx=component[1],rx=component[2],imag=component[0],term=issued[0];
 always @*begin
  a0=0;a1=0;b0=0;b1=0;selected_h=0;coefficient=0;
  if(state==SCALE)begin a0=y[issued[0]?2:0];a1=y[issued[0]?3:1];b0=18'sd92682;b1=18'sd92682;end
  else begin
   selected_h=h[{tx,rx,term}];coefficient=imag?(term?cosine[tx]:sine[tx]):(term?sine[tx]:cosine[tx]);
   a0={8'd0,selected_h[16:0]};a1={{16{selected_h[25]}},selected_h[25:17]};b0=coefficient;b1=coefficient;
  end
 end
 (* use_dsp="yes" *) reg signed [42:0] product0,product1;
 reg product_valid,product_scale;reg [3:0] product_index;reg signed [44:0] accumulator;
 wire signed [44:0] full_product=$signed({{2{product0[42]}},product0})+($signed({{2{product1[42]}},product1})<<<17);
 wire signed [44:0] component_sum=product_index[0]&&!product_index[1]?accumulator-full_product:accumulator+full_product;
 wire signed [44:0] rounded_component=(component_sum+45'sd32768)>>>16;
 wire signed [42:0] scaled0=(product0+43'sd2048)>>>12,scaled1=(product1+43'sd2048)>>>12;
 assign busy=state!=IDLE || valid;
 assign cfg_ready=!rst && !cancel && state==IDLE && !valid;
 assign in_ready=!rst && !cancel && configured && !cfg_valid && state==IDLE && (!valid || out_ready);
 assign out_valid=valid && !rst && !cancel;
 integer n;
 always @(posedge clk)begin
  if(rst || cancel)begin
   state<=IDLE;configured<=0;valid<=0;owner<=0;context_id<=0;generation<=0;epoch<=0;issued<=0;product_valid<=0;product_scale<=0;product_index<=0;product0<=0;product1<=0;accumulator<=0;
   out_id<=0;out_context<=0;out_generation<=0;out_epoch<=0;out_symbol<=0;out_error<=0;out_y<=0;out_h<=0;
   for(n=0;n<8;n=n+1)h[n]<=0;for(n=0;n<4;n=n+1)y[n]<=0;for(n=0;n<2;n=n+1)begin phase[n]<=0;cosine[n]<=0;sine[n]<=0;end
  end else begin
   product_valid<=issue;
   if(out_valid && out_ready)valid<=0;
   if(cfg_valid && cfg_ready)begin configured<=1;owner<=cfg_id;context_id<=cfg_context;generation<=cfg_generation;epoch<=cfg_epoch;for(n=0;n<8;n=n+1)h[n]<=cfg_h[n*26+:26];end
   if(in_valid && in_ready)begin
    out_id<=owner;out_context<=context_id;out_generation<=generation;out_epoch<=epoch;out_symbol<=in_symbol;out_error<=0;
    for(n=0;n<4;n=n+1)y[n]<=in_y[n*21+:21];phase[0]<=in_phases[31:0];phase[1]<=in_phases[63:32];state<=READ0;
    if(in_error!=0 || in_id!=owner || in_context!=context_id || in_generation!=generation || in_epoch!=epoch)begin
     valid<=1;state<=IDLE;out_y<=0;out_h<=0;out_error<=in_error!=0?in_error:3'd3;
    end
   end
   if(state==READ0 || state==READ1)begin
    sine[state==READ1]<=bin[11]?-$signed({1'b0,sin_value}):$signed({1'b0,sin_value});
    cosine[state==READ1]<=bin[11]^bin[10]?-$signed({1'b0,cos_value}):$signed({1'b0,cos_value});
    state<=state==READ0?READ1:SCALE;issued<=0;
   end
   if(issue)begin
    product0<=a0*b0;product1<=a1*b1;product_scale<=state==SCALE;product_index<=issued[3:0];issued<=issued+1'b1;
    if(state==SCALE && issued==1)begin state<=ROTATE;issued<=0;end
    if(state==ROTATE && issued==15)state<=FLUSH;
   end
   if(product_valid)begin
    if(product_scale)begin
     out_y[(product_index[0]?2:0)*26+:26]<=scaled0[25:0];out_y[(product_index[0]?3:1)*26+:26]<=scaled1[25:0];
    end else if(!product_index[0])accumulator<=full_product;
    else begin out_h[product_index[3:1]*27+:27]<=rounded_component[26:0];if(product_index==15)begin valid<=1;state<=IDLE;end end
   end
  end
 end
endmodule
