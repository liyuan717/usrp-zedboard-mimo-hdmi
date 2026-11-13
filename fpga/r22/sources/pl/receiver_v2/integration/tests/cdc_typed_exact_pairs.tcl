# Read-only exact-pair physical observation for a frozen typed CDC inventory.
# Usage: vivado -mode batch -source cdc_typed_exact_pairs.tcl -tclargs <dcp> <pairs.tsv> <fresh-out>
if {$argc != 3} { error "usage: cdc_typed_exact_pairs.tcl <dcp> <pairs.tsv> <fresh-out>" }
set dcp [file normalize [lindex $argv 0]]
set pairs_file [file normalize [lindex $argv 1]]
set out [file normalize [lindex $argv 2]]
if {![file isfile $dcp] || ![file isfile $pairs_file]} { error "missing DCP or pair inventory" }
if {[file exists $out]} { error "refusing existing output directory" }
file mkdir $out
file copy [info script] [file join $out cdc_typed_exact_pairs.tcl]
set_param general.maxThreads 2
proc finite {value label} {
  if {![string is double -strict $value] || [regexp -nocase {nan|inf} $value]} { error "nonfinite $label: $value" }
  return $value
}
proc exact_pin {name} {
  # Resolve the full cell name first; this avoids treating [bit] as a Tcl or
  # glob pattern in a direct pin query.  REF_PIN_NAME is exact on that cell.
  if {![regexp {^(.*)/([^/]+)$} $name unused cell_name ref_pin]} {
    error "malformed pin name $name"
  }
  set cell [get_cells -hier -quiet -filter "NAME == $cell_name"]
  if {[llength $cell] != 1} { error "expected exactly one cell for pin $name, got $cell" }
  set pins [get_pins -quiet -of_objects $cell -filter "REF_PIN_NAME == $ref_pin"]
  if {[llength $pins] != 1} { error "expected exactly one pin $name, got $pins" }
  return [lindex $pins 0]
}
proc exact_cell {name} {
  set cells [get_cells -hier -quiet -filter "NAME == $name"]
  if {[llength $cells] != 1} { error "expected exactly one cell $name, got $cells" }
  return [lindex $cells 0]
}
proc require_async_reg {cell label} {
  set raw [get_property ASYNC_REG $cell]
  set normalized [string tolower [string trim $raw]]
  # Vivado can serialize boolean properties as 1 rather than TRUE.
  if {$normalized ni {1 true yes}} {
    error "$label lacks asserted ASYNC_REG: raw='$raw' normalized='$normalized' cell=$cell"
  }
  return $raw
}
proc observed_delay {from to kind} {
  set path [get_timing_paths -quiet -delay_type $kind -from $from -to $to -max_paths 1]
  set ignored 0
  if {[llength $path] == 0} {
    set path [get_timing_paths -quiet -user_ignored -delay_type $kind -from $from -to $to -max_paths 1]
    set ignored 1
  }
  if {[llength $path] != 1} { error "no measurable $kind path $from -> $to" }
  return [list [finite [get_property DATAPATH_DELAY $path] "$kind delay"] $ignored [get_property ENDPOINT_CLOCK $path]]
}
proc sync_stage {kind destination bit sfh} {
  if {$kind eq "wfifo_enable"} {
    set m1_name [string map {dout_enable_m1_reg dout_enable_m1_reg} [file dirname $destination]]
    set m2_name [string map {dout_enable_m1_reg dout_enable_reg} [file dirname $destination]]
  } elseif {$kind eq "hdmi_gray"} {
    set m1_name [file dirname $destination]
    set m2_name [string map {vdma_raddr_g_m1_reg vdma_raddr_g_m2_reg} $m1_name]
  } else { error "sync_stage called for unsupported kind $kind" }
  # destination is a pin path; reconstruct exact register cell names preserving bit.
  regsub {/D$} $destination {} m1_name
  if {$kind eq "wfifo_enable"} {
    regsub {dout_enable_m1_reg} $m1_name {dout_enable_reg} m2_name
  } else {
    regsub {vdma_raddr_g_m1_reg} $m1_name {vdma_raddr_g_m2_reg} m2_name
  }
  set m1 [exact_cell $m1_name]
  set m2 [exact_cell $m2_name]
  set m1_async [require_async_reg $m1 "$kind bit=$bit m1"]
  set m2_async [require_async_reg $m2 "$kind bit=$bit m2"]
  set q [exact_pin "$m1_name/Q"]
  set d [exact_pin "$m2_name/D"]
  set nets [get_nets -quiet -of_objects $q]
  if {[llength $nets] != 1} { error "$kind bit=$bit has nonunique m1 Q net" }
  set sinks [get_pins -quiet -of_objects [lindex $nets 0] -filter {DIRECTION == IN}]
  if {[llength $sinks] != 1 || [lindex $sinks 0] ne $d} { error "$kind bit=$bit m1 Q is not sole fanout to m2 D: $sinks" }
  set normal [get_timing_paths -quiet -from $q -to $d -max_paths 1]
  if {[llength $normal] != 1} { error "$kind bit=$bit m1->m2 lacks normal timed path (no user_ignored fallback allowed)" }
  set slack [finite [get_property SLACK $normal] "$kind m1->m2 slack"]
  if {$slack < 0} { error "$kind bit=$bit m1->m2 has negative normal slack $slack" }
  set normal_delay [finite [get_property DATAPATH_DELAY $normal] "$kind m1->m2 delay"]
  puts $sfh "$kind\t$bit\t$m1_name\t$m2_name\t$normal_delay\t$slack\t[get_property ENDPOINT_CLOCK $normal]\t$m1_async\t$m2_async\tSOLE_FANOUT"
  flush $sfh
}

