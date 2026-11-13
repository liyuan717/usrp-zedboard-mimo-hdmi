# Separate from route so the actual routed CDC/timing/IO reports are reviewed
# before producing an explicitly diagnostic bitstream. Not V3 signoff.
if {[llength $argv] ni {1 2}} {error "usage: -tclargs prepared-platform-directory ?threads?"}
lassign $argv directory threads
if {$threads eq ""} {set threads 2}
if {![string is integer -strict $threads] || $threads<1 || $threads>8} {error "invalid bit generation concurrency"}
set platform [file normalize $directory]
set out [file join $platform project]
set_param general.maxThreads $threads
open_checkpoint [file join $out g0_source_review_routed.dcp]
foreach type {max min} {
 set p [get_timing_paths -quiet -delay_type $type -max_paths 1]
 if {[llength $p]!=1 || [get_property SLACK $p]<0} {error "negative/missing routed $type path"}
}
set severe [get_drc_violations -quiet -filter {SEVERITY == Error || SEVERITY == {Critical Warning}}]
if {[llength $severe]} {error "severe routed DRC: $severe"}
check_timing -override_defaults {no_clock unconstrained_internal_endpoints no_input_delay no_output_delay} -verbose -file [file join $out io_timing_inventory.rpt]
write_bitstream -force [file join $out g0_source.bit]
puts "G0_SOURCE_CANDIDATE_BIT_GENERATED_NOT_FULL_V3_ACCEPTANCE"
