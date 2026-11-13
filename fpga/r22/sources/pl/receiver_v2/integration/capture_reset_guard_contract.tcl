# Static/physical contract for rfv2_capture_reset_guard.
#
# This file only *observes* a synthesized or routed design.  It does not add
# exceptions, modify a checkpoint, or turn an asynchronous assertion path into
# a timing waiver.  The companion capture_reset_guard_cdc.xdc owns the three
# source-to-first-stage max-delay constraints.  Source text alone is not
# physical evidence; invoke validate post_route on the new routed DCP.

namespace eval receiver_v2_capture_contract {
  variable root "receiver_v2_i/rf_capture_guard/inst"
  variable din_clock "clk_out1_receiver_v2_rf_sampling_clock_0"
  variable dout_clock "clk_fpga_0"
  variable din_period 62.500
  variable dout_period 20.000

  proc require_one {objects label} {
    if {[llength $objects] != 1} {
      error "$label: expected exactly one object, got '$objects'"
    }
    return [lindex $objects 0]
  }

  proc finite_positive {value label} {
    if {![string is double -strict $value] ||
        [regexp -nocase {nan|inf} $value] || $value <= 0.0} {
      error "$label: expected finite positive value, got '$value'"
    }
    return $value
  }

  proc finite_nonnegative {value label} {
    if {![string is double -strict $value] ||
        [regexp -nocase {nan|inf} $value] || $value < 0.0} {
      error "$label: expected finite nonnegative value, got '$value'"
    }
    return $value
  }

  proc bool_asserted {raw label} {
    # Vivado serializes boolean properties as either TRUE or 1 depending on
    # database phase/version.  False-ish values remain errors.
    set normalized [string tolower [string trim $raw]]
    if {$normalized ni {1 true yes}} {
      error "$label: ASYNC_REG must be asserted; raw='$raw' normalized='$normalized'"
    }
    return $normalized
  }

  proc exact_cell {name} {
    return [require_one [get_cells -hier -quiet -filter "NAME == $name"] "cell $name"]
  }

  proc exact_ff {name} {
    set cell [exact_cell $name]
    set ref [get_property REF_NAME $cell]
    if {$ref ni {FDRE FDCE}} {
      error "cell $name: expected FDRE/FDCE, got REF_NAME='$ref'"
    }
    return $cell
  }

  proc pin_of {cell ref_pin} {
    return [require_one [get_pins -quiet -of_objects $cell \
      -filter "REF_PIN_NAME == $ref_pin"] "pin $ref_pin of $cell"]
  }

  proc assert_clock {cell expected_name expected_period label} {
    set clock [require_one [get_clocks -quiet -of_objects [pin_of $cell C]] "$label clock"]
    set name [get_property NAME $clock]
    set period [get_property PERIOD $clock]
    if {$name ne $expected_name} {
      error "$label: expected clock '$expected_name', got '$name'"
    }
    finite_positive $period "$label period"
    if {[expr {abs(double($period) - double($expected_period))}] > 0.001} {
      error "$label: expected period $expected_period ns, got $period ns on '$name'"
    }
    return $clock
  }

  proc assert_async_ff {cell label} {
    bool_asserted [get_property ASYNC_REG $cell] "$label ($cell)"
  }

  proc assert_sole_fanout {m1 m2 label} {
    set q [pin_of $m1 Q]
    set d [pin_of $m2 D]
    set net [require_one [get_nets -quiet -of_objects $q] "$label m1 Q net"]
    set sinks [get_pins -quiet -of_objects $net -filter {DIRECTION == IN}]
    if {[llength $sinks] != 1 || [lindex $sinks 0] ne $d} {
      error "$label: m1 Q must have sole fanout to m2 D; actual='$sinks', expected='$d'"
    }
  }

