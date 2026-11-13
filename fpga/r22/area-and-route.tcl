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
set diag [open {@R22_ROUTE@/reset-mapping.tsv} w]
set bcell [get_cells -hier -quiet -filter {NAME =~ */g0_source_receiver/inst/receiver/core/batch/*busy_reg* && IS_SEQUENTIAL == 1}]
foreach cell $bcell {
 set q [get_pins -quiet -of_objects $cell -filter {REF_PIN_NAME == Q}]
 set nets [get_nets -quiet -segments -of_objects $q]
 puts $diag [join [list busy_ff $cell [get_property REF_NAME $cell] $q $nets] "	"]
}
puts $diag [join [list busy_sequential_cell_count [llength $bcell]] "	"]
close $diag
close_design
set opt [file join [get_property DIRECTORY [get_runs impl_1]] receiver_v2_wrapper_opt.dcp]
if {![file isfile $opt]} {
 launch_runs impl_1 -to_step opt_design -jobs 1
 wait_on_run impl_1
}
if {![file isfile $opt]} {error "R22 default opt checkpoint absent: [get_property STATUS [get_runs impl_1]]"}
open_checkpoint $opt
report_utilization -hierarchical -file {@R22_ROUTE@/before-area-utilization.rpt}
opt_design -directive ExploreArea
report_utilization -hierarchical -file {@R22_ROUTE@/after-area-utilization.rpt}
report_control_sets -file {@R22_ROUTE@/after-area-control-sets.rpt}
write_checkpoint -force {@R22_ROUTE@/area-opt.dcp}
place_design
phys_opt_design
write_checkpoint -force {@R22_ROUTE@/area-placed.dcp}
report_timing_summary -file {@R22_ROUTE@/placed-timing.rpt}
report_design_analysis -congestion -file {@R22_ROUTE@/placed-congestion.rpt}
if {[catch {route_design -directive Explore} route_error]} {
 catch {report_route_status -file {@R22_ROUTE@/failed-route-status.rpt}}
 catch {report_design_analysis -congestion -file {@R22_ROUTE@/failed-route-congestion.rpt}}
 error $route_error
}
write_checkpoint -force {@R22_ROUTE@/first-routed.dcp}
report_timing_summary -file {@R22_ROUTE@/first-timing.rpt}
report_bus_skew -file {@R22_ROUTE@/first-bus-skew.rpt}
phys_opt_design -directive ExploreWithAggressiveHoldFix
set current_route [report_route_status -return_string]
if {![regexp {# of nets with routing errors[. ]+:[ ]+0[ ]+:} $current_route] ||
      ![regexp {# of routable nets[. ]+:[ ]+([0-9]+)} $current_route -> total] ||
      ![regexp {# of fully routed nets[. ]+:[ ]+([0-9]+)} $current_route -> routed] || $routed != $total} {
 route_design -preserve
}
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
