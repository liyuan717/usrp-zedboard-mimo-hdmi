// SPDX-License-Identifier: GPL-2.0-or-later
// Diagnostic snapshot only. Does not drive the receive or transmit datapath.
module rf_rx_channel_observer (
  input adc_clk, input adc_rst, input adc_valid, input [11:0] adc_data,
  input up_clk, input up_rstn, output [31:0] snapshot_up
);
  reg [15:0] valid_count = 0;
  reg [11:0] last_sample = 0;
  reg valid_seen = 0, nonzero_seen = 0, changed_seen = 0;
  always @(posedge adc_clk or posedge adc_rst) begin
    if (adc_rst) begin
      valid_count <= 0; last_sample <= 0;
      valid_seen <= 0; nonzero_seen <= 0; changed_seen <= 0;
    end else if (adc_valid) begin
      if (valid_count != 16'hffff) valid_count <= valid_count + 1'b1;
      valid_seen <= 1;
      if (adc_data != 0) nonzero_seen <= 1;
      if (valid_seen && adc_data != last_sample) changed_seen <= 1;
      last_sample <= adc_data;
    end
  end
  // up_xfer_status OR-accumulates its input and would corrupt sample words.
  // Reuse ADI's held-data handshake in the reverse direction for snapshots.
  up_xfer_cntrl #(.DATA_WIDTH(32)) i_observer_xfer (
    .up_rstn(~adc_rst), .up_clk(adc_clk),
    .up_data_cntrl({valid_seen, nonzero_seen, changed_seen, adc_valid,
                    valid_count, last_sample}), .up_xfer_done(),
    .d_rst(~up_rstn), .d_clk(up_clk), .d_data_cntrl(snapshot_up)
  );
endmodule
