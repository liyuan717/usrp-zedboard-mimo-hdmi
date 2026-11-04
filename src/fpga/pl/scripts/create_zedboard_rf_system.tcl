# ZedBoard PS7 + FMCOMMS3/AD9361 CMOS 2R2T + Detector system.
# Usage:
#   ADI_HDL_DIR=<isolated vendor tree> ADI_ALLOW_VERSION_MISMATCH=YES \
#   vivado -mode batch -source pl/scripts/create_zedboard_rf_system.tcl \
#     -tclargs build/zedboard_rf {validate|synth|implement}
#
# This is intentionally a separate project from create_zedboard_digital_system.
# It preserves the digital address/IRQ ABI and adds the two RF DMA engines.
set root [file normalize [file join [file dirname [info script]] ../..]]
source [file join $root pl scripts rf_signoff_checks.tcl]
set out [file join $root build zedboard_rf]
set mode validate
if {$argc > 2} {error "Usage: create_zedboard_rf_system.tcl ?fresh_output_directory? ?validate|synth|implement?"}
if {$argc >= 1} {set out [file normalize [lindex $argv 0]]}
if {$argc == 2} {set mode [lindex $argv 1]}
if {$mode ni {validate synth implement}} {error "Invalid build mode: $mode"}
if {[file exists [file join $out mimo_zedboard_rf.xpr]]} {error "Choose a fresh output directory"}
if {![string match "2025.2*" [version -short]]} {error "ZedBoard RF build is locked to Vivado 2025.2"}
set part xc7z020clg484-1
if {[info exists ::env(MIMO_ZEDBOARD_PART)]} {set part $::env(MIMO_ZEDBOARD_PART)}
if {[llength [get_parts -quiet $part]] != 1} {error "Unsupported target part $part"}
set adi_commit 4366bd63c1fe56b680b69145981a4493c6f8753a
if {![info exists ::env(ADI_HDL_DIR)]} {error "Set ADI_HDL_DIR to the isolated, hash-locked ADI HDL tree"}
if {![info exists ::env(ADI_ALLOW_VERSION_MISMATCH)] || $::env(ADI_ALLOW_VERSION_MISMATCH) ne "YES"} {
  error "ADI HDL declares a different Vivado version; explicitly set ADI_ALLOW_VERSION_MISMATCH=YES"
}
set adi_root [file normalize $::env(ADI_HDL_DIR)]
if {![file isdirectory [file join $adi_root library]]} {error "ADI_HDL_DIR has no library directory: $adi_root"}
if {![file exists [file join [file dirname $adi_root] source-manifest.json]]} {error "ADI source manifest is required"}
set rf_interface cmos_2r2t_1v8
if {[info exists ::env(MIMO_RF_INTERFACE)]} {set rf_interface $::env(MIMO_RF_INTERFACE)}
if {$rf_interface ni {cmos_2r2t_1v8 lvds_2r2t_2v5}} {error "Unsupported RF interface profile: $rf_interface"}
set is_lvds [expr {$rf_interface eq "lvds_2r2t_2v5"}]
set rx_observer 0
if {[info exists ::env(MIMO_RF_RX_OBSERVER)]} {
  if {$::env(MIMO_RF_RX_OBSERVER) ni {0 1}} {error "MIMO_RF_RX_OBSERVER must be 0 or 1"}
  set rx_observer $::env(MIMO_RF_RX_OBSERVER)
}
if {$rx_observer && !$is_lvds} {error "RX observer is diagnostic LVDS only"}
set rx_observer_abi [expr {$rx_observer ? 1 : 0}]
if {[info exists ::env(MIMO_RF_RX_OBSERVER_ABI)]} {set rx_observer_abi $::env(MIMO_RF_RX_OBSERVER_ABI)}
if {($rx_observer && $rx_observer_abi ni {1 2 3}) || (!$rx_observer && $rx_observer_abi != 0)} {
  error "Observer ABI disagrees with diagnostic selection"
}
set observer_manifest_fd [open [file join [file dirname $adi_root] source-manifest.json] r]
set observer_manifest_text [read $observer_manifest_fd]
close $observer_manifest_fd
set observer_vendor [regexp {"diagnostic_only"\s*:\s*true} $observer_manifest_text]
if {$observer_vendor != $rx_observer} {error "Observer vendor manifest and explicit diagnostic selection disagree"}
if {$rx_observer && ![regexp "\"rx_observer_abi\"\\s*:\\s*${rx_observer_abi}(?:\\s|,)" $observer_manifest_text]} {
  error "Observer ABI differs from vendor manifest"
}
set buffered_rx 0
if {[info exists ::env(MIMO_RF_RX_BUFFERED)]} {
  if {$::env(MIMO_RF_RX_BUFFERED) ni {0 1}} {error "MIMO_RF_RX_BUFFERED must be 0 or 1"}
  set buffered_rx $::env(MIMO_RF_RX_BUFFERED)
}
if {$buffered_rx && !$is_lvds} {error "Buffered RX is qualified only for the 2.5-V LVDS profile"}
if {$is_lvds && $buffered_rx && $part ne "xc7z020clg484-1"} {
  error "Formal RF release is locked to xc7z020clg484-1; part overrides are diagnostic only"
}
set hdmi_demo 0
set enable_hdmi_value ""
set legacy_hdmi_value ""
if {[info exists ::env(ENABLE_HDMI)]} {
  if {$::env(ENABLE_HDMI) ni {0 1}} {error "ENABLE_HDMI must be 0 or 1"}
  set enable_hdmi_value $::env(ENABLE_HDMI)
}
if {[info exists ::env(MIMO_HDMI_DEMO)]} {
  if {$::env(MIMO_HDMI_DEMO) ni {0 1}} {error "MIMO_HDMI_DEMO must be 0 or 1"}
  set legacy_hdmi_value $::env(MIMO_HDMI_DEMO)
}
if {$enable_hdmi_value ne "" && $legacy_hdmi_value ne "" &&
    $enable_hdmi_value ne $legacy_hdmi_value} {
  error "ENABLE_HDMI conflicts with legacy MIMO_HDMI_DEMO"
}
if {$enable_hdmi_value ne ""} {
  set hdmi_demo $enable_hdmi_value
} elseif {$legacy_hdmi_value ne ""} {
  set hdmi_demo $legacy_hdmi_value
}
set display_mode off
if {[info exists ::env(DISPLAY_MODE)]} {
  set display_mode $::env(DISPLAY_MODE)
}
if {$display_mode ni {off static dynamic}} {
  error "DISPLAY_MODE must be off, static or dynamic"
}
if {!$hdmi_demo && $display_mode ne "off"} {
  error "ENABLE_HDMI=0 requires DISPLAY_MODE=off"
}
set rx_cyclic 0
if {[info exists ::env(MIMO_RF_RX_CYCLIC)]} {
  if {$::env(MIMO_RF_RX_CYCLIC) ni {0 1}} {error "MIMO_RF_RX_CYCLIC must be 0 or 1"}
  set rx_cyclic $::env(MIMO_RF_RX_CYCLIC)
}
if {$rx_cyclic && (!$is_lvds || !$buffered_rx)} {
  error "Cyclic RX requires the buffered 2.5-V LVDS profile"
}
set_param general.maxThreads 2
create_project mimo_zedboard_rf $out -part $part
set_property target_language Verilog [current_project]
set_property simulator_language Mixed [current_project]
set_property source_mgmt_mode All [current_project]
# The RF DMA clock is a deliberately fixed 2R2T sample-domain clock.  Add the
# local primitive wrapper before BD elaboration so it can be instantiated as a
# module-reference cell below.
add_files -norecurse [file join $root pl rtl rf_static_clkdiv.v]
if {$is_lvds} {
  add_files -norecurse [file join $root pl rtl rf_static_clkdiv4.v]
  add_files -norecurse [file join $root pl rtl rf_lvds_forward_clock.v]
}
if {$buffered_rx} {
  add_files -norecurse [file join $root pl rtl rf_rx_elastic_fifo.v]
  add_files -norecurse [file join $root pl rtl rf_rx_dma_enable_monitor.v]
}
# A separately named continuous-video diagnostic candidate may insert the
# same-clock RX health monitor.  The base RF topology remains unchanged unless
# the caller deliberately supplies this hook; in particular, existing RF
# images and their TX-DMAC ownership are never rewritten implicitly.
set rf_rx_health_enabled 0
if {[info exists ::env(MIMO_RF_RX_HEALTH_INTEGRATION_TCL)]} {
  set rf_rx_health_tcl [file normalize $::env(MIMO_RF_RX_HEALTH_INTEGRATION_TCL)]
  if {![file isfile $rf_rx_health_tcl]} {
    error "MIMO_RF_RX_HEALTH_INTEGRATION_TCL does not exist: $rf_rx_health_tcl"
  }
  source $rf_rx_health_tcl
  rf_rx_health_add_sources $root
  set rf_rx_health_enabled 1
}
# RX9 is a separately selected, passive status page for the RXTH extension.
# Keep its source and its one extra AXI target absent from every default/RX8
# build.  The integration script itself rejects any non-headless topology.
set rf_rxth_diag_enabled 0
if {[info exists ::env(MIMO_RF_RXTH_DIAG)]} {
  if {$::env(MIMO_RF_RXTH_DIAG) ni {0 1}} {error "MIMO_RF_RXTH_DIAG must be 0 or 1"}
  set rf_rxth_diag_enabled $::env(MIMO_RF_RXTH_DIAG)
}
if {$rf_rxth_diag_enabled} {
  if {![info exists ::env(MIMO_RF_RXTH_DIAG_INTEGRATION_TCL)]} {
    error "MIMO_RF_RXTH_DIAG=1 requires MIMO_RF_RXTH_DIAG_INTEGRATION_TCL"
  }
  set rf_rxth_diag_tcl [file normalize $::env(MIMO_RF_RXTH_DIAG_INTEGRATION_TCL)]
  if {![file isfile $rf_rxth_diag_tcl]} {error "RX9 diagnostic integration script missing: $rf_rxth_diag_tcl"}
  source $rf_rxth_diag_tcl
  rf_rx_transport_diag_add_sources $root
}
if {$hdmi_demo} {
  add_files -norecurse [file join $adi_root library common ad_iobuf.v]
}
set_property ip_repo_paths [list [file join $adi_root library]] [current_project]
update_ip_catalog
create_bd_design zedboard_rf_system

