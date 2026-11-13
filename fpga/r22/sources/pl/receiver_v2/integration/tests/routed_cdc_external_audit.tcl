# Read-only fine-grained routed audit.  It creates no timing exception, CDC
# waiver, or checkpoint.  A caller owns receipt policy and turns any recorded
# FAIL into a nonzero exit status after preserving all evidence.
# Usage: vivado -mode batch -source routed_cdc_external_audit.tcl -tclargs
#        <routed.dcp> <fresh-out> <integration-dir>
if {$argc != 3} { error "usage: routed_cdc_external_audit.tcl <routed.dcp> <fresh-out> <integration-dir>" }
set dcp [file normalize [lindex $argv 0]]
set out [file normalize [lindex $argv 1]]
set integration [file normalize [lindex $argv 2]]
if {![file isfile $dcp]} { error "missing DCP: $dcp" }
if {![file isdirectory $integration]} { error "missing integration directory: $integration" }
if {[file exists $out]} { error "refusing existing output directory: $out" }
file mkdir $out
file copy [info script] [file join $out routed_cdc_external_audit.tcl]

set ledger [open [file join $out audit_ledger.tsv] w]
puts $ledger "check\tresult\tdetail"
proc audit {ledger name script} {
  if {[catch {uplevel #0 $script} message]} {
    puts $ledger "$name\tFAIL\t[string map [list "\n" { } "\t" { }] $message]"
    flush $ledger
    puts "ROUTED_CDC_EXTERNAL_AUDIT_FAIL check=$name detail=$message"
    return 0
  }
  puts $ledger "$name\tPASS\tverified"
  flush $ledger
  puts "ROUTED_CDC_EXTERNAL_AUDIT_PASS check=$name"
  return 1
}

open_checkpoint $dcp
report_cdc -details -no_waiver -file [file join $out implementation_cdc.rpt]
report_timing_summary -delay_type min_max -report_unconstrained -max_paths 20 \
  -file [file join $out timing_summary.rpt]
report_drc -file [file join $out drc.rpt]
set timing_text [check_timing -verbose -return_string]
set timing_fh [open [file join $out check_timing_verbose.rpt] w]
puts -nonewline $timing_fh $timing_text
close $timing_fh

source [file join $integration capture_reset_guard_contract.tcl]
source [file join $integration release_assertion_contract.tcl]
source [file join $integration release_assertion_effect.tcl]
source [file join $integration hdmi_epoch_cdc_contract.tcl]
audit $ledger capture_reset_guard_post_route {receiver_v2_capture_contract::validate post_route}
audit $ledger release_assertion_topology {receiver_v2_release_assertion::validate post_route 1}
audit $ledger release_assertion_effect {
  set effects [receiver_v2_release_effect::validate 1]
  set fh [open [file join $out assertion_effects.tcldict] w]
  puts $fh $effects
  close $fh
}
audit $ledger hdmi_epoch_ready_cdc {receiver_v2_hdmi_epoch_cdc::validate_ready}
audit $ledger hdmi_epoch_ack_cdc {receiver_v2_hdmi_epoch_cdc::validate_ack}

# The input/output classification is checked here from the same checkpoint.
# The full external topology proof below reopens the DCP only after this design
# is closed; it remains independent and has its own immutable input hashes.
namespace eval ::receiver_v2_external_io_contract { variable library_only 1 }
source [file join $integration receiver_v2_external_io_contract.tcl]
audit $ledger external_io_portsets [list ::receiver_v2_external_io_contract::validate_check_timing_text $timing_text]
close_design

# This verifies the 12 XPM4FF inputs, static output drivers, and three
# forwarded clocks. It never manufactures IO delays or calls an offline proof
# a live FDD/electrical test.
audit $ledger external_io_static_topology [list ::receiver_v2_external_io_contract::run_contract $dcp [file join $out external_io] 0]
close $ledger
puts "ROUTED_CDC_EXTERNAL_AUDIT_COMPLETE evidence=$out"
