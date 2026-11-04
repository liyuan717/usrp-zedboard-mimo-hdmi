# HDMI transmitter presentation observer integration ABI 1.
#
# This file is deliberately a callable post-freeze integration fragment.  It
# must not be sourced by create_zedboard_rf_system.tcl or the RF transport
# scripts.  Apply it only to a derived HDMI-capable design after its matching
# Linux patch and DT overlay have been staged together.
#
# Required existing cells/pins (the reviewed HDMI topology):
#   ps7/FCLK_CLK0, reset_50m/peripheral_aresetn, control_xbar, interrupt_concat
#   axi_hdmi_dma/m_axis, axi_hdmi_core/hdmi_16_vsync
# AXIS is tapped by a third, passive monitor interface endpoint.  ADI's inferred
# AXIS members do not necessarily appear as stand-alone scalar BD nets; trying
# to attach individual TVALID/TREADY/TLAST pins can therefore fabricate or
# replace a production connection.  The monitor has no stream outputs and
# cannot own or drive HDMI backpressure.

# Capture at source time: [info script] inside a later procedure may refer to
# the caller's script, not this integration fragment.
set ::hdmi_presentation_observer_root [file normalize [file join [file dirname [info script]] ../..]]

proc hdmi_presentation_require_one {description objects} {
  if {[llength $objects] != 1} {error "$description: expected exactly one object, got [llength $objects]: $objects"}
  return [lindex $objects 0]
}

proc hdmi_presentation_axis_monitor_attach {source monitor} {
  set source_if [get_bd_intf_pins -quiet $source]
  set monitor_if [get_bd_intf_pins -quiet $monitor/S_AXIS]
  if {[llength $source_if] != 1 || [llength $monitor_if] != 1} {
    error "HDMI AXIS monitor pin cardinality source=$source monitor=$monitor"
  }
  set existing [get_bd_intf_nets -quiet -of_objects $source_if]
  if {[llength $existing] != 1} {
    error "HDMI AXIS source must have exactly one existing interface net: $source got $existing"
  }
  set before [get_bd_intf_pins -quiet -of_objects $existing]
  if {[llength $before] < 2 || [lsearch -exact $before $source_if] < 0} {
    error "HDMI AXIS source lacks producer/consumer before monitor attach: net=$existing endpoints=$before"
  }
  # Do not replay or reconnect the original DMA endpoint: append precisely one
  # X_INTERFACE_MODE=monitor endpoint to the established inferred AXIS net.
  connect_bd_intf_net -intf_net $existing $source_if $monitor_if
  set after [get_bd_intf_pins -quiet -of_objects $existing]
  foreach endpoint $before {
    if {[lsearch -exact $after $endpoint] < 0} {
      error "HDMI AXIS monitor replaced established endpoint $endpoint on $existing"
    }
  }
  if {[lsearch -exact $after $monitor_if] < 0 || [llength $after] != [expr {[llength $before] + 1}]} {
    error "HDMI AXIS monitor did not append exactly one endpoint: net=$existing before=$before after=$after"
  }
  puts "HDMI_AXIS_MONITOR_AUDIT_PASS source=$source monitor=$monitor/S_AXIS net=$existing endpoints=$after"
}

