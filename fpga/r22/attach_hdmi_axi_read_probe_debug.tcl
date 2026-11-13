# SPDX-License-Identifier: MIT
#
# Attach a passive HDMI DMAC AXI-read probe to a *derived debug* receiver_v2
# block design.  The HPO module reference must already be the copied-and-
# patched debug source described in ../patches/hdmi_axi_read_probe_debug.
#
# This script neither changes an AXI/AXIS producer nor adds an AXI-Lite slave:
# the debug registers live inside the existing HPO aperture (0x44a20000).

proc hdmi_axi_probe_require_one {description objects} {
  if {[llength $objects] != 1} {
    error "$description: expected exactly one object, got [llength $objects]: $objects"
  }
  return [lindex $objects 0]
}

proc hdmi_axi_probe_pin {name} {
  return [hdmi_axi_probe_require_one "required BD pin $name" [get_bd_pins -quiet $name]]
}

proc hdmi_axi_probe_net_for {source_name} {
  set source [hdmi_axi_probe_pin $source_name]
  return [hdmi_axi_probe_require_one "existing source net for $source_name" \
    [get_bd_nets -quiet -of_objects $source]]
}

proc hdmi_axi_probe_connect_passive {source_name sink_name} {
  set source [hdmi_axi_probe_pin $source_name]
  set sink [hdmi_axi_probe_pin $sink_name]
  set net [hdmi_axi_probe_net_for $source_name]
  set before [get_bd_pins -quiet -of_objects $net]
  if {[lsearch -exact $before $source] < 0} {
    error "HDMI probe source $source_name is not on its discovered net $net"
  }
  if {[llength [get_bd_nets -quiet -of_objects $sink]] != 0} {
    error "HDMI probe sink $sink_name is already connected; refusing to replace a net"
  }
  connect_bd_net -net $net $sink
  set after [get_bd_pins -quiet -of_objects $net]
  if {[lsearch -exact $after $sink] < 0 || [llength $after] != [expr {[llength $before] + 1}]} {
    error "HDMI probe did not append exactly one passive sink: $source_name -> $sink_name"
  }
  puts "HDMI_AXI_PROBE_TAP_PASS source=$source_name sink=$sink_name net=$net"
}

proc attach_hdmi_axi_read_probe_debug {} {
  set hpo [hdmi_axi_probe_require_one "debug HPO cell" [get_bd_cells -quiet hdmi_presentation_observer]]
  set dmac [hdmi_axi_probe_require_one "HDMI DMAC cell" [get_bd_cells -quiet axi_hdmi_dma]]

  # The copied HPO preserves its original module/instance name and only adds
  # inputs.  This keeps existing CDC selectors such as
  # hdmi_presentation_observer/vsync_m*_reg valid.
  # The debug BD is rebuilt from its copied source tree, so module-reference
  # ports are resolved at cell creation.  Do not call update_module_reference:
  # Vivado can return an empty error while reopening a moved XPR even though
  # the regenerated module already exposes the requested pins.
  foreach pin {probe_arvalid probe_arready probe_araddr probe_rvalid probe_rready probe_rlast probe_rresp} {
    hdmi_axi_probe_pin "hdmi_presentation_observer/$pin"
  }
  create_bd_cell -type module -reference hdmi_axi_read_bus_monitor hdmi_axi_read_monitor
  set source [hdmi_axi_probe_require_one "DMA read interface" [get_bd_intf_pins axi_hdmi_dma/m_src_axi]]
  set monitor [hdmi_axi_probe_require_one "passive read monitor" [get_bd_intf_pins hdmi_axi_read_monitor/READ_MONITOR]]
  set net [hdmi_axi_probe_require_one "existing DMA read interface net" [get_bd_intf_nets -of_objects $source]]
  set before [get_bd_intf_pins -of_objects $net]
  connect_bd_intf_net -intf_net $net $source $monitor
  set after [get_bd_intf_pins -of_objects $net]
  if {[llength $after] != [expr {[llength $before]+1}]} {error "monitor must append one interface endpoint"}
  foreach endpoint $before {
    if {[lsearch -exact $after $endpoint] < 0} {error "monitor removed existing endpoint $endpoint"}
  }
  connect_bd_net -net [hdmi_axi_probe_net_for ps7/FCLK_CLK0] [hdmi_axi_probe_pin hdmi_axi_read_monitor/aclk]
  foreach name {arvalid arready araddr rvalid rready rlast rresp} {
    connect_bd_net [hdmi_axi_probe_pin hdmi_axi_read_monitor/observed_$name] \
      [hdmi_axi_probe_pin hdmi_presentation_observer/probe_$name]
  }
  validate_bd_design
  puts "HDMI_AXI_PROBE_BD_VALIDATE_PASS hpo=$hpo dmac=$dmac"
}
