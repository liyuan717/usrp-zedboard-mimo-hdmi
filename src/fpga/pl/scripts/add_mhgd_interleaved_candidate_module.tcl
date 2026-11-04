# Vivado module-reference helper for the isolated IMBC AXI-Lite/AXIS core.
set ::mimo_interleaved_repo_root [file normalize [file join [file dirname [info script]] ../..]]
proc mimo_add_interleaved_candidate_module {{instance mimo_interleaved_candidate_0}} {
  global mimo_interleaved_repo_root
  if {[llength [get_bd_cells -quiet $instance]]} {error "BD cell already exists: $instance"}
  foreach name {mimo_cost_2x2 mimo_mhgd_interleaved_candidate mimo_axis_frame_tx mimo_mhgd_interleaved_axis_candidate mimo_mhgd_interleaved_axis_candidate_axil} {
    set path [file join $mimo_interleaved_repo_root pl rtl ${name}.sv]
    if {![file isfile $path]} {error "Missing interleaved candidate RTL: $path"}
    if {![llength [get_files -quiet $path]]} {add_files -norecurse $path}
    set_property file_type SystemVerilog [get_files $path]
  }
  set wrapper [file join $mimo_interleaved_repo_root pl rtl mimo_mhgd_interleaved_candidate_bd.v]
  if {![llength [get_files -quiet $wrapper]]} {add_files -norecurse $wrapper}
  set_property file_type Verilog [get_files $wrapper]
  update_compile_order -fileset sources_1
  set cell [create_bd_cell -type module -reference mimo_mhgd_interleaved_candidate_bd $instance]
  set_property -dict {CONFIG.AXIL_ADDR_WIDTH 6 CONFIG.BUILD_ID 1} $cell
  foreach bus {s_axi s_axis m_axis} {
    if {[llength [get_bd_intf_pins -quiet $instance/$bus]] != 1} {error "Interleaved candidate interface was not inferred: $instance/$bus"}
  }
  return $cell
}