proc add_hdmi_presentation_observer {{base 0x44A20000}} {
  set root $::hdmi_presentation_observer_root
  set rtl [file join $root pl rtl hdmi_presentation_observer.v]
  set axis_monitor_rtl [file join $root pl rtl rf_axis_status_monitor.v]
  foreach rtl_path [list $rtl $axis_monitor_rtl] {
    if {![file exists $rtl_path]} {error "Missing observer integration RTL: $rtl_path"}
  }
  if {[llength [get_bd_cells -quiet hdmi_presentation_observer]]} {error "observer cell already exists"}
  foreach cell {ps7 reset_50m control_xbar interrupt_concat axi_hdmi_dma axi_hdmi_core} {
    if {![llength [get_bd_cells -quiet $cell]]} {error "Required HDMI integration cell absent: $cell"}
  }
  foreach rtl_path [list $rtl $axis_monitor_rtl] {
    if {![llength [get_files -quiet $rtl_path]]} {add_files -norecurse $rtl_path}
  }
  set xdc [file join $root pl constraints hdmi_presentation_observer_cdc.xdc]
  if {![file exists $xdc]} {error "Missing observer CDC constraints: $xdc"}
  if {![llength [get_files -quiet $xdc]]} {add_files -fileset constrs_1 -norecurse $xdc}
  # This constraint targets synthesized hierarchy.  Applying it during
  # synthesis finds no stable synthesized cells and, more importantly, the
  # old flow marked it `used_in_implementation false`, so the actual route
  # never inherited the exception.  Keep it for implementation; the explicit
  # post-synthesis audit below reads the same immutable file when needed.
  set_property used_in_synthesis false [get_files $xdc]
  set_property used_in_implementation true [get_files $xdc]
  update_compile_order -fileset sources_1
  create_bd_cell -type module -reference hdmi_presentation_observer hdmi_presentation_observer
  create_bd_cell -type module -reference rf_axis_status_monitor hdmi_presentation_axis_monitor

  # AXI-Lite: reserve exactly one new control_xbar MI.  This is intentionally
  # a derived-design mutation, never a live RF-build mutation.
  set old_mi [get_property CONFIG.NUM_MI [get_bd_cells control_xbar]]
  if {$old_mi eq ""} {error "control_xbar has no CONFIG.NUM_MI"}
  set new_mi $old_mi
  set_property CONFIG.NUM_MI [expr {$old_mi + 1}] [get_bd_cells control_xbar]
  set mi [format "M%02d_AXI" $new_mi]
  foreach pin [list "control_xbar/$mi" "control_xbar/M[format %02d $new_mi]_ACLK" "control_xbar/M[format %02d $new_mi]_ARESETN"] {
    if {![llength [get_bd_pins -quiet $pin]] && ![llength [get_bd_intf_pins -quiet $pin]]} {error "control_xbar expansion missing $pin"}
  }
  connect_bd_intf_net [get_bd_intf_pins control_xbar/$mi] [get_bd_intf_pins hdmi_presentation_observer/S_AXI]
  connect_bd_net -net [hdmi_presentation_require_one "observer xbar clock net" [get_bd_nets -quiet -of_objects [get_bd_pins ps7/FCLK_CLK0]]] [get_bd_pins "control_xbar/M[format %02d $new_mi]_ACLK"]
  connect_bd_net -net [hdmi_presentation_require_one "observer xbar reset net" [get_bd_nets -quiet -of_objects [get_bd_pins reset_50m/peripheral_aresetn]]] [get_bd_pins "control_xbar/M[format %02d $new_mi]_ARESETN"]
  connect_bd_net -net [hdmi_presentation_require_one "observer FCLK net" [get_bd_nets -quiet -of_objects [get_bd_pins ps7/FCLK_CLK0]]] [get_bd_pins hdmi_presentation_observer/s_axi_aclk]
  connect_bd_net -net [hdmi_presentation_require_one "observer reset net" [get_bd_nets -quiet -of_objects [get_bd_pins reset_50m/peripheral_aresetn]]] [get_bd_pins hdmi_presentation_observer/s_axi_aresetn]

  hdmi_presentation_axis_monitor_attach axi_hdmi_dma/m_axis hdmi_presentation_axis_monitor
  connect_bd_net [get_bd_pins hdmi_presentation_axis_monitor/observed_tvalid] [get_bd_pins hdmi_presentation_observer/axis_tvalid]
  connect_bd_net [get_bd_pins hdmi_presentation_axis_monitor/observed_tready] [get_bd_pins hdmi_presentation_observer/axis_tready]
  connect_bd_net [get_bd_pins hdmi_presentation_axis_monitor/observed_tlast]  [get_bd_pins hdmi_presentation_observer/axis_tlast]
  set vsync_net [hdmi_presentation_require_one "HDMI VSYNC monitor net" [get_bd_nets -quiet -of_objects [get_bd_pins axi_hdmi_core/hdmi_16_vsync]]]
  connect_bd_net -net $vsync_net [get_bd_pins hdmi_presentation_observer/hdmi_vsync_async]

  # F2P[11] is stable after HDMI=6/I2C=7/RXTH=8/IMBC=9,10.  It maps to Linux
  # GIC SPI 55 for this Zynq interrupt topology.  Require a real unused net;
  # silently replacing a live RF interrupt is prohibited.
  set irq_pin [get_bd_pins interrupt_concat/In11]
  set irq_net [hdmi_presentation_require_one "F2P[11] existing net" [get_bd_nets -quiet -of_objects $irq_pin]]
  set irq_drivers [get_bd_pins -quiet -of_objects $irq_net -filter {DIR == O}]
  if {[llength $irq_drivers] != 1 || ![string match "*/constant_zero/dout" [lindex $irq_drivers 0]]} {
    error "F2P[11] is not the required unused constant-zero slot: drivers=$irq_drivers"
  }
  disconnect_bd_net $irq_net $irq_pin
  # The constant-zero net can fan out to other unused F2P slots.  Reusing it
  # here would create a multiple-driver net, not replace In11's source.
  set observer_irq_net [create_bd_net hdmi_presentation_observer_irq]
  connect_bd_net -net $observer_irq_net [get_bd_pins hdmi_presentation_observer/irq] $irq_pin
  set observer_irq_drivers [get_bd_pins -quiet -of_objects $observer_irq_net -filter {DIR == O}]
  set observer_irq_sinks [get_bd_pins -quiet -of_objects $observer_irq_net -filter {DIR == I}]
  if {[llength $observer_irq_drivers] != 1 || ![string match "*/hdmi_presentation_observer/irq" [lindex $observer_irq_drivers 0]] ||
      [llength $observer_irq_sinks] != 1 || ![string match "*/interrupt_concat/In11" [lindex $observer_irq_sinks 0]]} {
    error "observer IRQ net must contain exactly observer->In11: drivers=$observer_irq_drivers sinks=$observer_irq_sinks"
  }

  set cpu_space [hdmi_presentation_require_one "PS data address space" [get_bd_addr_spaces ps7/Data]]
  set seg [hdmi_presentation_require_one "observer AXI segment" [get_bd_addr_segs -of_objects [get_bd_intf_pins hdmi_presentation_observer/S_AXI]]]
  assign_bd_address -target_address_space $cpu_space -offset $base -range 64K $seg

  validate_bd_design
  puts "HDMI_PRESENTATION_OBSERVER_ATTACHED base=$base f2p=11 gic_spi=55"
}

