# G0 tagged ADC recorder; not G3/G4 full V3 implementation.
set_param general.maxThreads [lindex $argv 0]
set rxv2_root {@R22_ROOT@/sources}
set source_root $rxv2_root
set rxv2_adi [file join $rxv2_root vendor]
set rxv2_out {@R22_PROJECT@}
set rxv2_enable_hdmi 1
source {@R22_ROOT@/receiver_v2_topology_contract.tcl}
create_project receiver_v2 $rxv2_out -part xc7z020clg484-1
set_property target_language Verilog [current_project]
set_property simulator_language Mixed [current_project]
set_property ip_repo_paths [list [file join $rxv2_adi library]] [current_project]
update_ip_catalog
set_property include_dirs [list $rxv2_root] [current_fileset]
add_files -norecurse {@R22_ROOT@/sources/pl/rtl/rf_lvds_forward_clock.v}
add_files -norecurse {@R22_ROOT@/sources/pl/receiver_v2/control/rfv2_rf_clock_monitor.v}
add_files -norecurse {@R22_ROOT@/sources/pl/receiver_v2/control/rfv2_capture_reset_guard.v}
add_files -norecurse {@R22_ROOT@/sources/pl/rtl/hdmi_presentation_observer.v}
add_files -norecurse {@R22_ROOT@/sources/pl/rtl/rf_axis_status_monitor.v}
add_files -norecurse {@R22_ROOT@/sources/pl/rtl/hdmi_axi_read_probe.v}
add_files -norecurse {@R22_ROOT@/sources/pl/rtl/hdmi_axi_read_bus_monitor.v}
add_files -norecurse {@R22_ROOT@/sources/vendor/library/common/ad_iobuf.v}
add_files -norecurse {@R22_ROOT@/sources/pl/g0/g0_adc_counter.v}
proc rxv2_ip {name core {properties {}}} {
  set defs [get_ipdefs -all -quiet analog.com:user:${core}:1.0]
  if {[llength $defs] != 1} {error "Missing/ambiguous ADI IP ${core}: $defs"}
  create_bd_cell -type ip -vlnv analog.com:user:${core}:1.0 $name
  if {[llength $properties]} {set_property -dict $properties [get_bd_cells $name]}
}
proc rxv2_net {args} {
  set objects {}
  foreach path $args {
    set object [get_bd_pins -quiet $path]
    if {![llength $object]} {set object [get_bd_ports -quiet $path]}
    if {[llength $object] != 1} {error "Missing/ambiguous net endpoint: $path"}
    lappend objects $object
  }
  # Vivado2025.2 errors on a redundant connection rather than treating it as
  # a no-op. Repeated clock/reset coverage must not hide genuine new joins.
  set common_net ""
  set all_connected 1
  foreach object $objects {
    set nets [get_bd_nets -quiet -of_objects $object]
    if {[llength $nets] != 1} {set all_connected 0; break}
    if {$common_net eq ""} {set common_net [lindex $nets 0]}
    if {[lindex $nets 0] ne $common_net} {set all_connected 0; break}
  }
  if {$all_connected} {return}
  connect_bd_net {*}$objects
}
proc rxv2_bus {a b} {connect_bd_intf_net [get_bd_intf_pins $a] [get_bd_intf_pins $b]}
proc rxv2_require_bd_pin {path} {
  if {[llength [get_bd_pins -quiet $path]] != 1} {error "RX v2 wrapper pin missing or ambiguous: $path"}
}
proc rxv2_require_bd_bus {path} {
  if {[llength [get_bd_intf_pins -quiet $path]] != 1} {error "RX v2 wrapper AXI interface missing or ambiguous: $path"}
}

