`timescale 1ns/1ps
// SPDX-License-Identifier: Apache-2.0
//
// Passive RX-DMAC CTRL write monitor.
//
// This block is an observation-only tap on the ADI AXI-Lite write address and
// data channels.  It never drives READY, VALID, address, or data; consequently
// it cannot backpressure or modify the DMAC control path.  The target DMAC
// CTRL register is word alias AWADDR[10:2] == 9'h100 (byte offset 0x0400;
// AWADDR[1:0] and upper bits are ignored by the ADI up_axi/register path).
// The ADI up_axi path also ignores WSTRB for this register, so every completed
// CTRL write changes dma_enabled to WDATA[0], including partial-byte writes.
//
// The ADI AXI-Lite master/controller contract used by this integration permits
// at most one outstanding write.  Address and write-data may nevertheless
// handshake in either order or together.  One pending address and one pending
// write-data slot pair the transaction.  B responses are not observed because
// a legal CTRL write to the local DMAC register is assumed to complete
// successfully.  dma_enabled is therefore only the observed CTRL bit, never
// a DMA-idle, transfer-completion, or B-response acknowledgement.
module rf_rx_dma_enable_monitor (
  (* X_INTERFACE_INFO = "xilinx.com:signal:clock:1.0 clk CLK" *)
  (* X_INTERFACE_PARAMETER = "ASSOCIATED_BUSIF AXI_MON, ASSOCIATED_RESET resetn, FREQ_HZ 50000000" *)
  input  wire        clk,
  (* X_INTERFACE_INFO = "xilinx.com:signal:reset:1.0 resetn RST" *)
  (* X_INTERFACE_PARAMETER = "POLARITY ACTIVE_LOW" *)
  input  wire        resetn,

  // Read-only observation of every AXI4-Lite channel.  AXI_MON is declared a
  // monitor interface so Vivado can attach it as a third endpoint without
  // making this block an AXI master or slave.
  (* X_INTERFACE_INFO = "xilinx.com:interface:aximm_rtl:1.0 AXI_MON AWADDR" *)
  (* X_INTERFACE_PARAMETER = "XIL_INTERFACENAME AXI_MON, PROTOCOL AXI4LITE, ADDR_WIDTH 32, DATA_WIDTH 32" *)
  (* X_INTERFACE_MODE = "monitor" *)
  input  wire [31:0] awaddr,
  (* X_INTERFACE_INFO = "xilinx.com:interface:aximm_rtl:1.0 AXI_MON AWPROT" *)
  input  wire [2:0]  awprot,
  (* X_INTERFACE_INFO = "xilinx.com:interface:aximm_rtl:1.0 AXI_MON AWVALID" *)
  input  wire        awvalid,
  (* X_INTERFACE_INFO = "xilinx.com:interface:aximm_rtl:1.0 AXI_MON AWREADY" *)
  input  wire        awready,
  (* X_INTERFACE_INFO = "xilinx.com:interface:aximm_rtl:1.0 AXI_MON WDATA" *)
  input  wire [31:0] wdata,
  (* X_INTERFACE_INFO = "xilinx.com:interface:aximm_rtl:1.0 AXI_MON WSTRB" *)
  input  wire [3:0]  wstrb,
  (* X_INTERFACE_INFO = "xilinx.com:interface:aximm_rtl:1.0 AXI_MON WVALID" *)
  input  wire        wvalid,
  (* X_INTERFACE_INFO = "xilinx.com:interface:aximm_rtl:1.0 AXI_MON WREADY" *)
  input  wire        wready,
  (* X_INTERFACE_INFO = "xilinx.com:interface:aximm_rtl:1.0 AXI_MON BRESP" *)
  input  wire [1:0]  bresp,
  (* X_INTERFACE_INFO = "xilinx.com:interface:aximm_rtl:1.0 AXI_MON BVALID" *)
  input  wire        bvalid,
  (* X_INTERFACE_INFO = "xilinx.com:interface:aximm_rtl:1.0 AXI_MON BREADY" *)
  input  wire        bready,
  (* X_INTERFACE_INFO = "xilinx.com:interface:aximm_rtl:1.0 AXI_MON ARADDR" *)
  input  wire [31:0] araddr,
  (* X_INTERFACE_INFO = "xilinx.com:interface:aximm_rtl:1.0 AXI_MON ARPROT" *)
  input  wire [2:0]  arprot,
  (* X_INTERFACE_INFO = "xilinx.com:interface:aximm_rtl:1.0 AXI_MON ARVALID" *)
  input  wire        arvalid,
  (* X_INTERFACE_INFO = "xilinx.com:interface:aximm_rtl:1.0 AXI_MON ARREADY" *)
  input  wire        arready,
  (* X_INTERFACE_INFO = "xilinx.com:interface:aximm_rtl:1.0 AXI_MON RDATA" *)
  input  wire [31:0] rdata,
  (* X_INTERFACE_INFO = "xilinx.com:interface:aximm_rtl:1.0 AXI_MON RRESP" *)
  input  wire [1:0]  rresp,
  (* X_INTERFACE_INFO = "xilinx.com:interface:aximm_rtl:1.0 AXI_MON RVALID" *)
  input  wire        rvalid,
  (* X_INTERFACE_INFO = "xilinx.com:interface:aximm_rtl:1.0 AXI_MON RREADY" *)
  input  wire        rready,

  output reg         dma_enabled,
  output wire        flush_sync
);

  reg        aw_pending = 1'b0;
  reg [31:0] awaddr_pending = 32'd0;
  reg        w_pending = 1'b0;
  reg [31:0] wdata_pending = 32'd0;

  wire aw_handshake = awvalid && awready;
  wire w_handshake = wvalid && wready;
  wire have_address = aw_pending || aw_handshake;
  wire have_wdata = w_pending || w_handshake;
  wire [31:0] paired_awaddr = aw_pending ? awaddr_pending : awaddr;
  wire [31:0] paired_wdata = w_pending ? wdata_pending : wdata;
  wire complete_write = have_address && have_wdata;

  // Disabled is deliberately level-sensitive: a FIFO connected downstream
  // remains flushing until an observed CTRL enable write completes.
  assign flush_sync = ~dma_enabled;

  always @(posedge clk) begin
    if (!resetn) begin
      aw_pending <= 1'b0;
      awaddr_pending <= 32'd0;
      w_pending <= 1'b0;
      wdata_pending <= 32'd0;
      dma_enabled <= 1'b0;
    end else if (complete_write) begin
      // One-outstanding-write AXI-Lite assumption: no second new transaction
      // may be accepted while a stored half is being paired in this cycle.
      aw_pending <= 1'b0;
      w_pending <= 1'b0;
      if (paired_awaddr[10:2] == 9'h100)
        dma_enabled <= paired_wdata[0];
    end else begin
      if (aw_handshake) begin
        aw_pending <= 1'b1;
        awaddr_pending <= awaddr;
      end
      if (w_handshake) begin
        w_pending <= 1'b1;
        wdata_pending <= wdata;
      end
    end
  end

endmodule
