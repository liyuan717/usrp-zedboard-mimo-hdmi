# The capture coordinator crosses three held scalar levels. Bound each source
# FF to the destination synchronizer FIRST stage by one destination period.
# Same-clock m1->m2 and local reset-release timing must remain normally timed.
# These statements do not waive asynchronous reset assertions or CDC warnings.
set_max_delay -datapath_only 62.500 -from [get_cells {receiver_v2_i/rf_capture_guard/inst/dst_fenced_r_reg}] -to [get_cells {receiver_v2_i/rf_capture_guard/inst/dst_fenced_sync_reg[0]}]
set_max_delay -datapath_only 20.000 -from [get_cells {receiver_v2_i/rf_capture_guard/inst/src_request_r_reg}] -to [get_cells {receiver_v2_i/rf_capture_guard/inst/src_request_sync_reg[0]}]
set_max_delay -datapath_only 20.000 -from [get_cells {receiver_v2_i/rf_capture_guard/inst/src_fresh_r_reg}] -to [get_cells {receiver_v2_i/rf_capture_guard/inst/src_fresh_sync_reg[0]}]
