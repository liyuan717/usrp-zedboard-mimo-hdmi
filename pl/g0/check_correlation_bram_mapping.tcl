# Actual inferred primitive contract for the isolated production-W33 proof.
# No timing waiver, source mutation, bitstream or hardware access.
if {[llength $argv]!=1} {error "usage: -tclargs correlation-numeric-probe-directory"}
set out [file normalize [lindex $argv 0]]
open_checkpoint [file join $out differential_corr_cost_probe.dcp]
set cells [get_cells -hier -quiet -filter {REF_NAME =~ RAMB* && NAME =~ history_ports*}]
if {[llength $cells]!=10} {error "expected8RAMB36+2RAMB18 history primitives, got $cells"}
set r36 0
set r18 0
set pins_checked 0
foreach cell $cells {
 set kind [get_property REF_NAME $cell]
 if {$kind eq "RAMB36E1"} {incr r36} elseif {$kind eq "RAMB18E1"} {incr r18} else {error "wrong primitive $cell $kind"}
 if {[get_property WRITE_MODE_A $cell] ne "READ_FIRST" || [get_property DOA_REG $cell]!=0 || [get_property DOB_REG $cell]!=0} {error "history latency/write mode changed at $cell"}
 foreach clock_pin {CLKARDCLK CLKBWRCLK} {
  set pin [get_pins -quiet -of_objects $cell -filter "REF_PIN_NAME == $clock_pin"]
  set clocks [get_clocks -quiet -of_objects $pin]
  if {[llength $clocks]!=1 || [get_property NAME $clocks] ne "processing" || abs([get_property PERIOD $clocks]-20)>0.001} {error "history port clock changed $cell/$clock_pin: $clocks"}
 }
 set writes [get_pins -quiet -of_objects $cell -filter {REF_PIN_NAME =~ WEBWE*}]
 if {![llength $writes]} {error "no portB write-enable pins at $cell"}
 foreach pin $writes {
  set nets [get_nets -quiet -segments -of_objects $pin]
  set drivers [get_pins -quiet -leaf -of_objects $nets -filter {DIRECTION == OUT}]
  set sources [get_cells -quiet -of_objects $drivers]
  if {[llength $sources]!=1 || [get_property REF_NAME $sources] ne "GND"} {error "portB is not read-only: $pin driven by $sources"}
  incr pins_checked
 }
}
if {$r36!=8 || $r18!=2} {error "wrong history mapping: R36=$r36 R18=$r18"}
puts "CORRELATION_HISTORY_PRIMITIVES_PASS RAMB36=$r36 RAMB18=$r18 readonly_B_bits=$pins_checked DOA_REG=0 DOB_REG=0 writeA=READ_FIRST clock_ns=20"
close_design
