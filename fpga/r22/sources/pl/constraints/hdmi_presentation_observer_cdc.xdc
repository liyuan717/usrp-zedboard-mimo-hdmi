# Passive observer VSYNC CDC only.  Do not apply an exception to AXIS/RF nets.
# m3_d is edge-history logic, not a synchronizer stage.
#
# An XDC file is *not* a general Tcl script.  In particular, Vivado ignores
# `if` statements in an XDC (Designutils 20-1307), which previously meant the
# `set_false_path` below was never executed.  Cardinality is fail-closed by
# the post-synthesis Tcl audit in add_hdmi_presentation_observer.tcl; this XDC
# contains constraints only, so the timing engine actually consumes them.
set hdmi_presentation_vsync_sync [get_cells -hier -quiet -filter {NAME =~ *hdmi_presentation_observer*/vsync_m1_reg || NAME =~ *hdmi_presentation_observer*/vsync_m2_reg || NAME =~ *hdmi_presentation_observer*/vsync_m3_reg}]
set_property ASYNC_REG TRUE $hdmi_presentation_vsync_sync
set hdmi_presentation_vsync_first [get_cells -hier -quiet -filter {NAME =~ *hdmi_presentation_observer*/vsync_m1_reg}]
set hdmi_presentation_vsync_first_d [get_pins -quiet -of_objects $hdmi_presentation_vsync_first -filter {REF_PIN_NAME == D}]
set_false_path -to $hdmi_presentation_vsync_first_d
