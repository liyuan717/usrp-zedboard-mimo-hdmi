# Source this file in an open Vivado Block Design, then call:
#   mimo_add_detector_module detector_0
# Returns a BD module-reference cell; no PS/DMA or RF ports are created.
set mimo_detector_repo_root [file normalize [file join [file dirname [info script]] ../..]]
proc mimo_add_detector_module {{instance detector_0}} {
    global mimo_detector_repo_root
    if {[current_bd_design -quiet] eq ""} {error "Open or create a Block Design first"}
    if {[llength [get_bd_cells -quiet $instance]]} {error "BD cell already exists: $instance"}
    foreach name {mimo_lcg_rng mimo_qam_stepper mimo_gradient_2x2 mimo_cost_2x2 mimo_axis_frame_rx mimo_mhgd_kernel_2x2 mimo_axis_frame_tx mimo_axi_lite_regs mimo_mhgd_axis_top} {
        set path [file join $mimo_detector_repo_root pl rtl ${name}.sv]
        if {![file exists $path]} {error "Missing Detector RTL: $path"}
        if {![llength [get_files -quiet $path]]} {add_files -norecurse $path}
        set_property file_type SystemVerilog [get_files $path]
    }
    update_compile_order -fileset sources_1
    set wrapper [file join $mimo_detector_repo_root pl rtl mimo_detector_bd.v]
    if {![llength [get_files -quiet $wrapper]]} {add_files -norecurse $wrapper}
    set_property file_type Verilog [get_files $wrapper]
    update_compile_order -fileset sources_1
    set cell [create_bd_cell -type module -reference mimo_detector_bd $instance]
    set_property -dict {CONFIG.AXIL_ADDR_WIDTH 6 CONFIG.BUILD_ID 1} $cell
    foreach bus {s_axi s_axis m_axis} {
        if {[llength [get_bd_intf_pins -quiet $instance/$bus]] != 1} {
            error "Detector interface was not inferred: $instance/$bus"
        }
    }
    return $cell
}
