// Fixed 2R2T LVDS sample-domain divider for the 4 MSPS / 16 MHz profile.
// Mode changes are intentionally not live: changing the AD9361 port mode or
// rate requires reset and a profile-specific bitstream rebuild.
module rf_static_clkdiv4 (
  input  wire clk,
  output wire clk_out
);
  BUFR #(
    .BUFR_DIVIDE("4"),
    .SIM_DEVICE("7SERIES")
  ) i_sample_clk_div4 (
    .I(clk), .CE(1'b1), .CLR(1'b0), .O(clk_out)
  );
endmodule
