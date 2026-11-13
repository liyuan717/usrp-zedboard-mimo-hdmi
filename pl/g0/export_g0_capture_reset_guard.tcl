lassign $argv threads checkpoint
set_param general.maxThreads $threads
open_checkpoint $checkpoint
set root receiver_v2_i/rf_capture_guard/inst
set core [get_cells $root]
if {[llength $core] != 1} {error "Missing R17 capture guard hierarchy"}
foreach domain {din dout} {
    set first [get_pins [format {%s/%s_release_sync_reg[0]/Q} $root $domain]]
    set second [get_pins [format {%s/%s_release_sync_reg[1]/D} $root $domain]]
    set p [get_timing_paths -from $first -to $second -max_paths 1]
    puts "RELEASE $domain SETUP_NS=[get_property SLACK $p] DATA_NS=[get_property DATAPATH_DELAY $p]"
    set h [get_timing_paths -from $first -to $second -delay_type min -max_paths 1]
    puts "RELEASE $domain HOLD_NS=[get_property SLACK $h]"
}
write_verilog -cell $core -mode funcsim -rename_top capture_guard_routed capture-routed.v
foreach corner {slow fast} {
    write_sdf -cell $core -process_corner $corner -rename_top capture_guard_routed -mode timesim -multithread $threads reset-$corner.sdf
}
write_verilog -cell $core -mode timesim -rename_top capture_guard_routed capture-timing.v
close_design
puts R17_CAPTURE_GUARD_ROUTED_EXPORT_COMPLETE
