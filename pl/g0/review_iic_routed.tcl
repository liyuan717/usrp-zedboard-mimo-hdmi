# Inspect the four routed IIC pad owners and export the controller for simulation.
lassign $argv threads checkpoint
set_param general.maxThreads $threads
open_checkpoint $checkpoint
set core [get_cells receiver_v2_i/axi_iic_main]
if {[llength $core]!=1} {error "Missing AXI IIC controller"}
foreach {name expected_pin} {iic_mux_scl[0] AB4 iic_mux_scl[1] AA18 iic_mux_sda[0] AB5 iic_mux_sda[1] Y16} {
    set port [get_ports $name]
    if {[llength $port]!=1 || [get_property DIRECTION $port] ne "INOUT" ||
        [get_property PACKAGE_PIN $port] ne $expected_pin ||
        [get_property IOSTANDARD $port] ne "LVCMOS33" ||
        [get_property PULLTYPE $port] ne "PULLUP"} {
        error "Unexpected package port configuration $name"
    }
    set output_starts [all_fanin -flat -startpoints_only -to $port]
    set owned [filter $output_starts {NAME =~ receiver_v2_i/axi_iic_main/*}]
    if {[llength $owned]==0 || [llength $owned]!=[llength $output_starts]} {
        error "Unexpected routed controller drive for $name"
    }
    set pad_nets [get_nets -segments -of_objects $port]
    set pad_driver [get_pins -leaf -of_objects $pad_nets -filter {DIRECTION == OUT}]
    set buffer [get_cells -of_objects $pad_driver]
    if {[llength $buffer]!=1 || [get_property REF_NAME $buffer] ne "OBUFT"} {
        error "Unexpected pad driver for $name: $buffer"
    }
    set data_nets [get_nets -segments -of_objects [get_pins $buffer/I]]
    set data_driver [get_pins -leaf -of_objects $data_nets -filter {DIRECTION == OUT}]
    set data_cell [get_cells -of_objects $data_driver]
    if {[llength $data_cell]!=1 || [get_property REF_NAME $data_cell] ne "GND"} {
        error "Unexpected open-drain data drive for $name: $data_cell"
    }
    puts "IIC_PORT $name $expected_pin $buffer DRIVE=GND OUTPUT_STARTS={$output_starts}"
}
write_verilog -cell $core -mode funcsim -rename_top receiver_v2_axi_iic_main_0 iic-routed.v
close_design
puts IIC_ROUTED_EXPORT_COMPLETE
