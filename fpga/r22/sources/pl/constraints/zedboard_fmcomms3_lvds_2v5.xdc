# ZedBoard-compatible FMC-LPC <-> AD-FMCOMMS3 standard LVDS 2R2T wiring.
#
# This file is selected only by MIMO_RF_INTERFACE=lvds_2r2t_2v5.  It is a
# separate route from the historical 1.8-V CMOS experiment.  The user has
# approved the 2.5-V VADJ selection; physical J18/VADJ and Bank34/35 VCCO
# measurement remain mandatory hardware-acceptance evidence.

# AD9361 -> FPGA receive differential pairs.  Explicit lines are required:
# Vivado's XDC reader does not reliably expand Tcl foreach commands.
set_property -dict {PACKAGE_PIN M19 IOSTANDARD LVDS_25 DIFF_TERM TRUE} [get_ports rx_clk_in_p]
set_property -dict {PACKAGE_PIN M20 IOSTANDARD LVDS_25 DIFF_TERM TRUE} [get_ports rx_clk_in_n]
set_property -dict {PACKAGE_PIN N19 IOSTANDARD LVDS_25 DIFF_TERM TRUE} [get_ports rx_frame_in_p]
set_property -dict {PACKAGE_PIN N20 IOSTANDARD LVDS_25 DIFF_TERM TRUE} [get_ports rx_frame_in_n]
set_property -dict {PACKAGE_PIN P17 IOSTANDARD LVDS_25 DIFF_TERM TRUE} [get_ports {rx_data_in_p[0]}]
set_property -dict {PACKAGE_PIN P18 IOSTANDARD LVDS_25 DIFF_TERM TRUE} [get_ports {rx_data_in_n[0]}]
set_property -dict {PACKAGE_PIN N22 IOSTANDARD LVDS_25 DIFF_TERM TRUE} [get_ports {rx_data_in_p[1]}]
set_property -dict {PACKAGE_PIN P22 IOSTANDARD LVDS_25 DIFF_TERM TRUE} [get_ports {rx_data_in_n[1]}]
set_property -dict {PACKAGE_PIN M21 IOSTANDARD LVDS_25 DIFF_TERM TRUE} [get_ports {rx_data_in_p[2]}]
set_property -dict {PACKAGE_PIN M22 IOSTANDARD LVDS_25 DIFF_TERM TRUE} [get_ports {rx_data_in_n[2]}]
set_property -dict {PACKAGE_PIN J18 IOSTANDARD LVDS_25 DIFF_TERM TRUE} [get_ports {rx_data_in_p[3]}]
set_property -dict {PACKAGE_PIN K18 IOSTANDARD LVDS_25 DIFF_TERM TRUE} [get_ports {rx_data_in_n[3]}]
set_property -dict {PACKAGE_PIN L21 IOSTANDARD LVDS_25 DIFF_TERM TRUE} [get_ports {rx_data_in_p[4]}]
set_property -dict {PACKAGE_PIN L22 IOSTANDARD LVDS_25 DIFF_TERM TRUE} [get_ports {rx_data_in_n[4]}]
set_property -dict {PACKAGE_PIN T16 IOSTANDARD LVDS_25 DIFF_TERM TRUE} [get_ports {rx_data_in_p[5]}]
set_property -dict {PACKAGE_PIN T17 IOSTANDARD LVDS_25 DIFF_TERM TRUE} [get_ports {rx_data_in_n[5]}]

# FPGA -> AD9361 transmit differential pairs.
set_property -dict {PACKAGE_PIN J21 IOSTANDARD LVDS_25} [get_ports tx_clk_out_p]
set_property -dict {PACKAGE_PIN J22 IOSTANDARD LVDS_25} [get_ports tx_clk_out_n]
set_property -dict {PACKAGE_PIN R20 IOSTANDARD LVDS_25} [get_ports tx_frame_out_p]
set_property -dict {PACKAGE_PIN R21 IOSTANDARD LVDS_25} [get_ports tx_frame_out_n]
set_property -dict {PACKAGE_PIN N17 IOSTANDARD LVDS_25} [get_ports {tx_data_out_p[0]}]
set_property -dict {PACKAGE_PIN N18 IOSTANDARD LVDS_25} [get_ports {tx_data_out_n[0]}]
set_property -dict {PACKAGE_PIN P20 IOSTANDARD LVDS_25} [get_ports {tx_data_out_p[1]}]
set_property -dict {PACKAGE_PIN P21 IOSTANDARD LVDS_25} [get_ports {tx_data_out_n[1]}]
set_property -dict {PACKAGE_PIN L17 IOSTANDARD LVDS_25} [get_ports {tx_data_out_p[2]}]
set_property -dict {PACKAGE_PIN M17 IOSTANDARD LVDS_25} [get_ports {tx_data_out_n[2]}]
set_property -dict {PACKAGE_PIN R19 IOSTANDARD LVDS_25} [get_ports {tx_data_out_p[3]}]
set_property -dict {PACKAGE_PIN T19 IOSTANDARD LVDS_25} [get_ports {tx_data_out_n[3]}]
set_property -dict {PACKAGE_PIN K19 IOSTANDARD LVDS_25} [get_ports {tx_data_out_p[4]}]
set_property -dict {PACKAGE_PIN K20 IOSTANDARD LVDS_25} [get_ports {tx_data_out_n[4]}]
set_property -dict {PACKAGE_PIN J16 IOSTANDARD LVDS_25} [get_ports {tx_data_out_p[5]}]
set_property -dict {PACKAGE_PIN J17 IOSTANDARD LVDS_25} [get_ports {tx_data_out_n[5]}]

