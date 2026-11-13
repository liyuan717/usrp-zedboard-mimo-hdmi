# Read-only, fail-closed evidence contract for the intentionally unconstrained
# board-control ports.  This script NEVER adds delays, false paths, waivers or
# timing exceptions.  It is deliberately separate from the project signoff
# gate: a structural proof is not an electrical interface timing constraint.
#
# Default usage:
#   vivado -mode batch -source receiver_v2_external_io_contract.tcl \
#     -tclargs <placed.dcp> <fresh-output-directory>
#
# Probe usage (diagnostic only; never reports static topology verification):
#   ... -tclargs --probe <placed.dcp> <fresh-output-directory>

namespace eval ::receiver_v2_external_io_contract {
  variable contract_tcl [file normalize [info script]]
  variable expected_inputs {}
  for {set bit 0} {$bit < 8} {incr bit} {
    lappend expected_inputs [format {gpio_status[%d]} $bit]
  }
  foreach signal {iic_mux_scl iic_mux_sda} {
    for {set bit 0} {$bit < 2} {incr bit} {
      lappend expected_inputs [format {%s[%d]} $signal $bit]
    }
  }
  variable expected_static_outputs {enable gpio_ctl[0] gpio_ctl[1] gpio_ctl[2] gpio_ctl[3] gpio_en_agc[0] gpio_resetb[0] gpio_sync[0] iic_mux_scl[0] iic_mux_scl[1] iic_mux_sda[0] iic_mux_sda[1] txnrx}
  variable expected_forwarded_outputs {hdmi_out_clk spi_clk tx_clk_out_p}
  variable expected_forwarded_clocks {hdmi_out_clk hdmi_forwarded spi_clk ad9361_spi_clk tx_clk_out_p ad9361_fb_clk}

  proc fail {message} { error "EXTERNAL_IO_CONTRACT_UNVERIFIED: $message" }

  proc parse_sha256_output {output} {
    # CertUtil versions emit either one contiguous 64-hex-character line or
    # one line of 32 whitespace-separated bytes.  Never scrape a digest from a
    # header/footer/sub-string: require exactly one complete digest line.
    set candidates {}
    foreach raw [split $output "\n"] {
      set line [string trim $raw]
      if {[regexp -nocase {^[0-9a-f]{64}$} $line]} {
        lappend candidates [string tolower $line]
      } elseif {[regexp -nocase {^[0-9a-f]{2}([[:space:]]+[0-9a-f]{2}){31}$} $line]} {
        set canonical [string map [list " " "" "\t" ""] $line]
        if {![regexp -nocase {^[0-9a-f]{64}$} $canonical]} {
          fail "malformed whitespace-separated SHA-256 line: $line"
        }
        lappend candidates [string tolower $canonical]
      }
    }
    if {[llength $candidates] != 1} {
      fail "SHA-256 output has [llength $candidates] complete digest lines, expected exactly one"
    }
    return [lindex $candidates 0]
  }

  proc sha256_file {path} {
    # Vivado 2025 Tcl on this Windows flow does not promise a Tcllib digest
    # package.  certutil is the OS-provided SHA-256 implementation; only an
    # absolute, existing file is ever passed, and its output is parsed strictly.
    set normalized [file normalize $path]
    if {![file isfile $normalized]} { fail "cannot hash absent file $normalized" }
    if {[catch {exec certutil -hashfile $normalized SHA256} output]} { fail "SHA-256 command failed for $normalized: $output" }
    return [parse_sha256_output $output]
  }

  proc unique_sorted {items} { return [lsort -unique $items] }

