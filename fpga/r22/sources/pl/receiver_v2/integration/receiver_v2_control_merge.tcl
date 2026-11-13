# Post-opt_design implementation hook for ZedBoard RXv2. Default Vivado opt
# has ALREADY executed. A separate pass retains those defaults and merges
# only equivalent control/LUT/FF drivers, as physically validated in r93/r102.
# No clock, reset semantics, timing exception or datapath schedule is changed.
if {[get_property PART [current_design]] ne "xc7z020clg484-1"} {
  error "RXv2 control merge requires the reviewed ZedBoard part"
}
set rxv2_merge_clock [get_clocks -quiet clk_fpga_0]
if {[llength $rxv2_merge_clock]!=1 ||
    abs([get_property PERIOD $rxv2_merge_clock]-20.0)>0.001} {
  error "RXv2 control merge requires the 50-MHz receiver clock"
}
set rxv2_merge_reports [file join [pwd] receiver_v2_control_merge]
file mkdir $rxv2_merge_reports
report_control_sets -verbose -file [file join $rxv2_merge_reports before.rpt]
opt_design -control_set_merge -merge_equivalent_drivers
report_control_sets -verbose -file [file join $rxv2_merge_reports after.rpt]
# The implementation-only XDC was loaded before opt_design. HDMI-disabled
# builds have no such endpoint and deliberately do not evaluate this contract.
if {[llength [get_cells -quiet -hier -filter {NAME == receiver_v2_i/axi_hdmi_core/inst/i_frame_mailbox}]]} {
  source [file join [file dirname [info script]] hdmi_mailbox_exact_contract.tcl]
  puts "RXV2_MAILBOX_POST_OPT_CONTRACT [receiver_v2_mailbox_contract::validate post_opt]"
}
puts "RXV2_EQUIVALENT_CONTROL_MERGE_COMPLETE (not timing acceptance)"
