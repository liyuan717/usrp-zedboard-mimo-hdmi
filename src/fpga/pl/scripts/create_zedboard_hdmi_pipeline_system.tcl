# SPDX-License-Identifier: MIT
# Independent HDMI + RXTH/MHGD derived candidate.  Never source this from the
# frozen RF builder; it first invokes the reviewed continuous topology in
# validate mode, then keeps that project open for the HDMI/HP1 mutation.
set hdmi_pipeline_root [file normalize [file join [file dirname [info script]] ../..]]
if {$argc != 2} {error "usage: <fresh-output> validate|synth|implement"}
set hdmi_pipeline_out [file normalize [lindex $argv 0]]
set hdmi_pipeline_action [lindex $argv 1]
if {$hdmi_pipeline_action ni {validate synth implement}} {error "invalid action"}
# Bootstrap owns this parent and writes its frozen manifest there.  The base
# builder receives a fresh child so it never mistakes the manifest for output.
if {![file isdirectory $hdmi_pipeline_out]} {error "bootstrap directory missing: $hdmi_pipeline_out"}
set hdmi_pipeline_vivado_out [file join $hdmi_pipeline_out vivado]
if {[file exists $hdmi_pipeline_vivado_out]} {error "Vivado child must be fresh: $hdmi_pipeline_vivado_out"}
set ::env(MIMO_CONTINUOUS_VIDEO_HEALTH_ALLOW) YES
# Preserve caller intent and force the imported builder to stop after its
# validated BD/project creation; no text/eval extraction of its source.
set saved_argv $argv; set saved_argc $argc
set argv [list $hdmi_pipeline_vivado_out validate]; set argc 2
source [file join $hdmi_pipeline_root pl scripts create_zedboard_continuous_video_health_system.tcl]
set argv $saved_argv; set argc $saved_argc

source [file join $hdmi_pipeline_root pl scripts add_hdmi_presentation_observer.tcl]
source [file join $hdmi_pipeline_root pl scripts hdmi_pipeline_post_synth_audit.tcl]
add_hdmi_presentation_observer

