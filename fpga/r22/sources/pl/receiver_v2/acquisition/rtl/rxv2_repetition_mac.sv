`timescale 1ns/1ps
// Shared-MAC P/P repetition metric.  One accepted dual-RX sample consumes
// four 50-MHz MAC cycles plus check/accept cycles: six clocks per scan, or
// 8.33 MSPS service at the 4 MSPS input requirement.
module rxv2_repetition_mac #(
  parameter integer HISTORY = 216
) (
  input wire clk, input wire resetn,
  input wire [63:0] s_data, input wire s_valid, output wire s_ready,
  input wire flush,
  output reg candidate_valid,
  output reg signed [47:0] corr_re,
  output reg signed [47:0] corr_im,
  output reg [47:0] energy_a,
  output reg [47:0] energy_b
);
  localparam ST_IDLE=3'd0, ST_A0=3'd1, ST_A1=3'd2,
             ST_S0=3'd3, ST_S1=3'd4, ST_CHECK=3'd5;
  reg [2:0] state;
  reg signed [15:0] hist_i0[0:HISTORY-1],hist_q0[0:HISTORY-1];
  reg signed [15:0] hist_i1[0:HISTORY-1],hist_q1[0:HISTORY-1];
  integer n;
  reg [8:0] hist_count;
  reg signed [15:0] x_i0,x_q0,x_i1,x_q1;
  reg signed [47:0] wr,wi;
  reg [47:0] wea,web;
  reg signed [15:0] ui,uq,vi,vq;
  wire signed [31:0] p0=ui*vi, p1=uq*vq, p2=ui*vq, p3=uq*vi;
  wire signed [32:0] prod_re=p0+p1, prod_im=p2-p3;
  wire [31:0] mu=ui*ui+uq*uq, mv=vi*vi+vq*vq;
  function automatic [48:0] absv;
    input signed [47:0] v; begin absv=v[47] ? -v:v; end
  endfunction
  wire [48:0] l1=absv(wr)+absv(wi);
  wire [48:0] es={1'b0,wea}+{1'b0,web};
  assign s_ready=(state==ST_IDLE);
  always @(*) begin
    ui=0;uq=0;vi=0;vq=0;
    case(state)
      ST_A0: begin ui=hist_i0[127];uq=hist_q0[127];vi=x_i0;vq=x_q0; end
      ST_A1: begin ui=hist_i1[127];uq=hist_q1[127];vi=x_i1;vq=x_q1; end
      ST_S0: begin ui=hist_i0[215];uq=hist_q0[215];vi=hist_i0[87];vq=hist_q0[87]; end
      ST_S1: begin ui=hist_i1[215];uq=hist_q1[215];vi=hist_i1[87];vq=hist_q1[87]; end
      default: begin end
    endcase
  end
  always @(posedge clk) begin
    if(!resetn || flush) begin
      state<=ST_IDLE;hist_count<=0;corr_re<=0;corr_im<=0;energy_a<=0;energy_b<=0;candidate_valid<=0;
      for(n=0;n<HISTORY;n=n+1) begin hist_i0[n]<=0;hist_q0[n]<=0;hist_i1[n]<=0;hist_q1[n]<=0;end
    end else begin
      candidate_valid<=0;
      case(state)
        ST_IDLE: if(s_valid) begin
          x_i0<=s_data[15:0];x_q0<=s_data[31:16];x_i1<=s_data[47:32];x_q1<=s_data[63:48];
          state<=ST_A0;
        end
        ST_A0: begin
          if(hist_count>=128) begin wr<=corr_re+prod_re;wi<=corr_im+prod_im;wea<=energy_a+mu;web<=energy_b+mv;end
          else begin wr<=0;wi<=0;wea<=0;web<=0;end
          state<=ST_A1;
        end
        ST_A1: begin
          if(hist_count>=128) begin wr<=wr+prod_re;wi<=wi+prod_im;wea<=wea+mu;web<=web+mv;end
          state<=ST_S0;
        end
        ST_S0: begin
          if(hist_count>=216) begin wr<=wr-prod_re;wi<=wi-prod_im;wea<=wea-mu;web<=web-mv;end
          state<=ST_S1;
        end
        ST_S1: begin
          if(hist_count>=216) begin wr<=wr-prod_re;wi<=wi-prod_im;wea<=wea-mu;web<=web-mv;end
          state<=ST_CHECK;
        end
        ST_CHECK: begin
          corr_re<=wr;corr_im<=wi;energy_a<=wea;energy_b<=web;
          if (hist_count>=216 && wea>=(web>>1) && web>=(wea>>1) &&
              ((l1<<2)>=(es+(es>>1)))) begin
            candidate_valid <= 1'b1;
          end
          for(n=HISTORY-1;n>0;n=n-1) begin
            hist_i0[n]<=hist_i0[n-1];hist_q0[n]<=hist_q0[n-1];hist_i1[n]<=hist_i1[n-1];hist_q1[n]<=hist_q1[n-1];
          end
          hist_i0[0]<=x_i0;hist_q0[0]<=x_q0;hist_i1[0]<=x_i1;hist_q1[0]<=x_q1;
          if(hist_count<HISTORY) hist_count<=hist_count+1'b1;
          state<=ST_IDLE;
        end
        default:state<=ST_IDLE;
      endcase
    end
  end
endmodule
