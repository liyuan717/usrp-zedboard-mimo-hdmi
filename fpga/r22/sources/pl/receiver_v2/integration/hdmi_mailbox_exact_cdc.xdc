# Production-only tightening for the already-reviewed HDMI frame-mailbox
# payload crossing.  These nine literal same-bit bounds are additive to the
# reviewed vendor aggregate 6.000 ns bound and 1.000 ns bus-skew requirement:
# they do not relax, replace, or add any false-path/clock exception.
#
# Integration order: read this XDC after opening the completed synth_1 run and
# before opt_design.  This is the same pre-opt insertion point validated by
# diagnostic r214/r215; do not defer it until after placement or routing.
set_max_delay -datapath_only 1.500 -from [get_cells {receiver_v2_i/axi_hdmi_core/inst/i_frame_mailbox/src_payload_hold_reg[0]}] -to [get_cells {receiver_v2_i/axi_hdmi_core/inst/i_frame_mailbox/dst_addr_reg[0]}]
set_max_delay -datapath_only 1.500 -from [get_cells {receiver_v2_i/axi_hdmi_core/inst/i_frame_mailbox/src_payload_hold_reg[1]}] -to [get_cells {receiver_v2_i/axi_hdmi_core/inst/i_frame_mailbox/dst_addr_reg[1]}]
set_max_delay -datapath_only 1.500 -from [get_cells {receiver_v2_i/axi_hdmi_core/inst/i_frame_mailbox/src_payload_hold_reg[2]}] -to [get_cells {receiver_v2_i/axi_hdmi_core/inst/i_frame_mailbox/dst_addr_reg[2]}]
set_max_delay -datapath_only 1.500 -from [get_cells {receiver_v2_i/axi_hdmi_core/inst/i_frame_mailbox/src_payload_hold_reg[3]}] -to [get_cells {receiver_v2_i/axi_hdmi_core/inst/i_frame_mailbox/dst_addr_reg[3]}]
set_max_delay -datapath_only 1.500 -from [get_cells {receiver_v2_i/axi_hdmi_core/inst/i_frame_mailbox/src_payload_hold_reg[4]}] -to [get_cells {receiver_v2_i/axi_hdmi_core/inst/i_frame_mailbox/dst_addr_reg[4]}]
set_max_delay -datapath_only 1.500 -from [get_cells {receiver_v2_i/axi_hdmi_core/inst/i_frame_mailbox/src_payload_hold_reg[5]}] -to [get_cells {receiver_v2_i/axi_hdmi_core/inst/i_frame_mailbox/dst_addr_reg[5]}]
set_max_delay -datapath_only 1.500 -from [get_cells {receiver_v2_i/axi_hdmi_core/inst/i_frame_mailbox/src_payload_hold_reg[6]}] -to [get_cells {receiver_v2_i/axi_hdmi_core/inst/i_frame_mailbox/dst_addr_reg[6]}]
set_max_delay -datapath_only 1.500 -from [get_cells {receiver_v2_i/axi_hdmi_core/inst/i_frame_mailbox/src_payload_hold_reg[7]}] -to [get_cells {receiver_v2_i/axi_hdmi_core/inst/i_frame_mailbox/dst_addr_reg[7]}]
set_max_delay -datapath_only 1.500 -from [get_cells {receiver_v2_i/axi_hdmi_core/inst/i_frame_mailbox/src_payload_hold_reg[8]}] -to [get_cells {receiver_v2_i/axi_hdmi_core/inst/i_frame_mailbox/dst_addr_reg[8]}]
