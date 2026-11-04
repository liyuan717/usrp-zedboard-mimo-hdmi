// Fixed-rate RF sample-domain clock divider for the ZedBoard 2R2T candidate.
//
// The ADI reference design uses util_clkdiv with a BUFGMUX so software may
// switch live between 1R1T and 2R2T.  This design is intentionally not a
// run-time mode-switching design: changing AD9361 port mode while streaming
// invalidates FIFO/DMA framing.  A fixed two-way BUFR eliminates that dynamic
// clock mux and its unqualified CDC crossings.  Switching modes requires a
// reset/reconfiguration and a different bitstream/profile.
module rf_static_clkdiv (
  input  wire clk,
  output wire clk_out
);

  BUFR #(
    .BUFR_DIVIDE("2"),
    .SIM_DEVICE("7SERIES")
  ) i_sample_clk_div2 (
    .I   (clk),
    .CE  (1'b1),
    .CLR (1'b0),
    .O   (clk_out)
  );

endmodule
