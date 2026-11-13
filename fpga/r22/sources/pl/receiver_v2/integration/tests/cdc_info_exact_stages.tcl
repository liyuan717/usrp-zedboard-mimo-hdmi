# Read-only physical observer for every explicitly listed report_cdc Info row.
#
# Usage:
#   vivado -mode batch -source cdc_info_exact_stages.tcl -tclargs \
#       <final.dcp> <exact-info.tsv> <fresh-out>
#
# TSV header (exactly nine tab-separated columns):
# cdc_id severity description depth exception source_clock destination_clock source destination
#
# The TSV is deliberately an *input inventory*, not an approval.  This script
# refuses unknown topology families rather than inferring a synchronizer from
# the report's headline depth.  In particular, report_cdc depth is retained as
# evidence but is not used to manufacture a number of synchronizer stages.

if {$argc != 3} {
  error "usage: cdc_info_exact_stages.tcl <final.dcp> <exact-info.tsv> <fresh-out>"
}

set dcp [file normalize [lindex $argv 0]]
set inventory [file normalize [lindex $argv 1]]
set out [file normalize [lindex $argv 2]]
if {![file isfile $dcp] || ![file isfile $inventory]} { error "missing DCP or exact Info TSV" }
if {[file exists $out]} { error "refusing non-fresh output directory: $out" }
file mkdir $out
file copy [info script] [file join $out cdc_info_exact_stages.tcl]
file copy $inventory [file join $out exact_info.tsv]

namespace eval ::rfv2_info_exact {
  variable expected_header "cdc_id\tseverity\tdescription\tdepth\texception\tsource_clock\tdestination_clock\tsource\tdestination"
  # HPO1 adds the reviewed HDMI-VSYNC -> FCLK0 CDC-3 row.
  variable expected_rows 68

  proc fail {message} { error "CDC_INFO_EXACT_REFUSED: $message" }
  proc one {objects label} {
    if {[llength $objects] != 1} { fail "$label: expected exactly one object, got '$objects'" }
    # Preserve Vivado's collection object; converting it through lindex can
    # turn an indexed cell into an invalid plain-string -of_objects argument.
    return $objects
  }
  proc finite_nonnegative {value label} {
    if {![string is double -strict $value] || [regexp -nocase {nan|inf} $value] ||
        [catch {expr {double($value) >= 0.0}} ok] || !$ok} {
      fail "$label is not finite/nonnegative: '$value'"
    }
    return $value
  }
  proc finite_positive {value label} {
    finite_nonnegative $value $label
    if {$value <= 0.0} { fail "$label is not positive: '$value'" }
    return $value
  }
  proc cell_by_name {name} {
    return [one [get_cells -hier -quiet -filter "NAME == $name"] "cell $name"]
  }
  proc pin_of {cell ref} {
    return [one [get_pins -quiet -of_objects $cell -filter "REF_PIN_NAME == $ref"] "pin $ref of $cell"]
  }
  proc split_pin {name} {
    if {![regexp {^(.*)/([^/]+)$} $name ignored cell ref]} { fail "malformed exact pin '$name'" }
    return [list $cell $ref]
  }
  proc sequential {cell label} {
    set value [string tolower [string trim [get_property IS_SEQUENTIAL $cell]]]
    if {$value ni {1 true yes}} { fail "$label is not sequential: $cell raw='$value'" }
  }
  proc async_reg {cell label} {
    set value [string tolower [string trim [get_property ASYNC_REG $cell]]]
    if {$value ni {1 true yes}} { fail "$label lacks ASYNC_REG: $cell raw='$value'" }
  }
  proc expected_period {clock} {
    array set periods {
      clk_fpga_0 20.000
      clk_out1_receiver_v2_rf_sampling_clock_0 62.500
      mmcm_clk_0_s 6.750
      clk_out1_receiver_v2_delay_clock_0 5.000
    }
    if {![info exists periods($clock)]} { fail "unreviewed clock '$clock'" }
    return $periods($clock)
  }
  proc assert_clock {cell clock label} {
    set actual [one [get_clocks -quiet -of_objects [pin_of $cell C]] "$label clock"]
    if {[get_property NAME $actual] ne $clock} { fail "$label clock is [get_property NAME $actual], expected $clock" }
    set period [get_property PERIOD $actual]
    finite_positive $period "$label period"
    if {[expr {abs(double($period)-double([expected_period $clock])) > 0.001}]} {
      fail "$label period $period does not match [expected_period $clock]"
    }
  }
  proc assert_source {source source_clock} {
    if {[string first / $source] < 0} {
      if {$source_clock ne "input port clock"} { fail "top port source '$source' has non-port clock '$source_clock'" }
      one [get_ports -quiet $source] "top port $source"
      return PORT
    }
    if {$source_clock eq "input port clock"} { fail "sequential source '$source' marked input-port clock" }
    lassign [split_pin $source] cell_name ref
    if {$ref ne "C"} { fail "CDC source must be sequential /C or top port: '$source'" }
    set cell [cell_by_name $cell_name]
    sequential $cell "source $source"
    assert_clock $cell $source_clock "source $source"
    return $cell
  }
  proc cell_name_from_destination {destination expected_ref} {
    lassign [split_pin $destination] cell ref
    if {$ref ne $expected_ref} { fail "destination '$destination' expected /$expected_ref" }
    return $cell
  }
  proc replace_once {value from to label} {
    if {[string first $from $value] < 0} { fail "$label lacks exact token '$from': $value" }
    return [string map [list $from $to] $value]
  }