# Call only after synth_design/open_run synth_1.  BD construction has no
# synthesized cells, so querying get_cells or applying false paths there is
# invalid.  The XDC owns the CDC exception; this audits that it resolved.
#
# `get_timing_paths -to` deliberately retains a topological object even when
# that object is false-pathed (Vivado 2025.2 reports Slack=inf and
# "Timing Exception: False Path").  Therefore an empty collection is *not*
# the correct acceptance criterion.  The audit below proves the exception
# itself, its exact endpoints, both setup/hold coverage, and that neither
# synchronous stage of the three-flop chain was accidentally cut.
proc hdmi_presentation_observer_require_synced_stage {from_cell to_cell report_dir tag} {
  set from_clock [get_clocks -quiet -of_objects [get_pins -quiet -of_objects $from_cell -filter {REF_PIN_NAME == C}]]
  set to_clock [get_clocks -quiet -of_objects [get_pins -quiet -of_objects $to_cell -filter {REF_PIN_NAME == C}]]
  if {[llength $from_clock] != 1 || [llength $to_clock] != 1 || [lindex $from_clock 0] ne [lindex $to_clock 0]} {
    error "Observer synchronizer stage lacks one common propagated clock: $from_cell=$from_clock $to_cell=$to_clock"
  }
  set timing_report [file join $report_dir "hdmi_pipeline_observer_${tag}_timing.rpt"]
  set exception_report [file join $report_dir "hdmi_pipeline_observer_${tag}_exceptions.rpt"]
  report_timing -from $from_cell -to $to_cell -max_paths 2 -file $timing_report
  report_exceptions -from $from_cell -to $to_cell -file $exception_report
  set fd [open $timing_report r]; set timing_text [read $fd]; close $fd
  set fd [open $exception_report r]; set exception_text [read $fd]; close $fd
  if {![regexp {Slack \(MET\)} $timing_text] || ![regexp {Path Group:[[:space:]]+clk_fpga_0} $timing_text]} {
    error "Observer synchronizer stage is not actively timed by clk_fpga_0: $from_cell -> $to_cell"
  }
  if {[regexp {Timing Exception:[[:space:]]+False Path} $timing_text] || ![regexp {No valid timing exceptions found\.} $exception_text]} {
    error "Observer synchronizer stage has an unintended timing exception: $from_cell -> $to_cell"
  }
}