open_checkpoint $dcp
# The routed checkpoint restores timing data.  Do not force a full global
# `update_timing` here: on this diagnostic DCP it can exhaust the routed
# timing cache before the exact path queries begin.  Each query below asks
# Vivado for its own named path and is independently checked.
set pfd [open $pairs_file r]
gets $pfd header
if {$header ne "kind\tcdc_id\tsource_clock\tdestination_clock\tsource\tdestination\tsource_parent\tdestination_parent\tbit"} { error "unexpected frozen pair TSV header" }
set dfd [open [file join $out exact_pairs.tsv] w]
puts $dfd "kind\tcdc_id\tsource_clock_report\tdestination_clock_report\tsource\tdestination\tbit\tmin_ns\tmax_ns\tmin_user_ignored\tmax_user_ignored\tendpoint_clock"
set sfd [open [file join $out synchronizer_m1_m2.tsv] w]
puts $sfd "kind\tbit\tm1\tm2\tnormal_datapath_ns\tnormal_slack_ns\tendpoint_clock\tm1_async_reg_raw\tm2_async_reg_raw\tsole_fanout"
set ufd [open [file join $out unmeasured_handshake_sync.tsv] w]
puts $ufd "mechanism\tstatus\treason"
foreach mechanism {control_request_ack observer_request_ack status_request_ack wfifo_rinit_toggle_payload} {
  puts $ufd "$mechanism\tUNMEASURED\tnot_an_exact_two-flop-payload-pair measurement in this observation"
}
set count 0
while {[gets $pfd line] >= 0} {
  if {$line eq ""} {continue}
  set f [split $line "\t"]
  if {[llength $f] != 9} { error "malformed pair line: $line" }
  lassign $f kind cdc_id src_clk dst_clk source destination src_parent dst_parent bit
  puts "CDC_TYPED_PAIR_BEGIN index=$count kind=$kind bit=$bit"
  flush stdout
  set from [exact_pin $source]
  set to [exact_pin $destination]
  puts "CDC_TYPED_PAIR_ENDPOINTS index=$count"
  flush stdout
  lassign [observed_delay $from $to min] lo lo_ignored endpoint_clock
  puts "CDC_TYPED_PAIR_MIN index=$count"
  flush stdout
  lassign [observed_delay $from $to max] hi hi_ignored endpoint_clock_max
  puts "CDC_TYPED_PAIR_MAX index=$count"
  flush stdout
  if {$endpoint_clock ne $endpoint_clock_max} {error "min/max endpoint clocks differ for $source -> $destination"}
  puts $dfd "$kind\t$cdc_id\t$src_clk\t$dst_clk\t$source\t$destination\t$bit\t$lo\t$hi\t$lo_ignored\t$hi_ignored\t$endpoint_clock"
  flush $dfd
  if {$kind eq "wfifo_enable" || $kind eq "hdmi_gray"} { sync_stage $kind $destination $bit $sfd }
  incr count
}
close $pfd; close $dfd; close $sfd; close $ufd
if {$count == 0} {error "no exact pairs measured"}
# Preserve native bus-skew diagnostic output.  Its result is intentionally
# not converted into a timing waiver or a CDC pass.
report_bus_skew -file [file join $out native_bus_skew.rpt]
set status [open [file join $out status.txt] w]
puts $status "scope=PHYSICAL_OBSERVATION_ONLY"
puts $status "release_ready=false"
puts $status "final_audit_required=true"
puts $status "exact_pairs=$count"
puts $status "native_bus_skew=REPORTED_SEPARATELY_NOT_USED_AS_CDC_PASS"
puts $status "handshake_syncers=UNMEASURED_EXCEPT_TYPED_WFIFO_ENABLE_AND_GRAY_M1_M2"
close $status
puts "CDC_TYPED_EXACT_PAIR_OBSERVATION_COMPLETE pairs=$count (not CDC signoff)"
close_design
