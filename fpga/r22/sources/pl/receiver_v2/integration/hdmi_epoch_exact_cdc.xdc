# Exact source-to-first-stage physical bounds for the HDMI reset-epoch
# handshake. This file deliberately names each source/destination cell without
# wildcards; the companion contract rejects any replica or altered hierarchy.
set_max_delay -datapath_only 20.000 \
  -from [get_cells {receiver_v2_i/axi_hdmi_core/inst/i_tx_core/hdmi_epoch_ready_reg}] \
  -to   [get_cells {receiver_v2_i/axi_hdmi_core/inst/i_vdma/hdmi_epoch_ready_m1_reg}]
set_max_delay -datapath_only 6.750 \
  -from [get_cells {receiver_v2_i/axi_hdmi_core/inst/i_vdma/vdma_epoch_ack_reg}] \
  -to   [get_cells {receiver_v2_i/axi_hdmi_core/inst/i_tx_core/vdma_epoch_ack_m1_reg}]
