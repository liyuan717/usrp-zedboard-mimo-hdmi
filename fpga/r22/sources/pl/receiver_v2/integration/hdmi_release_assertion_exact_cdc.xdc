# Only the six asynchronous PRE assertion arcs of the two reviewed reset
# helpers. No stage D/CE or downstream business reset pin is excepted.
# release_assertion_contract.tcl validates exact source, primitives, clocks,
# first-stage sole fanout and normally timed release D chains.
set hdmi_assert_source [get_pins -quiet -hier -filter {NAME == "receiver_v2_i/axi_hdmi_core/inst/i_up/up_core_preset_reg/Q"}]
set hdmi_assert_pres [get_pins -quiet -hier -filter {NAME == "receiver_v2_i/axi_hdmi_core/inst/i_up/i_core_rst_reg/release_m1_reg/PRE" || NAME == "receiver_v2_i/axi_hdmi_core/inst/i_up/i_core_rst_reg/release_m2_reg/PRE" || NAME == "receiver_v2_i/axi_hdmi_core/inst/i_up/i_core_rst_reg/rst_reg/PRE" || NAME == "receiver_v2_i/axi_hdmi_core/inst/i_up/i_vdma_rst_reg/release_m1_reg/PRE" || NAME == "receiver_v2_i/axi_hdmi_core/inst/i_up/i_vdma_rst_reg/release_m2_reg/PRE" || NAME == "receiver_v2_i/axi_hdmi_core/inst/i_up/i_vdma_rst_reg/rst_reg/PRE"}]
set_false_path -through $hdmi_assert_source -to $hdmi_assert_pres
