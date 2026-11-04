# Formal F04-fix candidate; historical direct-FIFO LVDS/CMOS remain selectable.
# Keep a fresh build directory so no preceding bit/XSA/evidence is replaced.
set ::env(MIMO_RF_INTERFACE) lvds_2r2t_2v5
set ::env(MIMO_RF_RX_BUFFERED) 1
source [file join [file dirname [info script]] create_zedboard_rf_system.tcl]
