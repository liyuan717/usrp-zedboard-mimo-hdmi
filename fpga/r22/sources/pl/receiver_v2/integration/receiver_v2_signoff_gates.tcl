# Vivado 2025.2 fail-closed report checks. No broad waivers or timing exceptions.
# Pure parsers below are also tested with real offline tool reports.
namespace eval receiver_v2_signoff {
  variable script_dir [file dirname [file normalize [info script]]]
  variable timing_checks {no_clock constant_clock pulse_width_clock
    unconstrained_internal_endpoints no_input_delay no_output_delay
    multiple_clock generated_clocks loops partial_input_delay
    partial_output_delay latch_loops}
}

proc receiver_v2_signoff::assert_route_stage_ready {status checkpoint_exists} {
  # A completed first route with failing timing is a legal input to the
  # following phys_opt/re-route, NOT a release pass. Incomplete/error runs
  # remain rejected. Final coverage/CDC/skew/setup/hold gates are unchanged.
  if {!$checkpoint_exists || ![regexp -nocase {^route_design Complete!?(, Failed Timing!)?$} $status]} {
    error "RX v2 route stage incomplete: status=$status checkpoint_exists=$checkpoint_exists"
  }
  return 1
}

proc receiver_v2_signoff::assert_timing_coverage {report {classified_io {}}} {
  variable timing_checks
  if {$classified_io ne {}} {
    if {![llength [info commands ::receiver_v2_external_io_contract::validate_check_timing_text]] ||
        $classified_io ne [::receiver_v2_external_io_contract::validate_check_timing_text $report]} {
      error "External IO classification is not the exact report port set"
    }
  }
  if {[string first "check_timing report" $report] < 0} {
    error "Unrecognized check_timing report"
  }
  set counts [dict create]
  foreach line [split $report \n] {
    if {[regexp {checking ([a-z_]+) \(([0-9]+)\)} $line unused name count]} {
      if {[dict exists $counts $name] && [dict get $counts $name] != $count} {
        error "Inconsistent timing check count for $name"
      }
      dict set counts $name $count
    }
  }
  foreach name $timing_checks {
    if {![dict exists $counts $name]} {error "Missing timing coverage check $name"}
    set expected 0
    # Only the caller's successful same-design physical port proof can supply
    # this classification. All other timing coverage checks remain zero.
    if {$classified_io ne {}} {
      if {[lsort [dict keys $classified_io]] ne {input output_high output_low}} {
        error "Invalid classified external IO proof shape"
      }
      if {$name eq "no_input_delay"} {set expected [llength [dict get $classified_io input]]}
      if {$name eq "no_output_delay"} {
        set expected [expr {[llength [dict get $classified_io output_high]] + [llength [dict get $classified_io output_low]]}]
      }
    }
    if {[dict get $counts $name] != $expected} {
      error "Timing coverage failed: $name=[dict get $counts $name]"
    }
  }
  return $counts
}

proc receiver_v2_signoff::assert_cdc {report} {
  if {[string first "CDC Report" $report] < 0} {error "Unrecognized CDC report"}
  set summary_rows 0
  set total 0
  foreach line [split $report \n] {
    # Actual -details summary format: CDC-1 Critical 1 <description>.
    # Detail rows start with a row number and must not be counted twice.
    if {[regexp {^[ \t]*(CDC-[0-9]+)[ \t]+([A-Za-z]+)[ \t]+([0-9]+)[ \t]+} $line unused id severity count]} {
      incr summary_rows
      incr total $count
      if {$severity ne "Info"} {error "Unreviewed CDC finding: $id $severity count=$count"}
    }
  }
  if {$summary_rows == 0 && [string first "All paths are Safely Timed." $report] < 0} {
    error "CDC report has no recognized results"
  }
  return $total
}

