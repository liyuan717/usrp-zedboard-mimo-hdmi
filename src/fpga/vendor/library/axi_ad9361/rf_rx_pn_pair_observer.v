// SPDX-License-Identifier: GPL-2.0-or-later
// ABI3: each CPU word independently holds one coherent ADC-domain packet.
// This observer never drives the datapath or the real PN monitor.
module rf_rx_pn_pair_observer #(
  parameter Q_OR_I_N = 0,
  parameter PAIR_STATES = 0
) (
  input adc_clk, input adc_rst, input adc_valid,
  input [11:0] adc_data, input [11:0] adc_data_q,
  input [3:0] pn_sel,
  input up_clk, input up_rstn, output [31:0] snapshot_up
);
  wire [11:0] data_i = Q_OR_I_N ? adc_data_q : adc_data;
  wire [11:0] data_q = Q_OR_I_N ? adc_data : adc_data_q;
  wire [11:0] q_rev;
  genvar k;
  generate for(k=0;k<12;k=k+1) begin: reverse_q
    assign q_rev[k] = data_q[11-k];
  end endgenerate
  wire [15:0] state_now = {data_i, q_rev[3:0]};
  wire iq_match = data_i[7:0] == q_rev[11:4];
  reg [15:0] previous_state = 0;
  reg have_previous = 0;
  reg previous_iq_match = 0;
  reg [3:0] previous_sel = 0;
  reg [31:0] packet = 0;
  wire [15:0] predicted = {previous_state[14:0],
                          (^previous_state[15:4]) ^ (^previous_state[2:1])};
  always @(posedge adc_clk or posedge adc_rst) begin
    if (adc_rst) begin
      previous_state <= 0;
      have_previous <= 0;
      previous_iq_match <= 0;
      previous_sel <= 0;
      packet <= 0;
    end else if (adc_valid) begin
      // Adjacent means consecutive adc_valid events, across any invalid gaps.
      // PAIR_STATES holds raw reconstruction even on an I/Q mismatch; unlike
      // the checker, it does not substitute DEAD. Zero is not a valid PN seed.
      if (PAIR_STATES) begin
        packet <= have_previous ? {previous_state, state_now} : 32'd0;
      end else begin
        packet <= {1'b1, have_previous, iq_match,
                   (have_previous && previous_iq_match && iq_match &&
                    previous_sel == pn_sel && pn_sel != 4'h9 &&
                    previous_state != 0 && predicted == state_now),
                   pn_sel, data_i, data_q};
      end
      previous_state <= state_now;
      have_previous <= 1;
      previous_iq_match <= iq_match;
      previous_sel <= pn_sel;
    end
  end
  // Held-data handshake, not up_xfer_status (which OR-accumulates data).
  up_xfer_cntrl #(.DATA_WIDTH(32)) i_observer_xfer (
    .up_rstn(~adc_rst), .up_clk(adc_clk),
    .up_data_cntrl(packet), .up_xfer_done(),
    .d_rst(~up_rstn), .d_clk(up_clk), .d_data_cntrl(snapshot_up)
  );
endmodule