proc rf_ip {name core {properties {}}} {
  set defs [get_ipdefs -all -quiet analog.com:user:${core}:1.0]
  if {[llength $defs] != 1} {error "Missing or ambiguous packaged ADI IP: $core ($defs)"}
  create_bd_cell -type ip -vlnv analog.com:user:${core}:1.0 $name
  if {[llength $properties]} {set_property -dict $properties [get_bd_cells $name]}
}
proc rf_net {args} {
  set objects {}
  foreach path $args {
    set object [get_bd_pins -quiet $path]
    if {![llength $object]} {set object [get_bd_ports -quiet $path]}
    if {[llength $object] != 1} {error "Missing/ambiguous RF net endpoint: $path"}
    lappend objects $object
  }
  connect_bd_net {*}$objects
}
proc rf_bus {a b} {connect_bd_intf_net [get_bd_intf_pins $a] [get_bd_intf_pins $b]}
proc rf_reset {name clk resetn locked zero} {
  create_bd_cell -type ip -vlnv xilinx.com:ip:proc_sys_reset:5.0 $name
  set_property CONFIG.C_AUX_RESET_HIGH 1 [get_bd_cells $name]
  rf_net $resetn $name/ext_reset_in
  rf_net $clk $name/slowest_sync_clk
  rf_net $locked $name/dcm_locked
  rf_net $zero $name/aux_reset_in $name/mb_debug_sys_rst
}

# PS7 configuration follows the digital project and intentionally keeps the
# board preset's DDR/MIO ownership.  The explicit DDR entries prevent Vivado
# from silently replacing the reviewed lane-2/3 PCB-delay references with
# negative values after the Custom-memory update.  FCLK0 is 50 MHz control AXI.
create_bd_cell -type ip -vlnv xilinx.com:ip:processing_system7:5.5 ps7
set_property CONFIG.PCW_IMPORT_BOARD_PRESET ZedBoard [get_bd_cells ps7]
set_property -dict [list \
  CONFIG.PCW_USE_M_AXI_GP0 1 CONFIG.PCW_USE_S_AXI_HP0 1 \
  CONFIG.PCW_S_AXI_HP0_DATA_WIDTH 64 CONFIG.PCW_USE_FABRIC_INTERRUPT 1 \
  CONFIG.PCW_IRQ_F2P_INTR 1 CONFIG.PCW_EN_CLK0_PORT 1 CONFIG.PCW_EN_RST0_PORT 1 \
  CONFIG.PCW_FPGA0_PERIPHERAL_FREQMHZ 50.0 \
  CONFIG.PCW_CRYSTAL_PERIPHERAL_FREQMHZ 33.333333 \
  CONFIG.PCW_DDR_RAM_HIGHADDR 0x1FFFFFFF \
  CONFIG.PCW_UIPARAM_DDR_PARTNO Custom \
  CONFIG.PCW_UIPARAM_DDR_MEMORY_TYPE {DDR 3} \
  CONFIG.PCW_UIPARAM_DDR_DRAM_WIDTH {16 Bits} \
  CONFIG.PCW_UIPARAM_DDR_DEVICE_CAPACITY {2048 MBits} \
  CONFIG.PCW_UIPARAM_DDR_ROW_ADDR_COUNT 14 \
  CONFIG.PCW_UIPARAM_DDR_FREQ_MHZ 400.0 \
  CONFIG.PCW_UIPARAM_DDR_CL 6 CONFIG.PCW_UIPARAM_DDR_CWL 5 \
  CONFIG.PCW_UIPARAM_DDR_T_RCD 6 CONFIG.PCW_UIPARAM_DDR_T_RP 6 \
  CONFIG.PCW_UIPARAM_DDR_T_RC 55.0 CONFIG.PCW_UIPARAM_DDR_T_RAS_MIN 40.0 \
  CONFIG.PCW_UIPARAM_DDR_T_FAW 50.0 \
  CONFIG.PCW_UIPARAM_DDR_DQS_TO_CLK_DELAY_0 0.025 \
  CONFIG.PCW_UIPARAM_DDR_DQS_TO_CLK_DELAY_1 0.028 \
  CONFIG.PCW_UIPARAM_DDR_DQS_TO_CLK_DELAY_2 0.001 \
  CONFIG.PCW_UIPARAM_DDR_DQS_TO_CLK_DELAY_3 0.001] [get_bd_cells ps7]
make_bd_intf_pins_external [get_bd_intf_pins ps7/DDR]
make_bd_intf_pins_external [get_bd_intf_pins ps7/FIXED_IO]
set_property name DDR [get_bd_intf_ports DDR_0]
set_property name FIXED_IO [get_bd_intf_ports FIXED_IO_0]

# Detector path: retain the existing digital ABI exactly.
create_bd_cell -type ip -vlnv xilinx.com:ip:axi_dma:7.1 detector_dma
set_property -dict [list CONFIG.c_include_sg 0 CONFIG.c_include_mm2s 1 CONFIG.c_include_s2mm 1 \
  CONFIG.c_include_mm2s_dre 0 CONFIG.c_include_s2mm_dre 0 CONFIG.c_m_axi_mm2s_data_width 64 \
  CONFIG.c_m_axi_s2mm_data_width 64 CONFIG.c_m_axis_mm2s_tdata_width 64 \
  CONFIG.c_s_axis_s2mm_tdata_width 64 CONFIG.c_mm2s_burst_size 16 \
  CONFIG.c_s2mm_burst_size 16 CONFIG.c_addr_width 32 CONFIG.c_sg_length_width 14] [get_bd_cells detector_dma]
source [file join $root pl scripts add_detector_module.tcl]
mimo_add_detector_module detector_0

# The base RF design has six AXI memory masters and seven control targets.
# The optional HDMI demo adds one memory master and four control targets.
set control_targets 7
set memory_masters 6
if {$hdmi_demo} {
  incr control_targets 4
  incr memory_masters 1
}
# The RXTH+IMBC continuous extension adds two AXI-Lite slaves, one AXI-Lite
# DMA control page, two ADI-DMAC memory masters and two simple-DMA memory
# masters.  Reserve those interface pins when the interconnect is created;
# changing NUM_MI after the crossbar exists can leave a newly visible port
# outside the PS address map.  The extension has a topology-relative route
# index, rather than assuming that the HDMI-only M11..M13/S07..S10 numbering
# exists in a headless image.
set continuous_transport_extension 0
if {[info exists ::env(MIMO_RF_CONTINUOUS_TRANSPORT_EXTENSION)]} {
  if {$::env(MIMO_RF_CONTINUOUS_TRANSPORT_EXTENSION) ni {0 1}} {error "MIMO_RF_CONTINUOUS_TRANSPORT_EXTENSION must be 0 or 1"}
  set continuous_transport_extension $::env(MIMO_RF_CONTINUOUS_TRANSPORT_EXTENSION)
}
if {$continuous_transport_extension} {
  # Base endpoints are M00..M06 and S00..S05.  HDMI, if selected, owns the
  # next M07..M10 and S06.  Preserve that deployed HDMI ABI while allowing a
  # headless receiver to reserve M07..M09/S06..S09 instead.
  set ::rf_continuous_control_first [expr {$hdmi_demo ? 11 : 7}]
  set ::rf_continuous_memory_first [expr {$hdmi_demo ? 7 : 6}]
  incr control_targets 3
  incr memory_masters 4
}
if {$rf_rxth_diag_enabled} {
  if {!$continuous_transport_extension || !$rf_rx_health_enabled} {
    error "RX9 diagnostics require the isolated RXTH continuous transport"
  }
  if {$hdmi_demo} {error "RX9 diagnostic ABI reserves headless control_xbar/M10 only"}
  # Headless base M00..M06 plus RXTH/IMBC M07..M09 leave M10 unused.
  set ::rf_rxth_diag_control_index 10
  incr control_targets
}
create_bd_cell -type ip -vlnv xilinx.com:ip:axi_interconnect:2.1 control_xbar
set_property CONFIG.NUM_MI $control_targets [get_bd_cells control_xbar]
# Independent memory masters feed HP0.  SmartConnect is used rather than
# axi_interconnect here because Vivado 2025.2 repeatedly propagates an invalid
# zero S03/S05 acceptance depth into axi_interconnect for write-only/read-only
# ADI DMA ports.  SmartConnect is functionally equivalent for this 1-MI fabric
# and owns the required protocol/width conversion safely.
create_bd_cell -type ip -vlnv xilinx.com:ip:smartconnect:1.0 memory_xbar
set_property -dict [list CONFIG.NUM_SI $memory_masters CONFIG.NUM_MI 1 CONFIG.NUM_CLKS 1] [get_bd_cells memory_xbar]
rf_bus ps7/M_AXI_GP0 control_xbar/S00_AXI
foreach {mi bus} {
  M00_AXI detector_0/s_axi M01_AXI detector_dma/S_AXI_LITE
} {rf_bus control_xbar/$mi $bus}
rf_bus detector_dma/M_AXI_MM2S memory_xbar/S00_AXI
rf_bus detector_dma/M_AXI_S2MM memory_xbar/S01_AXI
rf_bus memory_xbar/M00_AXI ps7/S_AXI_HP0
rf_bus detector_dma/M_AXIS_MM2S detector_0/s_axis
rf_bus detector_0/m_axis detector_dma/S_AXIS_S2MM

