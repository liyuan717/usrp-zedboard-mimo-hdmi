#!/usr/bin/env python3
"""Create a fail-closed active-reset HDMI overlay from the frozen r5 ADI HDL.

The input tree is read-only.  Only a fresh output directory is written.  The
parent build must explicitly consume the generated overlay; this tool does not
repackage an IP, touch a Vivado project, or claim a bitstream result.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path


HERE = Path(__file__).resolve().parent
SOURCE_DIR = HERE / "hdmi_reset_patch_sources"

EXPECTED_SHA256 = {
    "library/axi_hdmi_tx/axi_hdmi_tx.v": "a2e14ab0a19e1dd57042c6df455df6360d270bc03b9ea6d3d1846026939b1420",
    "library/axi_hdmi_tx/axi_hdmi_tx_core.v": "3dcdf3b9d6830a9679da2fe732117d96859242931f37101c72873babd9ee4092",
    "library/axi_hdmi_tx/axi_hdmi_tx_vdma.v": "b271f457b5ff8ee4ed87e192c588e916ead92b7de21bc8b5e7b38c0d6295c692",
    "library/common/up_hdmi_tx.v": "ffd377f89fc6b34de58df72689121ba0caa825eec6fbc3edba391392c8634ec7",
    "library/common/ad_rst.v": "721cf03b9b6146a487aa4687482b3781230b0d5ea523741c946d4eb1c8dfd95f",
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def replace_once(data: str, before: str, after: str, label: str) -> str:
    count = data.count(before)
    if count != 1:
        raise RuntimeError(f"{label}: expected exactly one patch anchor, found {count}")
    return data.replace(before, after)


def read_verified(root: Path, relative: str) -> str:
    path = root / relative
    if not path.is_file():
        raise RuntimeError(f"missing pinned r5 input: {path}")
    got = sha256(path)
    expected = EXPECTED_SHA256[relative]
    if got != expected:
        raise RuntimeError(f"{relative}: SHA-256 {got}, expected pinned r5 {expected}")
    return path.read_text(encoding="utf-8")


def patch_top(data: str) -> str:
    # Keep the public HDMI ABI intact while making the final pixel outputs
    # actual output registers. This is required for the existing IOB=TRUE DE
    # constraint and keeps data/DE/sync at one common pixel latency.
    for before, after, label in (
        ("  output                  hdmi_16_hsync,\n  output                  hdmi_16_vsync,\n  output                  hdmi_16_data_e,\n  output      [15:0]      hdmi_16_data,\n  output      [15:0]      hdmi_16_es_data,",
         "  output reg              hdmi_16_hsync,\n  output reg              hdmi_16_vsync,\n  output reg              hdmi_16_data_e,\n  output reg [15:0]       hdmi_16_data,\n  output reg [15:0]       hdmi_16_es_data,", "16-bit output registers"),
        ("  output                  hdmi_24_hsync,\n  output                  hdmi_24_vsync,\n  output                  hdmi_24_data_e,\n  output      [23:0]      hdmi_24_data,",
         "  output reg              hdmi_24_hsync,\n  output reg              hdmi_24_vsync,\n  output reg              hdmi_24_data_e,\n  output reg [23:0]       hdmi_24_data,", "24-bit output registers"),
        ("  output                  vga_hsync,\n  output                  vga_vsync,\n  output      [7:0]       vga_red,\n  output      [7:0]       vga_green,\n  output      [7:0]       vga_blue,",
         "  output reg              vga_hsync,\n  output reg              vga_vsync,\n  output reg [7:0]        vga_red,\n  output reg [7:0]        vga_green,\n  output reg [7:0]        vga_blue,", "VGA output registers"),
        ("  output                  hdmi_36_hsync,\n  output                  hdmi_36_vsync,\n  output                  hdmi_36_data_e,\n  output      [35:0]      hdmi_36_data,",
         "  output reg              hdmi_36_hsync,\n  output reg              hdmi_36_vsync,\n  output reg              hdmi_36_data_e,\n  output reg [35:0]       hdmi_36_data,", "36-bit output registers"),
    ):
        data = replace_once(data, before, after, "axi_hdmi_tx.v " + label)
    data = replace_once(
        data,
        "  wire            reference_rst;\n  wire            vdma_rst;\n",
        "  wire            reference_rst;\n  wire            vdma_rst;\n"
        "  wire            common_epoch_fault_s;\n"
        "  wire            hdmi_epoch_ready_s;\n"
        "  wire            vdma_epoch_ack_s;\n"
        "  wire            hdmi_display_valid_s;\n"
        "  wire            hdmi_16_hsync_s;\n"
        "  wire            hdmi_16_vsync_s;\n"
        "  wire            hdmi_16_data_e_s;\n"
        "  wire    [15:0]  hdmi_16_data_s;\n"
        "  wire    [15:0]  hdmi_16_es_data_s;\n"
        "  wire            hdmi_24_hsync_s;\n"
        "  wire            hdmi_24_vsync_s;\n"
        "  wire            hdmi_24_data_e_s;\n"
        "  wire    [23:0]  hdmi_24_data_s;\n"
        "  wire            vga_hsync_s;\n"
        "  wire            vga_vsync_s;\n"
        "  wire            hdmi_36_data_e_s;\n"
        "  wire            hdmi_36_hsync_s;\n"
        "  wire            hdmi_36_vsync_s;\n"
        "  wire    [35:0]  hdmi_36_data_s;\n"
        "  wire    [ 7:0]  vga_red_s;\n"
        "  wire    [ 7:0]  vga_green_s;\n"
        "  wire    [ 7:0]  vga_blue_s;\n"
        "  wire            vdma_bridge_ready_s;\n"
        "  wire            vdma_bridge_valid_s;\n"
        "  wire    [63:0]  vdma_bridge_data_s;\n"
        "  wire            vdma_bridge_last_s;\n",
        "axi_hdmi_tx.v reset wires",
    )
    data = replace_once(
        data,
        "    .vdma_rst (vdma_rst),\n    .vdma_ovf (vdma_ovf_s),",
        "    .vdma_rst (vdma_rst),\n"
        "    .core_rst_async (common_epoch_fault_s),\n"
        "    .vdma_ovf (vdma_ovf_s),",
        "axi_hdmi_tx.v up HDMI connection",
    )
    data = replace_once(
        data,
        "    .vdma_rst (vdma_rst),\n    .vdma_valid (vdma_valid),",
        "    .vdma_rst (vdma_rst),\n"
        "    .hdmi_epoch_ready (hdmi_epoch_ready_s),\n"
        "    .vdma_epoch_ack (vdma_epoch_ack_s),\n"
        "    .vdma_valid (vdma_bridge_valid_s),",
        "axi_hdmi_tx.v VDMA connection",
    )
    data = replace_once(
        data,
        "    .vdma_valid (vdma_bridge_valid_s),\n"
        "    .vdma_data (vdma_data),\n"
        "    .vdma_ready (vdma_ready),\n"
        "    .vdma_end_of_frame (vdma_end_of_frame),",
        "    .vdma_valid (vdma_bridge_valid_s),\n"
        "    .vdma_data (vdma_bridge_data_s),\n"
        "    .vdma_ready (vdma_bridge_ready_s),\n"
        "    .vdma_end_of_frame (vdma_bridge_last_s),",
        "axi_hdmi_tx.v VDMA bridge ports",
    )
    data = replace_once(
        data,
        "  axi_hdmi_tx_vdma i_vdma (\n",
        "  // DMA BRAM backpressure is isolated from raw reset/epoch state by a\n"
        "  // registered two-entry bridge. Its reset epoch discards through TLAST.\n"
        "  axi_hdmi_tx_axis_epoch_bridge i_axis_epoch_bridge (\n"
        "    .clk (vdma_clk),\n"
        "    .rst (vdma_rst),\n"
        "    .s_axis_tvalid (vdma_valid),\n"
        "    .s_axis_tdata (vdma_data),\n"
        "    .s_axis_tlast (vdma_end_of_frame),\n"
        "    .s_axis_tready (vdma_ready),\n"
        "    .m_axis_tvalid (vdma_bridge_valid_s),\n"
        "    .m_axis_tdata (vdma_bridge_data_s),\n"
        "    .m_axis_tlast (vdma_bridge_last_s),\n"
        "    .m_axis_tready (vdma_bridge_ready_s),\n"
        "    .drop_to_tlast ());\n\n"
        "  axi_hdmi_tx_vdma i_vdma (\n",
        "axi_hdmi_tx.v AXIS epoch bridge",
    )
    data = replace_once(
        data,
        "    .reference_rst (reference_rst),\n    .hdmi_16_hsync (hdmi_16_hsync),",
        "    .reference_rst (reference_rst),\n"
        "    .vdma_epoch_ack (vdma_epoch_ack_s),\n"
        "    .hdmi_epoch_ready (hdmi_epoch_ready_s),\n"
        "    .hdmi_display_valid (hdmi_display_valid_s),\n"
        "    .hdmi_16_hsync (hdmi_16_hsync),",
        "axi_hdmi_tx.v core connection",
    )
    for before, after, label in (
        ("    .hdmi_16_hsync (hdmi_16_hsync),", "    .hdmi_16_hsync (hdmi_16_hsync_s),", "16b HS"),
        ("    .hdmi_16_vsync (hdmi_16_vsync),", "    .hdmi_16_vsync (hdmi_16_vsync_s),", "16b VS"),
        ("    .hdmi_16_data_e (hdmi_16_data_e),", "    .hdmi_16_data_e (hdmi_16_data_e_s),", "16b DE"),
        ("    .hdmi_16_data (hdmi_16_data),", "    .hdmi_16_data (hdmi_16_data_s),", "16b data"),
        ("    .hdmi_16_es_data (hdmi_16_es_data),", "    .hdmi_16_es_data (hdmi_16_es_data_s),", "16b ES"),
        ("    .hdmi_24_hsync (hdmi_24_hsync),", "    .hdmi_24_hsync (hdmi_24_hsync_s),", "24b HS"),
        ("    .hdmi_24_vsync (hdmi_24_vsync),", "    .hdmi_24_vsync (hdmi_24_vsync_s),", "24b VS"),
        ("    .hdmi_24_data_e (hdmi_24_data_e),", "    .hdmi_24_data_e (hdmi_24_data_e_s),", "24b DE"),
        ("    .hdmi_24_data (hdmi_24_data),", "    .hdmi_24_data (hdmi_24_data_s),", "24b data"),
        ("    .vga_hsync(vga_hsync),", "    .vga_hsync(vga_hsync_s),", "VGA HS"),
        ("    .vga_vsync(vga_vsync),", "    .vga_vsync(vga_vsync_s),", "VGA VS"),
        ("    .hdmi_36_hsync (hdmi_36_hsync),", "    .hdmi_36_hsync (hdmi_36_hsync_s),", "36b HS"),
        ("    .hdmi_36_vsync (hdmi_36_vsync),", "    .hdmi_36_vsync (hdmi_36_vsync_s),", "36b VS"),
        ("    .hdmi_36_data_e (hdmi_36_data_e),", "    .hdmi_36_data_e (hdmi_36_data_e_s),", "36b DE"),
        ("    .hdmi_36_data (hdmi_36_data),", "    .hdmi_36_data (hdmi_36_data_s),", "36b data"),
        ("    .vga_red(vga_red),", "    .vga_red(vga_red_s),", "VGA red"),
        ("    .vga_green(vga_green),", "    .vga_green(vga_green_s),", "VGA green"),
        ("    .vga_blue(vga_blue),", "    .vga_blue(vga_blue_s),", "VGA blue"),
    ):
        data = replace_once(data, before, after, "axi_hdmi_tx.v " + label)
    data = replace_once(
        data,
        "  // hdmi output clock\n",
        "  // Final pixel stage: data, DE, and sync all take exactly one extra\n"
        "  // reference-clock cycle. reference_rst asynchronously blanks the IOB\n"
        "  // registers even when pixel generation is stopped.\n"
        "  always @(posedge reference_clk or posedge reference_rst) begin\n"
        "    if (reference_rst == 1'b1) begin\n"
        "      hdmi_16_hsync <= 1'b0; hdmi_16_vsync <= 1'b0; hdmi_16_data_e <= 1'b0;\n"
        "      hdmi_16_data <= 16'd0; hdmi_16_es_data <= 16'd0;\n"
        "      hdmi_24_hsync <= 1'b0; hdmi_24_vsync <= 1'b0; hdmi_24_data_e <= 1'b0; hdmi_24_data <= 24'd0;\n"
        "      vga_hsync <= 1'b0; vga_vsync <= 1'b0; vga_red <= 8'd0; vga_green <= 8'd0; vga_blue <= 8'd0;\n"
        "      hdmi_36_hsync <= 1'b0; hdmi_36_vsync <= 1'b0; hdmi_36_data_e <= 1'b0; hdmi_36_data <= 36'd0;\n"
        "    end else begin\n"
        "      hdmi_16_hsync <= hdmi_16_hsync_s; hdmi_16_vsync <= hdmi_16_vsync_s;\n"
        "      hdmi_16_data_e <= hdmi_display_valid_s ? hdmi_16_data_e_s : 1'b0;\n"
        "      hdmi_16_data <= hdmi_display_valid_s ? hdmi_16_data_s : 16'd0;\n"
        "      hdmi_16_es_data <= hdmi_display_valid_s ? hdmi_16_es_data_s : 16'd0;\n"
        "      hdmi_24_hsync <= hdmi_24_hsync_s; hdmi_24_vsync <= hdmi_24_vsync_s;\n"
        "      hdmi_24_data_e <= hdmi_display_valid_s ? hdmi_24_data_e_s : 1'b0;\n"
        "      hdmi_24_data <= hdmi_display_valid_s ? hdmi_24_data_s : 24'd0;\n"
        "      vga_hsync <= vga_hsync_s; vga_vsync <= vga_vsync_s;\n"
        "      vga_red <= hdmi_display_valid_s ? vga_red_s : 8'd0;\n"
        "      vga_green <= hdmi_display_valid_s ? vga_green_s : 8'd0;\n"
        "      vga_blue <= hdmi_display_valid_s ? vga_blue_s : 8'd0;\n"
        "      hdmi_36_hsync <= hdmi_36_hsync_s; hdmi_36_vsync <= hdmi_36_vsync_s;\n"
        "      hdmi_36_data_e <= hdmi_display_valid_s ? hdmi_36_data_e_s : 1'b0;\n"
        "      hdmi_36_data <= hdmi_display_valid_s ? hdmi_36_data_s : 36'd0;\n"
        "    end\n"
        "  end\n\n"
        "  // hdmi output clock\n",
        "axi_hdmi_tx.v display kill",
    )
    return data


def patch_up(data: str) -> str:
    data = replace_once(
        data,
        "  input                   vdma_clk,\n  output                  vdma_rst,\n",
        "  input                   vdma_clk,\n  output                  vdma_rst,\n"
        "  output                  core_rst_async,\n",
        "up_hdmi_tx.v interface",
    )
    old = """  ad_rst i_core_rst_reg (
    .rst_async(up_core_preset),
    .clk(hdmi_clk),
    .rstn(),
    .rst(hdmi_rst));

  ad_rst i_vdma_rst_reg (
    .rst_async(up_core_preset),
    .clk(vdma_clk),
    .rstn(),
    .rst(vdma_rst));
