# SPDX-License-Identifier: Apache-2.0
#
# Continuous-TX/RX + HDMI diagnostic candidate.
#
# This is deliberately distinct from the finite-TX video bring-up image.  It
# leaves finite ownership out of the block design and retains the standard ADI
# cyclic TX DMAC path:
#   PS-owned TX ring -> rf_tx_dmac -> util_upack2 -> util_rfifo -> AD9361 DAC.
# RX keeps the ordinary cyclic ADI S2MM DMA for the established IIO scan ABI,
# while a second cyclic ADI S2MM DMA owns a true RXTH capture branch.  That
# branch has its own buffered FIFO, exact metadata record FIFO, MSB12 adapter,
# and framer; RXTH headers therefore never enter ordinary IIO samples. Linux
# must own both descriptor/ring lifecycles; a PL CYCLIC setting alone does not
# create a video producer or a safe buffer handoff.
#
# Usage (fresh output directory only):
#   $env:MIMO_CONTINUOUS_VIDEO_HEALTH_ALLOW='YES'
#   vivado -mode batch -source create_zedboard_continuous_video_health_system.tcl \
#       -tclargs <fresh-output> validate|synth|implement
#
# `implement` is inherited from the base script and remains fail-closed on
# timing/CDC/physical budgets.  It must not be used as a board-success claim.

set continuous_root [file normalize [file join [file dirname [info script]] ../..]]
if {![info exists ::env(MIMO_CONTINUOUS_VIDEO_HEALTH_ALLOW)] ||
    $::env(MIMO_CONTINUOUS_VIDEO_HEALTH_ALLOW) ne "YES"} {
  error "Set MIMO_CONTINUOUS_VIDEO_HEALTH_ALLOW=YES to build the separate continuous video-health candidate"
}

set transport_hook [file join $continuous_root pl scripts add_rf_rx_continuous_transport.tcl]
if {![file isfile $transport_hook]} {
  error "Missing RXTH continuous integration hook: $transport_hook"
}
if {![info exists ::env(ADI_HDL_DIR)]} {
  error "ADI_HDL_DIR must name the isolated, candidate-patched ADI HDL tree"
}
set continuous_adi_root [file normalize $::env(ADI_HDL_DIR)]
set continuous_mailbox_component [file join $continuous_adi_root library axi_hdmi_tx component.xml]
set continuous_mmcm_source [file join $continuous_adi_root library xilinx common ad_mmcm_drp.v]
foreach path [list $continuous_mailbox_component $continuous_mmcm_source] {
  if {![file isfile $path]} {error "Continuous video-health candidate missing required vendor input: $path"}
}
set continuous_fd [open $continuous_mailbox_component r]
set continuous_component_text [read $continuous_fd]
close $continuous_fd
if {![regexp {axi_hdmi_tx_frame_mailbox\.v} $continuous_component_text]} {
  error "ADI HDMI component is not the candidate CDC-mailbox package"
}
set continuous_fd [open $continuous_mmcm_source r]
set continuous_mmcm_text [read $continuous_fd]
close $continuous_fd
if {![regexp {parameter\s+MMCM_CLK2_DIV\s*=\s*3\.000} $continuous_mmcm_text]} {
  error "Candidate vendor tree must retain CLKOUT2=/3 to avoid the xc7z020 auxiliary-clock pulse-width violation"
}

# Lock this wrapper to the one reviewed board/RF profile.  All state-changing
# DMAC control remains software-owned; these environment values merely select
# the static topology and cannot activate the transmitter by themselves.
foreach {name value} {
  MIMO_RF_INTERFACE lvds_2r2t_2v5
  MIMO_RF_RX_BUFFERED 1
  MIMO_RF_RX_CYCLIC 1
  MIMO_RF_RX_OBSERVER 1
  MIMO_RF_RX_OBSERVER_ABI 3
  ENABLE_HDMI 1
  DISPLAY_MODE dynamic
} {
  if {[info exists ::env($name)] && $::env($name) ne $value} {
    error "$name must be $value for the continuous video-health candidate, got $::env($name)"
  }
  set ::env($name) $value
}
# The base builder names this extension variable for historical health-monitor
# compatibility.  Here it selects the complete RXTH isolation extension.
set ::env(MIMO_RF_RX_HEALTH_INTEGRATION_TCL) $transport_hook
set ::env(MIMO_RF_CONTINUOUS_TRANSPORT_EXTENSION) 1
# Avoid `generate_target all` simulation collateral on the 32-GB build host;
# implementation consumes the synthesis target products generated below.
set ::env(MIMO_RF_IP_TARGET) synthesis

source [file join [file dirname [info script]] create_zedboard_rf_system.tcl]
