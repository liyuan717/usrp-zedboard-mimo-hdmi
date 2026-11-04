# SPDX-License-Identifier: Apache-2.0
#
# Continuous RXTH+IMBC candidate extension.  The video-health wrapper and the
# headless RF-receiver wrapper select the same isolated transport; topology
# indices are supplied by the base builder instead of hard-coding HDMI slots.
#
# The ordinary ADI `rf_rx_dmac` path is deliberately left intact for its IIO
# scan ABI.  RXTH is an independent capture branch: CPack is broadcast into a
# second elastic FIFO, normalized from low-12 sign extension to MSB12, framed,
# and written by a dedicated Xilinx S2MM DMA.  There is no AXIS tee downstream
# of an ADI DMA and no RXTH header can enter the IIO device.

set rf_continuous_transport_root [file normalize [file join [file dirname [info script]] ../..]]

proc rf_rx_health_add_sources {root} {
  global rf_continuous_transport_root
  foreach name {
    rf_rx_msb12_normalizer.v
    rf_rx_overflow_fault_cdc.v
    rf_rx_transport_metadata_async_fifo.v
    rf_rx_transport_framer.v
  } {
    set path [file join $root pl rtl $name]
    if {![file isfile $path]} {error "Missing RXTH RTL: $path"}
    if {![llength [get_files -quiet $path]]} {add_files -norecurse $path}
  }
  set metadata_source [file join $root pl rtl rf_rx_transport_metadata_source.v]
  # RX9 may reproduce one frozen pre-fix diagnostic candidate without
  # reverting the active RXTH source.  The override is accepted only from the
  # repository's immutable build snapshot; ordinary RX8/production flows have
  # no override and use the active source above.
  if {[info exists ::env(MIMO_RF_RX9_PREFX_METADATA_SOURCE)]} {
    set expected [file normalize [file join $root build rx8_loopback_20261026 epoch-pre-fix rf_rx_transport_metadata_source.v]]
    set requested [file normalize $::env(MIMO_RF_RX9_PREFX_METADATA_SOURCE)]
    if {$requested ne $expected || ![file isfile $requested]} {
      error "RX9 pre-fix metadata override is not the frozen approved snapshot"
    }
    set metadata_source $requested
  }
  if {![file isfile $metadata_source]} {error "Missing RXTH metadata source: $metadata_source"}
  if {![llength [get_files -quiet $metadata_source]]} {add_files -norecurse $metadata_source}
  add_files -fileset constrs_1 -norecurse [file join $root pl constraints rf_rx_overflow_fault_cdc.xdc]
  source [file join $root pl scripts add_mhgd_interleaved_candidate_module.tcl]
  update_compile_order -fileset sources_1
}