add_files -norecurse {@R22_ROOT@/sources/pl/v3/diagnostic_latest_mailbox_cost_probe.sv}
add_files -norecurse {@R22_ROOT@/sources/pl/v3/ingress_recovery_control_cost_probe.sv}
add_files -norecurse {@R22_ROOT@/sources/pl/v3/compact_stream_fifo_cost_probe.sv}
add_files -norecurse {@R22_ROOT@/sources/pl/v3/rf_rx_elastic_fifo.v}
add_files -norecurse {@R22_ROOT@/sources/pl/v3/continuous_adc_ingress_cost_probe.sv}
add_files -norecurse {@R22_ROOT@/sources/pl/v3/differential_input_cost_probe.sv}
add_files -norecurse {@R22_ROOT@/sources/pl/v3/differential_corr_cost_probe.sv}
add_files -norecurse {@R22_ROOT@/sources/pl/v3/differential_frontend_cost_probe.sv}
add_files -norecurse {@R22_ROOT@/sources/pl/v3/differential_score_cost_probe.sv}
add_files -norecurse {@R22_ROOT@/sources/pl/v3/differential_acquisition_cost_probe.sv}
add_files -norecurse {@R22_ROOT@/sources/pl/v3/phase_angle_cost_probe.sv}
add_files -norecurse {@R22_ROOT@/sources/pl/v3/fine_nco_config_cost_probe.sv}
add_files -norecurse {@R22_ROOT@/sources/pl/v3/nco_cost_probe.sv}
add_files -norecurse {@R22_ROOT@/sources/pl/v3/rrc_cost_probe.sv}
add_files -norecurse {@R22_ROOT@/sources/pl/v3/timing_request_cost_probe.sv}
add_files -norecurse {@R22_ROOT@/sources/pl/v3/interpolation_cost_probe.sv}
add_files -norecurse {@R22_ROOT@/sources/pl/v3/frontend_chain_cost_probe.sv}
add_files -norecurse {@R22_ROOT@/sources/pl/v3/training_channel_cost_probe.sv}
add_files -norecurse {@R22_ROOT@/sources/pl/v3/training_frontend_cost_probe.sv}
add_files -norecurse {@R22_ROOT@/sources/pl/v3/coarse_startup_schedule_cost_probe.sv}
add_files -norecurse {@R22_ROOT@/sources/pl/v3/acquiring_continuous_frontend_cost_probe.sv}
add_files -norecurse {@R22_ROOT@/sources/pl/v3/coherent_window_cost_probe.sv}
add_files -norecurse {@R22_ROOT@/sources/pl/v3/coherent_peak_cost_probe.sv}
add_files -norecurse {@R22_ROOT@/sources/pl/v3/coherent_timing_cost_probe.sv}
add_files -norecurse {@R22_ROOT@/sources/pl/v3/causal_sfo_cost_probe.sv}
add_files -norecurse {@R22_ROOT@/sources/pl/v3/causal_tracking_timing_cost_probe.sv}
add_files -norecurse {@R22_ROOT@/sources/pl/v3/tracking_job_dispatch_cost_probe.sv}
add_files -norecurse {@R22_ROOT@/sources/pl/v3/tracking_acquiring_frontend_cost_probe.sv}
add_files -norecurse {@R22_ROOT@/sources/pl/v3/bpsk_header_cost_probe.sv}
add_files -norecurse {@R22_ROOT@/sources/pl/v3/pilot_angle_cost_probe.sv}
add_files -norecurse {@R22_ROOT@/sources/pl/v3/header_pilot_math_cost_probe.sv}
add_files -norecurse {@R22_ROOT@/sources/pl/v3/pilot_smoothing_cost_probe.sv}
add_files -norecurse {@R22_ROOT@/sources/pl/v3/pilot_tracking_cost_probe.sv}
add_files -norecurse {@R22_ROOT@/sources/pl/v3/frame_replay_cost_probe.sv}
add_files -norecurse {@R22_ROOT@/sources/pl/v3/frame_acquiring_frontend_cost_probe.sv}
add_files -norecurse {@R22_ROOT@/sources/pl/v3/payload_phase_cost_probe.sv}
add_files -norecurse {@R22_ROOT@/sources/pl/v3/payload_rotate_cost_probe.sv}
add_files -norecurse {@R22_ROOT@/sources/pl/v3/payload_ml_cost_probe.sv}
add_files -norecurse {@R22_ROOT@/sources/pl/v3/payload_math_cost_probe.sv}
add_files -norecurse {@R22_ROOT@/sources/pl/v3/payload_datapath_cost_probe.sv}
add_files -norecurse {@R22_ROOT@/sources/pl/v3/payload_schedule_cost_probe.sv}
add_files -norecurse {@R22_ROOT@/sources/pl/v3/frame_domain_cost_probe.sv}
add_files -norecurse {@R22_ROOT@/sources/pl/v3/link_packet_cost_probe.sv}
add_files -norecurse {@R22_ROOT@/sources/pl/v3/payload_link_cost_probe.sv}
add_files -norecurse {@R22_ROOT@/sources/pl/v3/fec_inverse_cost_probe.sv}
add_files -norecurse {@R22_ROOT@/sources/pl/v3/fec_memory_cost_probe.sv}
add_files -norecurse {@R22_ROOT@/sources/pl/v3/fec_stream_recovery_cost_probe.sv}
add_files -norecurse {@R22_ROOT@/sources/pl/v3/link_fec_ingress_cost_probe.sv}
add_files -norecurse {@R22_ROOT@/sources/pl/v3/source_record_bridge_cost_probe.sv}
add_files -norecurse {@R22_ROOT@/sources/pl/v3/record_acquiring_frontend_cost_probe.sv}
add_files -norecurse {@R22_ROOT@/sources/pl/v3/dma_batch_stream_cost_probe.sv}
add_files -norecurse {@R22_ROOT@/sources/pl/v3/batched_acquiring_frontend_cost_probe.sv}
add_files -norecurse {@R22_ROOT@/sources/pl/v3/adc_dc_calibration_cost_probe.sv}
add_files -norecurse {@R22_ROOT@/sources/pl/v3/calibrated_batched_frontend_cost_probe.sv}
add_files -norecurse {@R22_ROOT@/sources/pl/g0/g0_source_receiver.v}
create_bd_design receiver_v2

