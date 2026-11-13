# Typed structural gate for release_assertion_exact_cdc.xdc. Source this after
# capture_reset_guard_contract.tcl. No exceptions are added by this observer.
namespace eval receiver_v2_release_assertion {
  proc validate {phase {hdmi_enabled 1}} {
    if {$phase ni {post_synth post_route}} {error "invalid release assertion phase $phase"}
    receiver_v2_capture_contract::check_fault_cone
    # Validate the two local release D chains and local clock identities too.
    receiver_v2_capture_contract::check_local_release din clk_out1_receiver_v2_rf_sampling_clock_0 62.500 $phase
    receiver_v2_capture_contract::check_local_release dout clk_fpga_0 20.000 $phase
    foreach name {
      {receiver_v2_i/reset_rf/U0/PR_OUT_DFF[0].FDRE_PER/Q}
      {receiver_v2_i/reset_rf_lclk/U0/PR_OUT_DFF[0].FDRE_PER/Q}
      receiver_v2_i/rf_sampling_clock/inst/mmcm_adv_inst/LOCKED
      receiver_v2_i/delay_clock/inst/mmcm_adv_inst/LOCKED
      receiver_v2_i/rf_lclk_monitor/inst/ready_reg/Q
    } {
      receiver_v2_capture_contract::require_one [get_pins -quiet -hier -filter [format {NAME == "%s"} $name]] "reviewed fault launch pin $name"
    }
    if {$hdmi_enabled} {validate_hdmi $phase}
    puts "RELEASE_ASSERTION_TYPED_CONTRACT_PASS phase=$phase hdmi=$hdmi_enabled pin_only=1"
    return 1
  }

  proc validate_hdmi {phase} {
    set base receiver_v2_i/axi_hdmi_core/inst/i_up
    set source [receiver_v2_capture_contract::exact_cell $base/up_core_preset_reg]
    if {[get_property REF_NAME $source] ne "FDPE"} {error "unexpected HDMI reset source primitive"}
    receiver_v2_capture_contract::assert_clock $source clk_fpga_0 20.000 "HDMI reset source"
    set expected_pres {}
    foreach {helper clock period} {
      i_core_rst_reg mmcm_clk_0_s 6.750
      i_vdma_rst_reg clk_fpga_0 20.000
    } {
      set stages {}
      foreach stage {release_m1_reg release_m2_reg rst_reg} {
        set cell [receiver_v2_capture_contract::exact_cell $base/$helper/$stage]
        if {[get_property REF_NAME $cell] ne "FDPE"} {error "HDMI release stage must be FDPE: $cell"}
        receiver_v2_capture_contract::assert_clock $cell $clock $period "$helper/$stage"
        if {$stage ne "rst_reg"} {receiver_v2_capture_contract::assert_async_ff $cell "$helper/$stage"}
        set pre [receiver_v2_capture_contract::pin_of $cell PRE]
        set roots [all_fanin -flat -startpoints_only -to $pre]
        if {$roots ne [list [receiver_v2_capture_contract::pin_of $source C]]} {
          error "unexpected HDMI PRE assertion roots $pre: $roots"
        }
        lappend expected_pres $pre
        lappend stages $cell
      }
      foreach {a b} [list [lindex $stages 0] [lindex $stages 1] [lindex $stages 1] [lindex $stages 2]] {
        receiver_v2_capture_contract::assert_sole_fanout $a $b "$helper release chain"
        if {$phase eq "post_route"} {receiver_v2_capture_contract::assert_normal_stage_timing $a $b "$helper release chain"}
      }
    }
    set sinks [lsort [all_fanout -flat -endpoints_only -from [receiver_v2_capture_contract::pin_of $source Q]]]
    if {$sinks ne [lsort $expected_pres]} {error "HDMI async reset bypass/extra endpoint: $sinks"}
    puts "HDMI_RELEASE_ASSERTION_TOPOLOGY_PASS pres=6 normal_local_d_chains=4 phase=$phase"
  }
}