proc receiver_v2_signoff::assert_bram_async_controls {report} {
  # These rules are WARNING severity in Vivado, but describe possible memory
  # corruption. They are release blockers, not optional QoR advice.
  if {[string first "Report DRC" $report] < 0 ||
      ![regexp {Checks:[ \t]+REQP-1839[ \t]+REQP-1840} $report]} {
    error "Unrecognized/incomplete asynchronous BRAM-control DRC report"
  }
  set counts {}
  foreach line [split $report \n] {
    if {[regexp {^[ \t]*Checks found:[ \t]+([0-9]+)[ \t\r]*$} $line unused count]} {lappend counts $count}
  }
  if {$counts ne {0} || [regexp {REQP-18(39|40)#[0-9]+} $report]} {
    error "Unsafe asynchronous BRAM controls remain: checks=$counts"
  }
  return 0
}

proc receiver_v2_signoff::require_rf_buffer_brams {} {
  set result [dict create]
  foreach {label prefix minimum_bits} {
    elastic receiver_v2_i/rf_rx_elastic/inst/ram_i/ 65536
    wfifo receiver_v2_i/rf_rx_fifo/inst/i_mem/ 1024
  } {
    set cells [get_cells -hier -quiet -filter "NAME =~ $prefix* && REF_NAME =~ RAMB*"]
    if {![llength $cells]} {error "Required $label RAM was optimized away; DRC cannot prove its controls"}
    set bits 0
    foreach cell $cells {
      switch -- [get_property REF_NAME $cell] {
        RAMB36E1 {incr bits 36864}
        RAMB18E1 {incr bits 18432}
        default {error "Unexpected $label BRAM primitive $cell"}
      }
    }
    if {$bits < $minimum_bits} {error "$label BRAM capacity below physical profile: $bits bits"}
    dict set result $label [dict create cells $cells physical_bits $bits]
  }
  return $result
}

# Bus-skew is a separate native timing analysis: nonnegative setup/hold WNS
# alone does not certify either of these asynchronous held/Gray bundles.
proc receiver_v2_signoff::assert_hdmi_bus_skew {report} {
  if {[string first "Bus Skew Report" $report] < 0} {error "Unrecognized bus-skew report"}
  set requirements {}
  set endpoints {}
  set slacks {}
  foreach line [split $report \n] {
    if {[regexp {^Requirement: ([0-9.]+)ns[ \t\r]*$} $line unused bound]} {
      lappend requirements $bound
    }
    if {[regexp {^Endpoints: ([0-9]+)[ \t\r]*$} $line unused count]} {
      lappend endpoints $count
    }
    if {[regexp {^Slack \(([^)]+)\) :[ \t]+([^ \t\r]+)ns} $line unused state slack]} {
      if {$state ne "MET" || ![string is double -strict $slack] ||
          ![expr {$slack>=0 && $slack<1e100}]} {
        error "HDMI bus-skew violation or nonfinite slack: $line"
      }
      lappend slacks $slack
    }
  }
  if {[llength $requirements]!=2 || [llength $slacks]!=2 || $endpoints ne {9 9}} {
    error "Missing/extra HDMI bus-skew constraint or endpoint evidence"
  }
  if {[lsort -real $requirements] ne {1.000 6.750}} {
    error "Unexpected HDMI mailbox/Gray bus-skew bounds: $requirements"
  }
  foreach anchor {
    receiver_v2_i/axi_hdmi_core/inst/i_frame_mailbox/src_payload_hold_reg
    receiver_v2_i/axi_hdmi_core/inst/i_frame_mailbox/dst_addr_reg
    receiver_v2_i/axi_hdmi_core/inst/i_tx_core/hdmi_raddr_g_reg
    receiver_v2_i/axi_hdmi_core/inst/i_vdma/vdma_raddr_g_m1_reg
  } {
    for {set bit 0} {$bit<9} {incr bit} {
      if {[string first [format {%s[%d]} $anchor $bit] $report]<0} {
        error "Missing exact HDMI bus-skew endpoint $anchor bit=$bit"
      }
    }
  }
  return $slacks
}

proc receiver_v2_signoff::run {out {typed_config {}}} {
  variable script_dir
  set brams [require_rf_buffer_brams]
  set bram_drc [report_drc -checks {REQP-1839 REQP-1840} -return_string]
  set bram_fd [open [file join $out implementation_bram_async_control.rpt] w]
  puts $bram_fd $bram_drc
  close $bram_fd
  assert_bram_async_controls $bram_drc
  # check_timing must precede CDC: report_cdc silently omits unclocked paths
  # and ports without input-delay coverage. Never accept CDC alone as proof.
  set timing [check_timing -verbose -return_string]
  set cdc [report_cdc -details -no_waiver -return_string]
  foreach {name contents} [list implementation_check_timing.rpt $timing implementation_cdc.rpt $cdc] {
    set fd [open [file join $out $name] w]
    puts $fd $contents
    close $fd
  }
  if {[llength [get_ports -quiet hdmi_out_clk]]==1} {
    set skew_file [file join $out implementation_bus_skew.rpt]
    report_bus_skew -file $skew_file
    set fd [open $skew_file r]
    set skew [read $fd]
    close $fd
    assert_hdmi_bus_skew $skew
  }
  set classified_io {}
  if {[llength [get_ports -quiet hdmi_out_clk]]==1} {
    namespace eval ::receiver_v2_external_io_contract { variable library_only 1 }
    source [file join $script_dir receiver_v2_external_io_contract.tcl]
    set io_fd [open [file join $out implementation_external_io_topology.txt] w]
    try {
      set classified_io [::receiver_v2_external_io_contract::validate_open_topology $io_fd $timing]
    } finally { close $io_fd }
  }
  set counts [assert_timing_coverage $timing $classified_io]
  if {$typed_config eq {}} {
    set endpoints [assert_cdc $cdc]
  } else {
    if {[lsort [dict keys $typed_config]] ne {manifest python vivado}} {
      error "Incomplete typed CDC tool/manifest configuration"
    }
    uplevel #0 [list source [file join $script_dir receiver_v2_typed_cdc_gate.tcl]]
    receiver_v2_typed_cdc_gate::validate [file join $out typed_cdc_release] $script_dir \
      [dict get $typed_config manifest] [dict get $typed_config python] [dict get $typed_config vivado]
    set endpoints "EXACT_TYPED_ROUTED_PROOF_VERIFIED"
  }
  set fd [open [file join $out implementation_coverage_gate.txt] w]
  puts $fd "timing_checks=$counts"
  puts $fd "cdc_info_endpoints=$endpoints"
  puts $fd "required_rf_brams=$brams"
  puts $fd "RXV2_COVERAGE_CDC_GATE_PASS"
  close $fd
}