  # Explicit, reviewed topology registry.  Return the complete named chain
  # from the report destination (m1) through its local settling stages.  This
  # has intentionally no catch-all *_m1 inference.
  proc cdc3_chain {destination} {
    set first [cell_name_from_destination $destination D]
    if {[regexp {syncstages_ff_reg\[0\](\[[0-9]+\])?$} $first]} {
      set result [list $first]
      for {set stage 1} {$stage < 4} {incr stage} {
        lappend result [replace_once $first {syncstages_ff_reg[0]} [format {syncstages_ff_reg[%d]} $stage] XPM4FF]
      }
      return $result
    }
    foreach {needle replacement tail} {
      enable_up_m1_reg enable_up_reg 0
      txnrx_up_m1_reg txnrx_up_reg 0
      up_xfer_state_m1_reg up_xfer_state_m2_reg 1
      d_xfer_state_m1_reg d_xfer_state_m2_reg 1
      d_xfer_toggle_m1_reg d_xfer_toggle_m2_reg 2
      up_xfer_toggle_m1_reg up_xfer_toggle_m2_reg 2
      d_count_run_m1_reg d_count_run_m2_reg 2
      up_count_running_m1_reg up_count_running_m2_reg 2
      dout_req_t_m1_reg dout_req_t_m2_reg 2
      din_ovf_m1_reg din_ovf_reg 0
      vdma_fs_toggle_m1_reg vdma_fs_toggle_m2_reg 0
      hdmi_epoch_ready_m1_reg hdmi_epoch_ready_m2_reg 0
      vdma_epoch_ack_m1_reg vdma_epoch_ack_m2_reg 0
      src_ack_m1_reg src_ack_m2_reg 2
      src_flush_ack_m1_reg src_flush_ack_m2_reg 2
      dst_valid_m1_reg dst_valid_m2_reg 2
      dst_flush_m1_reg dst_flush_m2_reg 2
      # HPO1 is an HDMI pixel-domain VSYNC sample synchronised into FCLK0.
      # Keep all three named settling stages in the exact physical audit;
      # only the async launch -> m1 crossing is false-pathed by its XDC.
      vsync_m1_reg vsync_m2_reg 2
    } {
      if {[string first $needle $first] >= 0} {
        set result [list $first [replace_once $first $needle $replacement CDC3]]
        if {$tail == 1} { lappend result [replace_once $first $needle [string map {_m1_reg _reg} $needle] CDC3] }
        if {$tail == 2} {
          lappend result [replace_once $first $needle [string map {_m1_reg _m3_reg} $needle] CDC3]
        }
        return $result
      }
    }
    if {[regexp {/(dst_fenced_sync_reg|src_fresh_sync_reg|src_request_sync_reg|ready_sync_reg|toggle_sync_reg)\[0\]$} $first]} {
      return [list $first [replace_once $first {[0]} {[1]} CDC3]]
    }
    fail "unknown CDC-3 destination topology '$destination'"
  }

  proc cdc9_chain {destination} {
    set first [cell_name_from_destination $destination PRE]
    if {[string first /rst_async_d1_reg $first] >= 0} {
      set d2 [replace_once $first rst_async_d1_reg rst_async_d2_reg CDC9]
      set sync [replace_once $first rst_async_d1_reg rst_sync_reg CDC9]
      set syncd [replace_once $first rst_async_d1_reg rst_sync_d_reg CDC9]
      set rst [replace_once $first rst_async_d1_reg rst_reg CDC9]
      return [list $first $d2 $sync $syncd $rst]
    }
    if {[string first /release_m1_reg $first] >= 0} {
      return [list $first [replace_once $first release_m1_reg release_m2_reg CDC9] [replace_once $first release_m1_reg rst_reg CDC9]]
    }
    fail "unknown CDC-9 reset topology '$destination'"
  }

