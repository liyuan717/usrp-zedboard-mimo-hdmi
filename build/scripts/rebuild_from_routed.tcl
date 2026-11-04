# Offline only; usage: vivado -mode batch -source rebuild_from_routed.tcl -tclargs <dcp> <new-output-dir>
if {$argc != 2} {error "expected dcp and fresh output directory"}
set dcp [file normalize [lindex $argv 0]]
set out [file normalize [lindex $argv 1]]
if {[file exists $out]} {error "output must be new"}
file mkdir $out
set_param general.maxThreads 2
open_checkpoint $dcp
report_utilization -file [file join $out utilization.rpt]
report_timing_summary -file [file join $out timing.rpt]
write_bitstream [file join $out regenerated.bit]
close_design
