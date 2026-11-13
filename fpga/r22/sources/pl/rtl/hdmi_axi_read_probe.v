// SPDX-License-Identifier: MIT
//
// Passive AXI read / AXIS health probe for the HDMI source DMA.
//
// This module observes handshake signals only.  It has no ready/valid or
// reset output and therefore cannot throttle, acknowledge, or otherwise
// change the source DMA, HP port, or HDMI AXIS stream.  All signals belong to
// the same FCLK0 50 MHz domain in the ZedBoard HDMI integration.
`timescale 1ns/1ps
module hdmi_axi_read_probe #(
  parameter integer C_AXI_ADDR_WIDTH = 29,
  parameter integer C_COUNTER_WIDTH = 32
) (
  input  wire                         clk,
  input  wire                         rstn,
  // Synchronous one-cycle (or longer) session clear.  This has no effect on
  // the observed interfaces; it clears only this probe's accounting state.
  input  wire                         clear,
  // Enables event accounting after a clear.  Deasserting this input freezes
  // the saved counters and address; it never gates or modifies the observed
  // AXI/AXIS interfaces.
  input  wire                         sample_valid,

  input  wire                         arvalid,
  input  wire                         arready,
  input  wire [C_AXI_ADDR_WIDTH-1:0]  araddr,
  input  wire                         rvalid,
  input  wire                         rready,
  input  wire                         rlast,
  input  wire [1:0]                   rresp,

  input  wire                         axis_tvalid,
  input  wire                         axis_tready,
  input  wire                         axis_tlast,

  output reg  [C_COUNTER_WIDTH-1:0]   ar_fire_count,
  output reg  [C_COUNTER_WIDTH-1:0]   ar_stall_cycle_count,
  output reg  [C_COUNTER_WIDTH-1:0]   r_fire_count,
  output reg  [C_COUNTER_WIDTH-1:0]   r_stall_cycle_count,
  output reg  [C_COUNTER_WIDTH-1:0]   r_last_count,
  output reg  [C_COUNTER_WIDTH-1:0]   axis_fire_count,
  output reg  [C_COUNTER_WIDTH-1:0]   axis_stall_cycle_count,
  output reg  [C_COUNTER_WIDTH-1:0]   axis_last_count,
  output reg  [C_AXI_ADDR_WIDTH-1:0]  last_araddr,
  output reg  [3:0]                   rresp_seen,
  // Bit 0 ARVALID, 1 ARREADY, 2 RVALID, 3 RREADY, 4 RLAST,
  // 5 AXIS TVALID, 6 AXIS TREADY, 7 AXIS TLAST, sampled live.
  output wire [7:0]                   live_status,
  // Sticky observation of the same bit positions.  In particular, bit 0
  // separates an unissued AR from a held AR, bit 2 proves a response reached
  // the source interface, and bit 5 proves that read data reached AXIS.
  output reg  [7:0]                   seen_status
);
  localparam [C_COUNTER_WIDTH-1:0] COUNT_MAX = {C_COUNTER_WIDTH{1'b1}};

  wire ar_fire = arvalid && arready;
  wire r_fire = rvalid && rready;
  wire axis_fire = axis_tvalid && axis_tready;

  assign live_status = {axis_tlast, axis_tready, axis_tvalid, rlast,
                        rready, rvalid, arready, arvalid};

  // Saturating counters make a long capture unambiguous: all ones denotes
  // "at least this many", never a silent wrap.  AXI allows multiple reads in
  // flight, so there intentionally is no inferred outstanding-transaction
  // count here; actual AR/R handshake counts remain authoritative.
  always @(posedge clk) begin
    if (!rstn || clear) begin
      ar_fire_count          <= {C_COUNTER_WIDTH{1'b0}};
      ar_stall_cycle_count   <= {C_COUNTER_WIDTH{1'b0}};
      r_fire_count           <= {C_COUNTER_WIDTH{1'b0}};
      r_stall_cycle_count    <= {C_COUNTER_WIDTH{1'b0}};
      r_last_count           <= {C_COUNTER_WIDTH{1'b0}};
      axis_fire_count        <= {C_COUNTER_WIDTH{1'b0}};
      axis_stall_cycle_count <= {C_COUNTER_WIDTH{1'b0}};
      axis_last_count        <= {C_COUNTER_WIDTH{1'b0}};
      last_araddr            <= {C_AXI_ADDR_WIDTH{1'b0}};
      rresp_seen             <= 4'b0000;
      seen_status            <= 8'b0000_0000;
    end else if (sample_valid) begin
      seen_status <= seen_status | live_status;
      if (ar_fire) begin
        if (ar_fire_count != COUNT_MAX)
          ar_fire_count <= ar_fire_count + {{(C_COUNTER_WIDTH-1){1'b0}},1'b1};
        last_araddr <= araddr;
      end
      if (arvalid && !arready && ar_stall_cycle_count != COUNT_MAX)
        ar_stall_cycle_count <= ar_stall_cycle_count + {{(C_COUNTER_WIDTH-1){1'b0}},1'b1};

      if (r_fire) begin
        if (r_fire_count != COUNT_MAX)
          r_fire_count <= r_fire_count + {{(C_COUNTER_WIDTH-1){1'b0}},1'b1};
        if (rlast && r_last_count != COUNT_MAX)
          r_last_count <= r_last_count + {{(C_COUNTER_WIDTH-1){1'b0}},1'b1};
      end
      if (rvalid && !rready && r_stall_cycle_count != COUNT_MAX)
        r_stall_cycle_count <= r_stall_cycle_count + {{(C_COUNTER_WIDTH-1){1'b0}},1'b1};
      // Capture a response as soon as it is offered, including when RREADY is
      // low.  The same response can persist for multiple cycles harmlessly.
      if (rvalid)
        rresp_seen[rresp] <= 1'b1;

      if (axis_fire) begin
        if (axis_fire_count != COUNT_MAX)
          axis_fire_count <= axis_fire_count + {{(C_COUNTER_WIDTH-1){1'b0}},1'b1};
        if (axis_tlast && axis_last_count != COUNT_MAX)
          axis_last_count <= axis_last_count + {{(C_COUNTER_WIDTH-1){1'b0}},1'b1};
      end
      if (axis_tvalid && !axis_tready && axis_stall_cycle_count != COUNT_MAX)
        axis_stall_cycle_count <= axis_stall_cycle_count + {{(C_COUNTER_WIDTH-1){1'b0}},1'b1};
    end
  end
endmodule