# Control reset and deterministic safe tie-offs.
foreach {name value} {constant_zero 0 constant_one 1} {
  create_bd_cell -type ip -vlnv xilinx.com:ip:xlconstant:1.1 $name
  set_property -dict [list CONFIG.CONST_WIDTH 1 CONFIG.CONST_VAL $value] [get_bd_cells $name]
}
create_bd_cell -type ip -vlnv xilinx.com:ip:proc_sys_reset:5.0 reset_50m
set_property CONFIG.C_AUX_RESET_HIGH 1 [get_bd_cells reset_50m]
rf_net ps7/FCLK_RESET0_N reset_50m/ext_reset_in
rf_net constant_one/dout reset_50m/dcm_locked
rf_net constant_zero/dout reset_50m/aux_reset_in reset_50m/mb_debug_sys_rst

# Optional HDMI demo path: the standard ZedBoard ADV7511 interface, HDMI
# clock generator, cyclic video DMA and the I2C mux used by the display driver.
# This is deliberately opt-in so the existing RF-only address/IRQ/bitstream
# contract remains unchanged when ENABLE_HDMI=0.
if {$hdmi_demo} {
  rf_ip axi_hdmi_clkgen axi_clkgen {CONFIG.CLKIN_PERIOD 20.000 CONFIG.VCO_DIV 1 CONFIG.VCO_MUL 20.000 CONFIG.CLK0_DIV 6.750}
  rf_ip axi_hdmi_core axi_hdmi_tx {CONFIG.INTERFACE 16_BIT CONFIG.OUT_CLK_POLARITY 1}
  rf_ip axi_hdmi_dma axi_dmac [list CONFIG.DMA_TYPE_SRC 0 CONFIG.DMA_TYPE_DEST 1 \
    CONFIG.CYCLIC true CONFIG.AXI_SLICE_SRC 0 CONFIG.AXI_SLICE_DEST 0 \
    CONFIG.DMA_2D_TRANSFER true CONFIG.DMA_DATA_WIDTH_SRC 64]
  create_bd_cell -type ip -vlnv xilinx.com:ip:axi_iic:2.1 axi_iic_main
  rf_ip sys_i2c_mixer util_i2c_mixer

  foreach {direction port} {O hdmi_out_clk O hdmi_vsync O hdmi_hsync O hdmi_data_e} {
    create_bd_port -dir $direction $port
  }
  create_bd_port -dir O -from 15 -to 0 hdmi_data
  rf_net axi_hdmi_core/hdmi_out_clk hdmi_out_clk
  rf_net axi_hdmi_core/hdmi_16_vsync hdmi_vsync
  rf_net axi_hdmi_core/hdmi_16_hsync hdmi_hsync
  rf_net axi_hdmi_core/hdmi_16_data_e hdmi_data_e
  rf_net axi_hdmi_core/hdmi_16_data hdmi_data

  create_bd_port -dir IO -from 1 -to 0 iic_mux_scl
  create_bd_port -dir IO -from 1 -to 0 iic_mux_sda
  rf_bus axi_iic_main/IIC sys_i2c_mixer/upstream
  foreach {name signal} {iic_mux_scl_iobuf scl iic_mux_sda_iobuf sda} {
    create_bd_cell -type module -reference ad_iobuf $name
    set_property CONFIG.DATA_WIDTH 2 [get_bd_cells $name]
    rf_net sys_i2c_mixer/downstream_${signal}_O $name/dio_i
    rf_net sys_i2c_mixer/downstream_${signal}_T $name/dio_t
    rf_net $name/dio_o sys_i2c_mixer/downstream_${signal}_I
    rf_net $name/dio_p iic_mux_${signal}
  }

  rf_net ps7/FCLK_CLK0 axi_hdmi_clkgen/clk
  rf_net axi_hdmi_clkgen/clk_0 axi_hdmi_core/reference_clk
  rf_net ps7/FCLK_CLK0 axi_hdmi_core/vdma_clk
  rf_net ps7/FCLK_CLK0 axi_hdmi_core/s_axi_aclk
  rf_net ps7/FCLK_CLK0 axi_hdmi_clkgen/s_axi_aclk
  rf_net ps7/FCLK_CLK0 axi_hdmi_dma/s_axi_aclk
  rf_net ps7/FCLK_CLK0 axi_hdmi_dma/m_src_axi_aclk
  rf_net ps7/FCLK_CLK0 axi_hdmi_dma/m_axis_aclk
  rf_net ps7/FCLK_CLK0 axi_iic_main/s_axi_aclk
  foreach pin {axi_hdmi_core/s_axi_aresetn axi_hdmi_clkgen/s_axi_aresetn \
      axi_hdmi_dma/s_axi_aresetn axi_hdmi_dma/m_src_axi_aresetn \
      axi_iic_main/s_axi_aresetn} {
    rf_net reset_50m/peripheral_aresetn $pin
  }
  rf_bus axi_hdmi_dma/m_axis axi_hdmi_core/s_axis
}

# 200 MHz IDELAYCTRL reference from the 50 MHz PS fabric clock.
create_bd_cell -type ip -vlnv xilinx.com:ip:clk_wiz:6.0 delay_clock
set_property -dict {CONFIG.PRIM_IN_FREQ 50.000 CONFIG.CLKOUT1_REQUESTED_OUT_FREQ 200.000 \
  CONFIG.PRIM_SOURCE No_buffer CONFIG.USE_RESET true CONFIG.RESET_TYPE ACTIVE_LOW} [get_bd_cells delay_clock]
rf_net ps7/FCLK_CLK0 delay_clock/clk_in1
rf_net ps7/FCLK_RESET0_N delay_clock/resetn

# AD9361 fixed 2R2T interface.  LVDS uses the standard six differential lanes;
# CMOS remains an explicitly selected historical diagnostic profile.  Delay
# reference is 200 MHz; hardware still performs a PN/tap-window calibration.
set adi_cmos_or_lvds [expr {$is_lvds ? 0 : 1}]
rf_ip axi_ad9361 axi_ad9361 [list CONFIG.ID 0 CONFIG.CMOS_OR_LVDS_N $adi_cmos_or_lvds CONFIG.MODE_1R1T 0 \
  CONFIG.DELAY_REFCLK_FREQUENCY 200 CONFIG.DAC_DDS_TYPE 1 CONFIG.DAC_DDS_CORDIC_DW 14 \
  CONFIG.ADC_INIT_DELAY 0 CONFIG.DAC_IODELAY_ENABLE 0 CONFIG.USE_SSI_CLK [expr {!$is_lvds}]]
