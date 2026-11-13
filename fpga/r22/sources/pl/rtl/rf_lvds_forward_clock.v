// Fixed 2R2T LVDS profile: forward the MMCM 0-degree clock, while ADI TX
// data/frame ODDRs use its 90-degree sibling. At 16 MHz this centers the
// external AD9361 sampling edges between both FPGA DDR launch edges.
// This is a real physical phase relation, not a timing exception.
module rf_lvds_forward_clock (
  (* X_INTERFACE_PARAMETER="FREQ_HZ 16000000" *)
  input wire clk,
  (* X_INTERFACE_PARAMETER="FREQ_HZ 16000000" *)
  output wire tx_clk_p,
  (* X_INTERFACE_PARAMETER="FREQ_HZ 16000000" *)
  output wire tx_clk_n
);
  wire forwarded;
  ODDR #(.DDR_CLK_EDGE("SAME_EDGE"), .INIT(1'b0), .SRTYPE("SYNC"))
    i_tx_data_oddr (.C(clk), .CE(1'b1), .D1(1'b1), .D2(1'b0),
                    .R(1'b0), .S(1'b0), .Q(forwarded));
  OBUFDS #(.IOSTANDARD("LVDS_25"), .SLEW("FAST"))
    i_tx_clock_buffer (.I(forwarded), .O(tx_clk_p), .OB(tx_clk_n));
endmodule
