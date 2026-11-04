# ZedBoard FMC-LPC <-> AD-FMCOMMS3 CMOS (2R2T) physical constraints.
#
# The carrier VADJ baseline is 1.8 V.  Do not change these ports to 2.5 V or
# LVDS: CMOS mode uses one conductor of each legacy differential pair as a
# separate single-ended signal.  The pin associations are mechanically derived
# from board/zedboard/fmc_signal_map.csv, which locks the reviewed carrier and
# daughtercard schematics.  XDC file readers do not support Tcl foreach, hence
# the intentionally explicit per-port assignments below.
#
# No input/output-delay budget is asserted here.  AD9361 timing depends on the
# programmed DATA_CLK rate plus measured carrier/daughtercard flight times.
# The RF signoff script deliberately fails closed until that board measurement
# record supplies min/max interface values; no false-path waiver is used.

# CMOS receive source-synchronous interface: DATA_CLK, RX_FRAME, D[11:0].
# Full-Port P0 is an AD9361 RX output.  FMCOMMS3 legacy nomenclature calls
# these conductors TX_D; each former LVDS N/P conductor is one CMOS bit.
set_property -dict {PACKAGE_PIN M19 IOSTANDARD LVCMOS18} [get_ports rx_clk_in]
set_property -dict {PACKAGE_PIN N19 IOSTANDARD LVCMOS18} [get_ports rx_frame_in]
set_property -dict {PACKAGE_PIN N18 IOSTANDARD LVCMOS18} [get_ports {rx_data_in[0]}]
set_property -dict {PACKAGE_PIN N17 IOSTANDARD LVCMOS18} [get_ports {rx_data_in[1]}]
set_property -dict {PACKAGE_PIN P21 IOSTANDARD LVCMOS18} [get_ports {rx_data_in[2]}]
set_property -dict {PACKAGE_PIN P20 IOSTANDARD LVCMOS18} [get_ports {rx_data_in[3]}]
set_property -dict {PACKAGE_PIN M17 IOSTANDARD LVCMOS18} [get_ports {rx_data_in[4]}]
set_property -dict {PACKAGE_PIN L17 IOSTANDARD LVCMOS18} [get_ports {rx_data_in[5]}]
set_property -dict {PACKAGE_PIN T19 IOSTANDARD LVCMOS18} [get_ports {rx_data_in[6]}]
set_property -dict {PACKAGE_PIN R19 IOSTANDARD LVCMOS18} [get_ports {rx_data_in[7]}]
set_property -dict {PACKAGE_PIN K20 IOSTANDARD LVCMOS18} [get_ports {rx_data_in[8]}]
set_property -dict {PACKAGE_PIN K19 IOSTANDARD LVCMOS18} [get_ports {rx_data_in[9]}]
set_property -dict {PACKAGE_PIN J17 IOSTANDARD LVCMOS18} [get_ports {rx_data_in[10]}]
set_property -dict {PACKAGE_PIN J16 IOSTANDARD LVCMOS18} [get_ports {rx_data_in[11]}]

# CMOS transmit source-synchronous interface: FB_CLK, TX_FRAME, D[11:0].
# Full-Port P1 is an AD9361 TX input.  FMCOMMS3 calls these RX_D, ordered N/P.
set_property -dict {PACKAGE_PIN J21 IOSTANDARD LVCMOS18} [get_ports tx_clk_out]
set_property -dict {PACKAGE_PIN R20 IOSTANDARD LVCMOS18} [get_ports tx_frame_out]
set_property -dict {PACKAGE_PIN P18 IOSTANDARD LVCMOS18} [get_ports {tx_data_out[0]}]
set_property -dict {PACKAGE_PIN P17 IOSTANDARD LVCMOS18} [get_ports {tx_data_out[1]}]
set_property -dict {PACKAGE_PIN P22 IOSTANDARD LVCMOS18} [get_ports {tx_data_out[2]}]
set_property -dict {PACKAGE_PIN N22 IOSTANDARD LVCMOS18} [get_ports {tx_data_out[3]}]
set_property -dict {PACKAGE_PIN M22 IOSTANDARD LVCMOS18} [get_ports {tx_data_out[4]}]
set_property -dict {PACKAGE_PIN M21 IOSTANDARD LVCMOS18} [get_ports {tx_data_out[5]}]
set_property -dict {PACKAGE_PIN K18 IOSTANDARD LVCMOS18} [get_ports {tx_data_out[6]}]
set_property -dict {PACKAGE_PIN J18 IOSTANDARD LVCMOS18} [get_ports {tx_data_out[7]}]
set_property -dict {PACKAGE_PIN L22 IOSTANDARD LVCMOS18} [get_ports {tx_data_out[8]}]
set_property -dict {PACKAGE_PIN L21 IOSTANDARD LVCMOS18} [get_ports {tx_data_out[9]}]
set_property -dict {PACKAGE_PIN T17 IOSTANDARD LVCMOS18} [get_ports {tx_data_out[10]}]
set_property -dict {PACKAGE_PIN T16 IOSTANDARD LVCMOS18} [get_ports {tx_data_out[11]}]