  proc require_exact_set {label actual expected} {
    # Preserve raw multiplicity before comparing sets. A duplicated row must
    # not be hidden by lsort -unique just because all expected names appear.
    if {[llength $actual] != [llength $expected]} {
      fail "$label raw port-list cardinality is [llength $actual], expected [llength $expected]: raw={$actual}"
    }
    set actual_unique [unique_sorted $actual]
    set expected_unique [unique_sorted $expected]
    if {[llength $actual_unique] != [llength $actual]} {
      fail "$label has duplicate port rows: raw={$actual}"
    }
    if {$actual_unique ne $expected_unique} {
      fail "$label exact port set mismatch: actual={$actual_unique} expected={$expected_unique}"
    }
  }

  proc collect_report_ports {text section} {
    # check_timing prints each section twice: once in the table of contents and
    # once with its body.  Locate the second exact heading, then accept only
    # bare port rows after its expected classification row.  Counts alone are
    # intentionally insufficient.
    set lines [split $text "\n"]
    switch -- $section {
      input {
        set heading {5. checking no_input_delay (12)}
        set classification {There are 12 input ports with no input delay specified. (HIGH)}
      }
      output-high {
        set heading {6. checking no_output_delay (16)}
        set classification {There are 13 ports with no output delay specified. (HIGH)}
      }
      output-low {
        set heading {6. checking no_output_delay (16)}
        set classification {There are 3 ports with no output delay but with a timing clock defined on it or propagating through it (LOW)}
      }
      default { fail "unknown check_timing section $section" }
    }
    set headers {}
    for {set i 0} {$i < [llength $lines]} {incr i} {
      if {[string trim [lindex $lines $i]] eq $heading} { lappend headers $i }
    }
    if {[llength $headers] != 2} { fail "check_timing $section heading count is [llength $headers], expected 2" }
    set index [lindex $headers 1]
    set found 0
    set started 0
    for {incr index} {$index < [llength $lines]} {incr index} {
      set t [string trim [lindex $lines $index]]
      if {!$found} {
        if {$t eq $classification} { set found 1 }
        continue
      }
      if {$t eq ""} {
        if {$started} { break }
        continue
      }
      if {![regexp {^[A-Za-z_][A-Za-z0-9_]*(\[[0-9]+\])?$} $t]} {
        fail "unexpected $section check_timing row: {$t}"
      }
      lappend ports $t
      set started 1
    }
    if {!$found} { fail "missing $section check_timing classification row" }
    if {![info exists ports] || [llength $ports] == 0} { fail "empty $section check_timing port list" }
    return $ports
  }

  proc validate_check_timing_text {text} {
    variable expected_inputs
    variable expected_static_outputs
    variable expected_forwarded_outputs
    set inputs [collect_report_ports $text input]
    set outputs_high [collect_report_ports $text output-high]
    set outputs_low [collect_report_ports $text output-low]
    require_exact_set no_input_delay_ports $inputs $expected_inputs
    require_exact_set no_output_delay_high_ports $outputs_high $expected_static_outputs
    require_exact_set no_output_delay_low_ports $outputs_low $expected_forwarded_outputs
    return [dict create input $inputs output_high $outputs_high output_low $outputs_low]
  }

  proc require_single {label objects} {
    if {[llength $objects] != 1} { fail "$label cardinality is [llength $objects], expected 1: {$objects}" }
    return [lindex $objects 0]
  }

  proc require_property {object property expected label} {
    set actual [get_property $property $object]
    if {$actual ne $expected} { fail "$label $property is {$actual}, expected {$expected}" }
  }

  proc finite_nonnegative {value label} {
    if {![string is double -strict $value]} {
      fail "$label is not finite nonnegative: {$value}"
    }
    # Tcl accepts implementation-specific numeric spellings. Canonicalizing
    # after conversion exposes Inf/NaN and floating-point overflow.
    if {[catch {expr {double($value)}} numeric] || [catch {format %.17g $numeric} canonical]} {
      fail "$label is not finite nonnegative: {$value}"
    }
    if {[string match -nocase *nan* $canonical] || [string match -nocase *inf* $canonical] ||
        [catch {expr {$numeric >= 0.0}} nonnegative] || !$nonnegative} {
      fail "$label is not finite nonnegative: {$value}"
    }
  }