"""
    new = """  // Shared epoch fault: assertion is asynchronous even if either local
  // clock is stopped; each domain releases independently and synchronously.
  assign core_rst_async = up_core_preset;

  axi_hdmi_tx_reset_sync i_core_rst_reg (
    .rst_async(up_core_preset),
    .clk(hdmi_clk),
    .rst(hdmi_rst));

  axi_hdmi_tx_reset_sync i_vdma_rst_reg (
    .rst_async(up_core_preset),
    .clk(vdma_clk),
    .rst(vdma_rst));
"""
    return replace_once(data, old, new, "up_hdmi_tx.v reset instances")


def patch_core(data: str) -> str:
    data = replace_once(
        data,
        "  input                   reference_clk,\n  input                   reference_rst,\n",
        "  input                   reference_clk,\n  input                   reference_rst,\n"
        "  input                   vdma_epoch_ack,\n",
        "axi_hdmi_tx_core.v reference interface",
    )
    data = replace_once(
        data,
        "  output  reg             hdmi_tpm_oos,\n  output  reg             hdmi_status,\n",
        "  output  reg             hdmi_tpm_oos,\n  output  reg             hdmi_status,\n"
        "  output  reg             hdmi_epoch_ready,\n"
        "  output  reg             hdmi_display_valid,\n",
        "axi_hdmi_tx_core.v epoch output",
    )
    data = replace_once(
        data,
        "  reg             hdmi_enable = 'd0;\n",
        "  reg             hdmi_enable = 'd0;\n"
        "  reg     [ 1:0]  hdmi_epoch_state = 2'd0;\n"
        "  (* ASYNC_REG = \"TRUE\" *) reg vdma_epoch_ack_m1 = 1'd0;\n"
        "  (* ASYNC_REG = \"TRUE\" *) reg vdma_epoch_ack_m2 = 1'd0;\n",
        "axi_hdmi_tx_core.v epoch state",
    )
    data = replace_once(
        data,
        "  wire    [15:0]  hdmi_es_data_s;\n",
        "  wire    [15:0]  hdmi_es_data_s;\n"
        "  wire            hdmi_epoch_run;\n",
        "axi_hdmi_tx_core.v epoch run wire",
    )
    marker = "  // status and enable\n"
    epoch = """  // A complete reset epoch is an acknowledgement protocol, not a delay.
  // The source keeps toggle and Gray at a known baseline until VDMA has observed
  // this specific epoch and returned its acknowledgement.
  localparam SRC_BASELINE = 2'd0;
  localparam SRC_WAIT_ACK = 2'd1;
  localparam SRC_RUN      = 2'd2;

  always @(posedge reference_clk or posedge reference_rst) begin
    if (reference_rst == 1'b1) begin
      hdmi_epoch_state <= SRC_BASELINE;
      hdmi_epoch_ready <= 1'b0;
      vdma_epoch_ack_m1 <= 1'b0;
      vdma_epoch_ack_m2 <= 1'b0;
    end else begin
      vdma_epoch_ack_m1 <= vdma_epoch_ack;
      vdma_epoch_ack_m2 <= vdma_epoch_ack_m1;
      case (hdmi_epoch_state)
        SRC_BASELINE: begin
          hdmi_epoch_ready <= 1'b0;
          hdmi_epoch_state <= SRC_WAIT_ACK;
        end
        SRC_WAIT_ACK: begin
          hdmi_epoch_ready <= 1'b1;
          if (vdma_epoch_ack_m2 == 1'b1)
            hdmi_epoch_state <= SRC_RUN;
        end
        default: begin // SRC_RUN
          if (vdma_epoch_ack_m2 == 1'b0) begin
            hdmi_epoch_ready <= 1'b0;
            hdmi_epoch_state <= SRC_BASELINE;
          end else begin
            hdmi_epoch_ready <= 1'b1;
          end
        end
      endcase
    end
  end

  assign hdmi_epoch_run = (hdmi_epoch_state == SRC_RUN) && vdma_epoch_ack_m2;

"""
    display = """  // Presentation may blank immediately on a stopped-clock reset, but
  // it cannot unblank until a new post-reset VDMA EOF returns through mailbox.
  always @(posedge reference_clk or posedge reference_rst) begin
    if (reference_rst == 1'b1) begin
      hdmi_display_valid <= 1'b0;
    end else if (hdmi_epoch_run == 1'b0) begin
      hdmi_display_valid <= 1'b0;
    end else if (hdmi_fs_ret_s == 1'b1) begin
      hdmi_display_valid <= 1'b1;
    end
  end

"""
    data = replace_once(data, marker, epoch + display + marker, "axi_hdmi_tx_core.v epoch/display FSM")
    old_counters = """  always @(posedge reference_clk) begin
    if (hdmi_hs_count >= hdmi_hl_width_s) begin
      hdmi_hs_count <= 0;
    end else begin
      hdmi_hs_count <= hdmi_hs_count + 1'b1;
    end
    if (hdmi_hs_count >= hdmi_hl_width_s) begin
      if (hdmi_vs_count >= hdmi_vf_width_s) begin
        hdmi_vs_count <= 0;
      end else begin
        hdmi_vs_count <= hdmi_vs_count + 1'b1;
      end
    end
  end
"""
    new_counters = """  always @(posedge reference_clk or posedge reference_rst) begin
    if ((reference_rst == 1'b1) || (hdmi_epoch_run == 1'b0)) begin
      hdmi_hs_count <= 16'd0;
      hdmi_vs_count <= 16'd0;
    end else begin
      if (hdmi_hs_count >= hdmi_hl_width_s) begin
        hdmi_hs_count <= 0;
      end else begin
        hdmi_hs_count <= hdmi_hs_count + 1'b1;
      end
      if (hdmi_hs_count >= hdmi_hl_width_s) begin
        if (hdmi_vs_count >= hdmi_vf_width_s) begin
          hdmi_vs_count <= 0;
        end else begin
          hdmi_vs_count <= hdmi_vs_count + 1'b1;
        end
      end
    end
  end
"""
    data = replace_once(data, old_counters, new_counters, "axi_hdmi_tx_core.v counters")
    data = replace_once(
        data,
        "  always @(posedge reference_clk) begin\n    if (reference_rst == 1'b1) begin\n      hdmi_fs_toggle <= 1'b0;\n      hdmi_fs <= 1'b0;\n",
        "  always @(posedge reference_clk or posedge reference_rst) begin\n    if ((reference_rst == 1'b1) || (hdmi_epoch_run == 1'b0)) begin\n      hdmi_fs_toggle <= 1'b0;\n      hdmi_fs <= 1'b0;\n",
        "axi_hdmi_tx_core.v frame toggle reset",
    )
    old_return = """  always @(posedge reference_clk) begin
      hdmi_fs_ret <= hdmi_fs_ret_s;
      hdmi_fs_waddr <= vdma_fs_waddr;
  end
"""
    new_return = """  always @(posedge reference_clk or posedge reference_rst) begin
    if ((reference_rst == 1'b1) || (hdmi_epoch_run == 1'b0)) begin
      hdmi_fs_ret <= 1'b0;
      hdmi_fs_waddr <= 9'd0;
    end else begin
      hdmi_fs_ret <= hdmi_fs_ret_s;
      hdmi_fs_waddr <= vdma_fs_waddr;
    end
  end
"""
    data = replace_once(data, old_return, new_return, "axi_hdmi_tx_core.v frame return")
    old_read = """  always @(posedge reference_clk) begin
    if (reference_rst == 1'b1) begin
      hdmi_raddr <= 10'd0;
    end else if (hdmi_fs == 1'b1) begin
      hdmi_raddr <= {hdmi_fs_waddr, 1'b0};
    end else if (hdmi_de_s == 1'b1) begin
      hdmi_raddr <= hdmi_raddr + 1'b1;
    end
    hdmi_raddr_g <= b2g(hdmi_raddr[9:1]);
  end
"""
    new_read = """  // BRAM read-address state has synchronous reset only.
  always @(posedge reference_clk) begin
    if ((reference_rst == 1'b1) || (hdmi_epoch_run == 1'b0)) begin
      hdmi_raddr <= 10'd0;
      hdmi_raddr_g <= 9'd0;
    end else begin
      if (hdmi_fs == 1'b1) begin
        hdmi_raddr <= {hdmi_fs_waddr, 1'b0};
      end else if (hdmi_de_s == 1'b1) begin
        hdmi_raddr <= hdmi_raddr + 1'b1;
      end
      hdmi_raddr_g <= b2g(hdmi_raddr[9:1]);
    end
  end
"""
    return replace_once(data, old_read, new_read, "axi_hdmi_tx_core.v Gray pointer")


def patch_vdma(data: str) -> str:
    data = replace_once(
        data,
        "  input                   vdma_rst,\n  input                   vdma_end_of_frame,\n",
        "  input                   vdma_rst,\n  input                   hdmi_epoch_ready,\n"
        "  output  reg             vdma_epoch_ack,\n"
        "  input                   vdma_end_of_frame,\n",
        "axi_hdmi_tx_vdma.v epoch interface",
    )
    data = replace_once(data, "  output  reg             vdma_ready,\n  output  reg             vdma_wr,\n",
                        "  output  wire            vdma_ready,\n  output  wire            vdma_wr,\n",
                        "axi_hdmi_tx_vdma.v effective outputs")
    data = replace_once(
        data,
        "  reg             vdma_fs_toggle_m1 = 1'd0;\n",
        "  (* ASYNC_REG = \"TRUE\" *) reg hdmi_epoch_ready_m1 = 1'd0;\n"
        "  (* ASYNC_REG = \"TRUE\" *) reg hdmi_epoch_ready_m2 = 1'd0;\n"
        "  reg             vdma_epoch_armed = 1'd0;\n"
        "  reg             vdma_ready_i = 1'd0;\n"
        "  reg             vdma_wr_i = 1'd0;\n"
        "  (* ASYNC_REG = \"TRUE\" *) reg vdma_fs_toggle_m1 = 1'd0;\n",
        "axi_hdmi_tx_vdma.v epoch registers",
    )
    data = replace_once(data, "  reg     [ 8:0]  vdma_raddr_g_m1 = 9'd0;\n",
                        "  (* ASYNC_REG = \"TRUE\" *) reg [ 8:0]  vdma_raddr_g_m1 = 9'd0;\n",
                        "axi_hdmi_tx_vdma.v Gray first stage")
    data = replace_once(
        data,
        "  wire            vdma_eof_fire;\n\n  // TLAST",
        "  wire            vdma_eof_fire;\n"
        "  wire            vdma_accept;\n\n"
        "  assign vdma_ready = ((vdma_rst == 1'b0) && (vdma_epoch_armed == 1'b1)) ? vdma_ready_i : 1'b0;\n"
        "  // The BRAM write-enable is driven only by this synchronous register.\n"
        "  // Reset may leave one old cycle resident, but presentation remains\n"
        "  // masked until a fresh post-reset EOF/mailbox epoch has completed.\n"
        "  assign vdma_wr = vdma_wr_i;\n"
        "  assign vdma_accept = vdma_valid & vdma_ready;\n\n"
        "  // TLAST",
        "axi_hdmi_tx_vdma.v acceptance gate",
    )
    data = replace_once(data, "assign vdma_eof_fire = vdma_valid & vdma_ready & vdma_end_of_frame;",
                        "assign vdma_eof_fire = vdma_accept & vdma_end_of_frame;",
                        "axi_hdmi_tx_vdma.v eof gate")
    marker = "  // hdmi frame sync\n"
    epoch = """  // Destination half of the reset epoch.  `hdmi_epoch_ready` can only
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

"""
    data = replace_once(data, marker, epoch + marker, "axi_hdmi_tx_vdma.v epoch FSM")
    old_fs = """  always @(posedge vdma_clk) begin
    if (vdma_rst == 1'b1) begin
      vdma_fs_toggle_m1 <= 1'd0;
      vdma_fs_toggle_m2 <= 1'd0;
      vdma_fs_toggle_m3 <= 1'd0;
    end else begin
      vdma_fs_toggle_m1 <= hdmi_fs_toggle;
      vdma_fs_toggle_m2 <= vdma_fs_toggle_m1;
      vdma_fs_toggle_m3 <= vdma_fs_toggle_m2;
    end
    hdmi_fs <= vdma_fs_toggle_m2 ^ vdma_fs_toggle_m3;
  end
"""
    new_fs = """  always @(posedge vdma_clk or posedge vdma_rst) begin
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
"""
    data = replace_once(data, old_fs, new_fs, "axi_hdmi_tx_vdma.v frame synchronizer")
    old_eof = """  // dma frame sync

  always @(posedge vdma_clk) begin
    if (vdma_rst == 1'b1) begin
      vdma_end_of_frame_d <= 1'b0;
      vdma_fs <=  1'b0;
    end else begin
      vdma_end_of_frame_d <= vdma_eof_fire;
      vdma_fs <= vdma_end_of_frame_d;
    end
  end
"""
    new_eof = """  // dma frame sync

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
"""
    data = replace_once(data, old_eof, new_eof, "axi_hdmi_tx_vdma.v EOF reset")
    old_commit = """  // sync dma and hdmi frames

  always @(posedge vdma_clk) begin
    if (vdma_rst == 1'b1) begin
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
"""
    new_commit = """  // sync dma and hdmi frames

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
"""
    data = replace_once(data, old_commit, new_commit, "axi_hdmi_tx_vdma.v commit reset")
    old_active = """  // accept new frame from dma

  always @(posedge vdma_clk) begin
    if (vdma_rst == 1'b1) begin
      vdma_active_frame <= 1'b0;
    end else begin
      if ((vdma_active_frame == 1'b1) && (vdma_eof_fire == 1'b1)) begin
        vdma_active_frame <= 1'b0;
      end else if ((vdma_active_frame == 1'b0) && (hdmi_fs == 1'b1)) begin
        vdma_active_frame <= 1'b1;
      end
    end
  end
"""
    new_active = """  // accept new frame from dma

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
"""
    data = replace_once(data, old_active, new_active, "axi_hdmi_tx_vdma.v active reset")
    old_write = """  always @(posedge vdma_clk) begin
    vdma_wr <= vdma_valid & vdma_ready;
    if (vdma_rst == 1'b1) begin
      vdma_waddr <= 9'd0;
    end else if (vdma_wr == 1'b1) begin
      vdma_waddr <= vdma_waddr + 1'b1;
    end
    vdma_wdata <= {vdma_data[55:32], vdma_data[23:0]};
  end
"""
    new_write = """  // BRAM write-side address/data/enable state has synchronous reset only.
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
"""
    data = replace_once(data, old_write, new_write, "axi_hdmi_tx_vdma.v write reset")
    old_tpm = """  always @(posedge vdma_clk) begin
    if ((vdma_rst == 1'b1) || (vdma_fs == 1'b1)) begin
      vdma_tpm_data <= 23'd0;
      vdma_tpm_oos <= 1'd0;
    end else if (vdma_wr == 1'b1) begin
      vdma_tpm_data <= vdma_tpm_data + 1'b1;
      vdma_tpm_oos <= vdma_tpm_oos_s;
    end
  end
"""
    new_tpm = """  always @(posedge vdma_clk or posedge vdma_rst) begin
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
"""
    data = replace_once(data, old_tpm, new_tpm, "axi_hdmi_tx_vdma.v TPM reset")
    old_gray = """  always @(posedge vdma_clk) begin
    if (vdma_rst == 1'b1) begin
      vdma_raddr_g_m1 <= 9'd0;
      vdma_raddr_g_m2 <= 9'd0;
    end else begin
      vdma_raddr_g_m1 <= hdmi_raddr_g;
      vdma_raddr_g_m2 <= vdma_raddr_g_m1;
    end
  end

  always @(posedge vdma_clk) begin
    vdma_raddr <= g2b(vdma_raddr_g_m2);
    vdma_addr_diff <= vdma_addr_diff_s[8:0];
    if (vdma_addr_diff >= RDY_THRESHOLD_HI) begin
      vdma_ready <= 1'b0;
    end else if (vdma_addr_diff <= RDY_THRESHOLD_LO) begin
      vdma_ready <= vdma_active_frame;
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
"""
    new_gray = """  always @(posedge vdma_clk or posedge vdma_rst) begin
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
"""
    return replace_once(data, old_gray, new_gray, "axi_hdmi_tx_vdma.v occupancy reset")


PATCHERS = {
    "library/axi_hdmi_tx/axi_hdmi_tx.v": patch_top,
    "library/axi_hdmi_tx/axi_hdmi_tx_core.v": patch_core,
    "library/axi_hdmi_tx/axi_hdmi_tx_vdma.v": patch_vdma,
    "library/common/up_hdmi_tx.v": patch_up,
}


def build_overlay(root: Path, out: Path) -> dict:
    root = root.resolve(strict=True)
    if out.exists():
        raise RuntimeError(f"refusing to overwrite output: {out}")
    contents = {relative: read_verified(root, relative) for relative in EXPECTED_SHA256}
    out.mkdir(parents=True)
    overlay = out / "overlay"
    output_hashes: dict[str, str] = {}
    for relative, patcher in PATCHERS.items():
        target = overlay / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(patcher(contents[relative]), encoding="utf-8", newline="\n")
        output_hashes[str(target.relative_to(out)).replace("\\", "/")] = sha256(target)
    for helper_name in ("axi_hdmi_tx_reset_sync.v", "axi_hdmi_tx_axis_epoch_bridge.v"):
        helper = SOURCE_DIR / helper_name
        if not helper.is_file():
            raise RuntimeError(f"missing tracked helper: {helper}")
        helper_target = overlay / "library/axi_hdmi_tx" / helper_name
        helper_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(helper, helper_target)
        output_hashes[str(helper_target.relative_to(out)).replace("\\", "/")] = sha256(helper_target)
    receipt = {
        "schema": "receiver-v2-hdmi-active-reset-overlay/v1",
        "status": "PATCHED_SOURCE_ONLY",
        "input_root": str(root),
        "input_hashes": {relative: sha256(root / relative) for relative in EXPECTED_SHA256},
        "output_hashes": output_hashes,
        "input_common_ad_rst_preserved": True,
        "requires_explicit_ip_repackage": True,
        "hardware_verified": False,
    }
    (out / "receipt.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adi-root", type=Path, required=True, help="read-only r5 tree containing library/")
    parser.add_argument("--out", type=Path, required=True, help="new, empty overlay output directory")
    args = parser.parse_args()
    receipt = build_overlay(args.adi_root, args.out.resolve())
    print(f"HDMI_ACTIVE_RESET_OVERLAY_READY {args.out.resolve()}")
    print(f"HDMI_ACTIVE_RESET_STATUS {receipt['status']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
