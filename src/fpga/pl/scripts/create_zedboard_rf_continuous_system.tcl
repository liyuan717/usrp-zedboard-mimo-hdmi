# Continuous-stream RF candidate.  This rewrites only build-selection defaults;
# it must use a fresh output directory and is not a hardware-tested artifact.
set ::env(MIMO_RF_INTERFACE) lvds_2r2t_2v5
set ::env(MIMO_RF_RX_BUFFERED) 1
set ::env(MIMO_RF_RX_CYCLIC) 1
source [file join [file dirname [info script]] create_zedboard_rf_system.tcl]