  proc require_clock_object {clock expected expected_period label} {
    if {[get_property NAME $clock] ne $expected} { fail "$label clock is [get_property NAME $clock], expected $expected" }
    set actual_period [get_property PERIOD $clock]
    finite_nonnegative $actual_period "$label clock period"
    if {[catch {expr {abs(double($actual_period) - double($expected_period)) <= 0.000001}} matches] || !$matches} {
      fail "$label clock period is $actual_period ns, expected $expected_period ns"
    }
  }

  proc require_clock {cell expected expected_period label} {
    set clockpin [require_single "$label clock pin" [get_pins -quiet -of_objects $cell -filter {IS_CLOCK == 1}]]
    set clock [require_single "$label clock" [get_clocks -quiet -of_objects $clockpin]]
    require_clock_object $clock $expected $expected_period $label
  }

  proc validate_input {port_name report} {
    set port [require_single "input port $port_name" [get_ports -quiet $port_name]]
    set direction [get_property DIRECTION $port]
    if {$direction ni {IN INOUT}} { fail "input $port_name direction is $direction" }
    set endpoints [all_fanout -from $port -flat -endpoints_only]
    set endpoint [require_single "input $port_name first fanout endpoint" $endpoints]
    set firstcell [require_single "input $port_name first fanout cell" [get_cells -quiet -of_objects $endpoint]]
    if {![regexp {syncstages_ff_reg\[0\](\[[0-9]+\])?$} $firstcell]} {
      fail "input $port_name first endpoint is not XPM stage0: $firstcell"
    }
    set previous ""
    for {set stage 0} {$stage < 4} {incr stage} {
      regsub {syncstages_ff_reg\[0\]} $firstcell [format {syncstages_ff_reg[%d]} $stage] cellname
      set cell [require_single "input $port_name XPM stage $stage" [get_cells -quiet $cellname]]
      require_property $cell REF_NAME FDRE "input $port_name stage $stage"
      require_property $cell ASYNC_REG 1 "input $port_name stage $stage"
      require_clock $cell clk_fpga_0 20.0 "input $port_name stage $stage"
      if {$stage > 0} {
        set from [get_pins $previous/Q]
        set to [get_pins $cellname/D]
        set targets [all_fanout -from $from -flat -endpoints_only]
        if {[llength $targets] != 1 || [lindex $targets 0] ne $to} {
          fail "input $port_name stage [expr {$stage - 1}] has extra/incorrect fanout: {$targets}"
        }
        set path [require_single "input $port_name stage $stage timing path" [get_timing_paths -quiet -from $from -to $to -max_paths 2]]
        finite_nonnegative [get_property SLACK $path] "input $port_name stage $stage slack"
        finite_nonnegative [get_property DATAPATH_DELAY $path] "input $port_name stage $stage datapath delay"
        puts $report "INPUT $port_name STAGE=$stage CELL=$cell SLACK_NS=[get_property SLACK $path] DATAPATH_NS=[get_property DATAPATH_DELAY $path]"
      }
      set previous $cellname
    }
  }

  proc validate_gpio_output {port_name expected_index report} {
    set port [require_single "GPIO output port $port_name" [get_ports -quiet $port_name]]
    require_property $port DIRECTION OUT "GPIO output $port_name"
    set endpoints [all_fanin -to $port -flat -startpoints_only]
    set endpoint [require_single "GPIO output $port_name startpoint" $endpoints]
    set cell [require_single "GPIO output $port_name owner cell" [get_cells -quiet -of_objects $endpoint]]
    set expected "receiver_v2_i/rf_gpio/U0/gpio_core_1/Dual.gpio_Data_Out_reg\[$expected_index\]"
    if {$cell ne $expected} { fail "GPIO output $port_name owner is $cell, expected $expected" }
    require_property $cell REF_NAME FDRE "GPIO output $port_name"
    require_clock $cell clk_fpga_0 20.0 "GPIO output $port_name"
    puts $report "STATIC_GPIO_OUTPUT $port_name OWNER=$cell"
  }

