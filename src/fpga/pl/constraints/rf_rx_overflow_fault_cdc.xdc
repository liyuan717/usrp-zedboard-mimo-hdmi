# Sticky RX overflow request/acknowledge CDC.
#
# Load this file only after integrating rf_rx_overflow_fault_cdc below the
# candidate instance named rf_rx_transport_adc_fault_cdc.  The module's RTL,
# rather than this file, marks all five cross-domain destination registers
# ASYNC_REG and prevents SRL extraction.
#
# These two exceptions deliberately cover only the asynchronous flights into
# synchronizer stage 0.  They do not hide the stage0->stage1->stage2 paths,
# normal local logic, metadata buses, IQ buses, or unrelated clock crossings.
# No global set_clock_groups and no blanket false path is used here.

set rf_rx_fault_src_pending [get_cells -hier -filter {NAME =~ */rf_rx_transport_adc_fault_cdc/inst/src_pending_reg}]
set rf_rx_fault_dst_first [get_cells -hier -filter {NAME =~ */rf_rx_transport_adc_fault_cdc/inst/dst_pending_sync_0_reg}]
set rf_rx_fault_dst_ack [get_cells -hier -filter {NAME =~ */rf_rx_transport_adc_fault_cdc/inst/dst_pending_sync_2_reg}]
set rf_rx_fault_src_ack_first [get_cells -hier -filter {NAME =~ */rf_rx_transport_adc_fault_cdc/inst/src_ack_sync_0_reg}]

# 16-MHz l_clk source pending -> first 50-MHz destination synchronizer FF.
set_max_delay -datapath_only 20.000 \
  -from $rf_rx_fault_src_pending -to $rf_rx_fault_dst_first

# 50-MHz held destination fault -> first 16-MHz source acknowledge FF.
set_max_delay -datapath_only 62.500 \
  -from $rf_rx_fault_dst_ack -to $rf_rx_fault_src_ack_first
