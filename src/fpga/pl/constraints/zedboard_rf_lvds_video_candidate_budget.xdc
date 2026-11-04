# Finite-video candidate RF source-synchronous timing budget.
#
# profile_id=lvds_2r2t_2v5
# profile_status=PREBOARD_CONSERVATIVE_APPROVED
# source_budget=build/zedboard_rf_lvds_budget_v5_20261015.xdc
# clock_binding=existing_clockwizard_rx_clk_in_p
# Numerical delays are the reviewed offline engineering model from the source
# budget above, not measured board timing.  This derivative exists only because
# the source file names `ad9361_rx_clk`, while this top-level Clocking Wizard
# exports that source clock as `rx_clk_in_p`.
#
# RX_CLK is 16 MHz in the fixed 4 MSPS / divider-4 LVDS profile.  Reuse the
# input clock already emitted by the Clocking Wizard; do not override it with
# a second create_clock.  XDC files are parsed in Vivado's restricted managed
# mode, so cardinality assertions live in the candidate Tcl after routing.
set rf_video_rx_clk [get_clocks -quiet rx_clk_in_p]
set_input_jitter $rf_video_rx_clk 0.625

set rf_video_rx_data_ports [get_ports -quiet {rx_frame_in_p rx_data_in_p[*]}]
set_input_delay -clock $rf_video_rx_clk -min -0.500 $rf_video_rx_data_ports
set_input_delay -clock $rf_video_rx_clk -max 2.000 $rf_video_rx_data_ports
# DDR data can be launched on either edge; retain BOTH launch edges.
set_input_delay -clock $rf_video_rx_clk -clock_fall -add_delay -min -0.500 $rf_video_rx_data_ports
set_input_delay -clock $rf_video_rx_clk -clock_fall -add_delay -max 2.000 $rf_video_rx_data_ports

set rf_video_tx_clock_pin [get_pins -quiet -hierarchical -filter {NAME =~ */rf_fb_clock/*i_tx_data_oddr/C}]
set rf_video_tx_clock_port [get_ports -quiet tx_clk_out_p]
create_generated_clock -name ad9361_fb_clk -source $rf_video_tx_clock_pin -divide_by 1 $rf_video_tx_clock_port

set rf_video_tx_data_ports [get_ports -quiet {tx_frame_out_p tx_data_out_p[*]}]
set_output_delay -clock ad9361_fb_clk -min -0.750 $rf_video_tx_data_ports
set_output_delay -clock ad9361_fb_clk -max 1.750 $rf_video_tx_data_ports
set_output_delay -clock ad9361_fb_clk -clock_fall -add_delay -min -0.750 $rf_video_tx_data_ports
set_output_delay -clock ad9361_fb_clk -clock_fall -add_delay -max 1.750 $rf_video_tx_data_ports
