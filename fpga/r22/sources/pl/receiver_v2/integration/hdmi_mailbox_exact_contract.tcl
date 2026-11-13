# Post-netlist contract for hdmi_mailbox_exact_cdc.xdc.
# Source this as ordinary Tcl (not as XDC) after the production helper has
# been read.  The phase argument is one of post_synth, post_opt, post_route.
# Only post_route queries timing paths; neither the payload mailbox protocol
# nor any synchronizer/fanout rule is inferred or changed here.

namespace eval receiver_v2_mailbox_contract {}

proc receiver_v2_mailbox_contract::finite {value label} {
  if {![string is double -strict $value] || [regexp -nocase {nan|inf} $value]} {
    error "RXV2 HDMI mailbox nonfinite $label: $value"
  }
  return $value
}

proc receiver_v2_mailbox_contract::one_cell {name} {
  set cells [get_cells -quiet -hier -filter "NAME == $name"]
  if {[llength $cells] != 1} {
    error "RXV2 HDMI mailbox endpoint mismatch: $name -> $cells"
  }
  return [lindex $cells 0]
}

proc receiver_v2_mailbox_contract::one_clock {cell expected_name expected_period} {
  set pins [get_pins -quiet -of_objects $cell -filter {REF_PIN_NAME == C}]
  set clocks [get_clocks -quiet -of_objects $pins]
  if {[llength $pins] != 1 || [llength $clocks] != 1 ||
      [get_property NAME $clocks] ne $expected_name ||
      abs([get_property PERIOD $clocks] - $expected_period) > 0.001} {
    error "RXV2 HDMI mailbox clock mismatch at $cell: pins=$pins clocks=$clocks"
  }
}

proc receiver_v2_mailbox_contract::validate {phase} {
  if {$phase ni {post_synth post_opt post_route}} {
    error "RXV2 HDMI mailbox contract unknown phase: $phase"
  }
  set sources {}
  set destinations {}
  for {set bit 0} {$bit < 9} {incr bit} {
    set source [one_cell [format {receiver_v2_i/axi_hdmi_core/inst/i_frame_mailbox/src_payload_hold_reg[%d]} $bit]]
    set destination [one_cell [format {receiver_v2_i/axi_hdmi_core/inst/i_frame_mailbox/dst_addr_reg[%d]} $bit]]
    if {[get_property REF_NAME $source] ne "FDRE" || [get_property REF_NAME $destination] ne "FDRE"} {
      error "RXV2 HDMI mailbox requires FDRE endpoints at bit $bit"
    }
    one_clock $source clk_fpga_0 20.000
    one_clock $destination mmcm_clk_0_s 6.750
    lappend sources $source
    lappend destinations $destination
  }

  if {$phase ne "post_route"} {
    return [dict create phase $phase endpoints 9]
  }

  set pair_evidence [dict create]
  for {set bit 0} {$bit < 9} {incr bit} {
    set source [lindex $sources $bit]
    set destination [lindex $destinations $bit]
    set path [get_timing_paths -from $source -to $destination -delay_type max -max_paths 1]
    if {[llength $path] != 1} {
      error "RXV2 HDMI mailbox missing ordinary max path at bit $bit"
    }
    set requirement [finite [get_property REQUIREMENT $path] requirement]
    set slack [finite [get_property SLACK $path] slack]
    set data_delay [finite [get_property DATAPATH_DELAY $path] data_delay]
    if {![string is double -strict $requirement] || abs($requirement - 1.500) > 0.001 ||
        ![string is double -strict $slack] || $slack < 0 ||
        ![string is double -strict $data_delay] || $data_delay < 0} {
      error "RXV2 HDMI mailbox exact constraint/path failed at bit $bit: requirement=$requirement slack=$slack data_delay=$data_delay"
    }
    dict set pair_evidence $bit [dict create requirement_ns $requirement slack_ns $slack data_delay_ns $data_delay]
  }
  return [dict create phase $phase endpoints 9 pairs $pair_evidence]
}