rf_net delay_clock/clk_out1 axi_ad9361/delay_clk
set rf_locked delay_clock/locked
if {$is_lvds} {
  foreach {direction port} {I rx_clk_in_p I rx_clk_in_n I rx_frame_in_p I rx_frame_in_n O tx_frame_out_p O tx_frame_out_n} {
    create_bd_port -dir $direction $port; rf_net $port axi_ad9361/$port
  }
  foreach {direction port} {I rx_data_in_p I rx_data_in_n O tx_data_out_p O tx_data_out_n} {
    create_bd_port -dir $direction -from 5 -to 0 $port; rf_net $port axi_ad9361/$port
  }
  foreach port {enable txnrx} {create_bd_port -dir O $port; rf_net axi_ad9361/$port $port}
  # Center BOTH DDR capture edges in the 16-MHz source-synchronous eye.  This
  # is an actual MMCM phase shift, not a relaxed input-delay/hold exception.
  # Native ADI USE_SSI_CLK=0 uses .clk for the I/O logic; its unused raw clock
  # receiver is optimized away.  Software must keep DATA_CLK at 16 MHz and
  # perform native PN training at the current 4-MSPS rate, never sweep rates.
  create_bd_cell -type ip -vlnv xilinx.com:ip:clk_wiz:6.0 rf_sampling_clock
  set_property -dict {CONFIG.PRIM_IN_FREQ 16.000 CONFIG.PRIM_SOURCE Differential_clock_capable_pin \
    CONFIG.CLKOUT1_REQUESTED_OUT_FREQ 16.000 CONFIG.CLKOUT1_REQUESTED_PHASE 90.000 \
    CONFIG.CLKOUT2_USED true CONFIG.CLKOUT2_REQUESTED_OUT_FREQ 16.000 CONFIG.CLKOUT2_REQUESTED_PHASE 0.000 \
    CONFIG.USE_RESET true CONFIG.RESET_TYPE ACTIVE_LOW} [get_bd_cells rf_sampling_clock]
  rf_net rx_clk_in_p rf_sampling_clock/clk_in1_p
  rf_net rx_clk_in_n rf_sampling_clock/clk_in1_n
  rf_net ps7/FCLK_RESET0_N rf_sampling_clock/resetn
  rf_net rf_sampling_clock/clk_out1 axi_ad9361/clk
  # Forwarded TX clock is deliberately 90 degrees earlier than TX data;
  # the original zero-skew ADI clock ODDR is unused/optimized away. Both
  # clocks share the same MMCM, and STA must retain their real relationship.
  create_bd_cell -type module -reference rf_lvds_forward_clock rf_fb_clock
  rf_net rf_sampling_clock/clk_out2 rf_fb_clock/clk
  create_bd_port -dir O tx_clk_out_p
  create_bd_port -dir O tx_clk_out_n
  rf_net rf_fb_clock/tx_clk_p tx_clk_out_p
  rf_net rf_fb_clock/tx_clk_n tx_clk_out_n
  create_bd_cell -type ip -vlnv xilinx.com:ip:util_vector_logic:2.0 rf_clock_locks
  set_property -dict {CONFIG.C_SIZE 1 CONFIG.C_OPERATION and} [get_bd_cells rf_clock_locks]
  rf_net delay_clock/locked rf_clock_locks/Op1
  rf_net rf_sampling_clock/locked rf_clock_locks/Op2
  set rf_locked rf_clock_locks/Res
} else {
  rf_net axi_ad9361/l_clk axi_ad9361/clk
  foreach {direction port} {I rx_clk_in I rx_frame_in O tx_clk_out O tx_frame_out} {
    create_bd_port -dir $direction $port; rf_net $port axi_ad9361/$port
  }
  foreach {direction port} {I rx_data_in O tx_data_out} {
    create_bd_port -dir $direction -from 11 -to 0 $port; rf_net $port axi_ad9361/$port
  }
  foreach port {enable txnrx} {create_bd_port -dir O $port; rf_net axi_ad9361/$port $port}
}
rf_net constant_zero/dout axi_ad9361/tdd_sync
# Do not carry the ADI reference design's live 1R1T/2R2T clock mux into this
# candidate.  Its mode-select originates in the control domain and was the
# source of critical CDC paths in implementation.  The software contract fixes
# the board to 2R2T for this bitstream; changing port mode requires reset and a
# profile-specific rebuild.  CMOS is /2; the standard LVDS 4 MSPS / 16 MHz
# profile uses a static /4 sample domain.
set sample_clk_cell rf_static_clkdiv
if {$is_lvds} {set sample_clk_cell rf_static_clkdiv4}
create_bd_cell -type module -reference $sample_clk_cell $sample_clk_cell
rf_net axi_ad9361/l_clk ${sample_clk_cell}/clk
set rf_stream_clk ${sample_clk_cell}/clk_out
if {$is_lvds} {set rf_stream_clk ps7/FCLK_CLK0}
# In the low-rate LVDS profile, the util_wfifo/util_rfifo burst handshake
# alone owns the clock boundary. Run pack/unpack and ALL DMAC interfaces at
# 50 MHz, avoiding a second asynchronous descriptor boundary in request_arb.
# ADC/DAC valid is still one sample per four 16-MHz interface cycles; the
# sample_clock_divider receipt is this logical protocol ratio, not a claim
# that the DMA bus runs at 4 MHz. The unused /4 primitive can be optimized.
rf_reset reset_rf $rf_stream_clk ps7/FCLK_RESET0_N $rf_locked constant_zero/dout
# The AD9361 exported reset is synchronous to its interface clk, not
# necessarily l_clk. Synchronize release explicitly for the FIFO l_clk side
# and the overflow event source; do not hide reset-domain crossings in XDC.
create_bd_cell -type ip -vlnv xilinx.com:ip:proc_sys_reset:5.0 reset_rf_lclk
set_property -dict {CONFIG.C_EXT_RESET_HIGH 1 CONFIG.C_AUX_RESET_HIGH 1} [get_bd_cells reset_rf_lclk]
rf_net axi_ad9361/l_clk reset_rf_lclk/slowest_sync_clk
rf_net axi_ad9361/rst reset_rf_lclk/ext_reset_in
rf_net $rf_locked reset_rf_lclk/dcm_locked
rf_net constant_zero/dout reset_rf_lclk/aux_reset_in reset_rf_lclk/mb_debug_sys_rst

# RX: four signed I/Q lanes -> dual-clock FIFO -> cpack -> ADI S2MM DMAC.
rf_ip rf_rx_fifo util_wfifo {CONFIG.NUM_OF_CHANNELS 4 CONFIG.DIN_ADDRESS_WIDTH 4 CONFIG.DIN_DATA_WIDTH 16 CONFIG.DOUT_DATA_WIDTH 16}
rf_ip rf_rx_pack util_cpack2 {CONFIG.NUM_OF_CHANNELS 4 CONFIG.SAMPLE_DATA_WIDTH 16 CONFIG.INTERFACE_TYPE 1}
rf_ip rf_rx_dmac axi_dmac [list CONFIG.DMA_TYPE_SRC [expr {$buffered_rx ? 1 : 2}] CONFIG.DMA_TYPE_DEST 0 CONFIG.CYCLIC $rx_cyclic CONFIG.SYNC_TRANSFER_START 1 CONFIG.AXIS_TUSER_SYNC 0 CONFIG.AXI_SLICE_SRC 0 CONFIG.AXI_SLICE_DEST 0 CONFIG.DMA_2D_TRANSFER 0 CONFIG.DMA_SG_TRANSFER 1 CONFIG.DMA_DATA_WIDTH_SRC 64 CONFIG.DMA_DATA_WIDTH_SG 64 CONFIG.CACHE_COHERENT 0 CONFIG.DMA_AXI_ADDR_WIDTH 32]
rf_net axi_ad9361/l_clk rf_rx_fifo/din_clk
rf_net reset_rf_lclk/peripheral_reset rf_rx_fifo/din_rst
rf_net $rf_stream_clk rf_rx_fifo/dout_clk rf_rx_pack/clk
if {$buffered_rx} {
  create_bd_cell -type module -reference rf_rx_elastic_fifo rf_rx_elastic_buffer
  rf_net $rf_stream_clk rf_rx_elastic_buffer/clk rf_rx_dmac/s_axis_aclk
  rf_net reset_rf/peripheral_aresetn rf_rx_elastic_buffer/resetn
  rf_net rf_rx_pack/packed_fifo_wr_data rf_rx_elastic_buffer/packed_fifo_wr_data
  rf_net rf_rx_elastic_buffer/packed_fifo_wr_overflow rf_rx_pack/packed_fifo_wr_overflow
  # packed_sync is a *word alignment* signal (high on every full four-lane
  # sample), not a capture boundary.  Flush only while Linux disables this
  # DMAC via CTRL.ENABLE, preserving leftovers across short descriptors.
  create_bd_cell -type module -reference rf_rx_dma_enable_monitor rf_rx_dma_enable_monitor
  rf_net $rf_stream_clk rf_rx_dma_enable_monitor/clk
  # Mirror the DMAC control plane's reset, not the RF MMCM lock reset. If
  # lock drops, FIFO data are discarded but the observed CTRL bit must not
  # silently diverge from the DMAC register. Software abort/restart is still
  # mandatory after a clock interruption; this is not a DMA-idle handshake.
  rf_net reset_50m/peripheral_aresetn rf_rx_dma_enable_monitor/resetn
  # The read-only AXI_MON interface joins the entire existing AXI-Lite net
  # below. Scalar input-to-input taps would override/disconnect the slave's
  # constituent write-channel pins in IP Integrator.
  rf_net rf_rx_dma_enable_monitor/flush_sync rf_rx_elastic_buffer/flush_sync
  create_bd_cell -type ip -vlnv xilinx.com:ip:util_vector_logic:2.0 rf_rx_capture_write_gate
  set_property -dict {CONFIG.C_OPERATION and CONFIG.C_SIZE 1} [get_bd_cells rf_rx_capture_write_gate]
  # Continuous disabled flush intentionally retains *no* old sample. The
  # FIFO's standalone single-cycle flush may accept a coincident new word;
  # gating ingress here makes CTRL.ENABLE the precise capture-window boundary.
  rf_net rf_rx_pack/packed_fifo_wr_en rf_rx_capture_write_gate/Op1
  rf_net rf_rx_dma_enable_monitor/dma_enabled rf_rx_capture_write_gate/Op2
  rf_net rf_rx_capture_write_gate/Res rf_rx_elastic_buffer/packed_fifo_wr_en
  rf_bus rf_rx_elastic_buffer/M_AXIS rf_rx_dmac/s_axis
} else {
  rf_net $rf_stream_clk rf_rx_dmac/fifo_wr_clk
  rf_bus rf_rx_pack/packed_fifo_wr rf_rx_dmac/fifo_wr
}
# The monitor only observes the already-buffered 50-MHz AXIS path and the
# matching packed-FIFO overflow alarm.  It deliberately refuses adc_dovf
# (AD9361 l_clk) and unavailable FIFO accept_write counters, so this hook is
# legal only after the direct elastic-buffer -> RX-DMAC connection exists.
if {$rf_rx_health_enabled} {
  if {!$buffered_rx} {error "RX health monitor requires buffered LVDS RX"}
  rf_rx_health_integrate 0x44A10000
}
rf_net reset_rf/peripheral_aresetn rf_rx_fifo/dout_rstn
rf_net reset_rf/peripheral_reset rf_rx_pack/reset
foreach {i channel} {0 i0 1 q0 2 i1 3 q1} {
  foreach field {enable valid data} {rf_net axi_ad9361/adc_${field}_$channel rf_rx_fifo/din_${field}_$i}
  rf_net rf_rx_fifo/dout_enable_$i rf_rx_pack/enable_$i
  rf_net rf_rx_fifo/dout_data_$i rf_rx_pack/fifo_wr_data_$i
}
rf_net rf_rx_fifo/din_ovf axi_ad9361/adc_dovf
if {$rf_rx_health_enabled} {
  # The health hook was intentionally inserted before lane wiring so its AXIS
  # endpoints could be checked.  Now that the AD9361 l_clk-domain overflow net
  # exists, prove it did not acquire a monitor connection.
  rf_rx_health_assert_no_adc_dovf_connection
}
rf_net rf_rx_fifo/dout_valid_0 rf_rx_pack/fifo_wr_en
rf_net rf_rx_pack/fifo_wr_overflow rf_rx_fifo/dout_ovf
rf_net rf_rx_pack/packed_sync rf_rx_dmac/sync