proc audit_hdmi_presentation_observer_post_synth {report_dir} {
  if {![file isdirectory $report_dir]} {error "Observer audit report directory does not exist: $report_dir"}
  set xdc [get_files -quiet *hdmi_presentation_observer_cdc.xdc]
  if {[llength $xdc] != 1} {error "Expected exactly one loaded observer CDC XDC, got $xdc"}
  # XDC is implementation-only because it selects synthesized cells.  Apply
  # the exact same file now, after synthesis, so this audit proves the real
  # first-stage exception rather than relying on a pre-elaboration query.
  read_xdc [lindex $xdc 0]
  set sync_cells [get_cells -hier -quiet -filter {NAME =~ *hdmi_presentation_observer*/vsync_m1_reg || NAME =~ *hdmi_presentation_observer*/vsync_m2_reg || NAME =~ *hdmi_presentation_observer*/vsync_m3_reg}]
  if {[llength $sync_cells] != 3} {error "Expected three synthesized VSYNC synchronizer flops, got $sync_cells"}
  set first_sync [get_cells -hier -quiet -filter {NAME =~ *hdmi_presentation_observer*/vsync_m1_reg}]
  if {[llength $first_sync] != 1} {error "Expected one synthesized VSYNC first-stage flop, got $first_sync"}
  set first_d [get_pins -quiet -of_objects $first_sync -filter {REF_PIN_NAME == D}]
  if {[llength $first_d] != 1} {error "Expected one synthesized VSYNC first-stage D pin, got $first_d"}
  foreach sync_cell $sync_cells {
    if {[get_property ASYNC_REG $sync_cell] ni {1 TRUE}} {error "Observer synchronizer lacks ASYNC_REG: $sync_cell"}
  }
  set first_paths [get_timing_paths -quiet -to $first_d -max_paths 2]
  if {[llength $first_paths] != 1} {error "Observer first stage must expose exactly one monitored async path, got $first_paths"}
  set first_path [lindex $first_paths 0]
  set first_start [get_property STARTPOINT_PIN $first_path]
  set first_end [get_property ENDPOINT_PIN $first_path]
  if {![string match "*/axi_hdmi_core/inst/i_tx_core/hdmi_16_vsync_reg/C" $first_start] || $first_end ne $first_d} {
    error "Observer first-stage false path has unexpected endpoints: start=$first_start end=$first_end"
  }
  set first_timing_report [file join $report_dir hdmi_pipeline_observer_first_stage_timing.rpt]
  set first_exception_report [file join $report_dir hdmi_pipeline_observer_first_stage_exceptions.rpt]
  report_timing -to $first_d -max_paths 2 -file $first_timing_report
  report_exceptions -to $first_d -file $first_exception_report
  set fd [open $first_timing_report r]; set first_timing_text [read $fd]; close $fd
  set fd [open $first_exception_report r]; set first_exception_text [read $fd]; close $fd
  if {![regexp {Timing Exception:[[:space:]]+False Path} $first_timing_text]} {
    error "Observer async first stage lacks an effective false-path exception"
  }
  if {[regexp {No valid timing exceptions found\.} $first_exception_text]} {
    error "Observer async first stage has no timing exception"
  }
  set false_rows [regexp -all -line {^[[:space:]]*[0-9]+.*[[:space:]]false[[:space:]]+false[[:space:]]*$} $first_exception_text]
  if {$false_rows != 1} {error "Observer async first stage must have one exact setup+hold false path, got $false_rows"}
  set m2 [hdmi_presentation_require_one "observer m2 cell" [get_cells -hier -quiet -filter {NAME =~ *hdmi_presentation_observer*/vsync_m2_reg}]]
  set m3 [hdmi_presentation_require_one "observer m3 cell" [get_cells -hier -quiet -filter {NAME =~ *hdmi_presentation_observer*/vsync_m3_reg}]]
  hdmi_presentation_observer_require_synced_stage $first_sync $m2 $report_dir m1_to_m2
  hdmi_presentation_observer_require_synced_stage $m2 $m3 $report_dir m2_to_m3
  puts "HDMI_PRESENTATION_OBSERVER_POST_SYNTH_AUDIT_PASS"
}
