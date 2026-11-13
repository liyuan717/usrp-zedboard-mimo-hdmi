# The monitor crosses two single-bit levels into clk50 through explicit 2FF
# ASYNC_REG chains. Bound routing to FIRST stages by one destination period.
# This is not a synchronous phase relationship to the AD9361-derived clock.
# No exception is applied to the first-to-second-stage clk50 register paths.
# Exact target cardinalities are checked after synthesis by the builder.
set_max_delay -datapath_only 20.000 -from [get_cells {receiver_v2_i/rf_lclk_monitor/inst/source_toggle_reg}] -to [get_cells {receiver_v2_i/rf_lclk_monitor/inst/toggle_sync_reg[0]}]
# source_ready comes from the source-clock reset controller; resetn also gates
# this first stage. Use actual sequential cone roots, not a generated-clock
# name that may not exist yet when a synthesized checkpoint is opened.
set_max_delay -datapath_only 20.000 -from [all_fanin -flat -startpoints_only -only_cells -to [get_pins {receiver_v2_i/rf_lclk_monitor/inst/ready_sync_reg[0]/D}]] -to [get_cells {receiver_v2_i/rf_lclk_monitor/inst/ready_sync_reg[0]}]
