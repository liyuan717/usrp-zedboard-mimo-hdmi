###############################################################################
## Copyright (C) 2014-2023, 2026 Analog Devices, Inc. All rights reserved.
## Short identifier: ADIBSD
##
## Redistribution and use in source and binary forms, with or without modification,
## are permitted provided that the following conditions are met:
##     - Redistributions of source code must retain the above copyright
##       notice, this list of conditions and the following disclaimer.
##     - Redistributions in binary form must reproduce the above copyright
##       notice, this list of conditions and the following disclaimer in
##       the documentation and/or other materials provided with the
##       distribution.
##     - Neither the name of Analog Devices, Inc. nor the names of its
##       contributors may be used to endorse or promote products derived
##       from this software without specific prior written permission.
##     - The use of this software may or may not infringe the patent rights
##       of one or more patent holders. This license does not release you
##       from the requirement that you obtain separate licenses from these
##       patent holders to use this software.
##     - Use of the software either in source or binary form, must be run
##       on or directly connected to an Analog Devices Inc. component.
##
## THIS SOFTWARE IS PROVIDED BY ANALOG DEVICES "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES,
## INCLUDING, BUT NOT LIMITED TO, NON-INFRINGEMENT, MERCHANTABILITY AND FITNESS FOR A
## PARTICULAR PURPOSE ARE DISCLAIMED.
##
## IN NO EVENT SHALL ANALOG DEVICES BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL,
## EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, INTELLECTUAL PROPERTY
## RIGHTS, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR
## BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT,
## STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF
## THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
###############################################################################

set_property ASYNC_REG TRUE [get_cells -hier -filter {name =~ *vdma_fs_toggle*}]
set_property ASYNC_REG TRUE [get_cells -hier -filter {name =~ *vdma_raddr_g*}]

set_false_path -from [get_cells -hier -filter {name =~ *hdmi_fs_toggle_reg      && IS_SEQUENTIAL}] -to [get_cells -hier -filter {name =~ *vdma_fs_toggle_m1_reg     && IS_SEQUENTIAL}]
set_false_path -from [get_cells -hier -filter {name =~ *hdmi_raddr_g*           && IS_SEQUENTIAL}] -to [get_cells -hier -filter {name =~ *vdma_raddr_g_m1*          && IS_SEQUENTIAL}]
# Candidate-only replacement for the former unconstrained 9-bit frame-address
# crossing. `i_frame_mailbox` holds its source bundle until a four-phase
# valid/acknowledge transaction completes. The 6.000 ns bound
# is intentionally below the 148.148 MHz reference period (6.750 ns), leaving
# an entire additional two-stage synchronization window before capture; 1 ns
# bus skew keeps the 9-bit held bundle coherent.  These are internal FPGA
# datapath requirements, not external ADV7511 timing assumptions.
set_max_delay -datapath_only 6.000 \
  -from [get_cells -hier -filter {name =~ *i_frame_mailbox/src_payload_hold_reg* && IS_SEQUENTIAL}] \
  -to [get_cells -hier -filter {name =~ *i_frame_mailbox/dst_addr_reg* && IS_SEQUENTIAL}]
set_bus_skew 1.000 \
  -from [get_cells -hier -filter {name =~ *i_frame_mailbox/src_payload_hold_reg* && IS_SEQUENTIAL}] \
  -to [get_cells -hier -filter {name =~ *i_frame_mailbox/dst_addr_reg* && IS_SEQUENTIAL}]
set_false_path -from [get_cells -hier -filter {name =~ *i_frame_mailbox/dst_ack_reg && IS_SEQUENTIAL}] \
  -to [get_cells -hier -filter {name =~ *i_frame_mailbox/src_ack_m1_reg && IS_SEQUENTIAL}]
set_false_path -from [get_cells -hier -filter {name =~ *i_frame_mailbox/src_valid_reg && IS_SEQUENTIAL}] \
  -to [get_cells -hier -filter {name =~ *i_frame_mailbox/dst_valid_m1_reg && IS_SEQUENTIAL}]
set_false_path -from [get_cells -hier -filter {name =~ *i_frame_mailbox/src_flush_reg && IS_SEQUENTIAL}] \
  -to [get_cells -hier -filter {name =~ *i_frame_mailbox/dst_flush_m1_reg && IS_SEQUENTIAL}]
set_false_path -from [get_cells -hier -filter {name =~ *i_frame_mailbox/dst_flush_ack_reg && IS_SEQUENTIAL}] \
  -to [get_cells -hier -filter {name =~ *i_frame_mailbox/src_flush_ack_m1_reg && IS_SEQUENTIAL}]