# PS7 remains the board preset owner of DDR/MIO.  RF and HDMI memory are
# deliberately separate HP ports: no HDMI backpressure can enter HP0 RF DMA.
create_bd_cell -type ip -vlnv xilinx.com:ip:processing_system7:5.5 ps7
set_property CONFIG.PCW_IMPORT_BOARD_PRESET ZedBoard [get_bd_cells ps7]
set_property -dict [list \
  CONFIG.PCW_USE_M_AXI_GP0 1 CONFIG.PCW_USE_S_AXI_HP0 1 CONFIG.PCW_USE_S_AXI_HP1 1 \
  CONFIG.PCW_S_AXI_HP0_DATA_WIDTH 64 CONFIG.PCW_S_AXI_HP1_DATA_WIDTH 64 \
  CONFIG.PCW_USE_FABRIC_INTERRUPT 1 CONFIG.PCW_IRQ_F2P_INTR 1 \
  CONFIG.PCW_EN_CLK0_PORT 1 CONFIG.PCW_EN_RST0_PORT 1 \
  CONFIG.PCW_FPGA0_PERIPHERAL_FREQMHZ 50.0 \
  CONFIG.PCW_CRYSTAL_PERIPHERAL_FREQMHZ 33.333333 \
  CONFIG.PCW_DDR_RAM_HIGHADDR 0x1FFFFFFF \
  CONFIG.PCW_UIPARAM_DDR_PARTNO Custom CONFIG.PCW_UIPARAM_DDR_MEMORY_TYPE {DDR 3} \
  CONFIG.PCW_UIPARAM_DDR_DRAM_WIDTH {16 Bits} CONFIG.PCW_UIPARAM_DDR_DEVICE_CAPACITY {2048 MBits} \
  CONFIG.PCW_UIPARAM_DDR_ROW_ADDR_COUNT 14 CONFIG.PCW_UIPARAM_DDR_FREQ_MHZ 400.0 \
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

foreach {name value} {constant_zero 0 constant_one 1} {
  create_bd_cell -type ip -vlnv xilinx.com:ip:xlconstant:1.1 $name
  set_property -dict [list CONFIG.CONST_WIDTH 1 CONFIG.CONST_VAL $value] [get_bd_cells $name]
}
create_bd_cell -type ip -vlnv xilinx.com:ip:xlconstant:1.1 constant_zero16
set_property -dict {CONFIG.CONST_WIDTH 16 CONFIG.CONST_VAL 0} [get_bd_cells constant_zero16]
create_bd_cell -type ip -vlnv xilinx.com:ip:proc_sys_reset:5.0 reset_50m
set_property CONFIG.C_AUX_RESET_HIGH 1 [get_bd_cells reset_50m]
rxv2_net ps7/FCLK_RESET0_N reset_50m/ext_reset_in
rxv2_net constant_one/dout reset_50m/dcm_locked
rxv2_net constant_zero/dout reset_50m/aux_reset_in reset_50m/mb_debug_sys_rst

# Only actual GP0 targets are instantiated.  There is no detector_0, legacy
# AXI simple-DMA, RXTH raw DMA, TX-DMAC, DDS, util_upack, util_rfifo, or finite
# TX cell in this design.
set rxv2_control_targets [expr {$rxv2_enable_hdmi ? 9 : 5}]
create_bd_cell -type ip -vlnv xilinx.com:ip:axi_interconnect:2.1 control_xbar
set_property CONFIG.NUM_MI $rxv2_control_targets [get_bd_cells control_xbar]
create_bd_cell -type ip -vlnv xilinx.com:ip:smartconnect:1.0 rf_memory_xbar
set_property -dict {CONFIG.NUM_SI 1 CONFIG.NUM_MI 1 CONFIG.NUM_CLKS 1} [get_bd_cells rf_memory_xbar]
rxv2_bus ps7/M_AXI_GP0 control_xbar/S00_AXI
rxv2_bus rf_memory_xbar/M00_AXI ps7/S_AXI_HP0

# 200 MHz IDELAYCTRL reference plus a fixed 16 MHz / 90 degree receive clock.
create_bd_cell -type ip -vlnv xilinx.com:ip:clk_wiz:6.0 delay_clock
set_property -dict {CONFIG.PRIM_IN_FREQ 50.000 CONFIG.CLKOUT1_REQUESTED_OUT_FREQ 200.000 CONFIG.PRIM_SOURCE No_buffer CONFIG.USE_RESET true CONFIG.RESET_TYPE ACTIVE_LOW} [get_bd_cells delay_clock]
rxv2_net ps7/FCLK_CLK0 delay_clock/clk_in1
rxv2_net ps7/FCLK_RESET0_N delay_clock/resetn
create_bd_cell -type ip -vlnv xilinx.com:ip:clk_wiz:6.0 rf_sampling_clock
set_property -dict {CONFIG.PRIM_IN_FREQ 16.000 CONFIG.PRIM_SOURCE Differential_clock_capable_pin \
  CONFIG.CLKOUT1_REQUESTED_OUT_FREQ 16.000 CONFIG.CLKOUT1_REQUESTED_PHASE 90.000 \
  CONFIG.CLKOUT2_USED true CONFIG.CLKOUT2_REQUESTED_OUT_FREQ 16.000 \
  CONFIG.CLKOUT2_REQUESTED_PHASE 0.000 CONFIG.USE_RESET true CONFIG.RESET_TYPE ACTIVE_LOW} [get_bd_cells rf_sampling_clock]
rxv2_net ps7/FCLK_RESET0_N rf_sampling_clock/resetn

# The reviewed ADI core has no physical RX-only option.  Disable DAC data
# processing, but retain only its serial LVDS/control shell and safe pins.
rxv2_ip axi_ad9361 axi_ad9361 [list CONFIG.ID 0 CONFIG.CMOS_OR_LVDS_N 0 CONFIG.MODE_1R1T 0 \
  CONFIG.DELAY_REFCLK_FREQUENCY 200 CONFIG.ADC_DCFILTER_DISABLE 1 CONFIG.DAC_DATAPATH_DISABLE 1 \
  CONFIG.DAC_DDS_DISABLE 1 CONFIG.DAC_USERPORTS_DISABLE 1 \
  CONFIG.DAC_IQCORRECTION_DISABLE 1 CONFIG.TDD_DISABLE 1 \
  CONFIG.DAC_IODELAY_ENABLE 0 CONFIG.USE_SSI_CLK 0]
# ADI's inferred l_clk bus carries a generic 100-MHz BD default, not the
# configured LVDS DATA_CLK. This frozen 4-MSPS/2R2T profile uses 16 MHz, as do
# rf_sampling_clock and the external timing budget. Correct metadata at its
# source rather than lying about the coordinator's release clock or hiding a
# clock-domain mismatch. Routed clock identity/period is checked separately.
set_property CONFIG.FREQ_HZ $receiver_v2_topology::rf_interface_clock_hz [get_bd_pins axi_ad9361/l_clk]
rxv2_net delay_clock/clk_out1 axi_ad9361/delay_clk
foreach {direction port} {I rx_clk_in_p I rx_clk_in_n I rx_frame_in_p I rx_frame_in_n O tx_frame_out_p O tx_frame_out_n} {
  create_bd_port -dir $direction $port
  rxv2_net $port axi_ad9361/$port
}
foreach {direction port} {I rx_data_in_p I rx_data_in_n O tx_data_out_p O tx_data_out_n} {
  create_bd_port -dir $direction -from 5 -to 0 $port
  rxv2_net $port axi_ad9361/$port
}
foreach port {enable txnrx} {
  create_bd_port -dir O $port
  rxv2_net axi_ad9361/$port $port
}
rxv2_net rx_clk_in_p rf_sampling_clock/clk_in1_p
rxv2_net rx_clk_in_n rf_sampling_clock/clk_in1_n
rxv2_net rf_sampling_clock/clk_out1 axi_ad9361/clk
create_bd_cell -type module -reference rf_lvds_forward_clock rf_fb_clock
rxv2_net rf_sampling_clock/clk_out2 rf_fb_clock/clk
create_bd_port -dir O tx_clk_out_p
create_bd_port -dir O tx_clk_out_n
rxv2_net rf_fb_clock/tx_clk_p tx_clk_out_p
rxv2_net rf_fb_clock/tx_clk_n tx_clk_out_n
create_bd_cell -type ip -vlnv xilinx.com:ip:util_vector_logic:2.0 rf_clock_locks
set_property -dict {CONFIG.C_SIZE 1 CONFIG.C_OPERATION and} [get_bd_cells rf_clock_locks]
rxv2_net delay_clock/locked rf_clock_locks/Op1
rxv2_net rf_sampling_clock/locked rf_clock_locks/Op2
rxv2_net constant_zero/dout axi_ad9361/tdd_sync
# DAC input ports are deliberately tied, rather than left to implicit BD
# tie-offs.  Together with DAC_DATAPATH_DISABLE this makes the retained ADI
# serial shell a deterministic safety stub and not a latent DMA/DDS source.
rxv2_net constant_zero/dout axi_ad9361/dac_sync_in
foreach lane {i0 q0 i1 q1} {rxv2_net constant_zero16/dout axi_ad9361/dac_data_$lane}

create_bd_cell -type ip -vlnv xilinx.com:ip:proc_sys_reset:5.0 reset_rf
set_property CONFIG.C_AUX_RESET_HIGH 1 [get_bd_cells reset_rf]
rxv2_net ps7/FCLK_RESET0_N reset_rf/ext_reset_in
rxv2_net ps7/FCLK_CLK0 reset_rf/slowest_sync_clk
rxv2_net rf_clock_locks/Res reset_rf/dcm_locked
rxv2_net constant_zero/dout reset_rf/aux_reset_in reset_rf/mb_debug_sys_rst
create_bd_cell -type ip -vlnv xilinx.com:ip:proc_sys_reset:5.0 reset_rf_lclk
set_property CONFIG.C_AUX_RESET_HIGH 1 [get_bd_cells reset_rf_lclk]
rxv2_net axi_ad9361/l_clk reset_rf_lclk/slowest_sync_clk
rxv2_net axi_ad9361/rst reset_rf_lclk/ext_reset_in
rxv2_net rf_clock_locks/Res reset_rf_lclk/dcm_locked
rxv2_net constant_zero/dout reset_rf_lclk/aux_reset_in reset_rf_lclk/mb_debug_sys_rst

# Generated-PLL lock is not proof of AD9361 DATA_CLK. Observe actual l_clk
# heartbeat across an explicit2FF boundary and timeout if that clock stops.
create_bd_cell -type module -reference rfv2_rf_clock_monitor rf_lclk_monitor
set_property -dict [list CONFIG.FABRIC_CLOCK_HZ 50000000 CONFIG.SOURCE_CLOCK_HZ 16000000 CONFIG.TIMEOUT_CYCLES 8] [get_bd_cells rf_lclk_monitor]
rxv2_net ps7/FCLK_CLK0 rf_lclk_monitor/clk
rxv2_net reset_50m/peripheral_aresetn rf_lclk_monitor/resetn
rxv2_net axi_ad9361/l_clk rf_lclk_monitor/source_clk
rxv2_net reset_rf_lclk/peripheral_reset rf_lclk_monitor/source_reset
rxv2_net reset_rf_lclk/peripheral_aresetn rf_lclk_monitor/source_ready
create_bd_cell -type module -reference rfv2_capture_reset_guard rf_capture_guard
rxv2_net axi_ad9361/l_clk rf_capture_guard/din_clk
rxv2_net ps7/FCLK_CLK0 rf_capture_guard/dout_clk
rxv2_net reset_rf_lclk/peripheral_reset rf_capture_guard/din_raw_reset
rxv2_net reset_rf/peripheral_reset rf_capture_guard/dout_raw_reset
rxv2_net rf_clock_locks/Res rf_capture_guard/pll_locked
# The heartbeat remains on RAW resets: feeding guard reset back to this
# monitor would prevent its independent readiness handshake from ever starting.
rxv2_net rf_lclk_monitor/ready rf_capture_guard/source_ready
create_bd_cell -type ip -vlnv xilinx.com:ip:util_vector_logic:2.0 rf_path_ready_all
set_property -dict {CONFIG.C_SIZE 1 CONFIG.C_OPERATION and} [get_bd_cells rf_path_ready_all]
rxv2_net reset_rf/peripheral_aresetn rf_path_ready_all/Op1
rxv2_net rf_capture_guard/capture_ready rf_path_ready_all/Op2

# Actual four-lane ADC ingress.  CPack cannot be backpressured, so its only
# legal downstream adapter is the bounded elastic FIFO with explicit loss
# event/flush wiring into the root receiver wrapper.
rxv2_ip rf_rx_fifo util_wfifo {CONFIG.NUM_OF_CHANNELS 8 CONFIG.DIN_ADDRESS_WIDTH 4 CONFIG.DIN_DATA_WIDTH 16 CONFIG.DOUT_DATA_WIDTH 16}
rxv2_ip rf_rx_pack util_cpack2 {CONFIG.NUM_OF_CHANNELS 8 CONFIG.SAMPLE_DATA_WIDTH 16 CONFIG.INTERFACE_TYPE 1}
create_bd_cell -type module -reference g0_source_receiver g0_source_receiver
foreach pin {aclk aresetn packed_data packed_valid boundary_resetn capture_enable rf_path_ready capture_epoch_fence source_overflow flush_ack} {rxv2_require_bd_pin g0_source_receiver/$pin}
foreach bus {s_axi m_axis} {rxv2_require_bd_bus g0_source_receiver/$bus}
rxv2_net ps7/FCLK_CLK0 g0_source_receiver/aclk
rxv2_net rf_path_ready_all/Res g0_source_receiver/rf_path_ready
rxv2_net rf_capture_guard/capture_epoch_fence g0_source_receiver/capture_epoch_fence
rxv2_net g0_source_receiver/flush_ack rf_capture_guard/elastic_flush_ack
rxv2_net axi_ad9361/l_clk rf_rx_fifo/din_clk
rxv2_net ps7/FCLK_CLK0 rf_rx_pack/clk
rxv2_net rf_capture_guard/elastic_resetn g0_source_receiver/boundary_resetn
rxv2_net rf_capture_guard/wfifo_din_rst rf_rx_fifo/din_rst
rxv2_net rf_capture_guard/wfifo_dout_rstn rf_rx_fifo/dout_rstn
rxv2_net rf_capture_guard/cpack_reset rf_rx_pack/reset
rxv2_net rf_capture_guard/reader_data_enable g0_source_receiver/capture_enable
foreach {i channel} {0 i0 1 q0 2 i1 3 q1} {
  foreach field {enable data} {rxv2_net axi_ad9361/adc_${field}_$channel rf_rx_fifo/din_${field}_$i}
  create_bd_cell -type ip -vlnv xilinx.com:ip:util_vector_logic:2.0 rf_capture_valid_$i
  set_property -dict {CONFIG.C_SIZE 1 CONFIG.C_OPERATION and} [get_bd_cells rf_capture_valid_$i]
  rxv2_net axi_ad9361/adc_valid_$channel rf_capture_valid_$i/Op1
  rxv2_net rf_capture_guard/source_write_enable rf_capture_valid_$i/Op2
  rxv2_net rf_capture_valid_$i/Res rf_rx_fifo/din_valid_$i
  rxv2_net rf_rx_fifo/dout_enable_$i rf_rx_pack/enable_$i
  rxv2_net rf_rx_fifo/dout_data_$i rf_rx_pack/fifo_wr_data_$i
}
create_bd_cell -type module -reference g0_adc_counter adc_counter
rxv2_net axi_ad9361/l_clk adc_counter/clk
rxv2_net ps7/FCLK_RESET0_N adc_counter/por_n
rxv2_net rf_capture_guard/wfifo_din_rst adc_counter/source_reset
rxv2_net axi_ad9361/adc_valid_i0 adc_counter/sample_valid
for {set lane 4} {$lane<8} {incr lane} {
  set bit [expr {($lane-4)*16}]
  create_bd_cell -type ip -vlnv xilinx.com:ip:xlslice:1.0 adc_tag_$lane
  set_property -dict [list CONFIG.DIN_WIDTH 64 CONFIG.DIN_FROM [expr {$bit+15}] CONFIG.DIN_TO $bit CONFIG.DOUT_WIDTH 16] [get_bd_cells adc_tag_$lane]
  rxv2_net adc_counter/tag adc_tag_$lane/Din
  rxv2_net adc_tag_$lane/Dout rf_rx_fifo/din_data_$lane
  rxv2_net constant_one/dout rf_rx_fifo/din_enable_$lane
  rxv2_net rf_capture_valid_0/Res rf_rx_fifo/din_valid_$lane
  rxv2_net rf_rx_fifo/dout_enable_$lane rf_rx_pack/enable_$lane
  rxv2_net rf_rx_fifo/dout_data_$lane rf_rx_pack/fifo_wr_data_$lane
}
rxv2_net rf_rx_fifo/din_ovf axi_ad9361/adc_dovf
rxv2_net rf_rx_fifo/dout_valid_0 rf_rx_pack/fifo_wr_en
rxv2_net rf_rx_pack/packed_fifo_wr_data g0_source_receiver/packed_data
create_bd_cell -type ip -vlnv xilinx.com:ip:util_vector_logic:2.0 rf_capture_write
set_property -dict {CONFIG.C_SIZE 1 CONFIG.C_OPERATION and} [get_bd_cells rf_capture_write]
rxv2_net rf_rx_pack/packed_fifo_wr_en rf_capture_write/Op1
rxv2_net rf_capture_guard/cpack_write_enable rf_capture_write/Op2
rxv2_net rf_capture_write/Res g0_source_receiver/packed_valid
# Return the SAME registered loss event to both packing status and the native
# ADC-clock-domain status synchronizer. Leaving dout_ovf unconnected caused
# auto-tieoff to zero, removed din_ovf synchronizers and hid hardware losses
# from the AD9361 ADC status page (also invalidating their vendor XDC).
rxv2_net g0_source_receiver/source_overflow rf_rx_pack/packed_fifo_wr_overflow rf_rx_fifo/dout_ovf
# Do not feed combinational ingress_drop back into the queue's flush input:
# drop is gated by !flush_sync, so that connection creates a combinational
# loop exactly when full. The registered overflow event fences the next cycle.

# The only v2 data mover is a descriptor-backed S2MM stream writer.  It is a
# 512-byte-slot transport, not a legacy detector task/result DMA.
# A pool of prequeued finite descriptors is owned by the kernel. Unlike the
# ADI hw-SG cyclic API, each completion has its own cookie/callback and buffer
# identity even when IRQ/tasklet events coalesce. There is no SG memory master.
rxv2_ip rf_output_dmac axi_dmac {CONFIG.DMA_TYPE_SRC 1 CONFIG.DMA_TYPE_DEST 0 CONFIG.CYCLIC 0 CONFIG.FIFO_SIZE 16 CONFIG.SYNC_TRANSFER_START 0 CONFIG.AXIS_TUSER_SYNC 0 CONFIG.AXI_SLICE_SRC 0 CONFIG.AXI_SLICE_DEST 0 CONFIG.DMA_2D_TRANSFER 0 CONFIG.DMA_SG_TRANSFER 0 CONFIG.DMA_DATA_WIDTH_SRC 64 CONFIG.CACHE_COHERENT 0 CONFIG.DMA_AXI_ADDR_WIDTH 32}
rxv2_bus g0_source_receiver/m_axis rf_output_dmac/s_axis
rxv2_bus rf_output_dmac/m_dest_axi rf_memory_xbar/S00_AXI

# AD9361 control SPI and GPIO stay under the kernel PHY.  GPIO defaults are
# all zero at configuration; the builder provides no TX ownership client.
create_bd_cell -type ip -vlnv xilinx.com:ip:axi_quad_spi:3.2 rf_spi
set_property -dict {CONFIG.C_USE_STARTUP 0 CONFIG.C_NUM_SS_BITS 1 CONFIG.C_SPI_MODE 0 CONFIG.C_SPI_MEMORY 0 CONFIG.C_NUM_TRANSFER_BITS 8 CONFIG.C_SCK_RATIO 16} [get_bd_cells rf_spi]
foreach {name direction pin} {spi_clk O sck_o spi_mosi O io0_o spi_miso I io1_i spi_csn O ss_o} {
  create_bd_port -dir $direction $name
  rxv2_net $name rf_spi/$pin
}
rxv2_net constant_zero/dout rf_spi/sck_i rf_spi/io0_i
rxv2_net constant_one/dout rf_spi/ss_i
rxv2_net ps7/FCLK_CLK0 rf_spi/ext_spi_clk
create_bd_cell -type ip -vlnv xilinx.com:ip:axi_gpio:2.0 rf_gpio
set_property -dict {CONFIG.C_GPIO_WIDTH 9 CONFIG.C_ALL_OUTPUTS 1 CONFIG.C_IS_DUAL 1 CONFIG.C_GPIO2_WIDTH 8 CONFIG.C_ALL_INPUTS_2 1 CONFIG.C_DOUT_DEFAULT 0x00000000 CONFIG.C_INTERRUPT_PRESENT 0} [get_bd_cells rf_gpio]
foreach {i dest external} {0 gpio_resetb 1 1 gpio_en_agc 1 2 gpio_sync 1 3 axi_ad9361/up_enable 0 4 axi_ad9361/up_txnrx 0} {
  create_bd_cell -type ip -vlnv xilinx.com:ip:xlslice:1.0 gpio_bit$i
  set_property -dict [list CONFIG.DIN_WIDTH 9 CONFIG.DIN_FROM $i CONFIG.DIN_TO $i CONFIG.DOUT_WIDTH 1] [get_bd_cells gpio_bit$i]
  rxv2_net rf_gpio/gpio_io_o gpio_bit$i/Din
  if {$external} {create_bd_port -dir O $dest}
  rxv2_net gpio_bit$i/Dout $dest
}
create_bd_port -dir O -from 3 -to 0 gpio_ctl
create_bd_port -dir I -from 7 -to 0 gpio_status
rxv2_net gpio_status rf_gpio/gpio2_io_i
create_bd_cell -type ip -vlnv xilinx.com:ip:xlconcat:2.1 gpio_ctl_concat
set_property CONFIG.NUM_PORTS 4 [get_bd_cells gpio_ctl_concat]
foreach {i bit} {5 0 6 1 7 2 8 3} {
  create_bd_cell -type ip -vlnv xilinx.com:ip:xlslice:1.0 gpio_ctl_bit$bit
  set_property -dict [list CONFIG.DIN_WIDTH 9 CONFIG.DIN_FROM $i CONFIG.DIN_TO $i CONFIG.DOUT_WIDTH 1] [get_bd_cells gpio_ctl_bit$bit]
  rxv2_net rf_gpio/gpio_io_o gpio_ctl_bit$bit/Din
  rxv2_net gpio_ctl_bit$bit/Dout gpio_ctl_concat/In$bit
}
rxv2_net gpio_ctl_concat/dout gpio_ctl

# Optional HDMI uses its existing addresses and F2P[6:7], but its ADI video
# DMA is on HP1.  RF data can therefore keep draining HP0 when display stalls.
if {$rxv2_enable_hdmi} {
  rxv2_ip axi_hdmi_clkgen axi_clkgen {CONFIG.CLKIN_PERIOD 20.000 CONFIG.VCO_DIV 1 CONFIG.VCO_MUL 20.000 CONFIG.CLK0_DIV 6.750}
  rxv2_ip axi_hdmi_core axi_hdmi_tx {CONFIG.INTERFACE 16_BIT CONFIG.OUT_CLK_POLARITY 1}
  rxv2_ip axi_hdmi_dma axi_dmac {CONFIG.DMA_TYPE_SRC 0 CONFIG.DMA_TYPE_DEST 1 CONFIG.CYCLIC true CONFIG.AXI_SLICE_SRC 0 CONFIG.AXI_SLICE_DEST 0 CONFIG.DMA_2D_TRANSFER true CONFIG.DMA_DATA_WIDTH_SRC 64}
  create_bd_cell -type ip -vlnv xilinx.com:ip:axi_iic:2.1 axi_iic_main
  rxv2_ip sys_i2c_mixer util_i2c_mixer
  create_bd_cell -type ip -vlnv xilinx.com:ip:smartconnect:1.0 hdmi_memory_xbar
  set_property -dict {CONFIG.NUM_SI 1 CONFIG.NUM_MI 1 CONFIG.NUM_CLKS 1} [get_bd_cells hdmi_memory_xbar]
  rxv2_bus hdmi_memory_xbar/M00_AXI ps7/S_AXI_HP1
  rxv2_bus axi_hdmi_dma/m_src_axi hdmi_memory_xbar/S00_AXI
  rxv2_bus axi_hdmi_dma/m_axis axi_hdmi_core/s_axis
  foreach {direction port} {O hdmi_out_clk O hdmi_vsync O hdmi_hsync O hdmi_data_e} {create_bd_port -dir $direction $port}
  create_bd_port -dir O -from 15 -to 0 hdmi_data
  rxv2_net axi_hdmi_core/hdmi_out_clk hdmi_out_clk
  rxv2_net axi_hdmi_core/hdmi_16_vsync hdmi_vsync
  rxv2_net axi_hdmi_core/hdmi_16_hsync hdmi_hsync
  rxv2_net axi_hdmi_core/hdmi_16_data_e hdmi_data_e
  rxv2_net axi_hdmi_core/hdmi_16_data hdmi_data
  create_bd_port -dir IO -from 1 -to 0 iic_mux_scl
  create_bd_port -dir IO -from 1 -to 0 iic_mux_sda
  rxv2_bus axi_iic_main/IIC sys_i2c_mixer/upstream
  foreach {name signal} {iic_mux_scl_iobuf scl iic_mux_sda_iobuf sda} {
    create_bd_cell -type module -reference ad_iobuf $name
    set_property CONFIG.DATA_WIDTH 2 [get_bd_cells $name]
    rxv2_net sys_i2c_mixer/downstream_${signal}_O $name/dio_i
    rxv2_net sys_i2c_mixer/downstream_${signal}_T $name/dio_t
    rxv2_net $name/dio_o sys_i2c_mixer/downstream_${signal}_I
    rxv2_net $name/dio_p iic_mux_${signal}
  }
  foreach pin {axi_hdmi_clkgen/clk axi_hdmi_core/vdma_clk axi_hdmi_core/s_axi_aclk axi_hdmi_clkgen/s_axi_aclk axi_hdmi_dma/s_axi_aclk axi_hdmi_dma/m_src_axi_aclk axi_hdmi_dma/m_axis_aclk axi_iic_main/s_axi_aclk hdmi_memory_xbar/aclk} {rxv2_net ps7/FCLK_CLK0 $pin}
  rxv2_net axi_hdmi_clkgen/clk_0 axi_hdmi_core/reference_clk
  foreach pin {axi_hdmi_core/s_axi_aresetn axi_hdmi_clkgen/s_axi_aresetn axi_hdmi_dma/s_axi_aresetn axi_hdmi_dma/m_src_axi_aresetn axi_iic_main/s_axi_aresetn hdmi_memory_xbar/aresetn} {rxv2_net reset_50m/peripheral_aresetn $pin}
}

# Clock/reset every live AXI endpoint.  No clock is assigned to a retired
# detector/TX/RXTH endpoint because those cells do not exist.
set rxv2_clocks [list ps7/M_AXI_GP0_ACLK ps7/S_AXI_HP0_ACLK ps7/S_AXI_HP1_ACLK \
  control_xbar/ACLK control_xbar/S00_ACLK control_xbar/M00_ACLK control_xbar/M01_ACLK control_xbar/M02_ACLK control_xbar/M03_ACLK control_xbar/M04_ACLK \
  rf_memory_xbar/aclk reset_50m/slowest_sync_clk axi_ad9361/s_axi_aclk g0_source_receiver/aclk rf_rx_fifo/dout_clk \
  rf_output_dmac/s_axi_aclk rf_output_dmac/s_axis_aclk rf_output_dmac/m_dest_axi_aclk rf_spi/s_axi_aclk rf_gpio/s_axi_aclk]
if {$rxv2_enable_hdmi} {foreach pin {control_xbar/M05_ACLK control_xbar/M06_ACLK control_xbar/M07_ACLK control_xbar/M08_ACLK} {lappend rxv2_clocks $pin}}
foreach pin $rxv2_clocks {rxv2_net ps7/FCLK_CLK0 $pin}
set rxv2_resets [list control_xbar/ARESETN control_xbar/S00_ARESETN control_xbar/M00_ARESETN control_xbar/M01_ARESETN control_xbar/M02_ARESETN control_xbar/M03_ARESETN control_xbar/M04_ARESETN \
  rf_memory_xbar/aresetn axi_ad9361/s_axi_aresetn g0_source_receiver/aresetn rf_output_dmac/s_axi_aresetn rf_output_dmac/m_dest_axi_aresetn rf_spi/s_axi_aresetn rf_gpio/s_axi_aresetn]
if {$rxv2_enable_hdmi} {foreach pin {control_xbar/M05_ARESETN control_xbar/M06_ARESETN control_xbar/M07_ARESETN control_xbar/M08_ARESETN} {lappend rxv2_resets $pin}}
foreach pin $rxv2_resets {rxv2_net reset_50m/peripheral_aresetn $pin}

# GP0 target order is intentionally small and fixed.  HDMI preserves its
# historical address map despite its new independent HP1 master.
foreach {mi bus} {M00_AXI axi_ad9361/s_axi M01_AXI g0_source_receiver/s_axi M02_AXI rf_output_dmac/s_axi M03_AXI rf_spi/AXI_LITE M04_AXI rf_gpio/S_AXI} {rxv2_bus control_xbar/$mi $bus}
if {$rxv2_enable_hdmi} {
  foreach {mi bus} {M05_AXI axi_hdmi_clkgen/s_axi M06_AXI axi_hdmi_dma/s_axi M07_AXI axi_hdmi_core/s_axi M08_AXI axi_iic_main/S_AXI} {rxv2_bus control_xbar/$mi $bus}
}

create_bd_cell -type ip -vlnv xilinx.com:ip:xlconcat:2.1 interrupt_concat
set_property CONFIG.NUM_PORTS 16 [get_bd_cells interrupt_concat]
for {set i 0} {$i < 16} {incr i} {
  if {$i == 5 || $i == 8 || ($rxv2_enable_hdmi && ($i == 6 || $i == 7))} {continue}
  rxv2_net constant_zero/dout interrupt_concat/In$i
}
rxv2_net rf_spi/ip2intc_irpt interrupt_concat/In5
if {$rxv2_enable_hdmi} {
  rxv2_net axi_hdmi_dma/irq interrupt_concat/In6
  rxv2_net axi_iic_main/iic2intc_irpt interrupt_concat/In7
}
rxv2_net rf_output_dmac/irq interrupt_concat/In8
rxv2_net interrupt_concat/dout ps7/IRQ_F2P

if {$rxv2_enable_hdmi} {
  # Attach after the HDMI producer/consumer net and deliberately-unused
  # F2P[11] slot exist.  The fragment proves it only appends a monitor to the
  # established AXIS stream and replaces only the constant-zero IRQ slot.
  source [file join $rxv2_root pl scripts add_hdmi_presentation_observer.tcl]
  add_hdmi_presentation_observer $receiver_v2_topology::hdmi_observer_base
}

# Exact non-legacy software map.  0x43c00000, 0x40400000, 0x40420000 and
# 0x7c420000 intentionally have no BD segment in v2.
set rxv2_cpu_space [get_bd_addr_spaces ps7/Data]
foreach {offset bus} [list \
    $receiver_v2_topology::ad9361_base axi_ad9361/s_axi \
    $receiver_v2_topology::core_base g0_source_receiver/s_axi \
    $receiver_v2_topology::output_dmac_base rf_output_dmac/s_axi \
    $receiver_v2_topology::spi_base rf_spi/AXI_LITE \
    $receiver_v2_topology::gpio_base rf_gpio/S_AXI] {
  set seg [get_bd_addr_segs -of_objects [get_bd_intf_pins $bus]]
  if {[llength $seg] != 1} {error "Expected exactly one segment for $bus, got $seg"}
  assign_bd_address -target_address_space $rxv2_cpu_space -offset $offset -range 64K $seg
}
if {$rxv2_enable_hdmi} {
  foreach {offset bus} {0x79000000 axi_hdmi_clkgen/s_axi 0x43000000 axi_hdmi_dma/s_axi 0x70E00000 axi_hdmi_core/s_axi 0x41600000 axi_iic_main/S_AXI} {
    set seg [get_bd_addr_segs -of_objects [get_bd_intf_pins $bus]]
    if {[llength $seg] != 1} {error "Expected exactly one HDMI segment for $bus, got $seg"}
    assign_bd_address -target_address_space $rxv2_cpu_space -offset $offset -range 64K $seg
  }
}
foreach space {rf_output_dmac/m_dest_axi} {
  assign_bd_address -target_address_space [get_bd_addr_spaces $space] -offset 0 -range 512M [get_bd_addr_segs ps7/S_AXI_HP0/HP0_DDR_LOWOCM]
}
if {$rxv2_enable_hdmi} {
  assign_bd_address -target_address_space [get_bd_addr_spaces axi_hdmi_dma/m_src_axi] -offset 0 -range 512M [get_bd_addr_segs ps7/S_AXI_HP1/HP1_DDR_LOWOCM]
}

source {@R22_ROOT@/attach_hdmi_axi_read_probe_debug.tcl}
attach_hdmi_axi_read_probe_debug
validate_bd_design
save_bd_design
set bd [get_files */receiver_v2.bd]
set_property synth_checkpoint_mode None $bd
generate_target synthesis $bd
add_files -norecurse [make_wrapper -files $bd -top]
set_property top receiver_v2_wrapper [current_fileset]
add_files -fileset constrs_1 -norecurse {@R22_ROOT@/sources/pl/constraints/zedboard_fmcomms3_lvds_2v5.xdc}
add_files -fileset constrs_1 -norecurse {@R22_ROOT@/sources/pl/constraints/zedboard_hdmi_adv7511.xdc}
add_files -fileset constrs_1 -norecurse {@R22_ROOT@/sources/pl/constraints/zedboard_rf_sync_attributes.xdc}
add_files -fileset constrs_1 -norecurse {@R22_ROOT@/sources/pl/receiver_v2/integration/rf_clock_monitor_cdc.xdc}
add_files -fileset constrs_1 -norecurse {@R22_ROOT@/sources/external-budgets/MIMO_RF_EXTERNAL_BUDGET_XDC.xdc}
add_files -fileset constrs_1 -norecurse {@R22_ROOT@/sources/external-budgets/MIMO_RF_SPI_BUDGET_XDC.xdc}
add_files -fileset constrs_1 -norecurse {@R22_ROOT@/sources/pl/receiver_v2/integration/capture_reset_guard_cdc.xdc}
set_property USED_IN_SYNTHESIS false [get_files {@R22_ROOT@/sources/pl/receiver_v2/integration/capture_reset_guard_cdc.xdc}]
set_property USED_IN_IMPLEMENTATION true [get_files {@R22_ROOT@/sources/pl/receiver_v2/integration/capture_reset_guard_cdc.xdc}]
set_property PROCESSING_ORDER LATE [get_files {@R22_ROOT@/sources/pl/receiver_v2/integration/capture_reset_guard_cdc.xdc}]
add_files -fileset constrs_1 -norecurse {@R22_ROOT@/sources/pl/receiver_v2/integration/release_assertion_exact_cdc.xdc}
set_property USED_IN_SYNTHESIS false [get_files {@R22_ROOT@/sources/pl/receiver_v2/integration/release_assertion_exact_cdc.xdc}]
set_property USED_IN_IMPLEMENTATION true [get_files {@R22_ROOT@/sources/pl/receiver_v2/integration/release_assertion_exact_cdc.xdc}]
set_property PROCESSING_ORDER LATE [get_files {@R22_ROOT@/sources/pl/receiver_v2/integration/release_assertion_exact_cdc.xdc}]
add_files -fileset constrs_1 -norecurse {@R22_ROOT@/sources/pl/receiver_v2/integration/hdmi_release_assertion_exact_cdc.xdc}
set_property USED_IN_SYNTHESIS false [get_files {@R22_ROOT@/sources/pl/receiver_v2/integration/hdmi_release_assertion_exact_cdc.xdc}]
set_property USED_IN_IMPLEMENTATION true [get_files {@R22_ROOT@/sources/pl/receiver_v2/integration/hdmi_release_assertion_exact_cdc.xdc}]
set_property PROCESSING_ORDER LATE [get_files {@R22_ROOT@/sources/pl/receiver_v2/integration/hdmi_release_assertion_exact_cdc.xdc}]
add_files -fileset constrs_1 -norecurse {@R22_ROOT@/sources/pl/receiver_v2/integration/hdmi_gray_pointer_cdc.xdc}
set_property USED_IN_SYNTHESIS false [get_files {@R22_ROOT@/sources/pl/receiver_v2/integration/hdmi_gray_pointer_cdc.xdc}]
set_property USED_IN_IMPLEMENTATION true [get_files {@R22_ROOT@/sources/pl/receiver_v2/integration/hdmi_gray_pointer_cdc.xdc}]
set_property PROCESSING_ORDER LATE [get_files {@R22_ROOT@/sources/pl/receiver_v2/integration/hdmi_gray_pointer_cdc.xdc}]
add_files -fileset constrs_1 -norecurse {@R22_ROOT@/sources/pl/receiver_v2/integration/hdmi_mailbox_exact_cdc.xdc}
set_property USED_IN_SYNTHESIS false [get_files {@R22_ROOT@/sources/pl/receiver_v2/integration/hdmi_mailbox_exact_cdc.xdc}]
set_property USED_IN_IMPLEMENTATION true [get_files {@R22_ROOT@/sources/pl/receiver_v2/integration/hdmi_mailbox_exact_cdc.xdc}]
set_property PROCESSING_ORDER LATE [get_files {@R22_ROOT@/sources/pl/receiver_v2/integration/hdmi_mailbox_exact_cdc.xdc}]
add_files -fileset constrs_1 -norecurse {@R22_ROOT@/sources/pl/receiver_v2/integration/hdmi_epoch_exact_cdc.xdc}
set_property USED_IN_SYNTHESIS false [get_files {@R22_ROOT@/sources/pl/receiver_v2/integration/hdmi_epoch_exact_cdc.xdc}]
set_property USED_IN_IMPLEMENTATION true [get_files {@R22_ROOT@/sources/pl/receiver_v2/integration/hdmi_epoch_exact_cdc.xdc}]
set_property PROCESSING_ORDER LATE [get_files {@R22_ROOT@/sources/pl/receiver_v2/integration/hdmi_epoch_exact_cdc.xdc}]
set_property PROCESSING_ORDER LATE [get_files *rf_clock_monitor_cdc.xdc]
update_compile_order -fileset sources_1
write_bd_tcl -force [file join $rxv2_out rebuild_g0_adc_bd.tcl]
write_hw_platform -fixed -force -file [file join $rxv2_out g0_source_pre.xsa]
puts "G0_SOURCE_PLATFORM_VALIDATED"
launch_runs synth_1 -jobs 1
wait_on_run synth_1
if {[get_property PROGRESS [get_runs synth_1]] ne "100%"} {error "G0 ADC synthesis failed"}
open_run synth_1
if {[llength [get_cells -hier -quiet -filter {IS_BLACKBOX == 1}]]} {error "G0 contains black boxes"}
report_utilization -hierarchical -file [file join $rxv2_out synthesis_utilization.rpt]
report_timing_summary -report_unconstrained -file [file join $rxv2_out synthesis_timing.rpt]
report_drc -file [file join $rxv2_out synthesis_drc.rpt]
report_cdc -details -no_waiver -file [file join $rxv2_out synthesis_cdc.rpt]
puts "G0_SOURCE_SYNTHESIS_COMPLETE_REVIEW_CDC_BEFORE_ROUTE"
close_project
