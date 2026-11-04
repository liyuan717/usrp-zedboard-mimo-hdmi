// SPDX-License-Identifier: GPL-2.0-or-later
// ABI 2: rolling four-clock DDR frame window and latest DDR input data.
// All observed inputs belong to l_clk; no receive/transmit signal is driven.
module rf_rx_frame_window_observer (
  input adc_clk, input adc_rst, input [1:0] frame,
  input [5:0] data_rise, input [5:0] data_fall, input r1_mode,
  input adc_valid, input adc_status, input delay_locked,
  input up_clk, input up_rstn, output [31:0] snapshot_up
);
  reg [7:0] frame_history = 0;
  reg [23:0] observation = 0;
  always @(posedge adc_clk or posedge adc_rst) begin
    if (adc_rst) begin
      frame_history <= 0;
      observation <= 0;
    end else begin
      frame_history <= {frame_history[5:0], frame};
      // Newest frame is bits 13:12; oldest is bits 19:18.
      // Receiver control/valid may refer to preceding pipeline stages,
      // rather than to the newest raw frame pair in this observation.
      observation <= {delay_locked, adc_status, adc_valid, r1_mode,
                      frame_history[5:0], frame, data_rise, data_fall};
    end
  end
  up_xfer_cntrl #(.DATA_WIDTH(32)) i_observer_xfer (
    .up_rstn(~adc_rst), .up_clk(adc_clk),
    .up_data_cntrl({8'hd9, observation}), .up_xfer_done(),
    .d_rst(~up_rstn), .d_clk(up_clk), .d_data_cntrl(snapshot_up)
  );
endmodule
