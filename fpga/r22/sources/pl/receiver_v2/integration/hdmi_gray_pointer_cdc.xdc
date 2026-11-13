# Exact native HDMI Gray-pointer CDC bundle constraint.
#
# This file is deliberately XDC-parser-safe: it contains only the final
# constraint and exact object queries. Vivado read_xdc does not support Tcl
# control flow such as for/if/unset. Structural/clock/synchronizer checks live
# in hdmi_gray_pointer_contract.tcl, after synthesis/implementation.
#
# This neither changes clocks/false paths nor constrains i_frame_mailbox.
set_bus_skew 6.750 \
  -from [get_cells [list \
    {receiver_v2_i/axi_hdmi_core/inst/i_tx_core/hdmi_raddr_g_reg[0]} \
    {receiver_v2_i/axi_hdmi_core/inst/i_tx_core/hdmi_raddr_g_reg[1]} \
    {receiver_v2_i/axi_hdmi_core/inst/i_tx_core/hdmi_raddr_g_reg[2]} \
    {receiver_v2_i/axi_hdmi_core/inst/i_tx_core/hdmi_raddr_g_reg[3]} \
    {receiver_v2_i/axi_hdmi_core/inst/i_tx_core/hdmi_raddr_g_reg[4]} \
    {receiver_v2_i/axi_hdmi_core/inst/i_tx_core/hdmi_raddr_g_reg[5]} \
    {receiver_v2_i/axi_hdmi_core/inst/i_tx_core/hdmi_raddr_g_reg[6]} \
    {receiver_v2_i/axi_hdmi_core/inst/i_tx_core/hdmi_raddr_g_reg[7]} \
    {receiver_v2_i/axi_hdmi_core/inst/i_tx_core/hdmi_raddr_g_reg[8]}]] \
  -to [get_cells [list \
    {receiver_v2_i/axi_hdmi_core/inst/i_vdma/vdma_raddr_g_m1_reg[0]} \
    {receiver_v2_i/axi_hdmi_core/inst/i_vdma/vdma_raddr_g_m1_reg[1]} \
    {receiver_v2_i/axi_hdmi_core/inst/i_vdma/vdma_raddr_g_m1_reg[2]} \
    {receiver_v2_i/axi_hdmi_core/inst/i_vdma/vdma_raddr_g_m1_reg[3]} \
    {receiver_v2_i/axi_hdmi_core/inst/i_vdma/vdma_raddr_g_m1_reg[4]} \
    {receiver_v2_i/axi_hdmi_core/inst/i_vdma/vdma_raddr_g_m1_reg[5]} \
    {receiver_v2_i/axi_hdmi_core/inst/i_vdma/vdma_raddr_g_m1_reg[6]} \
    {receiver_v2_i/axi_hdmi_core/inst/i_vdma/vdma_raddr_g_m1_reg[7]} \
    {receiver_v2_i/axi_hdmi_core/inst/i_vdma/vdma_raddr_g_m1_reg[8]}]]