# Single-ended FMC controls use the approved 2.5-V VADJ bank voltage.
set_property -dict {PACKAGE_PIN G20 IOSTANDARD LVCMOS25} [get_ports {gpio_status[0]}]
set_property -dict {PACKAGE_PIN G21 IOSTANDARD LVCMOS25} [get_ports {gpio_status[1]}]
set_property -dict {PACKAGE_PIN E19 IOSTANDARD LVCMOS25} [get_ports {gpio_status[2]}]
set_property -dict {PACKAGE_PIN E20 IOSTANDARD LVCMOS25} [get_ports {gpio_status[3]}]
set_property -dict {PACKAGE_PIN G19 IOSTANDARD LVCMOS25} [get_ports {gpio_status[4]}]
set_property -dict {PACKAGE_PIN F19 IOSTANDARD LVCMOS25} [get_ports {gpio_status[5]}]
set_property -dict {PACKAGE_PIN E15 IOSTANDARD LVCMOS25} [get_ports {gpio_status[6]}]
set_property -dict {PACKAGE_PIN D15 IOSTANDARD LVCMOS25} [get_ports {gpio_status[7]}]
set_property -dict {PACKAGE_PIN A18 IOSTANDARD LVCMOS25} [get_ports {gpio_ctl[0]}]
set_property -dict {PACKAGE_PIN A19 IOSTANDARD LVCMOS25} [get_ports {gpio_ctl[1]}]
set_property -dict {PACKAGE_PIN D22 IOSTANDARD LVCMOS25} [get_ports {gpio_ctl[2]}]
set_property -dict {PACKAGE_PIN C22 IOSTANDARD LVCMOS25} [get_ports {gpio_ctl[3]}]
set_property -dict {PACKAGE_PIN G15 IOSTANDARD LVCMOS25} [get_ports gpio_en_agc]
set_property -dict {PACKAGE_PIN G16 IOSTANDARD LVCMOS25} [get_ports gpio_sync]
set_property -dict {PACKAGE_PIN A16 IOSTANDARD LVCMOS25} [get_ports gpio_resetb]
set_property -dict {PACKAGE_PIN J20 IOSTANDARD LVCMOS25} [get_ports enable]
set_property -dict {PACKAGE_PIN K21 IOSTANDARD LVCMOS25} [get_ports txnrx]
# Pre-board electrical candidate, selected by the r105 post-place sensitivity
# audit with the unchanged conservative mode-1 external timing budget.
# Faster SCK reduces round-trip MISO delay; slower/weaker MOSI adds real
# output hold delay. These are physical driver settings, NOT timing waivers.
# Full routed timing and bench signal-integrity checks are still required.
set_property -dict {PACKAGE_PIN F18 IOSTANDARD LVCMOS25 DRIVE 12 SLEW SLOW} [get_ports spi_csn]
set_property -dict {PACKAGE_PIN E18 IOSTANDARD LVCMOS25 DRIVE 16 SLEW FAST} [get_ports spi_clk]
set_property -dict {PACKAGE_PIN E21 IOSTANDARD LVCMOS25 DRIVE 4 SLEW SLOW} [get_ports spi_mosi]
set_property -dict {PACKAGE_PIN D21 IOSTANDARD LVCMOS25} [get_ports spi_miso]
set_property PULLUP true [get_ports spi_csn]

# The Clock Wizard owns the single 16 MHz primary clock and its 0.625 ns
# input-jitter constraint. Do not recreate/rename that clock here: doing so
# overwrites scoped IP constraints (XDCC-7). The generated timing budget
# refers to the unique clock object on rx_clk_in_p, retaining its jitter.