# TX: ADI MM2S DMAC -> unpack -> dual-clock FIFO -> four AD9361 DAC lanes.
rf_ip rf_tx_fifo util_rfifo {CONFIG.NUM_OF_CHANNELS 4 CONFIG.DIN_ADDRESS_WIDTH 4 CONFIG.DIN_DATA_WIDTH 16 CONFIG.DOUT_DATA_WIDTH 16}
rf_ip rf_tx_unpack util_upack2 {CONFIG.NUM_OF_CHANNELS 4 CONFIG.SAMPLE_DATA_WIDTH 16}
rf_ip rf_tx_dmac axi_dmac {CONFIG.DMA_TYPE_SRC 0 CONFIG.DMA_TYPE_DEST 1 CONFIG.CYCLIC 1 CONFIG.AXI_SLICE_SRC 0 CONFIG.AXI_SLICE_DEST 0 CONFIG.DMA_2D_TRANSFER 0 CONFIG.DMA_SG_TRANSFER 1 CONFIG.DMA_DATA_WIDTH_DEST 64 CONFIG.DMA_DATA_WIDTH_SG 64 CONFIG.CACHE_COHERENT 0 CONFIG.DMA_AXI_ADDR_WIDTH 32}
rf_net axi_ad9361/l_clk rf_tx_fifo/dout_clk
rf_net reset_rf_lclk/peripheral_reset rf_tx_fifo/dout_rst
rf_net $rf_stream_clk rf_tx_fifo/din_clk rf_tx_unpack/clk rf_tx_dmac/m_axis_aclk
rf_net reset_rf/peripheral_aresetn rf_tx_fifo/din_rstn
rf_net reset_rf/peripheral_reset rf_tx_unpack/reset
foreach {i channel} {0 i0 1 q0 2 i1 3 q1} {
  foreach field {enable valid data} {rf_net axi_ad9361/dac_${field}_$channel rf_tx_fifo/dout_${field}_$i}
  rf_net rf_tx_unpack/enable_$i rf_tx_fifo/din_enable_$i
  rf_net rf_tx_unpack/fifo_rd_valid rf_tx_fifo/din_valid_in_$i
  rf_net rf_tx_unpack/fifo_rd_data_$i rf_tx_fifo/din_data_$i
}
rf_net rf_tx_fifo/dout_unf axi_ad9361/dac_dunf
rf_net rf_tx_unpack/fifo_rd_en rf_tx_fifo/din_valid_0
rf_net rf_tx_unpack/fifo_rd_underflow rf_tx_fifo/din_unf
rf_bus rf_tx_dmac/m_axis rf_tx_unpack/s_axis
if {$is_lvds} {
  foreach dmac {rf_rx_dmac rf_tx_dmac} {
    set_property -dict {CONFIG.ASYNC_CLK_REQ_SRC false CONFIG.ASYNC_CLK_SRC_DEST false \
      CONFIG.ASYNC_CLK_DEST_REQ false CONFIG.ASYNC_CLK_REQ_SG false \
      CONFIG.ASYNC_CLK_SRC_SG false CONFIG.ASYNC_CLK_DEST_SG false} [get_bd_cells $dmac]
  }
}

# Dedicated PS-visible SPI and safe GPIO control.  No unwanted TX is emitted
# while GPIO reset is at its documented all-zero default.
create_bd_cell -type ip -vlnv xilinx.com:ip:axi_quad_spi:3.2 rf_spi
# AD9361 uses a conventional 4-wire control SPI bus.  Disable the Quad-SPI
# flash-memory personality: it leaves internal IOB-constrained IO0/IO2/IO3
# registers without package IO and produces a post-place critical warning.
# C_SPI_MODE=0 means STANDARD SPI personality, not CPHA=0. Linux sets the
# runtime SPICR to CPOL=0/CPHA=1 from spi-cpha, matching the ADI PHY contract.
set_property -dict {CONFIG.C_USE_STARTUP 0 CONFIG.C_NUM_SS_BITS 1 CONFIG.C_SPI_MODE 0 CONFIG.C_SPI_MEMORY 0 CONFIG.C_NUM_TRANSFER_BITS 8 CONFIG.C_SCK_RATIO 16} [get_bd_cells rf_spi]
foreach {name direction pin} {spi_clk O sck_o spi_mosi O io0_o spi_miso I io1_i spi_csn O ss_o} {
  create_bd_port -dir $direction $name
  rf_net $name rf_spi/$pin
}
rf_net constant_zero/dout rf_spi/sck_i rf_spi/io0_i
rf_net constant_one/dout rf_spi/ss_i
rf_net ps7/FCLK_CLK0 rf_spi/ext_spi_clk
create_bd_cell -type ip -vlnv xilinx.com:ip:axi_gpio:2.0 rf_gpio
# Channel 1: reset/AGC/sync/AD9361 enables and four spare outputs.  Channel 2:
# eight readback/control-status inputs, retaining the single documented GPIO ABI.
set_property -dict {CONFIG.C_GPIO_WIDTH 9 CONFIG.C_ALL_OUTPUTS 1 CONFIG.C_IS_DUAL 1 CONFIG.C_GPIO2_WIDTH 8 CONFIG.C_ALL_INPUTS_2 1 CONFIG.C_DOUT_DEFAULT 0x00000000 CONFIG.C_INTERRUPT_PRESENT 0} [get_bd_cells rf_gpio]
foreach {i dest external} {0 gpio_resetb 1 1 gpio_en_agc 1 2 gpio_sync 1 3 axi_ad9361/up_enable 0 4 axi_ad9361/up_txnrx 0} {
  create_bd_cell -type ip -vlnv xilinx.com:ip:xlslice:1.0 gpio_bit$i
  set_property -dict [list CONFIG.DIN_WIDTH 9 CONFIG.DIN_FROM $i CONFIG.DIN_TO $i CONFIG.DOUT_WIDTH 1] [get_bd_cells gpio_bit$i]
  rf_net rf_gpio/gpio_io_o gpio_bit$i/Din
  if {$external} {create_bd_port -dir O $dest}
  rf_net gpio_bit$i/Dout $dest
}
# The four general control outputs and eight status inputs are externally
# visible and software-addressable so Linux can validate daughtercard wiring.
create_bd_port -dir O -from 3 -to 0 gpio_ctl
create_bd_port -dir I -from 7 -to 0 gpio_status
rf_net gpio_status rf_gpio/gpio2_io_i
create_bd_cell -type ip -vlnv xilinx.com:ip:xlconcat:2.1 gpio_ctl_concat
set_property CONFIG.NUM_PORTS 4 [get_bd_cells gpio_ctl_concat]
foreach {i bit} {5 0 6 1 7 2 8 3} {
  create_bd_cell -type ip -vlnv xilinx.com:ip:xlslice:1.0 gpio_ctl_bit$bit
  set_property -dict [list CONFIG.DIN_WIDTH 9 CONFIG.DIN_FROM $i CONFIG.DIN_TO $i CONFIG.DOUT_WIDTH 1] [get_bd_cells gpio_ctl_bit$bit]
  rf_net rf_gpio/gpio_io_o gpio_ctl_bit$bit/Din
  rf_net gpio_ctl_bit$bit/Dout gpio_ctl_concat/In$bit
}
rf_net gpio_ctl_concat/dout gpio_ctl

