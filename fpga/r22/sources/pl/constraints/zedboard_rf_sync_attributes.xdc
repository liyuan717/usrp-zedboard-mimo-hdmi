# Native util_wfifo/util_rfifo already contain two-stage synchronizers and
# vendor XDC marks their first stage. Mark the existing SECOND stage too;
# do not add a timing exception or pretend a multi-bit bus becomes atomic.
set rf_status_second [get_cells -hier -filter {NAME =~ */rf_rx_fifo/inst/din_ovf_reg || NAME =~ */rf_tx_fifo/inst/dout_unf_reg}]
# Cardinality/property checks run in audit_zedboard_rf_topology.tcl, not in
# XDC: Vivado's synthesis XDC reader does not support Tcl control flow.
set_property ASYNC_REG TRUE $rf_status_second
set rf_enable_second [get_cells -hier -filter {NAME =~ */rf_rx_fifo/inst/dout_enable_reg* || NAME =~ */rf_tx_fifo/inst/din_enable_reg*}]
set_property ASYNC_REG TRUE $rf_enable_second
# Enable bits are four independent booleans. The release software permits
# only all-four-channel scan masks and configures while RF is stopped;
# dynamic channel-set changes during a stream are forbidden. This property
# helps synchronizer placement but does NOT replace that protocol contract.
# IO0 is the output-only MOSI line. The vendor's capture-register IOB hint
# cannot apply to that internal, non-pad input. Remove only the invalid hint;
# MISO input and all actual IO delay constraints remain active.
set_property IOB FALSE [get_cells -hier -filter {NAME =~ */rf_spi/U0/NO_DUAL_QUAD_MODE.QSPI_NORMAL/IO0_I_REG}]
