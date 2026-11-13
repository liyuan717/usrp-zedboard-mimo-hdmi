# Read-only topology check. Run in a fresh evidence directory.
lassign $argv threads checkpoint contract
set_param general.maxThreads $threads
namespace eval ::receiver_v2_external_io_contract {variable library_only 1}
source $contract
open_checkpoint $checkpoint
set timing [check_timing -verbose -return_string]
set inventory [open check-timing.rpt w]
puts -nonewline $inventory $timing
close $inventory
set report [open topology.rpt w]
set result [::receiver_v2_external_io_contract::validate_open_topology $report $timing]
puts $report "CHECK_TIMING_PORTSETS_VERIFIED $result"
foreach line [::receiver_v2_external_io_contract::static_result_lines] {puts $report $line}
close $report
puts EXTERNAL_IO_STATIC_TOPOLOGY_ONLY_PASS
close_design