  proc assert_normal_stage_timing {m1 m2 label} {
    set path [get_timing_paths -quiet -from [pin_of $m1 Q] -to [pin_of $m2 D] -max_paths 1]
    # Deliberately no -user_ignored fallback: this is a local same-clock path.
    if {[llength $path] != 1} { error "$label: no ordinary m1->m2 timing path" }
    finite_positive [get_property DATAPATH_DELAY $path] "$label datapath delay"
    finite_nonnegative [get_property SLACK $path] "$label slack"
    set endpoint_clock [get_property ENDPOINT_CLOCK $path]
    if {$endpoint_clock eq ""} { error "$label: timed path lacks endpoint clock" }
    return $endpoint_clock
  }

  proc assert_bounded_crossing {source m1 bound_ns label} {
    # Match the cell-to-cell XDC endpoints, including the launch FF timing
    # reference, exactly as the independently routed mailbox contract does.
    set path [get_timing_paths -quiet -from $source -to $m1 -max_paths 1]
    # Deliberately no -user_ignored fallback: absence proves the XDC max-delay
    # has not produced an ordinary timing requirement on this checkpoint.
    if {[llength $path] != 1} { error "$label: no ordinary source->m1 timing path" }
    set delay [finite_positive [get_property DATAPATH_DELAY $path] "$label datapath delay"]
    set requirement [finite_positive [get_property REQUIREMENT $path] "$label requirement"]
    finite_nonnegative [get_property SLACK $path] "$label slack"
    if {[expr {abs(double($requirement) - double($bound_ns))}] > 0.001} {
      error "$label: expected max-delay requirement $bound_ns ns, got $requirement ns"
    }
    if {$delay > ($bound_ns + 0.001)} {
      error "$label: data path $delay ns exceeds bound $bound_ns ns"
    }
    return $path
  }

  proc check_sync_pair {source_name source_clock source_period m1_name m2_name dest_clock dest_period phase label {bound ""}} {
    set source [exact_ff $source_name]
    set m1 [exact_ff $m1_name]
    set m2 [exact_ff $m2_name]
    assert_clock $source $source_clock $source_period "$label source"
    assert_clock $m1 $dest_clock $dest_period "$label m1"
    assert_clock $m2 $dest_clock $dest_period "$label m2"
    assert_async_ff $m1 "$label m1"
    assert_async_ff $m2 "$label m2"
    assert_sole_fanout $m1 $m2 $label
    if {$phase eq "post_route"} {
      assert_normal_stage_timing $m1 $m2 $label
      if {$bound ne ""} { assert_bounded_crossing $source $m1 $bound $label }
    }
  }

  proc check_local_release {prefix clock_name period phase} {
    variable root
    set m1_name "$root/${prefix}_release_sync_reg\[0\]"
    set m2_name "$root/${prefix}_release_sync_reg\[1\]"
    set m1 [exact_ff $m1_name]
    set m2 [exact_ff $m2_name]
    assert_clock $m1 $clock_name $period "$prefix local release m1"
    assert_clock $m2 $clock_name $period "$prefix local release m2"
    assert_async_ff $m1 "$prefix local release m1"
    assert_async_ff $m2 "$prefix local release m2"
    assert_sole_fanout $m1 $m2 "$prefix local release"
    if {$phase eq "post_route"} { assert_normal_stage_timing $m1 $m2 "$prefix local release" }
  }

