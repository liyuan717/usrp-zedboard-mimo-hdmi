# Shared strict routed release gate for the HDMI/HP1 derived candidate.
# It is intentionally usable on a routed checkpoint for read-only preflight.
proc hdmi_pipeline_write_deployment_status {state setup hold pulse drc cdc coverage} {
  global hdmi_pipeline_out
  set fd [open [file join $hdmi_pipeline_out hdmi_pipeline_deployment_status.txt] w]
  puts $fd "artifact_state=$state"
  puts $fd "hardware_acceptance=PENDING"
  puts $fd "hardware_tested=0"
  puts $fd "setup_slack_ns=$setup"
  puts $fd "hold_slack_ns=$hold"
  puts $fd "pulse_width_pass=$pulse"
  puts $fd "drc_pass=$drc"
  puts $fd "cdc_pass=$cdc"
  puts $fd "timing_coverage=$coverage"
  close $fd
}

proc hdmi_pipeline_gate_routed {} {
  global hdmi_pipeline_out
  report_methodology -file [file join $hdmi_pipeline_out hdmi_pipeline_methodology.rpt]
  report_timing_summary -delay_type min_max -report_unconstrained -max_paths 20 -file [file join $hdmi_pipeline_out hdmi_pipeline_timing.rpt]
  report_pulse_width -all_violators -file [file join $hdmi_pipeline_out hdmi_pipeline_pulse_width.rpt]
  report_clock_interaction -file [file join $hdmi_pipeline_out hdmi_pipeline_clock_interaction.rpt]
  report_drc -file [file join $hdmi_pipeline_out hdmi_pipeline_drc.rpt]
  report_cdc -file [file join $hdmi_pipeline_out hdmi_pipeline_cdc.rpt]
  report_cdc -details -file [file join $hdmi_pipeline_out hdmi_pipeline_cdc_details.rpt]
  set fd [open [file join $hdmi_pipeline_out hdmi_pipeline_timing.rpt] r]; set coverage [rf_timing_coverage [read $fd] 1]; close $fd
  set fd [open [file join $hdmi_pipeline_out hdmi_pipeline_cdc.rpt] r]; set cdc_summary [read $fd]; close $fd
  set fd [open [file join $hdmi_pipeline_out hdmi_pipeline_cdc_details.rpt] r]; set cdc_detail [read $fd]; close $fd
  set fd [open [file join $hdmi_pipeline_out hdmi_pipeline_pulse_width.rpt] r]; set pulse_text [read $fd]; close $fd
  set setup_path [get_timing_paths -quiet -delay_type max -max_paths 1]
  set hold_path [get_timing_paths -quiet -delay_type min -max_paths 1]
  if {[llength $setup_path] != 1 || [llength $hold_path] != 1} {error "No routed setup/hold path available for HDMI release gate"}
  set setup [get_property SLACK $setup_path]
  set hold [get_property SLACK $hold_path]
  set pulse [expr {[string first "VIOLATED" $pulse_text] < 0}]
  set severe [get_drc_violations -quiet -filter {SEVERITY == Error || SEVERITY == {Critical Warning}}]
  set drc [expr {![llength $severe]}]
  set cdc_counts [rf_cdc_audit $cdc_summary $cdc_detail 1]
  set cdc [expr {[dict get $cdc_counts critical] == 0 && [dict get $cdc_counts unsafe] == 0 && [dict get $cdc_counts unknown] == 0}]
  set errors {}
  if {$setup < 0} {lappend errors "setup_slack=$setup"}
  if {$hold < 0} {lappend errors "hold_slack=$hold"}
  if {!$pulse} {lappend errors pulse_width}
  if {!$drc} {lappend errors "drc=$severe"}
  if {!$cdc} {lappend errors "cdc=$cdc_counts"}
  if {[llength $coverage]} {lappend errors "timing_coverage=$coverage"}
  if {[llength $errors]} {
    hdmi_pipeline_write_deployment_status TIMING_FAILED_NOT_DEPLOYABLE $setup $hold $pulse $drc $cdc $coverage
    error "HDMI pipeline release gate rejected before write_bitstream/write_hw_platform: $errors"
  }
  hdmi_pipeline_write_deployment_status PREBOARD_OFFLINE_SIGNOFF_PASSED_HARDWARE_ACCEPTANCE_PENDING $setup $hold $pulse $drc $cdc $coverage
  puts "HDMI_PIPELINE_GATES_PASS setup=$setup hold=$hold cdc_exception=[dict get $cdc_counts exception]"
}
