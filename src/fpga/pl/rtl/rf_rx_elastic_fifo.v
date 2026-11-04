`timescale 1ns/1ps
// SPDX-License-Identifier: Apache-2.0
//
// Synchronous elastic boundary for the FMCOMMS3 RX packed-data stream.
//
// The ADI CPack FIFO write interface has no ready signal: a word is either
// retained or the producer must be told about loss.  This module therefore
// presents a conventional 64-bit AXI-stream output while accepting one packed
// word per 50-MHz cycle.  DEPTH is the physical RAM depth: one location is
// deliberately left unused, so the default usable capacity is 1023 RAM words
// plus one registered output skid word = 1024 * 64 bit = 8 KiB (not 64 KiB).
// The empty RAM slot guarantees read_ptr != write_ptr whenever the RAM portion
// is full, avoiding a cross-port same-address BRAM collision on pop+push.
//
// flush_sync is synchronous to clk.  It atomically discards previously queued
// data, suppresses M_AXIS TVALID during the flush cycle, and still retains an
// input word asserted in that same cycle.  It is used at an RX stream boundary;
// it is not a substitute for handling packed_fifo_wr_overflow in software.
module rf_rx_elastic_fifo #(
  parameter integer DATA_WIDTH = 64,
  parameter integer DEPTH = 1024,
  parameter integer ADDR_WIDTH = 10
) (
  (* X_INTERFACE_INFO = "xilinx.com:signal:clock:1.0 clk CLK" *)
  (* X_INTERFACE_PARAMETER = "ASSOCIATED_BUSIF M_AXIS, ASSOCIATED_RESET resetn, FREQ_HZ 50000000" *)
  input  wire                  clk,
  (* X_INTERFACE_INFO = "xilinx.com:signal:reset:1.0 resetn RST" *)
  (* X_INTERFACE_PARAMETER = "POLARITY ACTIVE_LOW" *)
  input  wire                  resetn,

  // Asserted by the CPack domain, which is the same fixed 50-MHz domain here.
  input  wire                  flush_sync,
  input  wire                  packed_fifo_wr_en,
  input  wire [DATA_WIDTH-1:0] packed_fifo_wr_data,
  output reg                   packed_fifo_wr_overflow,
  // These two outputs are observability only.  They expose the exact
  // synchronous admission decision already made by this FIFO; they do not
  // add a READY path back to CPack.  The independent RXTH capture path uses
  // them to bind metadata to retained words rather than to raw TVALID.
  output wire                  accepted_write,
  output wire                  ingress_drop,

  (* X_INTERFACE_INFO = "xilinx.com:interface:axis:1.0 M_AXIS TDATA" *)
  (* X_INTERFACE_PARAMETER = "XIL_INTERFACENAME M_AXIS, TDATA_NUM_BYTES 8, HAS_TKEEP 1, HAS_TLAST 1" *)
  output wire [DATA_WIDTH-1:0] m_axis_tdata,
  (* X_INTERFACE_INFO = "xilinx.com:interface:axis:1.0 M_AXIS TKEEP" *)
  output wire [DATA_WIDTH/8-1:0] m_axis_tkeep,
  (* X_INTERFACE_INFO = "xilinx.com:interface:axis:1.0 M_AXIS TSTRB" *)
  output wire [DATA_WIDTH/8-1:0] m_axis_tstrb,
  (* X_INTERFACE_INFO = "xilinx.com:interface:axis:1.0 M_AXIS TVALID" *)
  output wire                  m_axis_tvalid,
  (* X_INTERFACE_INFO = "xilinx.com:interface:axis:1.0 M_AXIS TREADY" *)
  input  wire                  m_axis_tready,
  (* X_INTERFACE_INFO = "xilinx.com:interface:axis:1.0 M_AXIS TLAST" *)
  output wire                  m_axis_tlast
);

  // DEPTH is intentionally a power of two in the shipped 1024-word profile.
  // ram_count excludes out_valid/out_data, which form a one-word skid stage.
  // Keep one physical RAM location empty so a full pop+push never reads and
  // writes the same dual-port BRAM address in one clock cycle.
  localparam integer RAM_USABLE_WORDS = DEPTH - 1;
  // Use the Vivado XPM simple-dual-port primitive explicitly.  This prevents
  // synthesis from silently changing the source-array read latency or falling
  // back to LUTRAM.  Port A is the single write port and port B is the
  // synchronous one-cycle read port.
  reg [ADDR_WIDTH-1:0] write_ptr = {ADDR_WIDTH{1'b0}};
  reg [ADDR_WIDTH-1:0] read_ptr = {ADDR_WIDTH{1'b0}};
  reg [ADDR_WIDTH:0]   ram_count = {(ADDR_WIDTH+1){1'b0}};
  wire [DATA_WIDTH-1:0] ram_q;
  reg                  out_valid = 1'b0;

  wire pop_output;
  wire move_ram_to_output;
  wire can_accept_write;
  wire accept_write;
  wire [ADDR_WIDTH-1:0] mem_waddr;
  wire mem_wren;

  // flush_sync gates TVALID combinatorially: an old word cannot be transferred
  // during the cycle in which the queue is being discarded.
  assign m_axis_tdata  = ram_q;
  assign m_axis_tkeep  = {DATA_WIDTH/8{1'b1}};
  assign m_axis_tstrb  = {DATA_WIDTH/8{1'b1}};
  assign m_axis_tlast  = 1'b0;
  // resetn is intentionally a synchronous reset: out_valid is cleared on its
  // next rising edge.  Only flush_sync needs combinational masking to make
  // the selected flush cycle incapable of transferring an old word.
  assign m_axis_tvalid = out_valid & ~flush_sync;
  assign pop_output = m_axis_tvalid & m_axis_tready;

  // A B-port read is issued when the skid is empty, or its current word is
  // being consumed.  XPM READ_LATENCY_B=1 makes ram_q valid after this clock
  // edge, matching the out_valid state registered at the same edge.
  assign move_ram_to_output = (ram_count != 0) && (!out_valid || pop_output);
  assign can_accept_write = (ram_count < RAM_USABLE_WORDS) || move_ram_to_output;
  assign accept_write = packed_fifo_wr_en && can_accept_write;
  assign accepted_write = accept_write && !flush_sync && resetn;
  assign ingress_drop = packed_fifo_wr_en && !can_accept_write && !flush_sync && resetn;
  // A flush starts a fresh epoch, so its simultaneous input is always written
  // at slot zero regardless of the old queue occupancy.
  assign mem_waddr = flush_sync ? {ADDR_WIDTH{1'b0}} : write_ptr;
  assign mem_wren = resetn && packed_fifo_wr_en && (flush_sync || can_accept_write);

  xpm_memory_sdpram #(
    .ADDR_WIDTH_A(ADDR_WIDTH),
    .ADDR_WIDTH_B(ADDR_WIDTH),
    .AUTO_SLEEP_TIME(0),
    .BYTE_WRITE_WIDTH_A(DATA_WIDTH),
    .CLOCKING_MODE("common_clock"),
    .ECC_MODE("no_ecc"),
    .MEMORY_INIT_FILE("none"),
    .MEMORY_INIT_PARAM("0"),
    .MEMORY_OPTIMIZATION("true"),
    .MEMORY_PRIMITIVE("block"),
    .MEMORY_SIZE(DATA_WIDTH * DEPTH),
    .MESSAGE_CONTROL(0),
    .READ_DATA_WIDTH_B(DATA_WIDTH),
    .READ_LATENCY_B(1),
    .READ_RESET_VALUE_B("0"),
    .RST_MODE_A("SYNC"),
    .RST_MODE_B("SYNC"),
    // Keep the vendor model's parameter/protocol checks enabled in simulation.
    .SIM_ASSERT_CHK(1),
    .USE_EMBEDDED_CONSTRAINT(0),
    .USE_MEM_INIT(1),
    .WAKEUP_TIME("disable_sleep"),
    .WRITE_DATA_WIDTH_A(DATA_WIDTH),
    .WRITE_MODE_B("read_first")
  ) ram_i (
    .clka(clk),
    .ena(mem_wren),
    .wea(mem_wren),
    .addra(mem_waddr),
    .dina(packed_fifo_wr_data),
    .injectsbiterra(1'b0),
    .injectdbiterra(1'b0),
    .clkb(clk),
    .rstb(1'b0),
    .enb(move_ram_to_output && !flush_sync),
    .regceb(1'b1),
    .addrb(read_ptr),
    .doutb(ram_q),
    .sbiterrb(),
    .dbiterrb(),
    .sleep(1'b0)
  );

  always @(posedge clk) begin
    if (!resetn) begin
      write_ptr <= {ADDR_WIDTH{1'b0}};
      read_ptr <= {ADDR_WIDTH{1'b0}};
      ram_count <= {(ADDR_WIDTH+1){1'b0}};
      out_valid <= 1'b0;
      packed_fifo_wr_overflow <= 1'b0;
    end else if (flush_sync) begin
      // The write is not rejected on a flush. It becomes the first word in a
      // newly empty queue and is visible only after this flush cycle.
      write_ptr <= packed_fifo_wr_en ? {{(ADDR_WIDTH-1){1'b0}}, 1'b1} : {ADDR_WIDTH{1'b0}};
      read_ptr <= {ADDR_WIDTH{1'b0}};
      ram_count <= packed_fifo_wr_en ? {{ADDR_WIDTH{1'b0}}, 1'b1} : {(ADDR_WIDTH+1){1'b0}};
      out_valid <= 1'b0;
      packed_fifo_wr_overflow <= 1'b0;
    end else begin
      packed_fifo_wr_overflow <= packed_fifo_wr_en && !can_accept_write;

      if (move_ram_to_output) begin
        out_valid <= 1'b1;
        read_ptr <= read_ptr + 1'b1;
      end else if (pop_output) begin
        out_valid <= 1'b0;
      end

      if (accept_write) begin
        write_ptr <= write_ptr + 1'b1;
      end

      case ({accept_write, move_ram_to_output})
        2'b10: ram_count <= ram_count + 1'b1;
        2'b01: ram_count <= ram_count - 1'b1;
        default: ram_count <= ram_count;
      endcase
    end
  end

endmodule