# AD9361 control and SPI. RESET_B stays low through AXI GPIO until Linux owns
# the peripheral. Slew/drive remain tool-default pending measured output load.
set_property -dict {PACKAGE_PIN G20 IOSTANDARD LVCMOS18} [get_ports {gpio_status[0]}]
set_property -dict {PACKAGE_PIN G21 IOSTANDARD LVCMOS18} [get_ports {gpio_status[1]}]
set_property -dict {PACKAGE_PIN E19 IOSTANDARD LVCMOS18} [get_ports {gpio_status[2]}]
set_property -dict {PACKAGE_PIN E20 IOSTANDARD LVCMOS18} [get_ports {gpio_status[3]}]
set_property -dict {PACKAGE_PIN G19 IOSTANDARD LVCMOS18} [get_ports {gpio_status[4]}]
set_property -dict {PACKAGE_PIN F19 IOSTANDARD LVCMOS18} [get_ports {gpio_status[5]}]
set_property -dict {PACKAGE_PIN E15 IOSTANDARD LVCMOS18} [get_ports {gpio_status[6]}]
set_property -dict {PACKAGE_PIN D15 IOSTANDARD LVCMOS18} [get_ports {gpio_status[7]}]
set_property -dict {PACKAGE_PIN A18 IOSTANDARD LVCMOS18} [get_ports {gpio_ctl[0]}]
set_property -dict {PACKAGE_PIN A19 IOSTANDARD LVCMOS18} [get_ports {gpio_ctl[1]}]
set_property -dict {PACKAGE_PIN D22 IOSTANDARD LVCMOS18} [get_ports {gpio_ctl[2]}]
set_property -dict {PACKAGE_PIN C22 IOSTANDARD LVCMOS18} [get_ports {gpio_ctl[3]}]
set_property -dict {PACKAGE_PIN G15 IOSTANDARD LVCMOS18} [get_ports gpio_en_agc]
set_property -dict {PACKAGE_PIN G16 IOSTANDARD LVCMOS18} [get_ports gpio_sync]
set_property -dict {PACKAGE_PIN A16 IOSTANDARD LVCMOS18} [get_ports gpio_resetb]
set_property -dict {PACKAGE_PIN J20 IOSTANDARD LVCMOS18} [get_ports enable]
set_property -dict {PACKAGE_PIN K21 IOSTANDARD LVCMOS18} [get_ports txnrx]
set_property -dict {PACKAGE_PIN F18 IOSTANDARD LVCMOS18} [get_ports spi_csn]
set_property -dict {PACKAGE_PIN E18 IOSTANDARD LVCMOS18} [get_ports spi_clk]
set_property -dict {PACKAGE_PIN E21 IOSTANDARD LVCMOS18} [get_ports spi_mosi]
set_property -dict {PACKAGE_PIN D21 IOSTANDARD LVCMOS18} [get_ports spi_miso]
set_property PULLUP true [get_ports spi_csn]

# The selected pre-board candidate is 4.00 MSPS per complex stream, with the
# configured full-port CMOS DDR clock at 8 MHz (125 ns).  This is deliberately
# slower than the AD9361's 61.44 MHz maximum interface-clock envelope; it is
# not permission to change the RF register profile to 30.72 MSPS.  This source
# clock alone is not interface closure: generated profile XDC adds data/frame
# min/max delays and the release gate rejects an absent or unapproved budget.
create_clock -name ad9361_rx_clk -period 125.000 [get_ports rx_clk_in]