# HDMI is isolated on HP1.  RXTH/IMBC/RF DMACs remain on HP0.  In the HDMI
# continuous topology, base S06 is the HDMI DMA and the extension reserved
# S07..S10 for RXTH-dest/RXTH-SG/IMBC-MM2S/IMBC-S2MM.  Disconnect all five
# established consumers first, then compact the four HP0 endpoints onto
# S06..S09.  This avoids an unconnected SmartConnect SI and does not retain a
# hidden HDMI-to-HP0 path.
set ps [get_bd_cells ps7]
set_property -dict [list CONFIG.PCW_USE_S_AXI_HP1 {1} CONFIG.PCW_S_AXI_HP1_DATA_WIDTH {64} CONFIG.PCW_FPGA0_PERIPHERAL_FREQMHZ {50}] $ps
create_bd_cell -type ip -vlnv xilinx.com:ip:smartconnect:1.0 hdmi_hp1_smc
set_property -dict [list CONFIG.NUM_SI {1} CONFIG.NUM_MI {1}] [get_bd_cells hdmi_hp1_smc]
foreach endpoint {axi_hdmi_dma/m_src_axi rf_rx_transport_dmac/m_dest_axi rf_rx_transport_dmac/m_sg_axi interleaved_candidate_dma/M_AXI_MM2S interleaved_candidate_dma/M_AXI_S2MM} {
  if {[llength [get_bd_intf_pins -quiet $endpoint]] != 1} {error "Expected one HP endpoint: $endpoint"}
}
foreach p {S06_AXI S07_AXI S08_AXI S09_AXI S10_AXI} {
  set pin [get_bd_intf_pins -quiet memory_xbar/$p]
  if {[llength $pin] != 1} {error "Expected one existing memory_xbar port: $p"}
  set n [get_bd_intf_nets -quiet -of_objects $pin]
  if {[llength $n] != 1} {error "Expected one existing memory net at $p, got $n"}
  disconnect_bd_intf_net $n $pin
}
foreach {src dst} {rf_rx_transport_dmac/m_dest_axi S06_AXI rf_rx_transport_dmac/m_sg_axi S07_AXI interleaved_candidate_dma/M_AXI_MM2S S08_AXI interleaved_candidate_dma/M_AXI_S2MM S09_AXI} {
  connect_bd_intf_net [get_bd_intf_pins $src] [get_bd_intf_pins memory_xbar/$dst]
}
set_property CONFIG.NUM_SI {10} [get_bd_cells memory_xbar]
connect_bd_intf_net [get_bd_intf_pins axi_hdmi_dma/m_src_axi] [get_bd_intf_pins hdmi_hp1_smc/S00_AXI]
connect_bd_intf_net [get_bd_intf_pins hdmi_hp1_smc/M00_AXI] [get_bd_intf_pins ps7/S_AXI_HP1]
set hpclk [get_bd_nets -quiet -of_objects [get_bd_pins ps7/FCLK_CLK0]]
set hprst [get_bd_nets -quiet -of_objects [get_bd_pins reset_50m/peripheral_aresetn]]
if {[llength $hpclk] != 1 || [llength $hprst] != 1} {error "HP1 clock/reset net lookup failed"}
connect_bd_net -net $hpclk [get_bd_pins ps7/S_AXI_HP1_ACLK] [get_bd_pins hdmi_hp1_smc/aclk]
connect_bd_net -net $hprst [get_bd_pins hdmi_hp1_smc/aresetn]
set old_hdmi [get_bd_addr_segs -quiet -of_objects [get_bd_addr_spaces axi_hdmi_dma/m_src_axi]]
if {![llength $old_hdmi]} {error "HDMI DMA has no pre-existing HP0 address segment to migrate"}
set hp1_seg [get_bd_addr_segs -quiet ps7/S_AXI_HP1/HP1_DDR_LOWOCM]
if {[llength $hp1_seg] != 1} {error "Expected exactly one PS HP1 DDR segment, got $hp1_seg"}
# Address assignment is keyed by the initiating master address space.  The
# collection below is therefore the *master* mapping segment owned by this
# HDMI initiator, not PS7's shared HP0 slave segment.  Remove only that stale
# master map before assigning the reachable HP1 segment; there is no
# `unassign_bd_address` Tcl command.
foreach old $old_hdmi {
  if {![string match "*/axi_hdmi_dma/m_src_axi/*" $old]} {
    error "Refusing to delete a non-HDMI master address segment: $old"
  }
}
delete_bd_objs $old_hdmi
assign_bd_address -target_address_space [get_bd_addr_spaces axi_hdmi_dma/m_src_axi] -offset 0 -range 512M $hp1_seg
validate_bd_design
save_bd_design
set hdmi_pipeline_top_bd [get_files -quiet */zedboard_rf_system.bd]
if {[llength $hdmi_pipeline_top_bd] != 1} {
  error "Expected exactly one top-level zedboard_rf_system.bd, got $hdmi_pipeline_top_bd"
}
generate_target synthesis $hdmi_pipeline_top_bd
make_wrapper -files $hdmi_pipeline_top_bd -top
update_compile_order -fileset sources_1
if {$hdmi_pipeline_action eq "validate"} {puts "HDMI_PIPELINE_VALIDATE_PASS hp1=64 rf_hp0=preserved"; return}
source [file join $hdmi_pipeline_root pl scripts rf_signoff_checks.tcl]
source [file join $hdmi_pipeline_root pl scripts hdmi_pipeline_release_gate.tcl]