  proc validate_iic_output {port_name report} {
    set port [require_single "IIC output port $port_name" [get_ports -quiet $port_name]]
    require_property $port DIRECTION INOUT "IIC output $port_name"
    set endpoints [all_fanin -to $port -flat -startpoints_only]
    if {[llength $endpoints] == 0} { fail "IIC output $port_name has no architecture startpoints" }
    foreach endpoint $endpoints {
      set cell [require_single "IIC output $port_name owner cell" [get_cells -quiet -of_objects $endpoint]]
      if {![string match "receiver_v2_i/axi_iic_main/U0/X_IIC/*" $cell]} {
        fail "IIC output $port_name has non-XIIC startpoint $cell"
      }
      set ref [get_property REF_NAME $cell]
      if {$ref ni {FDRE FDSE}} { fail "IIC output $port_name startpoint $cell has unsupported type $ref" }
      require_clock $cell clk_fpga_0 20.0 "IIC output $port_name"
    }
    puts $report "STATIC_IIC_OUTPUT $port_name STARTPOINTS=[llength $endpoints] OWNER=axi_iic_main/U0/X_IIC"
  }

  proc validate_adi_control_output {port_name report} {
    set port [require_single "AD9361 control port $port_name" [get_ports -quiet $port_name]]
    require_property $port DIRECTION OUT "AD9361 control $port_name"
    set endpoint [require_single "AD9361 control $port_name startpoint" [all_fanin -to $port -flat -startpoints_only]]
    set cell [require_single "AD9361 control $port_name owner cell" [get_cells -quiet -of_objects $endpoint]]
    set suffix [expr {$port_name eq "enable" ? "i_enable/i_tx_data_oddr" : "i_txnrx/i_tx_data_oddr"}]
    set expected "receiver_v2_i/axi_ad9361/inst/i_dev_if/$suffix"
    if {$cell ne $expected} { fail "AD9361 control $port_name owner is $cell, expected $expected" }
    require_property $cell REF_NAME ODDR "AD9361 control $port_name"
    require_clock $cell clk_out1_receiver_v2_rf_sampling_clock_0 62.5 "AD9361 control $port_name"
    puts $report "STATIC_AD9361_CONTROL $port_name OWNER=$cell"
  }

  proc validate_forwarded_clock {port_name clock_name expected_period source_cell expected_ref report} {
    set port [require_single "forwarded clock port $port_name" [get_ports -quiet $port_name]]
    require_property $port DIRECTION OUT "forwarded clock $port_name"
    set clock [require_single "forwarded clock $port_name" [get_clocks -quiet -of_objects $port]]
    require_clock_object $clock $clock_name $expected_period "forwarded clock $port_name"
    # SOURCE_PINS on a top-level generated clock reports the port itself in
    # this Vivado version.  Prove the physical/netlist source instead: the
    # only startpoint must be the exact known forwarding register.
    set source_pin [require_single "forwarded clock $port_name topology source pin" [all_fanin -to $port -flat -startpoints_only]]
    set source [require_single "forwarded clock $port_name topology source cell" [get_cells -quiet -of_objects $source_pin]]
    if {$source ne $source_cell} { fail "forwarded clock $port_name source is $source, expected $source_cell" }
    require_property $source REF_NAME $expected_ref "forwarded clock $port_name source"
    puts $report "FORWARDED_CLOCK $port_name CLOCK=$clock_name SOURCE_CELL=$source SOURCE_PIN=$source_pin"
  }

  proc static_result_lines {} {
    # Kept pure so parser-only tests can ensure static success is never
    # accidentally renamed into a runtime/FDD pass.
    return [list \
      "STATIC_TOPOLOGY_ONLY=VERIFIED" \
      "RUNTIME_STATE=OFFLINE_VERIFIED_RUNTIME_PENDING" \
      "LIVE_FDD=UNVERIFIED: netlist topology cannot prove live SPI-programmed ENSM=FDD or PS readback."]
  }

