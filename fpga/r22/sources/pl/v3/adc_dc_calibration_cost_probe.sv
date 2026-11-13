`timescale 1ns/1ps
// Bounded startup DC mean, four raw signed16 lanes, no CPU RF preprocessing.
// Exact rational mean and nearest/ties-even output. One shared bit-serial
// divider, no payload RAM/DSP. Notch/interference detection is NOT implemented.
// No READY toward ADC; setup input is discarded until N+GUARD samples seen.
module adc_dc_calibration_cost_probe #(
 parameter integer N=80000,GUARD=2048,GAP_TIMEOUT=1000000
)(
 input wire clk,rst,enable,raw_valid,upstream_loss,
 input wire [63:0] raw_tag,raw_data,
 output reg out_valid,out_loss,output reg [63:0] out_tag,out_data,
 output wire calibrated,output reg [31:0] calibration_faults,range_drops,
 output reg [63:0] first_tag
);
 localparam WAIT=0,ACC=1,LOAD=2,DIVIDE=3,SETTLE=4,RUN=5;
 localparam CW=$clog2(N+GUARD+2),GW=$clog2(GAP_TIMEOUT+1);
 reg [2:0] state;
 reg [CW-1:0] seen;
 reg [GW-1:0] gap;
 reg [63:0] expected_tag;
 reg signed [32:0] sums[0:3];
 reg signed [16:0] bias[0:3];
 reg [3:0] fraction_high,fraction_tie;
 reg [1:0] lane;
 reg negative;reg [32:0] dividend,quotient;
 reg [16:0] remainder;reg [5:0] steps;
 wire [17:0] trial={remainder,dividend[32]};
 wire subtract=trial>=N;
 wire [17:0] reduced=subtract?trial-N:trial;
 wire [32:0] next_q={quotient[31:0],subtract};
 wire [17:0] fraction=negative && reduced!=0?N-reduced:reduced;
 wire tag_bad=state!=WAIT && raw_valid && raw_tag!=expected_tag;
 wire expired=state!=WAIT && !raw_valid && gap>=GAP_TIMEOUT-1;
 assign calibrated=(state==RUN || (state==SETTLE && seen>=N+GUARD));
 reg [63:0] corrected;
 reg overflow;reg signed [17:0] difference,value;
 integer j;
 always @* begin
  corrected=0;overflow=0;difference=0;value=0;
  for(integer k=0;k<4;k=k+1)begin
   difference=$signed(raw_data[k*16+:16])-$signed(bias[k]);
   value=difference-((fraction_high[k] || (fraction_tie[k] && difference[0]))?18'sd1:18'sd0);
   corrected[k*16+:16]=value[15:0];
   if(value < -18'sd32768 || value > 18'sd32767)overflow=1;
  end
 end
 always @(posedge clk)begin
  out_valid<=0;out_loss<=0;
  if(rst)begin
   state<=WAIT;seen<=0;gap<=0;expected_tag<=0;first_tag<=0;
   lane<=0;negative<=0;dividend<=0;quotient<=0;remainder<=0;steps<=0;
   fraction_high<=0;fraction_tie<=0;calibration_faults<=0;range_drops<=0;out_tag<=0;out_data<=0;
   for(j=0;j<4;j=j+1)begin sums[j]<=0;bias[j]<=0;end
  end else if(!enable)begin state<=WAIT;seen<=0;gap<=0;end
  else if(upstream_loss || tag_bad || expired)begin
   state<=WAIT;seen<=0;gap<=0;calibration_faults<=calibration_faults+1'b1;out_loss<=1;
  end else begin
   if(raw_valid)begin
    gap<=0;
    expected_tag<={raw_tag[63:48],raw_tag[47:0]+48'd1};
    if(state!=WAIT && seen<N+GUARD+1)seen<=seen+1'b1;
   end else if(state!=WAIT && gap<GAP_TIMEOUT)gap<=gap+1'b1;
   if(raw_valid && calibrated)begin
    out_tag<=raw_tag;out_data<=corrected;
    if(overflow)begin range_drops<=range_drops+1'b1;out_loss<=1;end
    else out_valid<=1;
   end
   case(state)
    WAIT:if(raw_valid)begin
     first_tag<=raw_tag;seen<=1;state<=ACC;
     for(j=0;j<4;j=j+1)sums[j]<=$signed(raw_data[j*16+:16]);
    end
    ACC:if(raw_valid)begin
     for(j=0;j<4;j=j+1)sums[j]<=sums[j]+$signed(raw_data[j*16+:16]);
     if(seen==N-1)begin lane<=0;state<=LOAD;end
    end
    LOAD:begin
     negative<=sums[lane]<0;dividend<=sums[lane]<0?-sums[lane]:sums[lane];
     quotient<=0;remainder<=0;steps<=33;state<=DIVIDE;
    end
    DIVIDE:begin
     dividend<={dividend[31:0],1'b0};quotient<=next_q;remainder<=reduced[16:0];steps<=steps-1'b1;
     if(steps==1)begin
      bias[lane]<=negative?-$signed(next_q[16:0])-(reduced!=0):$signed(next_q[16:0]);
      fraction_high[lane]<=fraction*2>N;fraction_tie[lane]<=fraction*2==N;
      if(lane==3)state<=SETTLE;
      else begin lane<=lane+1'b1;state<=LOAD;end
     end
    end
    SETTLE:if(seen>=N+GUARD)state<=RUN;
    RUN:begin end
    default:state<=WAIT;
   endcase
  end
 end
endmodule
