# Separate from structural checks: prove implementation-only exact exceptions
# were actually applied. Does NOT create or broaden any timing exception.
namespace eval receiver_v2_release_effect {
  proc coverage_row {text through_count sink_count} {
    set rows {}
    foreach line [split $text "\n"] {
      set columns [regexp -all -inline {\S+} $line]
      if {[llength $columns] && [string is integer -strict [lindex $columns 0]]} {
        lappend rows $columns
      }
    }
    if {[llength $rows] != 1} {error "expected exactly one applied false-path coverage row, got $rows"}
    set row [lindex $rows 0]
    # Vivado 2025.2: Position, 'False Path', Setup, Hold, (empty From),
    # Through count+'pins', To count+'pins', Endpoints, two 100% columns.
    # Both false flags mean an unqualified set_false_path (both checks).
    set expected [list False Path false false $through_count pins $sink_count pins $sink_count 100.00 100.00]
    if {[lrange $row 1 end] ne $expected} {error "unexpected assertion exception coverage: $row expected $expected"}
    return $row
  }

  proc resolve {names} {
    set result {}
    foreach name $names {
      set pin [receiver_v2_capture_contract::require_one [get_pins -quiet -hier -filter [format {NAME == "%s"} $name]] "assertion effect pin $name"]
      lappend result $pin
    }
    if {[llength [lsort -unique $result]] != [llength $names]} {error "duplicate resolved assertion pins"}
    return $result
  }

  proc check_set {label sources sinks} {
    set coverage [report_exceptions -through $sources -to $sinks -coverage -no_header -return_string]
    set row [coverage_row $coverage [llength $sources] [llength $sinks]]
    set arcs {}
    foreach source $sources {
      foreach sink $sinks {
        # Ordinary get_timing_paths also returns unconstrained/infinite paths;
        # explicitly exclude those here. -user_ignored is the installed tool's
        # query for false-path/clock-group-excluded paths, NOT a waiver.
        set timed [get_timing_paths -quiet -no_report_unconstrained -through $source -to $sink -max_paths 1]
        set ignored [get_timing_paths -quiet -user_ignored -through $source -to $sink -max_paths 1]
        if {[llength $timed] || [llength $ignored] != 1} {error "assertion exception ineffective $source -> $sink"}
        if {[get_property ENDPOINT_PIN $ignored] ne $sink} {error "assertion exception endpoint substitution $sink"}
        lappend arcs [list $source $sink]
        puts "RELEASE_ASSERTION_EFFECT_ARC label=$label through=$source to=$sink user_ignored=1 normally_timed=0"
      }
    }
    return [dict create sources $sources sinks $sinks coverage $coverage coverage_row $row verified_arcs $arcs]
  }

  proc validate {{hdmi_enabled 1}} {
    set sources [resolve {
      {receiver_v2_i/reset_rf/U0/PR_OUT_DFF[0].FDRE_PER/Q}
      {receiver_v2_i/reset_rf_lclk/U0/PR_OUT_DFF[0].FDRE_PER/Q}
      receiver_v2_i/rf_sampling_clock/inst/mmcm_adv_inst/LOCKED
      receiver_v2_i/delay_clock/inst/mmcm_adv_inst/LOCKED
      receiver_v2_i/rf_lclk_monitor/inst/ready_reg/Q
    }]
    set names {}
    foreach domain {din dout} {
      foreach bit {0 1} {lappend names [format {receiver_v2_i/rf_capture_guard/inst/%s_release_sync_reg[%d]/CLR} $domain $bit]}
    }
    set result [dict create rf [check_set rf $sources [resolve $names]]]
    if {$hdmi_enabled} {
      dict set result hdmi [validate_hdmi]
    }
    puts "RELEASE_ASSERTION_EFFECT_PASS rf_arcs=20 hdmi_enabled=$hdmi_enabled"
    return $result
  }

  proc validate_hdmi {} {
    set base receiver_v2_i/axi_hdmi_core/inst/i_up
    set names {}
    foreach helper {i_core_rst_reg i_vdma_rst_reg} {
      foreach stage {release_m1_reg release_m2_reg rst_reg} {lappend names $base/$helper/$stage/PRE}
    }
    return [check_set hdmi [resolve [list $base/up_core_preset_reg/Q]] [resolve $names]]
  }
}