  # Same topology proof on the already-open routed design, immediately before
  # export. This helper never opens/closes a design or changes constraints.
  proc validate_open_topology {report timing_text} {
    variable expected_inputs
    set sets [validate_check_timing_text $timing_text]
    foreach {clock_name expected_period} {clk_fpga_0 20.0 clk_out1_receiver_v2_rf_sampling_clock_0 62.5 hdmi_forwarded 6.75 ad9361_spi_clk 320.0 ad9361_fb_clk 62.5} {
      set clock [require_single "clock contract $clock_name" [get_clocks -quiet $clock_name]]
      require_clock_object $clock $clock_name $expected_period "clock contract $clock_name"
      puts $report "CLOCK_PERIOD CLOCK=$clock_name PERIOD_NS=[get_property PERIOD $clock]"
    }
    foreach input $expected_inputs { validate_input $input $report }
    foreach {port index} {gpio_ctl[0] 3 gpio_ctl[1] 2 gpio_ctl[2] 1 gpio_ctl[3] 0 gpio_en_agc[0] 7 gpio_resetb[0] 8 gpio_sync[0] 6} {
      validate_gpio_output $port $index $report
    }
    foreach port {iic_mux_scl[0] iic_mux_scl[1] iic_mux_sda[0] iic_mux_sda[1]} { validate_iic_output $port $report }
    foreach port {enable txnrx} { validate_adi_control_output $port $report }
    validate_forwarded_clock hdmi_out_clk hdmi_forwarded 6.75 "receiver_v2_i/axi_hdmi_core/inst/i_clk_oddr" ODDR $report
    validate_forwarded_clock spi_clk ad9361_spi_clk 320.0 "receiver_v2_i/rf_spi/U0/NO_DUAL_QUAD_MODE.QSPI_NORMAL/QSPI_LEGACY_MD_GEN.QSPI_CORE_INTERFACE_I/LOGIC_FOR_MD_0_GEN.SPI_MODULE_I/RATIO_NOT_EQUAL_4_GENERATE.SCK_O_NQ_4_NO_STARTUP_USED.SCK_O_NE_4_FDRE_INST" FDRE $report
    validate_forwarded_clock tx_clk_out_p ad9361_fb_clk 62.5 "receiver_v2_i/rf_fb_clock/inst/i_tx_data_oddr" ODDR $report
    return $sets
  }