proc rf_rx_health_integrate {{ignored_health_base 0x44A10000}} {
  global rf_continuous_control_first rf_continuous_memory_first
  foreach cell {ps7 reset_rf reset_50m control_xbar memory_xbar rf_rx_pack rf_rx_elastic_buffer rf_rx_dmac rf_rx_dma_enable_monitor} {
    if {![llength [get_bd_cells -quiet $cell]]} {error "RXTH integration missing prerequisite cell $cell"}
  }

  # The base builder has already reserved three control and four memory
  # endpoints.  For HDMI they are M11..M13/S07..S10; headless uses
  # M07..M09/S06..S09.  Do not resize a live interconnect here.
  if {![info exists ::env(MIMO_RF_CONTINUOUS_TRANSPORT_EXTENSION)] || $::env(MIMO_RF_CONTINUOUS_TRANSPORT_EXTENSION) ne "1"} {
    error "RXTH continuous extension was not reserved during base interconnect creation"
  }
  if {![info exists rf_continuous_control_first] || ![info exists rf_continuous_memory_first]} {
    error "RXTH continuous extension topology indices were not reserved by the base builder"
  }
  set rxth_control [format "M%02d_AXI" $rf_continuous_control_first]
  set imbc_control [format "M%02d_AXI" [expr {$rf_continuous_control_first + 1}]]
  set imbc_dma_control [format "M%02d_AXI" [expr {$rf_continuous_control_first + 2}]]
  set rxth_dest_memory [format "S%02d_AXI" $rf_continuous_memory_first]
  set rxth_sg_memory [format "S%02d_AXI" [expr {$rf_continuous_memory_first + 1}]]
  set imbc_mm2s_memory [format "S%02d_AXI" [expr {$rf_continuous_memory_first + 2}]]
  set imbc_s2mm_memory [format "S%02d_AXI" [expr {$rf_continuous_memory_first + 3}]]
  set rxth_control_clock [string map {_AXI _ACLK} $rxth_control]
  set imbc_control_clock [string map {_AXI _ACLK} $imbc_control]
  set imbc_dma_control_clock [string map {_AXI _ACLK} $imbc_dma_control]
  set rxth_control_reset [string map {_AXI _ARESETN} $rxth_control]
  set imbc_control_reset [string map {_AXI _ARESETN} $imbc_control]
  set imbc_dma_control_reset [string map {_AXI _ARESETN} $imbc_dma_control]

  # RXTH capture DMA is an independent ADI AXI-DMAC DMAengine provider.  Its
  # character consumer owns only `rf_rx_transport_dmac`; normal IIO scans
  # remain exactly on `rf_rx_dmac` at 0x7c400000.
  rf_ip rf_rx_transport_dmac axi_dmac {CONFIG.DMA_TYPE_SRC 1 CONFIG.DMA_TYPE_DEST 0 CONFIG.CYCLIC 1 CONFIG.SYNC_TRANSFER_START 0 CONFIG.AXIS_TUSER_SYNC 0 CONFIG.AXI_SLICE_SRC 0 CONFIG.AXI_SLICE_DEST 0 CONFIG.DMA_2D_TRANSFER 0 CONFIG.DMA_SG_TRANSFER 1 CONFIG.DMA_DATA_WIDTH_SRC 64 CONFIG.DMA_DATA_WIDTH_SG 64 CONFIG.CACHE_COHERENT 0 CONFIG.DMA_AXI_ADDR_WIDTH 32}

  # Separate candidate MHGD engine.  It remains a candidate-only module and
  # is not wired to legacy detector_0 or its release driver.
  create_bd_cell -type ip -vlnv xilinx.com:ip:axi_dma:7.1 interleaved_candidate_dma
  set_property -dict [list CONFIG.c_include_sg 0 CONFIG.c_include_mm2s 1 CONFIG.c_include_s2mm 1 \
    CONFIG.c_include_mm2s_dre 0 CONFIG.c_include_s2mm_dre 0 \
    CONFIG.c_m_axi_mm2s_data_width 64 CONFIG.c_m_axi_s2mm_data_width 64 \
    CONFIG.c_m_axis_mm2s_tdata_width 64 CONFIG.c_s_axis_s2mm_tdata_width 64 \
    CONFIG.c_mm2s_burst_size 16 CONFIG.c_s2mm_burst_size 16 CONFIG.c_addr_width 32 \
    CONFIG.c_sg_length_width 16] [get_bd_cells interleaved_candidate_dma]
  mimo_add_interleaved_candidate_module mimo_interleaved_candidate_0

  # The second FIFO is the RXTH raw-data epoch partner.  It receives the same
  # complete packed sample vector as IIO but has its own admission/flush
  # lifecycle, preventing either consumer's DMA backpressure from relabelling
  # the other consumer's data.  CPack may issue eight-word bursts around a
  # metadata/framer boundary.  The old 1024-word physical FIFO had only 1024
  # usable words including its skid stage, so closing a 1024-sample block in
  # the middle of one such burst could overflow before the new metadata was
  # accepted.  RXTH alone therefore uses 2048 physical entries (2047 RAM +
  # one skid); the legacy IIO elastic FIFO remains at its original 1024.
  create_bd_cell -type module -reference rf_rx_elastic_fifo rf_rx_transport_elastic_buffer
  set_property -dict {CONFIG.DEPTH 2048 CONFIG.ADDR_WIDTH 11} [get_bd_cells rf_rx_transport_elastic_buffer]
  set rxth_transport_fifo_depth [get_property CONFIG.DEPTH [get_bd_cells rf_rx_transport_elastic_buffer]]
  set rxth_transport_fifo_addr_width [get_property CONFIG.ADDR_WIDTH [get_bd_cells rf_rx_transport_elastic_buffer]]
  if {$rxth_transport_fifo_depth != 2048 || $rxth_transport_fifo_addr_width != 11} {
    error "RXTH transport FIFO headroom configuration readback failed: depth=$rxth_transport_fifo_depth addr_width=$rxth_transport_fifo_addr_width"
  }
  puts "RXTH_TRANSPORT_FIFO_HEADROOM_PASS depth=$rxth_transport_fifo_depth addr_width=$rxth_transport_fifo_addr_width"
  create_bd_cell -type module -reference rf_rx_msb12_normalizer rf_rx_transport_msb12
  create_bd_cell -type module -reference rf_rx_transport_metadata_source rf_rx_transport_metadata_source
  create_bd_cell -type module -reference rf_rx_transport_metadata_async_fifo rf_rx_transport_metadata_fifo
  create_bd_cell -type module -reference rf_rx_transport_framer rf_rx_transport_framer
  create_bd_cell -type module -reference rf_rx_dma_enable_monitor rf_rx_transport_dma_enable_monitor
  create_bd_cell -type module -reference rf_rx_overflow_fault_cdc rf_rx_transport_adc_fault_cdc

  # The transport monitor observes only the transport DMA control register;
  # its enable defines the transport capture epoch.  It is not attached to the
  # legacy IIO DMA AXI-Lite net.
  rf_net ps7/FCLK_CLK0 rf_rx_transport_dma_enable_monitor/clk
  rf_net reset_50m/peripheral_aresetn rf_rx_transport_dma_enable_monitor/resetn
  rf_net ps7/FCLK_CLK0 rf_rx_transport_elastic_buffer/clk rf_rx_transport_msb12/clk \
    rf_rx_transport_metadata_source/src_clk rf_rx_transport_metadata_fifo/src_clk \
    rf_rx_transport_metadata_fifo/dst_clk rf_rx_transport_framer/clk \
    rf_rx_transport_dmac/s_axis_aclk
  rf_net reset_rf/peripheral_aresetn rf_rx_transport_elastic_buffer/resetn \
    rf_rx_transport_msb12/resetn rf_rx_transport_metadata_source/src_resetn
  rf_net axi_ad9361/l_clk rf_rx_transport_adc_fault_cdc/src_clk
  rf_net reset_rf_lclk/peripheral_reset rf_rx_transport_adc_fault_cdc/src_reset
  rf_net ps7/FCLK_CLK0 rf_rx_transport_adc_fault_cdc/dst_clk
  rf_net reset_rf/peripheral_reset rf_rx_transport_adc_fault_cdc/dst_reset
  rf_net rf_rx_fifo/din_ovf rf_rx_transport_adc_fault_cdc/src_event

  # A raw data fault requests one coherent next-cycle epoch reset.  Gate the
  # ingress during that reset so the FIFO's documented same-cycle flush write
  # cannot become an unlabelled first sample.
  create_bd_cell -type ip -vlnv xilinx.com:ip:util_vector_logic:2.0 rf_rx_transport_reset_or
  set_property -dict {CONFIG.C_OPERATION or CONFIG.C_SIZE 1} [get_bd_cells rf_rx_transport_reset_or]
  create_bd_cell -type ip -vlnv xilinx.com:ip:util_vector_logic:2.0 rf_rx_transport_capture_gate
  set_property -dict {CONFIG.C_OPERATION and CONFIG.C_SIZE 1} [get_bd_cells rf_rx_transport_capture_gate]
  create_bd_cell -type ip -vlnv xilinx.com:ip:util_vector_logic:2.0 rf_rx_transport_capture_enable
  set_property -dict {CONFIG.C_OPERATION not CONFIG.C_SIZE 1} [get_bd_cells rf_rx_transport_capture_enable]
  # DMA ownership is a hard framer reset, separate from a source-fault epoch
  # flush.  Disabling RXTH-DMAC therefore suppresses M_AXIS immediately and
  # invalidates an in-flight descriptor; Linux must ABORT/reclaim it before a
  # later CTRL enable starts a fresh 8224-byte RXTH block.  A raw/source fault
  # leaves dma_enabled asserted, so the committed packet still drains intact.
  create_bd_cell -type ip -vlnv xilinx.com:ip:util_vector_logic:2.0 rf_rx_transport_framer_owner_reset
  set_property -dict {CONFIG.C_OPERATION and CONFIG.C_SIZE 1} [get_bd_cells rf_rx_transport_framer_owner_reset]
  rf_net rf_rx_transport_dma_enable_monitor/flush_sync rf_rx_transport_reset_or/Op1 \
    rf_rx_transport_metadata_source/transport_flush
  rf_net rf_rx_transport_metadata_source/raw_flush_request rf_rx_transport_reset_or/Op2
  rf_net rf_rx_transport_reset_or/Res rf_rx_transport_elastic_buffer/flush_sync \
    rf_rx_transport_metadata_fifo/transport_reset
  rf_net rf_rx_transport_reset_or/Res rf_rx_transport_capture_enable/Op1
  rf_net rf_rx_transport_capture_enable/Res rf_rx_transport_capture_gate/Op2
  # Source flush remains commit-immutable.  By contrast DMA owner disable
  # hard-resets the framer through the explicit AND below; it is a software
  # abort boundary, never a successful short RXTH block.
  rf_net reset_rf/peripheral_aresetn rf_rx_transport_framer_owner_reset/Op1
  rf_net rf_rx_transport_dma_enable_monitor/dma_enabled rf_rx_transport_framer_owner_reset/Op2
  rf_net rf_rx_transport_framer_owner_reset/Res rf_rx_transport_framer/resetn
  rf_net rf_rx_transport_reset_or/Res rf_rx_transport_framer/transport_flush
  rf_net rf_rx_pack/packed_fifo_wr_en rf_rx_transport_capture_gate/Op1 \
    rf_rx_transport_metadata_source/adc_sample_valid
  # This is the actual FIFO admission event, not raw valid.  It is the anchor
  # for the metadata block and therefore for every RXTH payload word.
  rf_net rf_rx_transport_capture_gate/Res rf_rx_transport_elastic_buffer/packed_fifo_wr_en
  rf_net rf_rx_transport_elastic_buffer/accepted_write rf_rx_transport_metadata_source/sample_written
  rf_net rf_rx_pack/packed_fifo_wr_data rf_rx_transport_elastic_buffer/packed_fifo_wr_data
  rf_net rf_rx_transport_elastic_buffer/ingress_drop rf_rx_transport_metadata_source/source_fifo_overflow
  # Held request/ack fault: no even-toggle cancellation and no clean data
  # admission while the source continues to assert overflow. The synchronized
  # fault holds epoch flush; a raw l_clk signal never enters 50-MHz logic.
  rf_net rf_rx_transport_adc_fault_cdc/dst_fault rf_rx_transport_metadata_source/adc_overflow
  rf_net rf_rx_transport_metadata_source/metadata_valid rf_rx_transport_metadata_fifo/src_valid
  rf_net rf_rx_transport_metadata_fifo/src_ready rf_rx_transport_metadata_source/metadata_ready
  rf_net rf_rx_transport_metadata_source/metadata_flags rf_rx_transport_metadata_fifo/src_flags
  rf_net rf_rx_transport_metadata_source/metadata_epoch rf_rx_transport_metadata_fifo/src_epoch
  rf_net rf_rx_transport_metadata_source/metadata_block_seq rf_rx_transport_metadata_fifo/src_block_seq
  rf_net rf_rx_transport_metadata_source/metadata_first_adc_sample rf_rx_transport_metadata_fifo/src_first_adc_sample
  rf_net rf_rx_transport_metadata_source/metadata_valid_sample_count rf_rx_transport_metadata_fifo/src_valid_sample_count
  rf_net rf_rx_transport_metadata_fifo/dst_valid rf_rx_transport_framer/meta_valid
  rf_net rf_rx_transport_framer/meta_ready rf_rx_transport_metadata_fifo/dst_ready
  rf_net rf_rx_transport_metadata_fifo/dst_flags rf_rx_transport_framer/meta_flags
  rf_net rf_rx_transport_metadata_fifo/dst_epoch rf_rx_transport_framer/meta_epoch
  rf_net rf_rx_transport_metadata_fifo/dst_block_seq rf_rx_transport_framer/meta_block_seq
  rf_net rf_rx_transport_metadata_fifo/dst_first_adc_sample rf_rx_transport_framer/meta_first_adc_sample
  rf_net rf_rx_transport_metadata_fifo/dst_valid_sample_count rf_rx_transport_framer/meta_valid_sample_count
  rf_bus rf_rx_transport_elastic_buffer/M_AXIS rf_rx_transport_msb12/S_AXIS
  rf_bus rf_rx_transport_msb12/M_AXIS rf_rx_transport_framer/S_AXIS
  rf_bus rf_rx_transport_framer/M_AXIS rf_rx_transport_dmac/s_axis

  # AXI-Lite and DDR routes.  These are explicit addresses and endpoints for
  # the two new Linux drivers; neither aliases a legacy node.
  rf_bus control_xbar/$rxth_control rf_rx_transport_dmac/s_axi
  set rxth_control_net [get_bd_intf_nets -of_objects [get_bd_intf_pins control_xbar/$rxth_control]]
  if {[llength $rxth_control_net] != 1} {error "RXTH ADI DMAC control interface net is not unique"}
  connect_bd_intf_net -intf_net $rxth_control_net [get_bd_intf_pins control_xbar/$rxth_control] \
    [get_bd_intf_pins rf_rx_transport_dma_enable_monitor/AXI_MON]
  rf_bus control_xbar/$imbc_control mimo_interleaved_candidate_0/s_axi
  rf_bus control_xbar/$imbc_dma_control interleaved_candidate_dma/S_AXI_LITE
  rf_bus rf_rx_transport_dmac/m_dest_axi memory_xbar/$rxth_dest_memory
  rf_bus rf_rx_transport_dmac/m_sg_axi memory_xbar/$rxth_sg_memory
  rf_bus interleaved_candidate_dma/M_AXI_MM2S memory_xbar/$imbc_mm2s_memory
  rf_bus interleaved_candidate_dma/M_AXI_S2MM memory_xbar/$imbc_s2mm_memory
  rf_bus interleaved_candidate_dma/M_AXIS_MM2S mimo_interleaved_candidate_0/s_axis
  rf_bus mimo_interleaved_candidate_0/m_axis interleaved_candidate_dma/S_AXIS_S2MM

  foreach pin [list control_xbar/$rxth_control_clock control_xbar/$imbc_control_clock \
      control_xbar/$imbc_dma_control_clock rf_rx_transport_dmac/s_axi_aclk \
      rf_rx_transport_dmac/m_dest_axi_aclk rf_rx_transport_dmac/m_sg_axi_aclk interleaved_candidate_dma/s_axi_lite_aclk \
      interleaved_candidate_dma/m_axi_mm2s_aclk interleaved_candidate_dma/m_axi_s2mm_aclk \
      mimo_interleaved_candidate_0/aclk] {rf_net ps7/FCLK_CLK0 $pin}
  foreach pin [list control_xbar/$rxth_control_reset control_xbar/$imbc_control_reset \
      control_xbar/$imbc_dma_control_reset rf_rx_transport_dmac/s_axi_aresetn \
      rf_rx_transport_dmac/m_dest_axi_aresetn rf_rx_transport_dmac/m_sg_axi_aresetn interleaved_candidate_dma/axi_resetn \
      mimo_interleaved_candidate_0/aresetn] {rf_net reset_50m/peripheral_aresetn $pin}

  set cpu_space [get_bd_addr_spaces ps7/Data]
  foreach {offset bus} {0x7C440000 rf_rx_transport_dmac/s_axi 0x7C430000 mimo_interleaved_candidate_0/s_axi} {
    set seg [get_bd_addr_segs -of_objects [get_bd_intf_pins $bus]]
    if {[llength $seg] != 1} {error "Expected exactly one transport/candidate register segment for $bus, got $seg"}
    assign_bd_address -target_address_space $cpu_space -offset $offset -range 64K $seg
  }
  foreach space {rf_rx_transport_dmac/m_dest_axi rf_rx_transport_dmac/m_sg_axi interleaved_candidate_dma/Data_MM2S interleaved_candidate_dma/Data_S2MM} {
    assign_bd_address -target_address_space [get_bd_addr_spaces $space] -offset 0 -range 512M [get_bd_addr_segs ps7/S_AXI_HP0/HP0_DDR_LOWOCM]
  }

  # The base script creates interrupt_concat after this early hook.  Publish a
  # deterministic extension for it to call instead of hard-coding a second
  # interrupt ABI at this location.
  set ::rf_continuous_extra_irqs [list \
    rf_rx_transport_dmac/irq \
    interleaved_candidate_dma/mm2s_introut \
    interleaved_candidate_dma/s2mm_introut]
}

