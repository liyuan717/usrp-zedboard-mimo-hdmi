// ***************************************************************************
// ***************************************************************************
// Copyright (C) 2014-2023 Analog Devices, Inc. All rights reserved.
//
// In this HDL repository, there are many different and unique modules, consisting
// of various HDL (Verilog or VHDL) components. The individual modules are
// developed independently, and may be accompanied by separate and unique license
// terms.
//
// The user should read each of these license terms, and understand the
// freedoms and responsibilities that he or she has by using this source/core.
//
// This core is distributed in the hope that it will be useful, but WITHOUT ANY
// WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR
// A PARTICULAR PURPOSE.
//
// Redistribution and use of source or resulting binaries, with or without modification
// of this file, are permitted under one of the following two license terms:
//
//   1. The GNU General Public License version 2 as published by the
//      Free Software Foundation, which can be found in the top level directory
//      of this repository (LICENSE_GPL2), and also online at:
//      <https://www.gnu.org/licenses/old-licenses/gpl-2.0.html>
//
// OR
//
//   2. An ADI specific BSD license, which can be found in the top level directory
//      of this repository (LICENSE_ADIBSD), and also on-line at:
//      https://github.com/analogdevicesinc/hdl/blob/main/LICENSE_ADIBSD
//      This will allow to generate bit files and not release the source code,
//      as long as it attaches to an ADI device.
//
// ***************************************************************************
// ***************************************************************************
// Transmit HDMI, video dma data in, hdmi separate syncs data out.

