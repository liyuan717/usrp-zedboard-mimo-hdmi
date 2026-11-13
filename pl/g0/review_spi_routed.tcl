# Routed SPI pad timing and isolated controller model. Trial delay edits stay
# in memory. No checkpoint or bitstream is written.
lassign $argv threads checkpoint
set_param general.maxThreads $threads
open_checkpoint $checkpoint
set core [get_cells receiver_v2_i/rf_spi]
set clock [get_clocks ad9361_spi_clk]
set miso [get_ports spi_miso]
if {[llength $core] != 1 || [llength $clock] != 1 ||
    [llength $miso] != 1 || abs([get_property PERIOD $clock]-320.0)>0.001 ||
    [get_property WAVEFORM $clock] ne {0.000 160.000}} {
    error "Unexpected SPI controller or forwarded clock"
}
set mosi [get_ports spi_mosi]
set csn [get_ports spi_csn]
foreach port [concat $mosi $csn] {
    foreach delay_type {max min} {
        set path [get_timing_paths -to $port -delay_type $delay_type -max_paths 1]
        if {[llength $path]!=1 || [get_property ENDPOINT_CLOCK $path] ne $clock} {
            error "Missing $delay_type path for $port"
        }
        puts "SPI_OUTPUT $port $delay_type SLACK_NS=[get_property SLACK $path]"
    }
}
report_timing -to [concat $mosi $csn] -delay_type min_max -max_paths 8 -file spi-output.rpt

set path [get_timing_paths -from $miso -delay_type max -max_paths 1]
set endpoint [get_property ENDPOINT_PIN $path]
if {![string match */IO1_I_REG/D $endpoint]} {error "Unexpected first MISO capture $endpoint"}
set cell [get_cells -of_objects [get_pins $endpoint]]
set ce [get_pins -of_objects $cell -filter {REF_PIN_NAME == CE}]
puts "MISO_CAPTURE CELL=$cell CE_NET=[get_nets -of_objects $ce] CE_STARTS=[all_fanin -flat -startpoints_only -to $ce]"
report_timing -from [get_pins $cell/Q] -delay_type min_max -max_paths 8 -file spi-miso-downstream.rpt
foreach budget {14.250 38.250} {
    set_input_delay -clock $clock -max $budget $miso
    set setup [get_timing_paths -from $miso -delay_type max -max_paths 1]
    set hold [get_timing_paths -from $miso -delay_type min -max_paths 1]
    if {[get_property ENDPOINT_PIN $setup] ne $endpoint || [get_property ENDPOINT_PIN $hold] ne $endpoint} {
        error "MISO capture endpoint changed"
    }
    puts "MISO_TRIAL BUDGET_NS=$budget SETUP_NS=[get_property SLACK $setup] HOLD_NS=[get_property SLACK $hold]"
    report_timing -from $miso -delay_type min_max -file spi-miso-$budget.rpt
}
foreach corner {slow fast} {
    write_sdf -cell $core -process_corner $corner -rename_top receiver_v2_rf_spi_routed -mode timesim -multithread $threads spi-$corner.sdf
}
write_verilog -cell $core -mode timesim -rename_top receiver_v2_rf_spi_routed rf-spi-routed.v
close_design
puts SPI_ROUTED_AUDIT_COMPLETE
