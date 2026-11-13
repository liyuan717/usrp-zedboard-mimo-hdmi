# Isolated G0 SOURCE receiver hardware candidate only. Route and report; bit generation is a separate
# step after review of these actual reports. Never waive a CDC globally.
lassign $argv directory threads retry reference
if {[llength $argv] ni {1 3 4}} {error {usage: -tclargs directory, or directory threads retry [reference-dcp]}}
if {$threads eq ""} {set threads 2; set retry 0}
if {![string is integer -strict $threads] || $threads < 1 || $threads > 8 || $retry ni {0 1}} {error "invalid route concurrency"}
set platform [file normalize $directory]
set reports [file join $platform project]
set integration [file join $platform sources pl receiver_v2 integration]
set_param general.maxThreads $threads
open_project [file join $reports receiver_v2.xpr]
if {[get_property PROGRESS [get_runs synth_1]] ne "100%"} {error "synthesis incomplete"}
open_run synth_1
foreach helper {capture_reset_guard_contract.tcl release_assertion_contract.tcl hdmi_gray_pointer_contract.tcl hdmi_mailbox_exact_contract.tcl hdmi_epoch_cdc_contract.tcl} {
 source [file join $integration $helper]
}
proc g0_counter_contract {} {
 set root receiver_v2_i/adc_counter/inst
 set m0 [receiver_v2_capture_contract::exact_cell "$root/reset_release_reg\[0\]"]
 set m1 [receiver_v2_capture_contract::exact_cell "$root/reset_release_reg\[1\]"]
 foreach ff [list $m0 $m1] {
  if {[get_property REF_NAME $ff] ne "FDCE"} {error "counter POR release requires FDCE"}
  receiver_v2_capture_contract::assert_async_ff $ff "counter POR release"
  receiver_v2_capture_contract::assert_clock $ff clk_out1_receiver_v2_rf_sampling_clock_0 62.500 "counter release"
 }
 receiver_v2_capture_contract::assert_sole_fanout $m0 $m1 "counter release chain"
 set state [get_cells -hier -quiet -filter {IS_SEQUENTIAL == 1 && (NAME =~ receiver_v2_i/adc_counter/inst/index_reg* || NAME =~ receiver_v2_i/adc_counter/inst/epoch_reg* || NAME == receiver_v2_i/adc_counter/inst/was_reset_reg)}]
 if {[llength $state] != 65} {error "counter state count changed: [llength $state]"}
 foreach ff $state {
  if {[get_property REF_NAME $ff] ne "FDRE"} {error "counter state has asynchronous control: $ff"}
  receiver_v2_capture_contract::assert_clock $ff clk_out1_receiver_v2_rf_sampling_clock_0 62.500 "counter state"
 }
 puts "G0_COUNTER_RESET_CONTRACT_PASS local_state=65 async_release_stages=2"
}
receiver_v2_capture_contract::validate post_synth
receiver_v2_release_assertion::validate post_synth 1
receiver_v2_gray_contract::validate
receiver_v2_mailbox_contract::validate post_synth
g0_counter_contract
close_design
if {$retry} {reset_run impl_1}
if {$reference ne ""} {
 set reference [file normalize $reference]
 if {![file isfile $reference]} {error "incremental reference checkpoint absent: $reference"}
 set run [get_runs impl_1]
 set_property AUTO_INCREMENTAL_CHECKPOINT 0 $run
 set_property incremental_checkpoint.directive TimingClosure $run
 if {![llength [get_files -quiet $reference]]} {
  add_files -fileset utils_1 -norecurse $reference
 }
 set_property incremental_checkpoint $reference $run
 puts "G0_SOURCE_INCREMENTAL_REFERENCE $reference"
}
launch_runs impl_1 -to_step route_design -jobs 1
wait_on_run impl_1
set routed [file join [get_property DIRECTORY [get_runs impl_1]] receiver_v2_wrapper_routed.dcp]
if {![file isfile $routed]} {error "route checkpoint absent: [get_property STATUS [get_runs impl_1]]"}
open_checkpoint $routed
phys_opt_design -directive ExploreWithAggressiveHoldFix
route_design
write_checkpoint -force [file join $reports g0_source_review_routed.dcp]
report_timing_summary -report_unconstrained -file [file join $reports implementation_timing.rpt]
report_drc -file [file join $reports implementation_drc.rpt]
report_cdc -details -no_waiver -file [file join $reports implementation_cdc.rpt]
report_bus_skew -file [file join $reports implementation_bus_skew.rpt]
report_utilization -hierarchical -file [file join $reports implementation_utilization.rpt]
report_route_status -file [file join $reports implementation_route_status.rpt]
report_pulse_width -all_violators -file [file join $reports implementation_pulse_width.rpt]
receiver_v2_capture_contract::validate post_route
receiver_v2_release_assertion::validate post_route 1
receiver_v2_gray_contract::validate
receiver_v2_mailbox_contract::validate post_route
receiver_v2_hdmi_epoch_cdc::validate_ready
receiver_v2_hdmi_epoch_cdc::validate_ack
g0_counter_contract
receiver_v2_capture_contract::assert_normal_stage_timing \
 [receiver_v2_capture_contract::exact_cell {receiver_v2_i/adc_counter/inst/reset_release_reg[0]}] \
 [receiver_v2_capture_contract::exact_cell {receiver_v2_i/adc_counter/inst/reset_release_reg[1]}] "counter POR release"
set severe [get_drc_violations -quiet -filter {SEVERITY == Error || SEVERITY == {Critical Warning}}]
if {[llength $severe]} {error "severe routed DRC: $severe"}
foreach type {max min} {
 set path [get_timing_paths -quiet -delay_type $type -max_paths 1]
 if {[llength $path]!=1 || [get_property SLACK $path]<0} {error "routed $type timing failed"}
}
puts "G0_SOURCE_ROUTE_COMPLETE_REVIEW_FULL_REPORTS_BEFORE_BITSTREAM"
close_project
