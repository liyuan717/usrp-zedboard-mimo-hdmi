// SPDX-License-Identifier: ADI-BSD
// Private reset synchronizer for the receiver-v2 HDMI overlay.
//
// Unlike the legacy common/ad_rst.v instance, assertion of rst is asynchronous
// to clk.  This is required because reference_clk may be stopped while software
// asserts the HDMI core reset.  Deassertion remains synchronized locally.
`timescale 1ns/100ps

module axi_hdmi_tx_reset_sync (
  input  wire rst_async,
  input  wire clk,
  output reg  rst
);

  (* ASYNC_REG = "TRUE" *) reg release_m1 = 1'b1;
  (* ASYNC_REG = "TRUE" *) reg release_m2 = 1'b1;

  always @(posedge clk or posedge rst_async) begin
    if (rst_async) begin
      release_m1 <= 1'b1;
      release_m2 <= 1'b1;
      rst <= 1'b1;
    end else begin
      release_m1 <= 1'b0;
      release_m2 <= release_m1;
      rst <= release_m2;
    end
  end

endmodule