# This derived project cannot use the frozen RF provenance names: it adds an
# observer, AXIS monitor, HP1 SmartConnect, and a different wrapper.  Capture
# the *actual* Vivado compile/constraint closure after all those mutations and
# before launch_runs.  A later verify prevents a bit/XSA from being emitted
# after any input change.
proc hdmi_pipeline_capture_inputs {} {
  global hdmi_pipeline_out hdmi_pipeline_root
  if {![info exists ::env(MIMO_RF_BUILD_PYTHON)] || ![file isfile $::env(MIMO_RF_BUILD_PYTHON)]} {
    error "MIMO_RF_BUILD_PYTHON must identify the pinned provenance interpreter"
  }
  set list_path [file join $hdmi_pipeline_out hdmi_pipeline_actual_inputs.tsv]
  set fd [open $list_path w]
  fconfigure $fd -encoding utf-8 -translation lf
  foreach input [get_files -compile_order sources -used_in synthesis -of_objects [get_filesets sources_1]] {
    puts $fd "synthesis\t[file normalize $input]"
  }
  foreach input [get_files -all -filter {FILE_TYPE == XDC}] {
    puts $fd "constraint\t[file normalize $input]"
  }
  foreach input [get_files -all [list *.bd *.xci]] {
    puts $fd "design\t[file normalize $input]"
  }
  foreach recipe {create_zedboard_hdmi_pipeline_system.tcl bootstrap_hdmi_pipeline_candidate.py run_hdmi_pipeline_candidate.ps1 hdmi_pipeline_provenance.py} {
    puts $fd "recipe\t[file join $hdmi_pipeline_root pl scripts $recipe]"
  }
  puts $fd "recipe\t[file join $hdmi_pipeline_out hdmi_pipeline_bootstrap.json]"
  close $fd
  puts [exec $::env(MIMO_RF_BUILD_PYTHON) -I [file join $hdmi_pipeline_root pl scripts hdmi_pipeline_provenance.py] capture --build $hdmi_pipeline_out --input-list $list_path]
}

proc hdmi_pipeline_assert_run {run description} {
  set candidate [get_runs $run]
  if {[llength $candidate] != 1} {error "Missing $description run: $run"}
  set status [get_property STATUS $candidate]
  if {[string match -nocase "*error*" $status] || [string match -nocase "*fail*" $status]} {
    error "$description failed: status=$status"
  }
}

hdmi_pipeline_capture_inputs
launch_runs synth_1 -jobs 1
wait_on_run synth_1
hdmi_pipeline_assert_run synth_1 synthesis
if {[get_property PROGRESS [get_runs synth_1]] ne "100%"} {error "HDMI pipeline synthesis did not complete"}
open_run synth_1
if {[llength [get_cells -hier -quiet -filter {IS_BLACKBOX == 1}]]} {error "HDMI pipeline synthesis left black boxes"}
hdmi_pipeline_audit_observer_source_and_synth
report_utilization -file [file join $hdmi_pipeline_out hdmi_pipeline_synthesis_utilization.rpt]
report_timing_summary -report_unconstrained -file [file join $hdmi_pipeline_out hdmi_pipeline_synthesis_timing.rpt]
report_drc -file [file join $hdmi_pipeline_out hdmi_pipeline_synthesis_drc.rpt]
puts "HDMI_PIPELINE_SYNTHESIS_PASS"
if {$hdmi_pipeline_action eq "synth"} {return}

set_property strategy Performance_ExplorePostRoutePhysOpt [get_runs impl_1]
launch_runs impl_1 -to_step route_design -jobs 1
wait_on_run impl_1
hdmi_pipeline_assert_run impl_1 route_design
set impl_dir [get_property DIRECTORY [get_runs impl_1]]
set routed_dcp [file join $impl_dir zedboard_rf_system_wrapper_routed.dcp]
if {![file exists $routed_dcp]} {error "HDMI pipeline route_design produced no routed checkpoint: $routed_dcp"}
open_checkpoint $routed_dcp
phys_opt_design -directive ExploreWithAggressiveHoldFix
route_design
write_checkpoint -force [file join $hdmi_pipeline_out hdmi_pipeline_signoff_route.dcp]
hdmi_pipeline_gate_routed
puts [exec $::env(MIMO_RF_BUILD_PYTHON) -I [file join $hdmi_pipeline_root pl scripts hdmi_pipeline_provenance.py] verify --build $hdmi_pipeline_out]
write_bitstream -force [file join $hdmi_pipeline_out hdmi_pipeline.bit]
write_hw_platform -fixed -include_bit -force -file [file join $hdmi_pipeline_out hdmi_pipeline.xsa]
puts [exec $::env(MIMO_RF_BUILD_PYTHON) -I [file join $hdmi_pipeline_root pl scripts hdmi_pipeline_provenance.py] seal --build $hdmi_pipeline_out]
puts "HDMI_PIPELINE_IMPLEMENT_PASS offline_only=1 hardware_acceptance=PENDING"
