# Post-synthesis/implementation contract for hdmi_gray_pointer_cdc.xdc.
# This is ordinary Tcl, intentionally not an XDC: it may use control flow to
# reject renamed FFs, changed clock domains, or broken two-FF synchronizers.

namespace eval receiver_v2_gray_contract {}

proc receiver_v2_gray_contract::one_cell {name} {
  set cells [get_cells -quiet -hier -filter "NAME == $name"]
  if {[llength $cells] != 1} { error "RXV2 HDMI Gray endpoint mismatch: $name -> $cells" }
  return [lindex $cells 0]
}

proc receiver_v2_gray_contract::one_clock {cell expected_name expected_period} {
  set pin [get_pins -quiet -of_objects $cell -filter {REF_PIN_NAME == C}]
  set clocks [get_clocks -quiet -of_objects $pin]
  if {[llength $pin] != 1 || [llength $clocks] != 1 ||
      [get_property NAME $clocks] ne $expected_name ||
      abs([get_property PERIOD $clocks] - $expected_period) > 0.001} {
    error "RXV2 HDMI Gray clock mismatch at $cell: pins=$pin clocks=$clocks"
  }
}

proc receiver_v2_gray_contract::validate {} {
  for {set bit 0} {$bit < 9} {incr bit} {
    set src [one_cell [format {receiver_v2_i/axi_hdmi_core/inst/i_tx_core/hdmi_raddr_g_reg[%d]} $bit]]
    set m1  [one_cell [format {receiver_v2_i/axi_hdmi_core/inst/i_vdma/vdma_raddr_g_m1_reg[%d]} $bit]]
    set m2  [one_cell [format {receiver_v2_i/axi_hdmi_core/inst/i_vdma/vdma_raddr_g_m2_reg[%d]} $bit]]
    # r6 asserts clear asynchronously so reset also works with stopped clocks;
    # those same clocked synchronizer stages map to FDCE rather than FDRE.
    # Both primitives are edge-triggered CE flops with the explicit zero-reset
    # semantics used here; do not admit latches or set-to-one variants.
    if {[get_property REF_NAME $src] ni {FDRE FDCE} ||
        [get_property REF_NAME $m1] ni {FDRE FDCE} || [get_property REF_NAME $m2] ni {FDRE FDCE} ||
        [get_property ASYNC_REG $m1] ni {1 TRUE} || [get_property ASYNC_REG $m2] ni {1 TRUE}} {
      error "RXV2 HDMI Gray synchronizer contract failed at bit $bit"
    }
    one_clock $src mmcm_clk_0_s 6.750
    one_clock $m1 clk_fpga_0 20.000
    one_clock $m2 clk_fpga_0 20.000
    set q [get_pins -quiet -of_objects $m1 -filter {REF_PIN_NAME == Q}]
    set m2d [get_pins -quiet -of_objects $m2 -filter {REF_PIN_NAME == D}]
    set sinks [get_pins -quiet -of_objects [get_nets -quiet -of_objects $q] -filter {DIRECTION == IN}]
    if {[llength $q] != 1 || [llength $m2d] != 1 || [llength $sinks] != 1 ||
        [lindex $sinks 0] ne [lindex $m2d 0]} {
      error "RXV2 HDMI Gray m1 has non-sole m2 fanout at bit $bit: $sinks"
    }
  }
  return 9
}
