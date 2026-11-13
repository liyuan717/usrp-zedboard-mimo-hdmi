# Actual XPM mapping swaps physical A/B: physicalA reads logicalB; physicalB
# reads/writes logicalA. Check connections and gates, not API port spelling.
if {[llength $argv]!=2} {error "usage: -tclargs RRC-numeric-probe-directory report-directory"}
set out [file normalize [lindex $argv 0]]
set report [file normalize [lindex $argv 1]]
open_checkpoint [file join $out rrc_cost_probe.dcp]
set cells [get_cells -hier -quiet -filter {REF_NAME =~ RAMB* && NAME =~ history_ports*}]
if {[llength $cells]!=6} {error "expected6R36 history primitives, got $cells"}
proc source_of {pin} {
 return [get_cells -quiet -of_objects [get_pins -quiet -leaf -of_objects [get_nets -quiet -segments -of_objects $pin] -filter {DIRECTION == OUT}]]
}
set checked 0
foreach cell $cells {
 if {[get_property REF_NAME $cell] ne "RAMB36E1" || [get_property RAM_MODE $cell] ne "TDP" || [get_property DOA_REG $cell]!=0 || [get_property DOB_REG $cell]!=0 || [get_property WRITE_MODE_A $cell] ne "READ_FIRST" || [get_property WRITE_MODE_B $cell] ne "READ_FIRST"} {error "RRC memory type/latency/mode mismatch $cell"}
 foreach p {CLKARDCLK CLKBWRCLK} {
  set clocks [get_clocks -quiet -of_objects [get_pins -quiet -of_objects $cell -filter "REF_PIN_NAME == $p"]]
  if {[llength $clocks]!=1 || [get_property NAME $clocks] ne "processing" || abs([get_property PERIOD $clocks]-20)>0.001} {error "RRC clock mismatch $cell/$p $clocks"}
 }
 foreach pin [get_pins -quiet -of_objects $cell -filter {REF_PIN_NAME =~ WEA*}] {
  set source [source_of $pin]
  if {[llength $source]!=1 || [get_property REF_NAME $source] ne "GND"} {error "physicalA (logicalB) not read-only $pin"}
  incr checked
 }
 foreach pin [get_pins -quiet -of_objects $cell -filter {REF_PIN_NAME =~ WEBWE*}] {
  set source [source_of $pin]
  if {[llength $source]!=1} {error "ambiguous physicalB write source $pin"}
  if {[get_property REF_NAME $source] eq "GND"} {continue}
  if {[get_property REF_NAME $source] ne "LUT2" || [get_property INIT $source] ne "4'h8"} {error "unexpected write gate $source"}
  foreach {p expected} {I0 wea I1 enb} {
   set nets [get_nets -quiet -segments -of_objects [get_pins -quiet -of_objects $source -filter "REF_PIN_NAME == $p"]]
   set match 0
   foreach n $nets {if {[regexp [format {/%s(\[0\])?$} $expected] $n]} {set match 1}}
   if {!$match} {error "write gate input differs from logical $expected at $source/$p: $nets"}
  }
 }
}
if {$checked!=24} {error "incorrect readonly write-pin count $checked"}
write_verilog -force -mode funcsim -rename_top rrc_netlist [file join $report rrc_netlist.v]
puts "RRC_HISTORY_PRIMITIVES_PASS RAMB36=6 readonly_physical_A_bits=$checked physical_B_write=logical_wea_AND_enb DOA_REG=0 DOB_REG=0 clocks_ns=20"
close_design