  proc check_fault_cone {} {
    variable root
    set expected_sinks {}
    foreach domain {din dout} {
      foreach bit {0 1} {
        set cell [exact_ff [format {%s/%s_release_sync_reg[%d]} $root $domain $bit]]
        if {[get_property REF_NAME $cell] ne "FDCE"} {error "release chain must use asynchronous clear: $cell"}
        lappend expected_sinks [pin_of $cell CLR]
      }
    }
    set expected_sinks [lsort $expected_sinks]
    # Observe the real board roots, not only presence of any reset fanin. The
    # two LOCKED outputs form the reviewed PLL-health AND upstream of guard.
    # Vivado all_fanin startpoints use C for launching FDREs, LOCKED for MMCMs.
    set expected_starts [lsort [list \
      {receiver_v2_i/reset_rf/U0/PR_OUT_DFF[0].FDRE_PER/C} \
      {receiver_v2_i/reset_rf_lclk/U0/PR_OUT_DFF[0].FDRE_PER/C} \
      receiver_v2_i/rf_sampling_clock/inst/mmcm_adv_inst/LOCKED \
      receiver_v2_i/delay_clock/inst/mmcm_adv_inst/LOCKED \
      receiver_v2_i/rf_lclk_monitor/inst/ready_reg/C]]
    foreach sink $expected_sinks {
      set starts [lsort [all_fanin -flat -startpoints_only -to $sink]]
      if {$starts ne $expected_starts} {
        error "capture fault assertion root mismatch for $sink: $starts"
      }
    }
    # All four module-local fault inputs must reach all four CLR pins and
    # NOTHING else. In particular, do not admit a bypass to business D/CE or
    # another reset pin simply because the source has a familiar name.
    foreach input {din_raw_reset dout_raw_reset pll_locked source_ready} {
      set pins [get_pins -quiet -hier -filter [format {NAME == "%s/%s"} $root $input]]
      set pin [require_one $pins "capture fault input $input"]
      set sinks [lsort [all_fanout -flat -endpoints_only -from $pin]]
      if {$sinks ne $expected_sinks} {error "capture fault input $input has missing/extra fanout: $sinks"}
    }
    puts "CAPTURE_FAULT_CONE_EXACT_PASS roots=5 local_inputs=4 clr_endpoints=4 no_business_bypass=1"
  }

  proc validate {phase} {
    variable root
    variable din_clock
    variable dout_clock
    variable din_period
    variable dout_period
    if {$phase ni {post_synth post_route}} {
      error "validate phase must be post_synth or post_route, got '$phase'"
    }

    # FCLK destination level returning to the din/16 MHz domain.
    check_sync_pair "$root/dst_fenced_r_reg" $dout_clock $dout_period \
      "$root/dst_fenced_sync_reg\[0\]" "$root/dst_fenced_sync_reg\[1\]" \
      $din_clock $din_period $phase "dst_fenced FCLK->din" 62.500

    # Two independent din/16 MHz source levels crossing into FCLK.
    check_sync_pair "$root/src_request_r_reg" $din_clock $din_period \
      "$root/src_request_sync_reg\[0\]" "$root/src_request_sync_reg\[1\]" \
      $dout_clock $dout_period $phase "src_request din->FCLK" 20.000
    check_sync_pair "$root/src_fresh_r_reg" $din_clock $din_period \
      "$root/src_fresh_sync_reg\[0\]" "$root/src_fresh_sync_reg\[1\]" \
      $dout_clock $dout_period $phase "src_fresh din->FCLK" 20.000

    # Local async-assert/synchronous-deassert release chains.
    check_local_release din $din_clock $din_period $phase
    check_local_release dout $dout_clock $dout_period $phase
    check_fault_cone

    if {$phase eq "post_route"} {
      puts "CAPTURE_RESET_GUARD_CONTRACT_POST_ROUTE_PASS physical_observation_only=1"
    } else {
      puts "CAPTURE_RESET_GUARD_CONTRACT_POST_SYNTH_PASS physical_checks_pending=1"
    }
    return 1
  }

  # Pure Tcl checks for callers building a mock/static test harness.  This
  # deliberately does not call Vivado object queries and does not claim any
  # physical validation of the capture guard.
  proc selftest {} {
    foreach raw {1 TRUE true yes} { bool_asserted $raw "selftest asserted" }
    foreach raw {0 FALSE no {}} {
      if {![catch {bool_asserted $raw "selftest false"}]} { error "selftest accepted false boolean '$raw'" }
    }
    foreach value {NaN Inf -0.1 0} {
      if {![catch {finite_positive $value "selftest nonpositive"}]} { error "selftest accepted '$value'" }
    }
    if {![catch {require_one {} "selftest cardinality"}]} { error "selftest accepted empty cardinality" }
    puts "CAPTURE_RESET_GUARD_CONTRACT_SELFTEST_PASS static_only=1"
    return 1
  }
}

# Optional no-design smoke test for CI or a Tcl-only shell.  Normal synthesis
# and route scripts source this file and call validate explicitly.
if {[info exists ::argc] && $::argc == 1 && [lindex $::argv 0] eq "--selftest"} {
  receiver_v2_capture_contract::selftest
}
