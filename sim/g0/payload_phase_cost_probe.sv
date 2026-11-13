`timescale 1ns/1ps
// Exact linear phase interpolation between true SG7 nodes, in phase32 turns.
// One group=64 data symbols. TX0 offsets2..65, TX1 offsets1..64, divisor66.
// Final group273 clamps to its last real node; missing earlier nodes are not
// supplied or invented by this arithmetic block. Caller owns node availability.
module payload_phase_cost_probe(
 input wire clk,rst,cancel,cfg_valid,output wire cfg_ready,
 input wire [31:0] cfg_id,cfg_context,cfg_generation,input wire [15:0] cfg_epoch,
 input wire [8:0] cfg_group,input wire [63:0] cfg_nominal,cfg_steps,
 input wire signed [40:0] cfg_left0,cfg_right0,cfg_left1,cfg_right1,
 output wire out_valid,input wire out_ready,
 output reg [31:0] out_id,out_context,out_generation,output reg [15:0] out_epoch,
 output reg [8:0] out_group,output reg [5:0] out_index,output wire [14:0] out_symbol,
 output wire [63:0] out_phases,output wire signed [40:0] out_residual0,out_residual1,
 output reg out_error,output wire out_last,busy
);
 localparam IDLE=0,PREPARE=1,DIVIDE=2,COMMIT=3,PRIME0=4,PRIME1=5,EMIT=6;
 reg [2:0] state;reg valid,tx,negative;reg [5:0] iteration;reg [6:0] remainder;
 reg [41:0] dividend,quotient;reg signed [41:0] delta[0:1],increment[0:1],floor_phase[0:1];
 reg [6:0] fraction[0:1],increment_fraction[0:1];reg [31:0] nominal[0:1],steps[0:1];
 wire [7:0] shifted_remainder={remainder,dividend[41]};
 wire subtract=shifted_remainder>=8'd66;
 wire [7:0] next_remainder=subtract?shifted_remainder-8'd66:shifted_remainder;
 wire [41:0] next_quotient={quotient[40:0],subtract};
 wire [7:0] sum0={1'b0,fraction[0]}+{1'b0,increment_fraction[0]},sum1={1'b0,fraction[1]}+{1'b0,increment_fraction[1]};
 wire carry0=sum0>=8'd66,carry1=sum1>=8'd66;
 wire signed [41:0] next0=floor_phase[0]+increment[0]+$signed({41'd0,carry0});
 wire signed [41:0] next1=floor_phase[1]+increment[1]+$signed({41'd0,carry1});
 wire signed [41:0] rounded0=floor_phase[0]+$signed({41'd0,fraction[0]>=33});
 wire signed [41:0] rounded1=floor_phase[1]+$signed({41'd0,fraction[1]>=33});
 assign busy=state!=IDLE || valid;
 assign cfg_ready=!rst && !cancel && state==IDLE && !valid;
 assign out_valid=valid && !rst && !cancel;
 assign out_symbol={out_group,out_index};
 assign out_phases=out_error?64'd0:{nominal[1]+rounded1[31:0],nominal[0]+rounded0[31:0]};
 assign out_residual0=out_error?41'sd0:rounded0[40:0];assign out_residual1=out_error?41'sd0:rounded1[40:0];
 assign out_last=out_error || out_index==63;
 integer n;
 always @(posedge clk)begin
  if(rst || cancel)begin
   state<=IDLE;valid<=0;tx<=0;negative<=0;iteration<=0;remainder<=0;dividend<=0;quotient<=0;
   out_id<=0;out_context<=0;out_generation<=0;out_epoch<=0;out_group<=0;out_index<=0;out_error<=0;
   for(n=0;n<2;n=n+1)begin delta[n]<=0;increment[n]<=0;floor_phase[n]<=0;fraction[n]<=0;increment_fraction[n]<=0;nominal[n]<=0;steps[n]<=0;end
  end else begin
   if(cfg_valid && cfg_ready)begin
    out_id<=cfg_id;out_context<=cfg_context;out_generation<=cfg_generation;out_epoch<=cfg_epoch;out_group<=cfg_group;out_index<=0;out_error<=cfg_group>273;
    floor_phase[0]<=cfg_left0;floor_phase[1]<=cfg_left1;fraction[0]<=0;fraction[1]<=0;
    delta[0]<=cfg_group==273?42'sd0:$signed({cfg_right0[40],cfg_right0})-$signed({cfg_left0[40],cfg_left0});
    delta[1]<=cfg_group==273?42'sd0:$signed({cfg_right1[40],cfg_right1})-$signed({cfg_left1[40],cfg_left1});
    nominal[0]<=cfg_nominal[31:0];nominal[1]<=cfg_nominal[63:32];steps[0]<=cfg_steps[31:0];steps[1]<=cfg_steps[63:32];tx<=0;state<=PREPARE;
    if(cfg_group>273)begin valid<=1;state<=EMIT;end
   end
   if(state==PREPARE)begin
    negative<=delta[tx][41];dividend<=delta[tx][41]?-$signed(delta[tx]):$unsigned(delta[tx]);quotient<=0;remainder<=0;iteration<=0;state<=DIVIDE;
   end
   if(state==DIVIDE)begin
    dividend<={dividend[40:0],1'b0};quotient<=next_quotient;remainder<=next_remainder[6:0];
    if(iteration==41)begin
     if(negative && next_remainder!=0)begin increment[tx]<=-$signed(next_quotient)-42'sd1;increment_fraction[tx]<=7'd66-next_remainder;end
     else begin increment[tx]<=negative?-$signed(next_quotient):$signed(next_quotient);increment_fraction[tx]<=next_remainder;end
     state<=COMMIT;
    end else iteration<=iteration+1'b1;
   end
   if(state==COMMIT)begin if(!tx)begin tx<=1;state<=PREPARE;end else state<=PRIME0;end
   if(state==PRIME0)begin
    floor_phase[0]<=next0;fraction[0]<=carry0?sum0-8'd66:sum0;
    floor_phase[1]<=next1;fraction[1]<=carry1?sum1-8'd66:sum1;state<=PRIME1;
   end
   if(state==PRIME1)begin floor_phase[0]<=next0;fraction[0]<=carry0?sum0-8'd66:sum0;valid<=1;state<=EMIT;end
   if(out_valid && out_ready)begin
    if(out_last)begin valid<=0;state<=IDLE;end
    else begin
     out_index<=out_index+1'b1;
     floor_phase[0]<=next0;fraction[0]<=carry0?sum0-8'd66:sum0;
     floor_phase[1]<=next1;fraction[1]<=carry1?sum1-8'd66:sum1;
     nominal[0]<=nominal[0]+steps[0];nominal[1]<=nominal[1]+steps[1];
    end
   end
  end
 end
endmodule