# AXI clock/reset routing: all memory/control AXI ports run at PS FCLK0 (50 MHz).
set clocks [list ps7/M_AXI_GP0_ACLK ps7/S_AXI_HP0_ACLK control_xbar/ACLK control_xbar/S00_ACLK \
  control_xbar/M00_ACLK control_xbar/M01_ACLK control_xbar/M02_ACLK control_xbar/M03_ACLK control_xbar/M04_ACLK control_xbar/M05_ACLK control_xbar/M06_ACLK \
  memory_xbar/aclk \
  reset_50m/slowest_sync_clk detector_0/aclk detector_dma/s_axi_lite_aclk detector_dma/m_axi_mm2s_aclk detector_dma/m_axi_s2mm_aclk \
  axi_ad9361/s_axi_aclk rf_rx_dmac/s_axi_aclk rf_rx_dmac/m_dest_axi_aclk rf_rx_dmac/m_sg_axi_aclk rf_tx_dmac/s_axi_aclk rf_tx_dmac/m_src_axi_aclk rf_tx_dmac/m_sg_axi_aclk rf_spi/s_axi_aclk rf_gpio/s_axi_aclk]
foreach pin $clocks {rf_net ps7/FCLK_CLK0 $pin}
set resets [list control_xbar/ARESETN control_xbar/S00_ARESETN control_xbar/M00_ARESETN control_xbar/M01_ARESETN control_xbar/M02_ARESETN control_xbar/M03_ARESETN control_xbar/M04_ARESETN control_xbar/M05_ARESETN control_xbar/M06_ARESETN \
  memory_xbar/aresetn \
  detector_0/aresetn detector_dma/axi_resetn axi_ad9361/s_axi_aresetn rf_rx_dmac/s_axi_aresetn rf_rx_dmac/m_dest_axi_aresetn rf_rx_dmac/m_sg_axi_aresetn rf_tx_dmac/s_axi_aresetn rf_tx_dmac/m_src_axi_aresetn rf_tx_dmac/m_sg_axi_aresetn rf_spi/s_axi_aresetn rf_gpio/s_axi_aresetn]
foreach pin $resets {rf_net reset_50m/peripheral_aresetn $pin}
if {$rf_rxth_diag_enabled} {
  # M10 belongs to the separately selected RX9 observer, but its interconnect
  # clock/reset must be valid during the base graph's first validation (which
  # precedes RX9 cell creation to avoid tying observer inputs to zero).
  rf_net ps7/FCLK_CLK0 control_xbar/M10_ACLK
  rf_net reset_50m/peripheral_aresetn control_xbar/M10_ARESETN
}
if {$hdmi_demo} {
  foreach pin {control_xbar/M07_ACLK control_xbar/M08_ACLK \
      control_xbar/M09_ACLK control_xbar/M10_ACLK} {
    rf_net ps7/FCLK_CLK0 $pin
  }
  foreach pin {control_xbar/M07_ARESETN control_xbar/M08_ARESETN \
      control_xbar/M09_ARESETN control_xbar/M10_ARESETN} {
    rf_net reset_50m/peripheral_aresetn $pin
  }
  foreach {mi bus} {M07_AXI axi_hdmi_clkgen/s_axi M08_AXI axi_hdmi_dma/s_axi \
      M09_AXI axi_hdmi_core/s_axi M10_AXI axi_iic_main/S_AXI} {
    rf_bus control_xbar/$mi $bus
  }
}
foreach {mi bus} {M02_AXI axi_ad9361/s_axi M04_AXI rf_tx_dmac/s_axi M05_AXI rf_spi/AXI_LITE M06_AXI rf_gpio/S_AXI} {rf_bus control_xbar/$mi $bus}
if {$buffered_rx} {
  rf_bus control_xbar/M03_AXI rf_rx_dmac/s_axi
  set rx_control_net [get_bd_intf_nets -of_objects [get_bd_intf_pins control_xbar/M03_AXI]]
  if {[llength $rx_control_net] != 1} {error "RX DMAC control interface net is not unique"}
  connect_bd_intf_net -intf_net $rx_control_net [get_bd_intf_pins control_xbar/M03_AXI] \
    [get_bd_intf_pins rf_rx_dma_enable_monitor/AXI_MON]
} else {
  rf_bus control_xbar/M03_AXI rf_rx_dmac/s_axi
}
foreach {si bus} {S02_AXI rf_rx_dmac/m_dest_axi S03_AXI rf_rx_dmac/m_sg_axi S04_AXI rf_tx_dmac/m_src_axi S05_AXI rf_tx_dmac/m_sg_axi} {rf_bus $bus memory_xbar/$si}
if {$hdmi_demo} {rf_bus axi_hdmi_dma/m_src_axi memory_xbar/S06_AXI}

# Fixed IRQ ABI: detector=0, detector-DMA=1/2, RF-RX=3, RF-TX=4, SPI=5.
create_bd_cell -type ip -vlnv xilinx.com:ip:xlconcat:2.1 interrupt_concat
set_property CONFIG.NUM_PORTS 16 [get_bd_cells interrupt_concat]
foreach {source dest} {detector_0/irq In0 detector_dma/mm2s_introut In1 detector_dma/s2mm_introut In2 rf_rx_dmac/irq In3 rf_tx_dmac/irq In4 rf_spi/ip2intc_irpt In5} {rf_net $source interrupt_concat/$dest}
set first_free_irq 6
if {$hdmi_demo} {
  foreach {source dest} {axi_hdmi_dma/irq In6 axi_iic_main/iic2intc_irpt In7} {
    rf_net $source interrupt_concat/$dest
  }
  set first_free_irq 8
}
# RXTH/IMBC have a stable Linux interrupt ABI at F2P[8..10], independent of
# whether HDMI exists.  In a headless candidate deliberately reserve and tie
# off F2P[6..7]; they must never be silently repurposed by this extension.
if {!$hdmi_demo && $rf_rx_health_enabled} {
  rf_net constant_zero/dout interrupt_concat/In6
  rf_net constant_zero/dout interrupt_concat/In7
  set first_free_irq 8
}
# A separately selected continuous extension may reserve additional IRQs only
# after the base/HDMI ABI has been fixed.  It must return the first untouched
# slot, so the following constant tie-offs can never erase a live endpoint.
if {$rf_rx_health_enabled && [llength [info procs rf_rx_health_irq_extend]]} {
  set first_free_irq [rf_rx_health_irq_extend interrupt_concat $first_free_irq]
}
for {set index $first_free_irq} {$index < 16} {incr index} {rf_net constant_zero/dout interrupt_concat/In$index}
rf_net interrupt_concat/dout ps7/IRQ_F2P

# Stable software ABI address map.
set cpu_space [get_bd_addr_spaces ps7/Data]
foreach {offset bus} {0x43C00000 detector_0/s_axi 0x40400000 detector_dma/S_AXI_LITE 0x79020000 axi_ad9361/s_axi 0x7C400000 rf_rx_dmac/s_axi 0x7C420000 rf_tx_dmac/s_axi 0x44A00000 rf_spi/AXI_LITE 0x41200000 rf_gpio/S_AXI} {
  set seg [get_bd_addr_segs -of_objects [get_bd_intf_pins $bus]]
  if {[llength $seg] != 1} {error "Expected exactly one register segment for $bus, got $seg"}
  assign_bd_address -target_address_space $cpu_space -offset $offset -range 64K $seg
}
if {$hdmi_demo} {
  foreach {offset bus} {0x79000000 axi_hdmi_clkgen/s_axi 0x43000000 axi_hdmi_dma/s_axi \
      0x70E00000 axi_hdmi_core/s_axi 0x41600000 axi_iic_main/S_AXI} {
    set seg [get_bd_addr_segs -of_objects [get_bd_intf_pins $bus]]
    if {[llength $seg] != 1} {error "Expected exactly one HDMI register segment for $bus, got $seg"}
    assign_bd_address -target_address_space $cpu_space -offset $offset -range 64K $seg
  }
}
foreach space {detector_dma/Data_MM2S detector_dma/Data_S2MM rf_rx_dmac/m_dest_axi rf_rx_dmac/m_sg_axi rf_tx_dmac/m_src_axi rf_tx_dmac/m_sg_axi} {
  assign_bd_address -target_address_space [get_bd_addr_spaces $space] -offset 0 -range 512M [get_bd_addr_segs ps7/S_AXI_HP0/HP0_DDR_LOWOCM]
}
if {$hdmi_demo} {
  assign_bd_address -target_address_space [get_bd_addr_spaces axi_hdmi_dma/m_src_axi] -offset 0 -range 512M [get_bd_addr_segs ps7/S_AXI_HP0/HP0_DDR_LOWOCM]
}

