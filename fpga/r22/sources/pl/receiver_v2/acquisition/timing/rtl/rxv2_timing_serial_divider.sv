`timescale 1ns/1ps
// Bounded signed restoring divider used only by the RXV2 timing estimator.
// Derived from channel/mrf1_channel_divider.sv, but kept timing-owned so the
// acquisition build has no dependency on the channel lane.
module rxv2_timing_serial_divider #(
    parameter integer NUM_W = 80, parameter integer DEN_W = 64, parameter integer OUT_W = 64
) (
    input wire clk, input wire resetn, input wire start,
    input wire signed [NUM_W-1:0] numerator, input wire [DEN_W-1:0] denominator,
    output reg busy, output reg done, output reg div_zero, output reg signed [OUT_W-1:0] quotient
);
  reg sign_q; reg [NUM_W-1:0] dividend_q, quotient_q; reg [DEN_W:0] remainder_q;
  reg [DEN_W-1:0] denominator_q; reg [6:0] count_q; reg [DEN_W:0] rem_next; reg [NUM_W-1:0] quo_next;
  always @* begin
    rem_next={remainder_q[DEN_W-1:0],dividend_q[NUM_W-1]}; quo_next={quotient_q[NUM_W-2:0],1'b0};
    if(rem_next>={1'b0,denominator_q}) begin rem_next=rem_next-{1'b0,denominator_q};quo_next[0]=1'b1;end
  end
  always @(posedge clk) begin
    if(!resetn) begin busy<=0;done<=0;div_zero<=0;quotient<=0;sign_q<=0;dividend_q<=0;quotient_q<=0;remainder_q<=0;denominator_q<=0;count_q<=0;end
    else begin
      done<=0;
      if(start&&!busy) begin
        div_zero<=(denominator==0);quotient<=0;
        if(denominator==0) done<=1;
        else begin busy<=1;sign_q<=numerator[NUM_W-1];dividend_q<=numerator[NUM_W-1]?(~numerator+1'b1):numerator;quotient_q<=0;remainder_q<=0;denominator_q<=denominator;count_q<=0;end
      end else if(busy) begin
        dividend_q<={dividend_q[NUM_W-2:0],1'b0};remainder_q<=rem_next;quotient_q<=quo_next;
        if(count_q==NUM_W-1) begin busy<=0;done<=1;quotient<=sign_q?-$signed(quo_next[OUT_W-1:0]):$signed(quo_next[OUT_W-1:0]);end
        else count_q<=count_q+1'b1;
      end
    end
  end
endmodule