  proc run_contract {dcp out probe} {
    variable expected_inputs
    variable contract_tcl
    if {[file exists $out]} { fail "refusing existing output directory $out" }
    file mkdir $out
    # Freeze before opening/parsing the checkpoint.  The report binds both the
    # supplied identities and the copies actually consumed by Vivado.
    set input_dcp [file normalize $dcp]
    set input_contract [file normalize $contract_tcl]
    set input_dcp_hash [sha256_file $input_dcp]
    set input_contract_hash [sha256_file $input_contract]
    set frozen_dcp [file join $out frozen_checkpoint.dcp]
    set frozen_contract [file join $out frozen_receiver_v2_external_io_contract.tcl]
    file copy -force $input_dcp $frozen_dcp
    file copy -force $input_contract $frozen_contract
    if {[sha256_file $frozen_dcp] ne $input_dcp_hash || [sha256_file $frozen_contract] ne $input_contract_hash} {
      fail "frozen DCP/Tcl hash differs from input"
    }
    open_checkpoint $frozen_dcp
    report_route_status -file [file join $out route_status.rpt]
    set report [open [file join $out external_io_contract.txt] w]
    puts $report "EXTERNAL_IO_CONTRACT_VERSION=1"
    # INPUT_* identifies the Tcl invocation arguments.  CHECKPOINT and
    # CONTRACT_TCL identify the second-level files Vivado actually opened.
    # Both hashes must agree; keeping both layers prevents path substitution.
    puts $report "INPUT_CHECKPOINT=$input_dcp"
    puts $report "INPUT_CHECKPOINT_SHA256=$input_dcp_hash"
    puts $report "INPUT_CONTRACT_TCL=$input_contract"
    puts $report "INPUT_CONTRACT_TCL_SHA256=$input_contract_hash"
    puts $report "CHECKPOINT=$frozen_dcp"
    puts $report "CHECKPOINT_SHA256=[sha256_file $frozen_dcp]"
    puts $report "CONTRACT_TCL=$frozen_contract"
    puts $report "CONTRACT_TCL_SHA256=[sha256_file $frozen_contract]"
    set timing_text [check_timing -verbose -return_string]
    set fh [open [file join $out check_timing_verbose.rpt] w]; puts -nonewline $fh $timing_text; close $fh
    set sets [validate_check_timing_text $timing_text]
    puts $report "CHECK_TIMING_PORTSETS_VERIFIED input={[dict get $sets input]} high={[dict get $sets output_high]} low={[dict get $sets output_low]}"
    foreach {clock_name expected_period} {clk_fpga_0 20.0 clk_out1_receiver_v2_rf_sampling_clock_0 62.5 hdmi_forwarded 6.75 ad9361_spi_clk 320.0 ad9361_fb_clk 62.5} {
      set clock [require_single "clock contract $clock_name" [get_clocks -quiet $clock_name]]
      require_clock_object $clock $clock_name $expected_period "clock contract $clock_name"
      puts $report "CLOCK_PERIOD CLOCK=$clock_name PERIOD_NS=[get_property PERIOD $clock]"
    }
    if {$probe} {
      foreach {port clock source} {hdmi_out_clk hdmi_forwarded * spi_clk ad9361_spi_clk * tx_clk_out_p ad9361_fb_clk *} {
        set c [get_clocks -quiet -of_objects [get_ports $port]]
        set starts [all_fanin -to [get_ports $port] -flat -startpoints_only]
        set cells {}
        foreach start $starts { foreach cell [get_cells -quiet -of_objects $start] { lappend cells "$cell:[get_property REF_NAME $cell]" } }
        puts $report "PROBE_CLOCK PORT=$port CLOCK=$c SOURCE_PROPERTY=[get_property SOURCE_PINS $c] STARTPOINTS=$starts CELLS=$cells"
      }
      close $report
      puts "EXTERNAL_IO_CONTRACT_PROBE_ONLY"
      return
    }
    validate_open_topology $report $timing_text
    # This proves only the DCP's static architecture.  It deliberately cannot
    # prove SPI-programmed ENSM state, DTB selection, PS ownership, calibration
    # transitions, or any board observation.  A separate Python admission
    # binds DT/driver/PS-source evidence, and hardware remains pending there.
    if {[sha256_file $input_dcp] ne $input_dcp_hash || [sha256_file $input_contract] ne $input_contract_hash} {
      close $report
      fail "input DCP/Tcl changed during static proof"
    }
    foreach line [static_result_lines] { puts $report $line }
    close $report
    puts "EXTERNAL_IO_STATIC_TOPOLOGY_ONLY_PASS"
  }
}

if {![info exists ::receiver_v2_external_io_contract::library_only]} {
  if {$argc == 3 && [lindex $argv 0] eq "--probe"} {
    ::receiver_v2_external_io_contract::run_contract [file normalize [lindex $argv 1]] [file normalize [lindex $argv 2]] 1
  } elseif {$argc == 2} {
    ::receiver_v2_external_io_contract::run_contract [file normalize [lindex $argv 0]] [file normalize [lindex $argv 1]] 0
  } else {
    error "Usage: <placed.dcp> <fresh-output-directory>, or --probe <placed.dcp> <fresh-output-directory>"
  }
}
