# Runs in the creator immediately before launch_runs synth_1.
# The list is from the actual Vivado fileset, not a guessed repository glob.
if {![info exists ::env(MIMO_RF_BUILD_PYTHON)]} {
  error "Set MIMO_RF_BUILD_PYTHON to a working external Python interpreter"
}
set rf_python [file normalize $::env(MIMO_RF_BUILD_PYTHON)]
if {![file isfile $rf_python]} {error "Missing RF provenance Python: $rf_python"}
set input_list [file join $out rf_actual_inputs.tsv]
set input_fd [open $input_list w]
fconfigure $input_fd -encoding utf-8 -translation lf
foreach input [get_files -compile_order sources -used_in synthesis -of_objects [get_filesets sources_1]] {
  puts $input_fd "synthesis\t[file normalize $input]"
}
foreach input [get_files -all -filter {FILE_TYPE == XDC}] {
  puts $input_fd "constraint\t[file normalize $input]"
}
foreach input [get_files -all [list *.bd *.xci]] {
  puts $input_fd "design\t[file normalize $input]"
}
set settings [file join $out rf_project_settings.txt]
set settings_fd [open $settings w]
foreach {name value} [list part [get_property PART [current_project]] top [get_property TOP [get_filesets sources_1]] \
  target_language [get_property TARGET_LANGUAGE [current_project]] simulator_language [get_property SIMULATOR_LANGUAGE [current_project]] \
  ip_repo_paths [get_property IP_REPO_PATHS [current_project]] implementation_strategy [get_property STRATEGY [get_runs impl_1]]] {
  puts $settings_fd "$name=$value"
}
close $settings_fd
foreach path [list $settings [file join $out rf_build_receipt.txt] [file join $out rf_bootstrap_inputs.json]] {
  puts $input_fd "design\t[file normalize $path]"
}
foreach name {
  pl/scripts/create_zedboard_rf_system.tcl
  pl/scripts/create_zedboard_rf_buffered_lvds_system.tcl
  pl/scripts/add_detector_module.tcl
  pl/scripts/rf_signoff_checks.tcl
  pl/scripts/rf_build_provenance.py
  pl/scripts/capture_zedboard_rf_inputs.tcl
  board/zedboard/rf_timing_budget_lvds_2r2t_2v5.json
  board/zedboard/rf_spi_timing_budget.json
} {
  puts $input_fd "recipe\t[file join $root $name]"
}
puts $input_fd "recipe\t[file join [file dirname $adi_root] source-manifest.json]"
foreach name {library/axi_dmac/axi_dmac_ip.tcl library/axi_dmac/address_generator.v library/util_pack/util_cpack2/util_cpack2_impl.v} {
  puts $input_fd "recipe\t[file join $adi_root $name]"
}
close $input_fd
puts [exec $rf_python -I [file join $root pl scripts rf_build_provenance.py] capture --repo $root --build $out --input-list $input_list]
