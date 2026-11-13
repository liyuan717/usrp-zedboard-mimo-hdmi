# Read-only inspection of the 13 explicitly reviewed 2FF synchronizer stages.
# Usage: vivado -mode batch -source cdc_typed_sync_stages.tcl -tclargs <routed.dcp> <fresh-out>
if {$argc != 2} { error "usage: cdc_typed_sync_stages.tcl <routed.dcp> <fresh-out>" }
set dcp [file normalize [lindex $argv 0]]
set out [file normalize [lindex $argv 1]]
if {![file isfile $dcp]} { error "missing DCP: $dcp" }
if {[file exists $out]} { error "refusing existing output directory: $out" }
file mkdir $out
file copy [info script] [file join $out cdc_typed_sync_stages.tcl]
set_param general.maxThreads 2

proc exact_cell {name} {
  set cells [get_cells -hier -quiet -filter "NAME == $name"]
  if {[llength $cells] != 1} { error "expected one cell '$name', got '$cells'" }
  return [lindex $cells 0]
}
proc pin_of {cell ref_pin} {
  set pins [get_pins -quiet -of_objects $cell -filter "REF_PIN_NAME == $ref_pin"]
  if {[llength $pins] != 1} { error "expected one $ref_pin pin of $cell, got '$pins'" }
  return [lindex $pins 0]
}
proc asserted_async {cell label} {
  set raw [get_property ASYNC_REG $cell]
  set normalized [string tolower [string trim $raw]]
  if {$normalized ni {1 true yes}} {
    error "$label ASYNC_REG not asserted: raw='$raw' normalized='$normalized' cell=$cell"
  }
  return $raw
}
proc finite_nonnegative {value label} {
  if {![string is double -strict $value] || [regexp -nocase {nan|inf} $value] || $value < 0} {
    error "$label is not finite/nonnegative: '$value'"
  }
  return $value
}
proc observe_pair {kind bit m1_name m2_name handle} {
  set m1 [exact_cell $m1_name]
  set m2 [exact_cell $m2_name]
  set a1 [asserted_async $m1 "$kind bit=$bit m1"]
  set a2 [asserted_async $m2 "$kind bit=$bit m2"]
  set q [pin_of $m1 Q]
  set d [pin_of $m2 D]
  set nets [get_nets -quiet -of_objects $q]
  if {[llength $nets] != 1} { error "$kind bit=$bit m1 Q has nonunique net '$nets'" }
  set sinks [get_pins -quiet -of_objects [lindex $nets 0] -filter {DIRECTION == IN}]
  if {[llength $sinks] != 1 || [lindex $sinks 0] ne $d} {
    error "$kind bit=$bit m1 Q fanout is not sole m2 D: '$sinks' expected '$d'"
  }
  # No user_ignored fallback: this proves a normal timed m1 -> m2 path.
  set timing [get_timing_paths -quiet -from $q -to $d -max_paths 1]
  if {[llength $timing] != 1} { error "$kind bit=$bit has no normal timed m1->m2 path" }
  set delay [finite_nonnegative [get_property DATAPATH_DELAY $timing] "$kind bit=$bit datapath"]
  set slack [finite_nonnegative [get_property SLACK $timing] "$kind bit=$bit slack"]
  set clk [get_property ENDPOINT_CLOCK $timing]
  if {$clk eq ""} { error "$kind bit=$bit missing endpoint clock" }
  puts $handle "$kind\t$bit\t$m1_name\t$m2_name\t$a1\t$a2\t$delay\t$slack\t$clk\tSOLE_FANOUT"
  flush $handle
  puts "CDC_TYPED_SYNC_STAGE_OK kind=$kind bit=$bit"
  flush stdout
}

open_checkpoint $dcp
set report [open [file join $out synchronizer_stages.tsv] w]
puts $report "kind\tbit\tm1\tm2\tm1_async_reg_raw\tm2_async_reg_raw\tnormal_datapath_ns\tnormal_slack_ns\tendpoint_clock\tfanout"
for {set bit 0} {$bit < 4} {incr bit} {
  observe_pair wfifo_enable $bit \
    "receiver_v2_i/rf_rx_fifo/inst/dout_enable_m1_reg\[$bit\]" \
    "receiver_v2_i/rf_rx_fifo/inst/dout_enable_reg\[$bit\]" $report
}
for {set bit 0} {$bit < 9} {incr bit} {
  observe_pair hdmi_gray $bit \
    "receiver_v2_i/axi_hdmi_core/inst/i_vdma/vdma_raddr_g_m1_reg\[$bit\]" \
    "receiver_v2_i/axi_hdmi_core/inst/i_vdma/vdma_raddr_g_m2_reg\[$bit\]" $report
}
 # Toggle request history may continue m2 -> m3, but this observation only
 # asserts the actual m1 -> m2 synchronizer pair and its sole fanout.
observe_pair wfifo_request_toggle 0 \
  "receiver_v2_i/rf_rx_fifo/inst/dout_req_t_m1_reg" \
  "receiver_v2_i/rf_rx_fifo/inst/dout_req_t_m2_reg" $report
# The return overflow indication is synchronized into the din clock domain.
observe_pair wfifo_overflow_return 0 \
  "receiver_v2_i/rf_rx_fifo/inst/din_ovf_m1_reg" \
  "receiver_v2_i/rf_rx_fifo/inst/din_ovf_reg" $report
close $report
set status [open [file join $out status.txt] w]
puts $status "scope=PHYSICAL_OBSERVATION_ONLY"
puts $status "release_ready=false"
puts $status "final_audit_required=true"
puts $status "synchronizer_pairs=15"
puts $status "routed_diagnostic_only=true"
close $status
puts "CDC_TYPED_SYNC_STAGES_COMPLETE pairs=15 (not CDC signoff)"
exit
