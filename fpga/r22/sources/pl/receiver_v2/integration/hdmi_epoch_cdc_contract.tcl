# Read-only exact contract for the HDMI active-reset epoch handshake.
# It creates no max-delay, false-path, or waiver. Until a reviewed XDC owns
# these exact pairs, a normally timed or negative source->m1 path is a FAIL.
namespace eval receiver_v2_hdmi_epoch_cdc {
  proc one {objects label} { if {[llength $objects] != 1} { error "$label expected one object, got '$objects'" }; return [lindex $objects 0] }
  proc cell {name} { return [one [get_cells -hier -quiet -filter "NAME == $name"] "cell $name"] }
  proc pin {cell ref} { return [one [get_pins -quiet -of_objects $cell -filter "REF_PIN_NAME == $ref"] "$cell/$ref"] }
  proc asserted_async {cell label} { if {[string tolower [string trim [get_property ASYNC_REG $cell]]] ni {1 true yes}} { error "$label has no asserted ASYNC_REG" } }
  proc finite_nonnegative {value label} { if {![string is double -strict $value] || [regexp -nocase {nan|inf} $value] || $value < 0} { error "$label is not finite/nonnegative: '$value'" } }
  proc finite_positive {value label} { finite_nonnegative $value $label; if {$value <= 0} { error "$label is not positive: '$value'" } }
  proc require_bound {requirement bound label} { finite_positive $requirement "$label max-delay requirement"; if {[expr {abs(double($requirement) - double($bound))}] > 0.001} { error "$label requirement $requirement ns != exact bound $bound ns" } }
  proc require_fdce {cell label} { if {[get_property REF_NAME $cell] ne "FDCE"} { error "$label expected FDCE, got [get_property REF_NAME $cell]" } }
  proc require_clock {cell name period label} {
    set clock [one [get_clocks -quiet -of_objects [pin $cell C]] "$label clock"]
    if {[get_property NAME $clock] ne $name} { error "$label clock [get_property NAME $clock] != $name" }
    set actual [get_property PERIOD $clock]
    finite_positive $actual "$label clock period"
    if {[expr {abs(double($actual)-double($period))}] > 0.001} { error "$label period $actual != $period" }
  }
  proc sole_sink {from expected label {local_feedback {}}} {
    # A local hierarchical net segment can hide the crossing sink. Traverse
    # all hierarchy, but still prove a DIRECT electrical net to the m1 D pin.
    set source_nets [get_nets -quiet -segments -of_objects $from]
    set sink_nets [get_nets -quiet -segments -of_objects $expected]
    set common {}
    foreach net $source_nets {if {[lsearch -exact $sink_nets $net] >= 0} {lappend common $net}}
    if {![llength $common]} {error "$label is not a direct source-Q to destination-D net"}
    set sinks [lsort [all_fanout -flat -endpoints_only -from $from]]
    set expected_sinks [lsort [concat [list $expected] $local_feedback]]
    if {$sinks ne $expected_sinks} { error "$label has missing/extra sink(s): $sinks expected=$expected_sinks" }
  }
  proc validate_pair {label source_name m1_name m2_name source_clock source_period destination_clock destination_period bound_ns} {
    set source [cell $source_name]; set m1 [cell $m1_name]; set m2 [cell $m2_name]
    require_fdce $source "$label source"; require_fdce $m1 "$label m1"; require_fdce $m2 "$label m2"
    asserted_async $m1 "$label m1"; asserted_async $m2 "$label m2"
    require_clock $source $source_clock $source_period "$label source"
    require_clock $m1 $destination_clock $destination_period "$label m1"
    require_clock $m2 $destination_clock $destination_period "$label m2"
    set feedback {}
    if {$label eq "ack"} {
      # The acknowledgement retains its value through a local next-state mux.
      # Its own D is the only permitted additional endpoint, never another CDC.
      set feedback [list [pin $source D]]
      set self_path [one [get_timing_paths -quiet -from $source -to $source -max_paths 1] "$label local feedback path"]
      finite_nonnegative [get_property SLACK $self_path] "$label local feedback slack"
    }
    sole_sink [pin $source Q] [pin $m1 D] "$label source->m1" $feedback
    set q [pin $m1 Q]; set d [pin $m2 D]
    sole_sink $q $d "$label m1->m2"
    set local [one [get_timing_paths -quiet -from $q -to $d -max_paths 1] "$label normal m1->m2 path"]
    finite_nonnegative [get_property SLACK $local] "$label normal m1->m2 slack"
    set crossing [one [get_timing_paths -quiet -from $source -to $m1 -max_paths 1] "$label exact source->m1 path"]
    finite_nonnegative [get_property SLACK $crossing] "$label source->m1 slack"
    finite_positive [get_property DATAPATH_DELAY $crossing] "$label source->m1 datapath delay"
    set requirement [get_property REQUIREMENT $crossing]
    require_bound $requirement $bound_ns "$label source->m1"
    puts "HDMI_EPOCH_CDC_EXACT_PASS label=$label source=$source_name m1=$m1_name m2=$m2_name"
  }
  proc validate_ready {} { validate_pair ready receiver_v2_i/axi_hdmi_core/inst/i_tx_core/hdmi_epoch_ready_reg receiver_v2_i/axi_hdmi_core/inst/i_vdma/hdmi_epoch_ready_m1_reg receiver_v2_i/axi_hdmi_core/inst/i_vdma/hdmi_epoch_ready_m2_reg mmcm_clk_0_s 6.750 clk_fpga_0 20.000 20.000 }
  proc validate_ack {} { validate_pair ack receiver_v2_i/axi_hdmi_core/inst/i_vdma/vdma_epoch_ack_reg receiver_v2_i/axi_hdmi_core/inst/i_tx_core/vdma_epoch_ack_m1_reg receiver_v2_i/axi_hdmi_core/inst/i_tx_core/vdma_epoch_ack_m2_reg clk_fpga_0 20.000 mmcm_clk_0_s 6.750 6.750 }
}