  proc assert_direct_local {from_cell to_cell clock label} {
    set q [pin_of [cell_by_name $from_cell] Q]
    set d [pin_of [cell_by_name $to_cell] D]
    set nets [get_nets -quiet -of_objects $q]
    if {[llength $nets] != 1} { fail "$label has non-unique Q net '$nets'" }
    set sinks [get_pins -quiet -of_objects [lindex $nets 0] -filter {DIRECTION == IN}]
    if {[llength $sinks] != 1 || [lindex $sinks 0] ne $d} {
      fail "$label Q fanout must be solely $d, got '$sinks'"
    }
    set path [one [get_timing_paths -quiet -from $q -to $d -max_paths 1] "$label normal local path"]
    finite_nonnegative [get_property SLACK $path] "$label local slack"
    finite_nonnegative [get_property DATAPATH_DELAY $path] "$label local delay"
    if {[get_property ENDPOINT_CLOCK $path] ne $clock} { fail "$label endpoint clock is [get_property ENDPOINT_CLOCK $path], expected $clock" }
  }
  proc assert_normal_local {from_cell to_cell clock label} {
    set q [pin_of [cell_by_name $from_cell] Q]
    set d [pin_of [cell_by_name $to_cell] D]
    set path [one [get_timing_paths -quiet -from $q -to $d -max_paths 1] "$label normal local path"]
    finite_nonnegative [get_property SLACK $path] "$label local slack"
    finite_nonnegative [get_property DATAPATH_DELAY $path] "$label local delay"
    if {[get_property ENDPOINT_CLOCK $path] ne $clock} { fail "$label endpoint clock is [get_property ENDPOINT_CLOCK $path], expected $clock" }
  }
  proc assert_crossing_exception {source source_kind destination exception destination_clock label} {
    if {$source_kind eq "PORT"} {
      if {$exception ne "False Path"} { fail "$label top-port source must be False Path, got '$exception'" }
      return
    }
    lassign [split_pin $destination] _ dest_ref
    set from [pin_of $source_kind C]
    set dest_cell [cell_name_from_destination $destination $dest_ref]
    set to [pin_of [cell_by_name $dest_cell] $dest_ref]
    if {$exception eq "False Path"} {
      set normal [get_timing_paths -quiet -no_report_unconstrained -from $from -to $to -max_paths 1]
      if {[llength $normal]} { fail "$label claims False Path but has normal source->first-stage path" }
      set ignored [one [get_timing_paths -quiet -user_ignored -from $from -to $to -max_paths 1] "$label ignored source->first-stage path"]
      if {[get_property ENDPOINT_PIN $ignored] ne $to} { fail "$label ignored endpoint substitution" }
      return
    }
    if {$exception ne "Max Delay Datapath Only"} { fail "$label unknown exception '$exception'" }
    set path [one [get_timing_paths -quiet -from $from -to $to -max_paths 1] "$label source->first-stage path"]
    finite_nonnegative [get_property SLACK $path] "$label crossing slack"
    finite_positive [get_property DATAPATH_DELAY $path] "$label crossing delay"
    set requirement [get_property REQUIREMENT $path]
    finite_positive $requirement "$label crossing requirement"
    if {[expr {abs(double($requirement)-double([expected_period $destination_clock])) > 0.001}]} {
      fail "$label max-delay requirement $requirement != destination period [expected_period $destination_clock]"
    }
  }
  proc verify_cdc3 {row detail} {
    array set r $row
    if {$r(cdc_id) ne "CDC-3" || $r(severity) ne "Info" || $r(description) ne "1-bit synchronized with ASYNC_REG property"} { fail "$detail is not exact CDC-3 Info" }
    if {![string is integer -strict $r(depth)] || $r(depth) < 2} { fail "$detail invalid CDC-3 depth '$r(depth)'" }
    set source_kind [assert_source $r(source) $r(source_clock)]
    set chain [cdc3_chain $r(destination)]
    if {[llength $chain] < 2} { fail "$detail exact CDC-3 chain is shorter than two stages" }
    set stage_index 0
    foreach cell_name $chain {
      set cell [cell_by_name $cell_name]
      sequential $cell "$detail stage"
      # The metastability boundary is exactly m1/m2.  Later state/edge
      # history registers can legally feed local XOR/FSM consumers; do not
      # relabel them synchronizer stages merely because report_cdc's depth is
      # greater than two.
      if {$stage_index < 2} { async_reg $cell "$detail metastability stage" }
      assert_clock $cell $r(destination_clock) "$detail stage"
      incr stage_index
    }
    for {set i 1} {$i < [llength $chain]} {incr i} {
      if {$i == 1} {
        assert_direct_local [lindex $chain 0] [lindex $chain 1] $r(destination_clock) "$detail m1->m2"
      } else {
        assert_normal_local [lindex $chain [expr {$i-1}]] [lindex $chain $i] $r(destination_clock) "$detail local-stage=$i"
      }
    }
    assert_crossing_exception $r(source) $source_kind $r(destination) $r(exception) $r(destination_clock) $detail
    return [join $chain {;}]
  }
  proc verify_cdc9 {row detail} {
    array set r $row
    if {$r(cdc_id) ne "CDC-9" || $r(severity) ne "Info" || $r(description) ne "Asynchronous reset synchronized with ASYNC_REG property"} { fail "$detail is not exact CDC-9 Info" }
    if {![string is integer -strict $r(depth)] || $r(depth) ni {3 5} || $r(exception) ne "False Path"} { fail "$detail invalid CDC-9 depth/exception" }
    set source_kind [assert_source $r(source) $r(source_clock)]
    if {$source_kind eq "PORT"} { fail "$detail reset source cannot be top port" }
    set chain [cdc9_chain $r(destination)]
    if {[llength $chain] != $r(depth)} { fail "$detail reset chain length [llength $chain] != report depth $r(depth)" }
    foreach cell_name $chain {
      set cell [cell_by_name $cell_name]
      sequential $cell "$detail reset stage"
      assert_clock $cell $r(destination_clock) "$detail reset stage"
    }
    foreach cell_name [lrange $chain 0 1] { async_reg [cell_by_name $cell_name] "$detail reset metastability stage" }
    for {set i 1} {$i < [llength $chain]} {incr i} {
      if {$i == 1} {
        assert_direct_local [lindex $chain 0] [lindex $chain 1] $r(destination_clock) "$detail reset m1->m2"
      } else {
        assert_normal_local [lindex $chain [expr {$i-1}]] [lindex $chain $i] $r(destination_clock) "$detail reset local-stage=$i"
      }
    }
    assert_crossing_exception $r(source) $source_kind $r(destination) $r(exception) $r(destination_clock) $detail
    return [join $chain {;}]
  }
  proc read_rows {path} {
    variable expected_header
    variable expected_rows
    set fh [open $path r]
    if {[gets $fh header] < 0 || $header ne $expected_header} { close $fh; fail "unexpected TSV header '$header'" }
    set rows {}
    set seen {}
    set line_no 1
    while {[gets $fh line] >= 0} {
      incr line_no
      if {$line eq ""} { close $fh; fail "blank TSV row at $line_no" }
      set fields [split $line "\t"]
      if {[llength $fields] != 9} { close $fh; fail "TSV row $line_no has [llength $fields] columns, expected 9" }
      lassign $fields cdc_id severity description depth exception source_clock destination_clock source destination
      foreach {name value} [list cdc_id $cdc_id severity $severity description $description depth $depth exception $exception source_clock $source_clock destination_clock $destination_clock source $source destination $destination] {
        if {$value eq ""} { close $fh; fail "TSV row $line_no has empty $name" }
      }
      set key [join [list $cdc_id $severity $description $depth $exception $source_clock $destination_clock $source $destination] \u001f]
      if {[dict exists $seen $key]} { close $fh; fail "duplicate exact Info TSV row $line_no" }
      dict set seen $key 1
      lappend rows [list cdc_id $cdc_id severity $severity description $description depth $depth exception $exception source_clock $source_clock destination_clock $destination_clock source $source destination $destination]
    }
    close $fh
    if {[llength $rows] != $expected_rows} { fail "expected exactly $expected_rows Info rows, got [llength $rows]" }
    return $rows
  }
}

