# Package ADI IP in a temporary staged HDL tree (optional argument: rf).
# The caller must set ADI_HDL_DIR and may set ADI_IGNORE_VERSION_CHECK=1.

if {![info exists ::env(ADI_HDL_DIR)]} {
    error "ADI_HDL_DIR is required"
}
set adi_root [file normalize $::env(ADI_HDL_DIR)]
set saved_dir [pwd]
set ips {axi_ad9361 axi_dmac}
if {$argc > 1 || ($argc == 1 && [lindex $argv 0] ne "rf")} {
    error "Usage: package_adi_ip_catalog.tcl ?rf?"
}
if {$argc == 1} {
    # DMAC core dependencies first; the remaining cores form the official
    # FMCOMMS2/3 four-channel RX/TX datapath.
    set ips {util_cdc util_axis_fifo axi_ad9361 axi_dmac xilinx/util_clkdiv
        util_wfifo util_rfifo util_pack/util_cpack2 util_pack/util_upack2}
}

foreach ip_path $ips {
    set ip_name [file tail $ip_path]
    set ip_dir [file join $adi_root library $ip_path]
    set ip_script [file join $ip_dir ${ip_name}_ip.tcl]
    if {![file exists $ip_script]} {
        error "missing ADI IP script: $ip_script"
    }
    cd $ip_dir
    puts "ADI_IP_PACKAGE_BEGIN $ip_name"
    source $ip_script
    if {[llength [get_projects -quiet]] > 0} {
        close_project
    }
    puts "ADI_IP_PACKAGE_PASS $ip_name"
    cd $saved_dir
}
puts "ADI_IP_PACKAGE_COMPLETE $adi_root"
