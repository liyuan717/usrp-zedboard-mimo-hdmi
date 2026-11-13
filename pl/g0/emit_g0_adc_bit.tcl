# Separate from route so the actual routed CDC/timing/IO reports are reviewed
# before producing an explicitly diagnostic bitstream. Not V3 signoff.
if {[llength $argv]!=1} {error "usage: -tclargs prepared-platform-directory"}
set platform [file normalize [lindex $argv 0]]
set out [file join $platform project]
set_param general.maxThreads 2
open_checkpoint [file join $out g0_adc_review_routed.dcp]
foreach type {max min} {
 set p [get_timing_paths -quiet -delay_type $type -max_paths 1]
 if {[llength $p]!=1 || [get_property SLACK $p]<0} {error "negative/missing routed $type path"}
}
set severe [get_drc_violations -quiet -filter {SEVERITY == Error || SEVERITY == {Critical Warning}}]
if {[llength $severe]} {error "severe routed DRC: $severe"}
check_timing -override_defaults {no_clock unconstrained_internal_endpoints no_input_delay no_output_delay} -verbose -file [file join $out io_timing_inventory.rpt]
write_bitstream -force [file join $out g0_adc.bit]
puts "G0_ADC_DIAGNOSTIC_BIT_GENERATED_NOT_FULL_V3_ACCEPTANCE"
