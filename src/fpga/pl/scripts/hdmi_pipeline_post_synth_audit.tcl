# SPDX-License-Identifier: MIT
# Shared post-synthesis HDMI presentation-observer gate.
#
# The formal HDMI pipeline and its checkpoint-only preflight both source this
# exact procedure.  Keeping the global output binding here prevents a copied
# or locally reimplemented gate from silently drifting from the release flow.
proc hdmi_pipeline_audit_observer_source_and_synth {} {
  global hdmi_pipeline_out
  set xdc [get_files -quiet *hdmi_presentation_observer_cdc.xdc]
  if {[llength $xdc] != 1} {error "Expected exactly one loaded observer CDC XDC, got $xdc"}
  set fd [open [lindex $xdc 0] r]; set text [read $fd]; close $fd
  if {![regexp {vsync_m1_reg} $text] || ![regexp {set_false_path -to \$hdmi_presentation_vsync_first_d} $text]} {
    error "Loaded observer CDC XDC does not constrain only the first synchronizer D pin"
  }
  # XDC accepts timing commands, not general Tcl flow control.  An `if` was
  # silently rejected by Vivado (Designutils 20-1307), so a superficially
  # correct file left this CDC path timed.  Keep cardinality checks in the
  # explicit post-synthesis audit and reject the ineffective pattern here.
  if {[regexp -line {^[[:space:]]*if[[:space:]]} $text]} {
    error "Observer CDC XDC must not use unsupported Tcl control flow"
  }
  audit_hdmi_presentation_observer_post_synth $hdmi_pipeline_out
}