proc rf_rx_health_address_finalize {} {
  # Validate the expanded interconnect before relocating this Xilinx AXI-DMA
  # segment.  In Vivado 2025.2 an immediately-created M12 path has not yet
  # populated the PS address reachability graph, even though the interface is
  # physically connected.  The initial validate builds that graph; `-force`
  # then relocates the auto-assigned segment to the driver ABI address.
  set cpu_space [get_bd_addr_spaces ps7/Data]
  set seg [get_bd_addr_segs -of_objects [get_bd_intf_pins interleaved_candidate_dma/S_AXI_LITE]]
  if {[llength $seg] != 1} {error "Expected exactly one candidate DMA register segment, got $seg"}
  assign_bd_address -force -target_address_space $cpu_space -offset 0x40420000 -range 64K $seg
}

proc rf_rx_health_irq_extend {concat first_free} {
  if {$first_free != 8} {error "RXTH/IMBC continuous IRQ ABI requires reserved F2P slots 6/7 and live slots 8..10, got first free $first_free"}
  foreach {source dest} {rf_rx_transport_dmac/irq In8 interleaved_candidate_dma/mm2s_introut In9 interleaved_candidate_dma/s2mm_introut In10} {
    rf_net $source $concat/$dest
  }
  return 11
}

proc rf_rx_health_assert_no_adc_dovf_connection {} {
  # The only source fault connection is the safe post-util_wfifo overflow
  # indication; a raw AD9361 l_clk pulse would be an illegal CDC shortcut.
  set pins [get_bd_pins -quiet -of_objects [get_bd_nets -quiet -of_objects [get_bd_pins rf_rx_transport_metadata_source/adc_overflow]]]
  if {[llength [get_bd_pins -quiet rf_rx_transport_metadata_source/adc_overflow]] != 1} {error "RXTH overflow pin missing"}
  if {[llength [get_bd_pins -quiet axi_ad9361/adc_dovf]] != 1} {error "AD9361 overflow pin missing"}
  if {[lsearch -exact $pins [get_bd_pins axi_ad9361/adc_dovf]] >= 0} {error "Illegal raw adc_dovf CDC into RXTH metadata source"}
}

