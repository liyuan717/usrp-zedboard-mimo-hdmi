# Read-only propagation audit of exact ADI control/status snapshot crossings.
# No waiver/exception edits. A synthesis estimate is not routed signoff.
# Usage: vivado -mode batch -source <this> -tclargs <finished.xpr|routed.dcp> <fresh-out>
if {$argc!=2} {error "Expected design and fresh output directory"}
set input [file normalize [lindex $argv 0]]
set out [file normalize [lindex $argv 1]]
if {[file exists $out]} {error "Refusing existing evidence directory"}
file mkdir $out
file copy [info script] [file join $out runner.tcl]
set_param general.maxThreads 2
if {[file extension $input] eq ".xpr"} {
  open_project $input
  if {[get_property PROGRESS [get_runs synth_1]] ne "100%"} {error "Synthesis not complete"}
  set checkpoint [file join [get_property DIRECTORY [get_runs synth_1]] receiver_v2_wrapper.dcp]
  open_run synth_1
} else {
  set checkpoint $input
  open_checkpoint $checkpoint
}
set fd [open [file join $out inputs.sha256] w]
foreach path [list $input $checkpoint [info script]] {puts $fd "$path\n[exec certutil.exe -hashfile $path SHA256]"}
close $fd
update_timing
if {[llength [get_clocks]]<3} {error "Incomplete clock constraints"}
set cdc [report_cdc -details -no_waiver -return_string]
set fd [open [file join $out cdc.rpt] w];puts $fd $cdc;close $fd
set fd [open [file join $out bundled_paths.tsv] w]
puts $fd "source\ttarget\tclock\tperiod_ns\tdatapath_delay_ns\tbound_met\tuser_ignored"
set count 0
set failures 0
foreach line [split $cdc \n] {
  if {![regexp {^\s*\d+\s+CDC-(15|17)\s+Warning\s+} $line]} {continue}
  set from [lindex $line end-1];set to [lindex $line end]
  if {![regexp {^(.*)/(up_xfer_data|d_xfer_data)_reg(\[[0-9]+\])?/C$} $from unused prefix kind bit]} {continue}
  set target_kind [expr {$kind eq "up_xfer_data" ? "d_data_cntrl_int" : "up_data_status_int"}]
  set expected [format {%s/%s_reg%s/D} $prefix $target_kind $bit]
  if {$to ne $expected} {error "Unexpected ADI bundled target $from -> $to"}
  set source [get_cells -of_objects [get_pins $from]]
  set target [get_pins $to]
  if {[llength $source]!=1||[llength $target]!=1} {error "Missing exact bundled endpoint"}
  set paths [get_timing_paths -quiet -from $source -to $target -max_paths 1]
  set ignored 0
  if {[llength $paths]==0} {
    set paths [get_timing_paths -quiet -user_ignored -from $source -to $target -max_paths 1]
    set ignored 1
  }
  if {[llength $paths]!=1} {error "No measurable propagation path: $from -> $to"}
  set delay [get_property DATAPATH_DELAY $paths]
  set clock [get_property ENDPOINT_CLOCK $paths]
  if {[llength [get_clocks -quiet $clock]]!=1} {error "Unknown destination clock $clock"}
  set period [get_property PERIOD [get_clocks $clock]]
  if {![string is double -strict $delay]||![string is double -strict $period]||$period<=0} {error "Non-numeric propagation evidence"}
  # The registered snapshot and request launch together. The actual ADI
  # m1/m2/m3 edge detector captures no earlier than two destination periods
  # after launch. Requiring <=one period leaves another period for setup/skew;
  # this is a conservative audit bound, NOT an invented synchronous slack.
  set met [expr {$delay<=$period}]
  if {!$met} {incr failures}
  puts $fd "$from\t$to\t$clock\t$period\t$delay\t$met\t$ignored"
  incr count
}
close $fd
if {$count==0} {error "No reviewed-kind bundled crossings found"}
puts "ADI_BUNDLED_PROPAGATION_OBSERVED paths=$count exceeded_one_period=$failures (no waivers; not full CDC acceptance)"
if {$failures} {error "Bundled propagation audit bound failed"}
