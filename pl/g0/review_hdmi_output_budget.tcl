# Verify the inherited, device-only and preboard budgets on a routed checkpoint.
# The in-memory trial never saves a checkpoint or bitstream.
lassign $argv threads checkpoint budget
set_param general.maxThreads $threads
open_checkpoint $checkpoint
set ports [get_ports {hdmi_data[*] hdmi_vsync hdmi_hsync hdmi_data_e}]
set expected {hdmi_vsync hdmi_hsync hdmi_data_e}
for {set bit 0} {$bit < 16} {incr bit} {
    lappend expected [format {hdmi_data[%d]} $bit]
}
if {[lsort $ports] ne [lsort $expected]} {error "Unexpected ADV7511 pin set: $ports"}

proc audit_video_paths {ports report stage} {
    set results {}
    foreach port $ports {
        set slacks {}
        foreach kind {max min} {
            set path [get_timing_paths -quiet -to $port -delay_type $kind -max_paths 1]
            if {[llength $path] != 1 || [get_property ENDPOINT_PIN $path] ne $port ||
                [get_property ENDPOINT_CLOCK $path] ne "hdmi_forwarded"} {
                error "Missing exact $kind forwarded-clock path to $port"
            }
            set slack [get_property SLACK $path]
            if {![string is double -strict $slack] || !(-Inf < $slack && $slack < Inf)} {
                error "Invalid $kind slack $slack to $port"
            }
            lappend slacks $slack
        }
        dict set results $port $slacks
        puts $report [join [list $stage $port {*}$slacks] "\t"]
    }
    return $results
}

set report [open hdmi-paths.tsv w]
puts $report "budget\tport\tsetup_ns\thold_ns"
set baseline [audit_video_paths $ports $report inherited]
set_output_delay -clock hdmi_forwarded -min -0.700 $ports
set device [audit_video_paths $ports $report device]
read_xdc $budget
set preboard [audit_video_paths $ports $report preboard]
close $report
foreach port $ports {
    lassign [dict get $baseline $port] old_setup old_hold
    lassign [dict get $device $port] dev_setup dev_hold
    lassign [dict get $preboard $port] board_setup board_hold
    if {abs($old_setup-$dev_setup) > 0.001 || abs($old_setup-$board_setup) > 0.001 ||
        abs($old_hold-$dev_hold-0.200) > 0.002 || abs($old_hold-$board_hold-0.700) > 0.002} {
        error "Budget change has unexpected effect on $port"
    }
}
puts "ADV7511_OUTPUT_BUDGET_TRIAL_COMPLETE paths=[llength $ports]"
close_design