validate_bd_design
if {$rf_rx_health_enabled && [llength [info procs rf_rx_health_address_finalize]]} {
  rf_rx_health_address_finalize
  validate_bd_design
}
if {$rf_rxth_diag_enabled} {
  # First let the base/RXTH address graph settle.  Creating status inputs
  # earlier would make this required first validation tie them to zero.  The
  # RX9 observer is then attached only to already-built production nets.
  rf_rx_transport_diag_create
  rf_rx_transport_diag_tap_all
}
validate_bd_design
save_bd_design
set bd [get_files */zedboard_rf_system.bd]
set_property synth_checkpoint_mode None $bd
# `all` also generates simulation and GUI collateral for every ADI IP.  The
# continuous candidate is built on a memory-constrained host and synthesis or
# implementation needs only synthesizable targets.  Keep full generation the
# default; a reviewed wrapper may opt into the smaller valid target set.
set rf_ip_target all
if {[info exists ::env(MIMO_RF_IP_TARGET)]} {set rf_ip_target $::env(MIMO_RF_IP_TARGET)}
if {$rf_ip_target ni {all synthesis}} {error "MIMO_RF_IP_TARGET must be all or synthesis"}
generate_target $rf_ip_target $bd
set wrappers [make_wrapper -files $bd -top]
add_files -norecurse $wrappers
set_property top zedboard_rf_system_wrapper [current_fileset]
set physical_xdc [file join $root pl constraints zedboard_fmcomms3_cmos_1v8.xdc]
if {$is_lvds} {set physical_xdc [file join $root pl constraints zedboard_fmcomms3_lvds_2v5.xdc]}
add_files -fileset constrs_1 -norecurse $physical_xdc
if {$hdmi_demo} {
  add_files -fileset constrs_1 -norecurse [file join $root pl constraints zedboard_hdmi_adv7511.xdc]
}
if {$is_lvds} {add_files -fileset constrs_1 -norecurse [file join $root pl constraints zedboard_rf_sync_attributes.xdc]}
# A generated source-synchronous timing-budget XDC is intentionally optional
# for BD/synthesis diagnostics, but mandatory before release artifacts.  It is
# supplied through a reviewed profile rather than guessed inside this script.
set external_budget_xdc ""
set external_budget_status ""
if {[info exists ::env(MIMO_RF_EXTERNAL_BUDGET_XDC)]} {
  set external_budget_xdc [file normalize $::env(MIMO_RF_EXTERNAL_BUDGET_XDC)]
  if {![file exists $external_budget_xdc]} {error "MIMO_RF_EXTERNAL_BUDGET_XDC does not exist: $external_budget_xdc"}
  set budget_fd [open $external_budget_xdc r]
  set budget_text [read $budget_fd]
  close $budget_fd
  if {![regexp {# profile_status=([A-Z_]+)} $budget_text -> external_budget_status]} {
    error "RF timing-budget XDC must be generated by generate_zedboard_rf_timing.py and carry profile_status"
  }
  if {$external_budget_status ni {PREBOARD_CONSERVATIVE_APPROVED MEASURED_APPROVED}} {
    error "RF timing-budget status is not approved for an offline candidate: $external_budget_status"
  }
  add_files -fileset constrs_1 -norecurse $external_budget_xdc
}
update_compile_order -fileset sources_1
set spi_budget_xdc ""
if {$is_lvds && [info exists ::env(MIMO_RF_SPI_BUDGET_XDC)]} {
  set spi_budget_xdc [file normalize $::env(MIMO_RF_SPI_BUDGET_XDC)]
  if {![file isfile $spi_budget_xdc]} {error "Missing SPI timing model: $spi_budget_xdc"}
  set spi_fd [open $spi_budget_xdc r]
  set spi_text [read $spi_fd]
  close $spi_fd
  if {$is_lvds && ![regexp {# spi_mode=1\y} $spi_text]} {
    error "LVDS SPI XDC must match ADI spi-cpha: runtime CPOL=0/CPHA=1"
  }
  add_files -fileset constrs_1 -norecurse $spi_budget_xdc
}
write_bd_tcl -force [file join $out rebuild_zedboard_rf_bd.tcl]
set fd [open [file join $out address_map.tsv] w]
puts $fd "segment\toffset\trange"
foreach seg [get_bd_addr_segs -of_objects [get_bd_addr_spaces]] {if {[get_property CLASS $seg] eq "bd_addr_seg"} {puts $fd "$seg\t[get_property OFFSET $seg]\t[get_property RANGE $seg]"}}
close $fd
set fd [open [file join $out rf_build_receipt.txt] w]
set interface_clock_hz [expr {$is_lvds ? 16000000 : 8000000}]
set sample_clock_divider [expr {$is_lvds ? 4 : 2}]
foreach line [list "vivado=[version -short]" "part=$part" "adi_commit=$adi_commit" "adi_hdl_dir=$adi_root" "interface_profile=$rf_interface" "vadj_mv=[expr {$is_lvds ? 2500 : 1800}]" "rx_channels=2" "tx_channels=2" "mode_cmos_2r2t=[expr {!$is_lvds}]" "mode_lvds_2r2t=$is_lvds" "rf_mode_switching=forbidden_requires_reset_and_rebuild" "sample_rate_hz=4000000" "interface_clock_hz=$interface_clock_hz" "sample_clock_divider=$sample_clock_divider" "idelay_refclk_mhz=200" "control_axi_clock_hz=50000000" "detector_base=0x43C00000" "detector_dma_base=0x40400000" "rf_base=0x79020000" "rf_rx_dma_base=0x7C400000" "rf_tx_dma_base=0x7C420000" "rf_spi_base=0x44A00000" "rf_gpio_base=0x41200000" "irq_detector=0" "irq_detector_dma_mm2s=1" "irq_detector_dma_s2mm=2" "irq_rf_rx_dma=3" "irq_rf_tx_dma=4" "irq_rf_spi=5" "external_budget_xdc=$external_budget_xdc" "external_budget_status=$external_budget_status" "artifact_state=DIAGNOSTIC_ONLY_NOT_DEPLOYABLE" "bitstream_hardware_acceptance=0"] {puts $fd $line}
puts $fd "rx_observer_abi=$rx_observer_abi"
puts $fd "diagnostic_only=$rx_observer"
puts $fd "hdmi_demo=$hdmi_demo"
puts $fd "enable_hdmi=$hdmi_demo"
puts $fd "display_mode=$display_mode"
puts $fd "rf_rx_health_monitor=$rf_rx_health_enabled"
if {$rf_rx_health_enabled} {puts $fd "rf_rx_health_base=0x44A10000"}
puts $fd "rx9_transport_diagnostic=$rf_rxth_diag_enabled"
if {$rf_rxth_diag_enabled} {puts $fd "rx9_transport_diagnostic_base=0x7C460000"}
if {$hdmi_demo} {
  puts $fd "hdmi_base=0x70E00000"
  puts $fd "hdmi_dma_base=0x43000000"
  puts $fd "hdmi_clock_base=0x79000000"
  puts $fd "i2c_base=0x41600000"
  puts $fd "irq_hdmi_dma=6"
  puts $fd "irq_i2c=7"
}
close $fd
puts "ZEDBOARD_RF_BD_VALIDATED True"
if {$mode ne "validate"} {
  if {$mode eq "implement"} {
    set_property strategy Performance_ExplorePostRoutePhysOpt [get_runs impl_1]
  }
  if {$is_lvds && $buffered_rx} {
    source [file join $root pl scripts capture_zedboard_rf_inputs.tcl]
  }
  launch_runs synth_1 -jobs 2
  wait_on_run synth_1
  if {[get_property PROGRESS [get_runs synth_1]] ne "100%"} {error "RF synthesis failed"}
  open_run synth_1
  if {[llength [get_cells -hier -quiet -filter {IS_BLACKBOX == 1}]]} {error "Black boxes remain"}
  report_utilization -file [file join $out synthesis_utilization.rpt]
  report_timing_summary -report_unconstrained -file [file join $out synthesis_timing.rpt]
  report_drc -file [file join $out synthesis_drc.rpt]
  if {[llength [info procs rf_rx_health_audit_fault_cdc]]} {
    rf_rx_health_audit_fault_cdc $out synthesis
  }
  puts "ZEDBOARD_RF_SYNTHESIS_COMPLETE True"
}
if {$mode eq "implement"} {
  # The AD9361 source-synchronous clock has a short internal hold path through
  # vendor DSP/SRL logic.  Ask Vivado for its non-destructive post-route
  # physical-optimization strategy rather than masking the path with a timing
  # exception; acceptance still requires non-negative setup and hold reports.
  # Route first.  Bit/XSA release is a separate, fail-closed phase below.
  launch_runs impl_1 -to_step route_design -jobs 2
  wait_on_run impl_1
  # A run intentionally stopped at route_design reports 83.33% because the
  # strategy still has its automatic post-route phys_opt step pending.  Check
  # the routed checkpoint instead of treating that expected progress value as
  # a failed implementation.
  set impl_dir [get_property DIRECTORY [get_runs impl_1]]
  set routed_dcp [file join $impl_dir zedboard_rf_system_wrapper_routed.dcp]
  set impl_status [get_property STATUS [get_runs impl_1]]
  if {![file exists $routed_dcp] || [string match -nocase "*error*" $impl_status] || [string match -nocase "*fail*" $impl_status]} {
    error "RF route_design did not complete: status=$impl_status checkpoint=$routed_dcp"
  }
  # A run stopped at route_design has no open implementation run.  Reopen the
  # exact routed checkpoint so the following physical hold repair operates on
  # the routed netlist, rather than failing on an unavailable post-route run.
  open_checkpoint $routed_dcp
  # This fixes the actual short FDRE-to-FDRE DDS hold paths with router/hold
  # buffers.  It is a physical fix, not an XDC waiver; retain its checkpoint
  # so an independent signoff invocation audits the same routed image.
  phys_opt_design -directive ExploreWithAggressiveHoldFix
  route_design
  write_checkpoint -force [file join $out rf_signoff_route.dcp]
  if {[llength [info procs rf_rx_health_audit_fault_cdc]]} {
    rf_rx_health_audit_fault_cdc $out implementation
  }
  report_methodology -file [file join $out implementation_methodology.rpt]
  report_timing_summary -report_unconstrained -file [file join $out implementation_timing.rpt]
  report_pulse_width -all_violators -file [file join $out implementation_pulse_width.rpt]
  report_drc -file [file join $out implementation_drc.rpt]
  report_cdc -file [file join $out implementation_cdc.rpt]
  if {$hdmi_demo} {
    report_cdc -details -file [file join $out implementation_cdc_details.rpt]
  }
  set timing_fd [open [file join $out implementation_timing.rpt] r]
  set coverage_errors [rf_timing_coverage [read $timing_fd] $hdmi_demo]
  close $timing_fd
  set cdc_fd [open [file join $out implementation_cdc.rpt] r]
  set cdc_text [read $cdc_fd]
  close $cdc_fd
  set cdc_details ""
  if {$hdmi_demo} {
    set cdc_details_fd [open [file join $out implementation_cdc_details.rpt] r]
    set cdc_details [read $cdc_details_fd]
    close $cdc_details_fd
  }
  set pulse_fd [open [file join $out implementation_pulse_width.rpt] r]
  set pulse_text [read $pulse_fd]
  close $pulse_fd
  set setup [get_timing_paths -quiet -delay_type max -max_paths 1]
  set hold [get_timing_paths -quiet -delay_type min -max_paths 1]
  if {[llength $setup] != 1 || [llength $hold] != 1} {error "No routed setup/hold path available for release gate"}
  set setup_slack [get_property SLACK $setup]
  set hold_slack [get_property SLACK $hold]
  set severe [get_drc_violations -quiet -filter {SEVERITY == Error || SEVERITY == {Critical Warning}}]
  set setup_pass [expr {$setup_slack >= 0}]
  set hold_pass [expr {$hold_slack >= 0}]
  # report_pulse_width lists explicit violations with this token.  Keep the
  # complete report as evidence; do not use a timing exception to silence it.
  set pulse_width_pass [expr {[string first "VIOLATED" $pulse_text] < 0}]
  set drc_pass [expr {![llength $severe]}]
  set cdc_counts [rf_cdc_audit $cdc_text $cdc_details $hdmi_demo]
  set cdc_pass [expr {[dict get $cdc_counts critical] == 0 && [dict get $cdc_counts unsafe] == 0 && [dict get $cdc_counts unknown] == 0}]
  set gate_errors {}
  if {!$setup_pass} {lappend gate_errors "setup_slack=$setup_slack"}
  if {!$hold_pass} {lappend gate_errors "hold_slack=$hold_slack"}
  if {!$pulse_width_pass} {lappend gate_errors "pulse_width"}
  if {!$drc_pass} {lappend gate_errors "drc=$severe"}
  if {!$cdc_pass} {lappend gate_errors "cdc=$cdc_counts"}
  if {$external_budget_xdc eq ""} {lappend gate_errors "external_budget_xdc_missing"}
  if {$is_lvds && $spi_budget_xdc eq ""} {lappend gate_errors "spi_external_budget_xdc_missing"}
  if {$is_lvds && [llength $coverage_errors]} {lappend gate_errors "timing_coverage=$coverage_errors"}
  if {[llength $gate_errors]} {
    set fd [open [file join $out rf_deployment_status.txt] w]
    puts $fd "artifact_state=TIMING_FAILED_NOT_DEPLOYABLE"
    puts $fd "setup_slack_ns=$setup_slack"
    puts $fd "hold_slack_ns=$hold_slack"
    puts $fd "setup_pass=$setup_pass"
    puts $fd "hold_pass=$hold_pass"
    puts $fd "pulse_width_pass=$pulse_width_pass"
    puts $fd "drc_pass=$drc_pass"
    puts $fd "cdc_pass=$cdc_pass"
    puts $fd "cdc_exception=[dict get $cdc_counts exception]"
    puts $fd "cdc_accepted_critical_endpoints=[dict get $cdc_counts accepted_critical]"
    puts $fd "cdc_accepted_unknown_endpoints=[dict get $cdc_counts accepted_unknown]"
    puts $fd "external_budget_xdc=$external_budget_xdc"
    puts $fd "release_gate_errors=$gate_errors"
    close $fd
    error "RF release gate rejected before write_bitstream/write_hw_platform: $gate_errors"
  }
  # write_bitstream operates on the in-memory, post-hold-fix implementation.
  # Do not relaunch impl_1 here: doing so would discard the audited checkpoint.
  if {$is_lvds && $buffered_rx} {
    puts [exec $rf_python -I [file join $root pl scripts rf_build_provenance.py] verify --repo $root --build $out]
  }
  write_bitstream -force [file join $out zedboard_rf.bit]
  write_hw_platform -fixed -include_bit -force -file [file join $out zedboard_rf.xsa]
  if {$is_lvds && $buffered_rx} {
    puts [exec $rf_python -I [file join $root pl scripts rf_build_provenance.py] seal --repo $root --build $out]
  }
  set fd [open [file join $out rf_deployment_status.txt] w]
  if {$external_budget_status eq "MEASURED_APPROVED"} {
    set artifact_state "OFFLINE_SIGNOFF_PASSED_HARDWARE_ACCEPTANCE_PENDING"
  } else {
    set artifact_state "PREBOARD_OFFLINE_CANDIDATE_PASSED_HARDWARE_ACCEPTANCE_PENDING"
  }
  if {$rx_observer} {set artifact_state "RX_OBSERVER_DIAGNOSTIC_ONLY_HARDWARE_ACCEPTANCE_PENDING"}
  puts $fd "artifact_state=$artifact_state"
  puts $fd "rx_observer_abi=$rx_observer_abi"
  puts $fd "diagnostic_only=$rx_observer"
  puts $fd "setup_slack_ns=$setup_slack"
  puts $fd "hold_slack_ns=$hold_slack"
  puts $fd "setup_pass=$setup_pass"
  puts $fd "hold_pass=$hold_pass"
  puts $fd "pulse_width_pass=$pulse_width_pass"
  puts $fd "drc_pass=$drc_pass"
  puts $fd "cdc_pass=$cdc_pass"
  puts $fd "cdc_exception=[dict get $cdc_counts exception]"
  puts $fd "cdc_accepted_critical_endpoints=[dict get $cdc_counts accepted_critical]"
  puts $fd "cdc_accepted_unknown_endpoints=[dict get $cdc_counts accepted_unknown]"
  puts $fd "interface_profile=$rf_interface"
  puts $fd "vadj_mv=[expr {$is_lvds ? 2500 : 1800}]"
  puts $fd "rx_channels=2"
  puts $fd "tx_channels=2"
  puts $fd "sample_rate_hz=4000000"
  puts $fd "interface_clock_hz=$interface_clock_hz"
  puts $fd "sample_clock_divider=$sample_clock_divider"
  puts $fd "sampling_clock_phase_degrees=[expr {$is_lvds ? 90 : 0}]"
  puts $fd "tx_fb_clock_phase_degrees=0"
  puts $fd "dma_stream_clock_hz=[expr {$is_lvds ? 50000000 : 4000000}]"
  puts $fd "spi_external_budget_xdc=$spi_budget_xdc"
  puts $fd "spi_runtime_mode=1"
  puts $fd "spi_clock_hz=3125000"
  puts $fd "rf_rx_transport=[expr {$buffered_rx ? {elastic_axis} : {direct_fifo}}]"
  puts $fd "rf_rx_dma_source_type=[expr {$buffered_rx ? 1 : 2}]"
  puts $fd "rf_rx_elastic_depth_words=[expr {$buffered_rx ? 1024 : 0}]"
  puts $fd "rf_rx_flush_mode=[expr {$buffered_rx ? {dmac_ctrl_enable_monitor} : {legacy_fifo}}]"
  puts $fd "io_coverage_pass=1"
  puts $fd "external_budget_xdc=$external_budget_xdc"
  puts $fd "external_budget_status=$external_budget_status"
  puts $fd "signoff_checkpoint=rf_signoff_route.dcp"
  close $fd
  puts "ZEDBOARD_RF_IMPLEMENTATION_COMPLETE True"
}
puts "ZEDBOARD_RF_PROJECT [file join $out mimo_zedboard_rf.xpr]"
