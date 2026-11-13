// G0 arithmetic/resource probe only. Not the frozen R06 interface or a PHY.
`timescale 1ns/1ps
module ml_cost_probe (
  input wire clk, input wire rst,
  input wire in_valid, output wire in_ready,
  input wire [63:0] y_in, input wire [111:0] h_in,
  output reg out_valid, input wire out_ready,
  output reg [3:0] decision, output reg [35:0] metric_out
);
  reg busy;
  reg [3:0] candidate, best_decision;
  reg [35:0] best_metric;
  reg signed [15:0] y[0:3];
  reg signed [13:0] h[0:7];
  integer pr0, pi0, pr1, pi1;
  reg signed [16:0] er0, ei0, er1, ei1;
  (* use_dsp = "yes" *) wire signed [33:0] m0 = er0 * er0;
  (* use_dsp = "yes" *) wire signed [33:0] m1 = ei0 * ei0;
  (* use_dsp = "yes" *) wire signed [33:0] m2 = er1 * er1;
  (* use_dsp = "yes" *) wire signed [33:0] m3 = ei1 * ei1;
  wire [35:0] metric = {2'b0,m0} + {2'b0,m1} + {2'b0,m2} + {2'b0,m3};
  assign in_ready = !busy && (!out_valid || out_ready);
  always @* begin
    // Bit set denotes -1; each TX symbol has independent real/imag signs.
    pr0 = (candidate[0] ? -$signed(h[0]) : $signed(h[0])) - (candidate[1] ? -$signed(h[1]) : $signed(h[1]))
        + (candidate[2] ? -$signed(h[2]) : $signed(h[2])) - (candidate[3] ? -$signed(h[3]) : $signed(h[3]));
    pi0 = (candidate[0] ? -$signed(h[1]) : $signed(h[1])) + (candidate[1] ? -$signed(h[0]) : $signed(h[0]))
        + (candidate[2] ? -$signed(h[3]) : $signed(h[3])) + (candidate[3] ? -$signed(h[2]) : $signed(h[2]));
    pr1 = (candidate[0] ? -$signed(h[4]) : $signed(h[4])) - (candidate[1] ? -$signed(h[5]) : $signed(h[5]))
        + (candidate[2] ? -$signed(h[6]) : $signed(h[6])) - (candidate[3] ? -$signed(h[7]) : $signed(h[7]));
    pi1 = (candidate[0] ? -$signed(h[5]) : $signed(h[5])) + (candidate[1] ? -$signed(h[4]) : $signed(h[4]))
        + (candidate[2] ? -$signed(h[7]) : $signed(h[7])) + (candidate[3] ? -$signed(h[6]) : $signed(h[6]));
    er0 = $signed(y[0]) - pr0; ei0 = $signed(y[1]) - pi0;
    er1 = $signed(y[2]) - pr1; ei1 = $signed(y[3]) - pi1;
  end
  integer i;
  always @(posedge clk) begin
    if (rst) begin
      busy <= 0; out_valid <= 0; candidate <= 0;
      best_metric <= 0; best_decision <= 0; decision <= 0; metric_out <= 0;
      for (i=0; i<4; i=i+1) y[i] <= 0;
      for (i=0; i<8; i=i+1) h[i] <= 0;
    end else begin
      if (out_valid && out_ready) out_valid <= 0;
      if (in_valid && in_ready) begin
        for (i=0; i<4; i=i+1) y[i] <= y_in[i*16+:16];
        for (i=0; i<8; i=i+1) h[i] <= h_in[i*14+:14];
        candidate <= 0; busy <= 1;
      end else if (busy) begin
        if (candidate == 0 || metric < best_metric) begin
          best_metric <= metric; best_decision <= candidate;
        end
        if (candidate == 15) begin
          metric_out <= metric < best_metric ? metric : best_metric;
          decision <= metric < best_metric ? candidate : best_decision;
          out_valid <= 1; busy <= 0;
        end else candidate <= candidate + 1'b1;
      end
    end
  end
endmodule