# Run on the actual synthesized/routed netlist, not in restricted synthesis
# XDC Tcl. Fail if a collection silently went empty, synchronizer properties
# disappeared, a blanket exception hid the path, or a local stage was relaxed.
proc rf_rx_health_audit_fault_cdc {out phase} {
  set leaf {*/rf_rx_transport_adc_fault_cdc/inst/}
  set cells [dict create]
  foreach name {src_pending src_ack_sync_0 src_ack_sync_1 dst_pending_sync_0 dst_pending_sync_1 dst_pending_sync_2} {
    set found [get_cells -quiet -hier -filter "NAME =~ ${leaf}${name}_reg"]
    if {[llength $found] != 1} {error "RX fault CDC expected one $name register, got $found"}
    dict set cells $name $found
    if {$name ne "src_pending" && ![get_property ASYNC_REG $found]} {
      error "RX fault CDC synchronizer lost ASYNC_REG: $found"
    }
    set clocks [get_clocks -quiet -of_objects [get_pins -quiet ${found}/C]]
    set period [expr {[string match "src_*" $name] ? 62.5 : 20.0}]
    if {[llength $clocks] != 1 || abs([get_property PERIOD $clocks]-$period) > 0.001} {
      error "RX fault CDC $name clock does not match $period ns: $clocks"
    }
  }
  set fd [open [file join $out ${phase}_rx_fault_cdc_audit.rpt] w]
  foreach {from to period} {
    src_pending dst_pending_sync_0 20.0
    dst_pending_sync_2 src_ack_sync_0 62.5
    dst_pending_sync_0 dst_pending_sync_1 20.0
    dst_pending_sync_1 dst_pending_sync_2 20.0
    src_ack_sync_0 src_ack_sync_1 62.5
  } {
    set paths [get_timing_paths -quiet -delay_type max -max_paths 1 \
      -from [dict get $cells $from] -to [dict get $cells $to]]
    if {[llength $paths] != 1 || abs([get_property REQUIREMENT $paths]-$period) > 0.001} {
      close $fd
      error "RX fault CDC path missing/incorrect requirement: $from -> $to, expected $period ns"
    }
    puts $fd "$from -> $to requirement_ns=[get_property REQUIREMENT $paths] slack_ns=[get_property SLACK $paths]"
  }
  puts $fd "six_registers_present=1 five_synchronizers_async_reg=1 local_stage_requirements_preserved=1"
  close $fd
  puts "RF_RX_FAULT_CDC_TOPOLOGY_AUDIT_PASS $phase"
}