set rows [::rfv2_info_exact::read_rows $inventory]
open_checkpoint $dcp
set report [open [file join $out cdc_info_exact_stages.tsv] w]
puts $report "index\tcdc_id\tdepth_report\texception\tsource\tdestination\tstage_chain\tresult"
set index 0
foreach row $rows {
  array set r $row
  set detail "row=$index $r(source) -> $r(destination)"
  if {$r(cdc_id) eq "CDC-3"} {
    set chain [::rfv2_info_exact::verify_cdc3 $row $detail]
  } elseif {$r(cdc_id) eq "CDC-9"} {
    set chain [::rfv2_info_exact::verify_cdc9 $row $detail]
  } else {
    ::rfv2_info_exact::fail "$detail unsupported CDC-ID $r(cdc_id)"
  }
  puts $report "$index\t$r(cdc_id)\t$r(depth)\t$r(exception)\t$r(source)\t$r(destination)\t$chain\tPASS"
  flush $report
  puts "CDC_INFO_EXACT_ROW_PASS index=$index id=$r(cdc_id)"
  flush stdout
  incr index
}
close $report
close_design
set status [open [file join $out status.txt] w]
puts $status "scope=PHYSICAL_OBSERVATION_ONLY"
puts $status "release_ready=false"
puts $status "hardware_verified=false"
puts $status "exact_info_rows=$index"
puts $status "candidate_review_required=true"
close $status
puts "CDC_INFO_EXACT_STAGES_COMPLETE rows=$index physical_observation_only=1"
