# Check the actual open Block Design, not merely builder source text.
# Must run after validate_bd_design. This proves topology only, not RTL timing.
namespace eval receiver_v2_capture_bd {}

proc receiver_v2_capture_bd::scalar {source destination fd} {
  set sp [get_bd_pins -quiet $source]
  set dp [get_bd_pins -quiet $destination]
  if {[llength $sp] != 1 || [llength $dp] != 1} {
    error "Capture topology missing/ambiguous pin: $source -> $destination"
  }
  set sn [get_bd_nets -quiet -of_objects $sp]
  set dn [get_bd_nets -quiet -of_objects $dp]
  if {[llength $sn] != 1 || $sn ne $dn} {
    error "Capture topology wrong net: $source -> $destination, source=$sn destination=$dn"
  }
  set drivers [get_bd_pins -quiet -of_objects $dn -filter {DIR == O}]
  if {[llength $drivers] != 1 || [lindex $drivers 0] ne [lindex $sp 0]} {
    error "Capture topology has unexpected drivers for $destination: $drivers"
  }
  puts $fd "$source -> $destination net=$sn sole_driver=true"
}

proc receiver_v2_capture_bd::bus {source destination fd} {
  set sp [get_bd_intf_pins -quiet $source]
  set dp [get_bd_intf_pins -quiet $destination]
  if {[llength $sp] != 1 || [llength $dp] != 1} {
    error "Capture topology missing AXIS boundary: $source -> $destination"
  }
  set sn [get_bd_intf_nets -quiet -of_objects $sp]
  set dn [get_bd_intf_nets -quiet -of_objects $dp]
  if {[llength $sn] != 1 || $sn ne $dn ||
      [llength [get_bd_intf_pins -quiet -of_objects $sn]] != 2} {
    error "Capture topology AXIS boundary has a bypass or missing gate: $source -> $destination"
  }
  puts $fd "$source <-> $destination interface=$sn endpoints=2"
}

proc receiver_v2_capture_bd::validate {report} {
  set fd [open $report w]
  try {
    foreach {source destination} {
      axi_ad9361/l_clk rf_capture_guard/din_clk
      axi_ad9361/l_clk rf_rx_fifo/din_clk
      ps7/FCLK_CLK0 rf_capture_guard/dout_clk
      ps7/FCLK_CLK0 rf_rx_fifo/dout_clk
      ps7/FCLK_CLK0 rf_rx_pack/clk
      ps7/FCLK_CLK0 rf_rx_elastic/clk
      ps7/FCLK_CLK0 rf_capture_gate/aclk
      reset_rf_lclk/peripheral_reset rf_capture_guard/din_raw_reset
      reset_rf/peripheral_reset rf_capture_guard/dout_raw_reset
      rf_clock_locks/Res rf_capture_guard/pll_locked
      rf_lclk_monitor/ready rf_capture_guard/source_ready
      reset_rf_lclk/peripheral_reset rf_lclk_monitor/source_reset
      reset_rf_lclk/peripheral_aresetn rf_lclk_monitor/source_ready
      rf_capture_guard/wfifo_din_rst rf_rx_fifo/din_rst
      rf_capture_guard/wfifo_dout_rstn rf_rx_fifo/dout_rstn
      rf_capture_guard/cpack_reset rf_rx_pack/reset
      rf_capture_guard/elastic_resetn rf_rx_elastic/resetn
      rf_capture_guard/capture_epoch_fence rf_receiver_v2_bd/capture_epoch_fence
      rf_rx_elastic/flush_ack rf_capture_guard/elastic_flush_ack
      rf_receiver_v2_bd/ingress_flush rf_rx_elastic/flush_sync
      rf_capture_guard/reader_data_enable rf_capture_gate/enable
      rf_capture_guard/cpack_write_enable rf_capture_write/Op2
      rf_rx_pack/packed_fifo_wr_en rf_capture_write/Op1
      rf_capture_write/Res rf_rx_elastic/packed_fifo_wr_en
      rf_capture_guard/capture_ready rf_path_ready_all/Op2
      rf_path_ready_all/Res rf_receiver_v2_bd/rf_path_ready
    } {scalar $source $destination $fd}
    for {set bit 0} {$bit < 4} {incr bit} {
      scalar rf_capture_guard/source_write_enable rf_capture_valid_$bit/Op2 $fd
      scalar rf_capture_valid_$bit/Res rf_rx_fifo/din_valid_$bit $fd
    }
    bus rf_rx_elastic/M_AXIS rf_capture_gate/s_axis $fd
    bus rf_capture_gate/m_axis rf_receiver_v2_bd/s_axis $fd
    puts $fd "CAPTURE_BD_TOPOLOGY_PASS topology_only=true no_timing_or_board_claim=true"
  } finally {close $fd}
  return "CAPTURE_BD_TOPOLOGY_PASS"
}
