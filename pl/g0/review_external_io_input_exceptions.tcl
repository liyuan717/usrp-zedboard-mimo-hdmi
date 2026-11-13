# Exercise exact async input exceptions on an open routed checkpoint in memory.
# No updated checkpoint, XDC, or bitstream is written.
lassign $argv threads checkpoint contract
set_param general.maxThreads $threads
namespace eval ::receiver_v2_external_io_contract {variable library_only 1}
source $contract
open_checkpoint $checkpoint

proc audit_interstage {from to} {
  set slacks {}
  foreach delay_type {max min} {
    set paths [get_timing_paths -quiet -from $from -to $to -delay_type $delay_type -max_paths 1]
    if {[llength $paths] != 1} {error "Missing $delay_type timing path $from -> $to"}
    set slack [get_property SLACK $paths]
    if {![string is double -strict $slack] || !($slack >= 0 && $slack < Inf)} {
      error "Invalid $delay_type slack for $from -> $to: $slack"
    }
    lappend slacks $slack
  }
  return $slacks
}

set before [check_timing -verbose -return_string]
set out [open check-before.rpt w]
puts -nonewline $out $before
close $out
set topology [open topology.rpt w]
set ports [::receiver_v2_external_io_contract::validate_open_topology $topology $before]
close $topology
set pairs {}
set margins {}
set report [open paths.tsv w]
puts $report "interface\tport\tstage\tstart_pin\tend_pin\tsetup_ns\thold_ns"
foreach name [dict get $ports input] {
  set port [get_ports $name]
  set pin [all_fanout -from $port -flat -endpoints_only]
  set first [get_cells -of_objects $pin]
  if {[llength $pin] != 1 || $pin ne "$first/D"} {error "$name does not reach exactly the first D pin"}
  lappend pairs [list $port $pin]
  set previous $first
  foreach stage {1 2 3} {
    regsub {syncstages_ff_reg\[0\]} $first [format {syncstages_ff_reg[%d]} $stage] current
    set from [get_pins $previous/Q]
    set to [get_pins $current/D]
    set slack [audit_interstage $from $to]
    lappend margins [list $from $to $slack]
    puts $report [join [list external $name $stage $from $to {*}$slack] "\t"]
    set previous $current
  }
}
set reset_root receiver_v2_i/rf_capture_guard/inst
foreach domain {din dout} {
  set from [get_pins [format {%s/%s_release_sync_reg[0]/Q} $reset_root $domain]]
  set to [get_pins [format {%s/%s_release_sync_reg[1]/D} $reset_root $domain]]
  set slack [audit_interstage $from $to]
  lappend margins [list $from $to $slack]
  puts $report [join [list reset $domain 1 $from $to {*}$slack] "\t"]
}
close $report
foreach pair $pairs {
  lassign $pair port pin
  set_false_path -from $port -to $pin
}
set after [check_timing -verbose -return_string]
set out [open check-after.rpt w]
puts -nonewline $out $after
close $out
foreach pattern {
  {There are 0 input ports with no input delay specified\.}
  {There are 12 input ports with no input delay but user has a false path constraint\.}
} {
  if {![regexp $pattern $after]} {error "Unexpected input exception classification: $pattern"}
}
if {![regexp {There are 12 input ports with no input delay but user has a false path constraint\. \(MEDIUM\)\n\n([^\n]+(?:\n[^\n]+)*)\n\n} $after -> exception_rows]} {
  error "Missing exact exception port list"
}
::receiver_v2_external_io_contract::require_exact_set input_false_path_ports [split $exception_rows "\n"] [dict get $ports input]
set output_heading {6. checking no_output_delay (16)}
if {[string range $before [string last $output_heading $before] end] ne
    [string range $after [string last $output_heading $after] end]} {
  error "External output or later timing classifications changed"
}
set out [open input-exceptions.rpt w]
foreach pair $pairs {
  lassign $pair port pin
  set report [report_exceptions -from $port -to $pin -return_string]
  puts $out "$port -> $pin\n$report"
  flush $out
  set active [regexp -all -line {^[ \t]*[0-9]+[ \t]+.*[ \t]+false[ \t]+false[ \t]*$} $report]
  if {$active != 1} {
    error "Missing active false path $port -> $pin. See input-exceptions.rpt"
  }
}
close $out
foreach entry $margins {
  lassign $entry from to expected
  if {[audit_interstage $from $to] ne $expected} {error "Interstage timing changed $from -> $to"}
}
puts "EXACT_ASYNC_INPUT_EXCEPTION_TRIAL_PASS ports=[llength $pairs] timed_pairs=[llength $margins]"
close_design
