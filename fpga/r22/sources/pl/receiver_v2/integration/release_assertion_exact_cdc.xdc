# Exact async assertion arcs ONLY. Companion release_assertion_contract.tcl
# must validate the actual FDCE/FDPE topology before synthesis/route admission.
# Local release D chains and every ordinary business D/CE path remain timed.
# No clock-group, register-wide or reset-net-wide exception is allowed.
set rf_assert_sources [get_pins -quiet -hier -filter {NAME == "receiver_v2_i/reset_rf/U0/PR_OUT_DFF[0].FDRE_PER/Q" || NAME == "receiver_v2_i/reset_rf_lclk/U0/PR_OUT_DFF[0].FDRE_PER/Q" || NAME == "receiver_v2_i/rf_sampling_clock/inst/mmcm_adv_inst/LOCKED" || NAME == "receiver_v2_i/delay_clock/inst/mmcm_adv_inst/LOCKED" || NAME == "receiver_v2_i/rf_lclk_monitor/inst/ready_reg/Q"}]
set rf_assert_clrs [get_pins -quiet -hier -filter {NAME == "receiver_v2_i/rf_capture_guard/inst/din_release_sync_reg[0]/CLR" || NAME == "receiver_v2_i/rf_capture_guard/inst/din_release_sync_reg[1]/CLR" || NAME == "receiver_v2_i/rf_capture_guard/inst/dout_release_sync_reg[0]/CLR" || NAME == "receiver_v2_i/rf_capture_guard/inst/dout_release_sync_reg[1]/CLR"}]
# Sequential Q / MMCM LOCKED are data pins, not valid -from clock/input
# startpoints in Vivado 2025.2. Match these EXACT data arcs with -through.
set_false_path -through $rf_assert_sources -to $rf_assert_clrs

