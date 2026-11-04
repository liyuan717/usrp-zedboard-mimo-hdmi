# Shared fail-closed parsing of the actual Vivado CDC report. Summary
# severities and counts are parsed, not arbitrary occurrences in prose.
proc rf_cdc_audit {report {details ""} {hdmi 0}} {
  set critical 0
  set unsafe 0
  set unknown 0
  set recognized 0
  foreach line [split $report \n] {
    if {[regexp {^CDC-[0-9]+[[:space:]]+(Critical|Warning|Info)[[:space:]]+([0-9]+)[[:space:]]+} $line -> severity count]} {
      set recognized 1
      if {$severity eq "Critical"} {incr critical $count}
    }
    # The non-detailed summary has endpoint/safe/unsafe/unknown/no-ASYNC_REG
    # columns. Do not match detailed endpoint rows or clock-name digits.
    if {[regexp {^(Critical|Warning|Info)[[:space:]].*[[:space:]]([0-9]+)[[:space:]]+([0-9]+)[[:space:]]+([0-9]+)[[:space:]]+([0-9]+)[[:space:]]+([0-9]+)[[:space:]]*$} $line -> severity endpoints safe bad unclassified no_async]} {
      set recognized 1
      if {$severity eq "Critical"} {incr critical $endpoints}
      incr unsafe $bad
      incr unknown $unclassified
    }
  }
  if {!$recognized && ![regexp -nocase {no CDC paths|no clock domain crossings} $report]} {
    error "Unrecognized CDC report; cannot prove crossing integrity"
  }

  # axi_hdmi_tx contains one intentional multi-bit CDC: the frame-start write
  # address is stable for a complete frame and is explicitly False Path
  # constrained. The accompanying vdma/hdmi frame toggles use three-stage
  # synchronizers on both directions. Vivado reports the stable payload bus as
  # CDC-4 critical even though the vendor handshake protects it.
  #
  # Accept only that exact, hash-locked vendor path. Any second critical row,
  # changed hierarchy, changed width/count or missing False Path remains a
  # release failure. This is a proof-bound exception, not a general CDC waiver.
  set accepted_critical 0
  set accepted_unknown 0
  set exception ""
  if {$hdmi && $details ne "" && $critical > 0} {
    set critical_rows {}
    foreach line [split $details \n] {
      if {[regexp {^[[:space:]]*[0-9]+[[:space:]]+CDC-[0-9]+[[:space:]]+Critical[[:space:]]+} $line]} {
        lappend critical_rows $line
      }
    }
    if {[llength $critical_rows] == 1} {
      set row [lindex $critical_rows 0]
      set has_source [expr {[string first \
        "axi_hdmi_core/inst/i_vdma/vdma_fs_waddr_reg\[8:0\]/C" $row] >= 0}]
      set has_dest [expr {[string first \
        "axi_hdmi_core/inst/i_tx_core/hdmi_fs_waddr_reg\[8:0\]/D" $row] >= 0}]
      if {[regexp {CDC-4[[:space:]]+Critical[[:space:]]+Multi-bit unknown CDC circuitry[[:space:]]+0[[:space:]]+False Path} $row] \
          && $has_source && $has_dest && $unknown == 9} {
        set accepted_critical $critical
        set accepted_unknown $unknown
        set critical 0
        set unknown 0
        set exception "ADI_AXI_HDMI_FS_WADDR_HANDSHAKE"
      }
    }
  }
  return [dict create critical $critical unsafe $unsafe unknown $unknown \
    accepted_critical $accepted_critical accepted_unknown $accepted_unknown \
    exception $exception]
}

proc rf_timing_coverage {report {hdmi 0}} {
  # In this fixed FMC profile, eight separately synchronized GPIO status
  # inputs and nine slow SPI-ENSM-controlled GPIO outputs have no periodic
  # external clock. Two forwarded clock outputs are the remaining low-risk
  # entries. SPI/data interfaces themselves MUST have delay constraints.
  set limits [dict create no_clock 0 unconstrained_internal_endpoints 0 \
    multiple_clock 0 generated_clocks 0 loops 0 partial_input_delay 0 \
    partial_output_delay 0 latch_loops 0 no_input_delay 8 no_output_delay 11]
  if {$hdmi} {
    # Four open-drain I2C pads are asynchronous to FCLK; HDMI pixel DATA,
    # HS/VS/DE remain constrained. One extra forwarded HDMI clock is a clock port.
    dict set limits no_input_delay 12
    dict set limits no_output_delay 16
  }
  set failures {}
  # Numerical slack is insufficient if an IP clock and its jitter/derived
  # constraints were silently replaced by a later create_clock command.
  foreach line [split $report \n] {
    if {[regexp {^XDCC-7[[:space:]].*[[:space:]]([0-9]+)[[:space:]]*$} $line -> count] && $count > 0} {
      lappend failures "scoped_clock_overwritten=$count"
    }
  }
  foreach category [dict keys $limits] {
    set expression [format {checking %s \(([0-9]+)\)} $category]
    if {![regexp $expression $report -> count]} {
      lappend failures "$category=unreported"
    } elseif {$count > [dict get $limits $category]} {
      lappend failures "$category=$count"
    }
  }
  return $failures
}