`timescale 1ns/100ps

module axi_hdmi_tx_vdma (

  // hdmi interface

  input                   hdmi_fs_toggle,
  input       [ 8:0]      hdmi_raddr_g,

  // vdma interface

  input                   vdma_clk,
  input                   vdma_rst,
  input                   hdmi_epoch_ready,
  (* DONT_TOUCH = "TRUE" *) output reg             vdma_epoch_ack,
  input                   vdma_end_of_frame,
  input                   vdma_valid,
  input       [63:0]      vdma_data,
  output  wire            vdma_ready,
  output  wire            vdma_wr,
  output  reg [ 8:0]      vdma_waddr,
  output  reg [47:0]      vdma_wdata,
  output  reg             vdma_fs_commit,
  output  reg [ 8:0]      vdma_fs_waddr,
  output  reg             vdma_tpm_oos,
  output  reg             vdma_ovf,
  output  reg             vdma_unf
);

  localparam      BUF_THRESHOLD_LO = 9'd3;
  localparam      BUF_THRESHOLD_HI = 9'd509;
  localparam      RDY_THRESHOLD_LO = 9'd450;
  localparam      RDY_THRESHOLD_HI = 9'd500;

  // internal registers

  (* ASYNC_REG = "TRUE" *) reg hdmi_epoch_ready_m1 = 1'd0;
  (* ASYNC_REG = "TRUE" *) reg hdmi_epoch_ready_m2 = 1'd0;
  reg             vdma_epoch_armed = 1'd0;
  reg             vdma_ready_i = 1'd0;
  reg             vdma_wr_i = 1'd0;
  (* ASYNC_REG = "TRUE" *) reg vdma_fs_toggle_m1 = 1'd0;
  reg             vdma_fs_toggle_m2 = 1'd0;
  reg             vdma_fs_toggle_m3 = 1'd0;
  reg     [22:0]  vdma_tpm_data = 23'd0;
  (* ASYNC_REG = "TRUE" *) reg [ 8:0]  vdma_raddr_g_m1 = 9'd0;
  reg     [ 8:0]  vdma_raddr_g_m2 = 9'd0;
  reg     [ 8:0]  vdma_raddr = 9'd0;
  reg     [ 8:0]  vdma_addr_diff = 9'd0;
  reg             vdma_almost_full = 1'd0;
  reg             vdma_almost_empty = 1'd0;
  reg             hdmi_fs = 1'd0;
  reg             vdma_fs = 1'd0;
  reg             vdma_end_of_frame_d = 1'd0;
  reg             vdma_active_frame = 1'd0;

  // internal wires

  wire    [47:0]  vdma_tpm_data_s;
  wire            vdma_tpm_oos_s;
  wire    [ 9:0]  vdma_addr_diff_s;
  wire            vdma_ovf_s;
  wire            vdma_unf_s;
  wire            vdma_eof_fire;
  wire            vdma_accept;

  assign vdma_ready = ((vdma_rst == 1'b0) && (vdma_epoch_armed == 1'b1)) ? vdma_ready_i : 1'b0;
  // The BRAM write-enable is driven only by this synchronous register.
  // Reset may leave one old cycle resident, but presentation remains
  // masked until a fresh post-reset EOF/mailbox epoch has completed.
  assign vdma_wr = vdma_wr_i;
  assign vdma_accept = vdma_valid & vdma_ready;

  // TLAST has frame semantics only on an accepted AXI-stream beat. A source
  // holding TLAST during backpressure must not duplicate frame completion.
  assign vdma_eof_fire = vdma_accept & vdma_end_of_frame;

  // grey to binary conversion

  function [8:0] g2b;
    input [8:0] g;
    reg   [8:0] b;
    begin
      b[8] = g[8];
      b[7] = b[8] ^ g[7];
      b[6] = b[7] ^ g[6];
      b[5] = b[6] ^ g[5];
      b[4] = b[5] ^ g[4];
      b[3] = b[4] ^ g[3];
      b[2] = b[3] ^ g[2];
      b[1] = b[2] ^ g[1];
      b[0] = b[1] ^ g[0];
      g2b = b;
    end
  endfunction

  // Destination half of the reset epoch.  `hdmi_epoch_ready` can only
  // become true after the source has asynchronously cleared its state.  The
  // source remains frozen until this acknowledgement returns.
  always @(posedge vdma_clk or posedge vdma_rst) begin
    if (vdma_rst == 1'b1) begin
      hdmi_epoch_ready_m1 <= 1'b0;
      hdmi_epoch_ready_m2 <= 1'b0;
      vdma_epoch_ack <= 1'b0;
      vdma_epoch_armed <= 1'b0;
    end else begin
      hdmi_epoch_ready_m1 <= hdmi_epoch_ready;
      hdmi_epoch_ready_m2 <= hdmi_epoch_ready_m1;
      if (hdmi_epoch_ready_m2 == 1'b0) begin
        vdma_epoch_ack <= 1'b0;
        vdma_epoch_armed <= 1'b0;
      end else if (vdma_epoch_armed == 1'b0) begin
        // This is a protocol baseline check, not a fixed-cycle delay.  Source
        // RUN is impossible before ack, hence the complete Gray vector is held.
        if ((vdma_fs_toggle_m2 == 1'b0) && (vdma_fs_toggle_m3 == 1'b0) &&
            (vdma_raddr_g_m2 == 9'd0)) begin
          vdma_epoch_ack <= 1'b1;
          vdma_epoch_armed <= 1'b1;
        end
      end
    end
  end

  // hdmi frame sync

  always @(posedge vdma_clk or posedge vdma_rst) begin
    if (vdma_rst == 1'b1) begin
      vdma_fs_toggle_m1 <= 1'd0;
      vdma_fs_toggle_m2 <= 1'd0;
      vdma_fs_toggle_m3 <= 1'd0;
      hdmi_fs <= 1'b0;
    end else begin
      vdma_fs_toggle_m1 <= hdmi_fs_toggle;
      vdma_fs_toggle_m2 <= vdma_fs_toggle_m1;
      vdma_fs_toggle_m3 <= vdma_fs_toggle_m2;
      hdmi_fs <= vdma_epoch_armed ? (vdma_fs_toggle_m2 ^ vdma_fs_toggle_m3) : 1'b0;
    end
  end

  // dma frame sync

  always @(posedge vdma_clk or posedge vdma_rst) begin
    if (vdma_rst == 1'b1) begin
      vdma_end_of_frame_d <= 1'b0;
      vdma_fs <= 1'b0;
    end else if (vdma_epoch_armed == 1'b0) begin
      vdma_end_of_frame_d <= 1'b0;
      vdma_fs <= 1'b0;
    end else begin
      vdma_end_of_frame_d <= vdma_eof_fire;
      vdma_fs <= vdma_end_of_frame_d;
    end
  end

  // sync dma and hdmi frames

  always @(posedge vdma_clk or posedge vdma_rst) begin
    if (vdma_rst == 1'b1) begin
      vdma_fs_commit <= 1'b0;
      vdma_fs_waddr <= 9'b0;
    end else if (vdma_epoch_armed == 1'b0) begin
      vdma_fs_commit <= 1'b0;
      vdma_fs_waddr <= 9'b0;
    end else begin
      // Commit one VDMA clock after the address capture so the mailbox never
      // observes a request and its payload changing on the same edge.
      vdma_fs_commit <= vdma_fs;
      if (vdma_fs) begin
        vdma_fs_waddr <= vdma_waddr ;
      end
    end
  end

  // accept new frame from dma

  always @(posedge vdma_clk or posedge vdma_rst) begin
    if (vdma_rst == 1'b1) begin
      vdma_active_frame <= 1'b0;
    end else if (vdma_epoch_armed == 1'b0) begin
      vdma_active_frame <= 1'b0;
    end else begin
      if ((vdma_active_frame == 1'b1) && (vdma_eof_fire == 1'b1)) begin
        vdma_active_frame <= 1'b0;
      end else if ((vdma_active_frame == 1'b0) && (hdmi_fs == 1'b1)) begin
        vdma_active_frame <= 1'b1;
      end
    end
  end

  // vdma write

  // BRAM write-side address/data/enable state has synchronous reset only.
  always @(posedge vdma_clk) begin
    if (vdma_rst == 1'b1) begin
      vdma_wr_i <= 1'b0;
      vdma_waddr <= 9'd0;
      vdma_wdata <= 48'd0;
    end else if (vdma_epoch_armed == 1'b0) begin
      vdma_wr_i <= 1'b0;
      vdma_waddr <= 9'd0;
      vdma_wdata <= 48'd0;
    end else begin
      vdma_wr_i <= vdma_accept;
      if (vdma_wr == 1'b1)
        vdma_waddr <= vdma_waddr + 1'b1;
      vdma_wdata <= {vdma_data[55:32], vdma_data[23:0]};
    end
  end

  // test error conditions

  assign vdma_tpm_data_s = {vdma_tpm_data, 1'b1, vdma_tpm_data, 1'b0};
  assign vdma_tpm_oos_s = (vdma_wdata == vdma_tpm_data_s) ? 1'b0 : vdma_wr;

  always @(posedge vdma_clk or posedge vdma_rst) begin
    if (vdma_rst == 1'b1) begin
      vdma_tpm_data <= 23'd0;
      vdma_tpm_oos <= 1'd0;
    end else if ((vdma_epoch_armed == 1'b0) || (vdma_fs == 1'b1)) begin
      vdma_tpm_data <= 23'd0;
      vdma_tpm_oos <= 1'd0;
    end else if (vdma_wr == 1'b1) begin
      vdma_tpm_data <= vdma_tpm_data + 1'b1;
      vdma_tpm_oos <= vdma_tpm_oos_s;
    end
  end

  // overflow or underflow status

  assign vdma_addr_diff_s = {1'b1, vdma_waddr} - vdma_raddr;
  assign vdma_ovf_s = (vdma_addr_diff < BUF_THRESHOLD_LO) ? vdma_almost_full : 1'b0;
  assign vdma_unf_s = (vdma_addr_diff > BUF_THRESHOLD_HI) ? vdma_almost_empty : 1'b0;

  always @(posedge vdma_clk or posedge vdma_rst) begin
    if (vdma_rst == 1'b1) begin
      vdma_raddr_g_m1 <= 9'd0;
      vdma_raddr_g_m2 <= 9'd0;
    end else begin
      vdma_raddr_g_m1 <= hdmi_raddr_g;
      vdma_raddr_g_m2 <= vdma_raddr_g_m1;
    end
  end

  always @(posedge vdma_clk or posedge vdma_rst) begin
    if (vdma_rst == 1'b1) begin
      vdma_raddr <= 9'd0;
      vdma_addr_diff <= 9'd0;
      vdma_ready_i <= 1'b0;
      vdma_almost_full <= 1'b0;
      vdma_almost_empty <= 1'b0;
      vdma_ovf <= 1'b0;
      vdma_unf <= 1'b0;
    end else if (vdma_epoch_armed == 1'b0) begin
      vdma_raddr <= 9'd0;
      vdma_addr_diff <= 9'd0;
      vdma_ready_i <= 1'b0;
      vdma_almost_full <= 1'b0;
      vdma_almost_empty <= 1'b0;
      vdma_ovf <= 1'b0;
      vdma_unf <= 1'b0;
    end else begin
      vdma_raddr <= g2b(vdma_raddr_g_m2);
      vdma_addr_diff <= vdma_addr_diff_s[8:0];
      if (vdma_addr_diff >= RDY_THRESHOLD_HI) begin
        vdma_ready_i <= 1'b0;
      end else if (vdma_addr_diff <= RDY_THRESHOLD_LO) begin
        vdma_ready_i <= vdma_active_frame;
      end
      if (vdma_addr_diff > BUF_THRESHOLD_HI) begin
        vdma_almost_full <= 1'b1;
      end else begin
        vdma_almost_full <= 1'b0;
      end
      if (vdma_addr_diff < BUF_THRESHOLD_LO) begin
        vdma_almost_empty <= 1'b1;
      end else begin
        vdma_almost_empty <= 1'b0;
      end
      vdma_ovf <= vdma_ovf_s;
      vdma_unf <= vdma_unf_s;
    end
  end

endmodule
