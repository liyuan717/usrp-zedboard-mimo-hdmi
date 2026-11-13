`timescale 1ns/1ps
// SPDX-License-Identifier: Apache-2.0
//
// Synchronous elastic boundary for the FMCOMMS3 RX packed-data stream.
//
// The ADI CPack FIFO write interface has no ready signal: a word is either
// retained or the producer must be told about loss.  This module therefore
// presents a conventional 64-bit AXI-stream output while accepting one packed
// word per 50-MHz cycle. A two-word registered write boundary separates the
// guard/reset protocol from BRAM controls. DEPTH is the physical RAM depth:
// two locations are deliberately left unused, so the default usable capacity
// remains 1024 words (1019 RAM + three output skids + up to three write-boundary
// words) = 8 KiB.
// The empty RAM slots guarantee read_ptr != write_ptr whenever the RAM portion
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
  (* X_INTERFACE_IGNORE="true" *) input wire flush_sync,
  // Registered acknowledgement that this boundary has actually applied a
  // flush edge. It acknowledges the boundary, not necessarily an empty queue:
  // the documented simultaneous input word is still retained. A reset guard
  // must hold CPACK writes off while using this ACK as an empty/epoch fence.
  (* X_INTERFACE_IGNORE="true" *) output reg flush_ack,
  (* X_INTERFACE_IGNORE="true" *) input wire packed_fifo_wr_en,
  (* X_INTERFACE_IGNORE="true" *) input wire [DATA_WIDTH-1:0] packed_fifo_wr_data,
  (* X_INTERFACE_IGNORE="true" *) output reg packed_fifo_wr_overflow,
  // These two outputs are observability only.  They expose the exact
  // synchronous admission decision already made by this FIFO; they do not
  // add a READY path back to CPack.  The independent RXTH capture path uses
  // them to bind metadata to retained words rather than to raw TVALID.
  output wire                  accepted_write,
  output wire                  ingress_drop,

  (* X_INTERFACE_INFO = "xilinx.com:interface:axis:1.0 M_AXIS TDATA" *)
  (* X_INTERFACE_PARAMETER = "XIL_INTERFACENAME M_AXIS, TDATA_NUM_BYTES 16, HAS_TKEEP 1, HAS_TLAST 1" *)
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
  // ram_count excludes the output skids and registered write boundary.
  // Keep five physical RAM locations outside ram_count: one avoids a
  // read/write collision, while the other four account for the output/read
  // pipeline and up to three input-boundary words at the documented
  // 1024-word total.  The third boundary word exists only on a same-edge
  // full-output pop, whose consumed output word supplies its global credit.
  // This is an accounting limit, not a physical BRAM partition.
  // Keep the spare address so a full pop+push never reads and
  // writes the same dual-port BRAM address in one clock cycle.
  localparam integer RAM_USABLE_WORDS = DEPTH - 5;
  // Use the Vivado XPM simple-dual-port primitive explicitly.  This prevents
  // synthesis from silently changing the source-array read latency or falling
  // back to LUTRAM.  Port A is the single write port and port B is the
  // synchronous one-cycle read port.
  // resetn is driven by the capture guard, whose reset state is asserted
  // asynchronously.  Sample it locally before it can reach any RAM control.
  // The producer has no READY, so the registered write boundary is also the
  // required data
  // companion to that command boundary; delaying only an enable would be
  // unsafe on a flush or continuous CPack stream.
  // Keep this registered command boundary through synthesis.  In particular,
  // do not let an OOC optimizer fold its D input (a guard-owned async-reset
  // state bit) into an XPM EN/WE control.
  (* ASYNC_REG = "TRUE", SHREG_EXTRACT = "NO", DONT_TOUCH = "TRUE" *) reg [1:0] resetn_sync = 2'b00;
  (* DONT_TOUCH = "TRUE" *) reg [DATA_WIDTH-1:0] wr0_data = {DATA_WIDTH{1'b0}};
  (* DONT_TOUCH = "TRUE" *) reg [DATA_WIDTH-1:0] wr1_data = {DATA_WIDTH{1'b0}};
  (* DONT_TOUCH = "TRUE" *) reg [DATA_WIDTH-1:0] wr2_data = {DATA_WIDTH{1'b0}};
  (* DONT_TOUCH = "TRUE" *) reg wr0_valid = 1'b0;
  (* DONT_TOUCH = "TRUE" *) reg wr1_valid = 1'b0;
  (* DONT_TOUCH = "TRUE" *) reg wr2_valid = 1'b0;
  reg [ADDR_WIDTH-1:0] write_ptr = {ADDR_WIDTH{1'b0}};
  reg [ADDR_WIDTH-1:0] read_ptr = {ADDR_WIDTH{1'b0}};
  reg [ADDR_WIDTH:0]   ram_count = {(ADDR_WIDTH+1){1'b0}};
  wire [DATA_WIDTH-1:0] ram_q;
  // Three output registers plus an explicit in-flight RAM-read token make
  // XPM ENB independent of downstream TREADY.  TREADY still resolves the
  // AXIS transfer on its actual clock edge; it only changes this local skid,
  // never a RAM control pin.
  (* DONT_TOUCH = "TRUE" *) reg [DATA_WIDTH-1:0] out0_data = {DATA_WIDTH{1'b0}};
  (* DONT_TOUCH = "TRUE" *) reg [DATA_WIDTH-1:0] out1_data = {DATA_WIDTH{1'b0}};
  (* DONT_TOUCH = "TRUE" *) reg [DATA_WIDTH-1:0] out2_data = {DATA_WIDTH{1'b0}};
  (* DONT_TOUCH = "TRUE" *) reg out0_valid = 1'b0;
  (* DONT_TOUCH = "TRUE" *) reg out1_valid = 1'b0;
  (* DONT_TOUCH = "TRUE" *) reg out2_valid = 1'b0;
  (* DONT_TOUCH = "TRUE" *) reg [1:0] out_count = 2'd0;
  (* DONT_TOUCH = "TRUE" *) reg rd_pending = 1'b0;
  // Global occupancy is the authoritative no-READY producer credit.  RAM
  // moves and skid shifts deliberately do not change it; only a producer
  // admission or a real AXIS pop can do so.
  (* DONT_TOUCH = "TRUE" *) reg [ADDR_WIDTH:0] total_count = {(ADDR_WIDTH+1){1'b0}};
  (* DONT_TOUCH = "TRUE" *) reg flush_level_q = 1'b0;
  (* DONT_TOUCH = "TRUE" *) reg flush_cmd = 1'b0;

  wire local_resetn;
  wire pop_output;
  wire rd_issue;
  wire rd_complete;
  wire can_accept_write;
  wire write_to_ram;
  wire wr_retire;
  wire wr_ready;
  wire producer_valid;
  wire flush_edge;
  wire global_credit,local_boundary_ready,admit_producer;
  wire [ADDR_WIDTH-1:0] mem_waddr,mem_raddr;
  wire mem_wren;

  // flush_sync gates TVALID combinatorially: an old word cannot be transferred
  // during the cycle in which the queue is being discarded.
  assign m_axis_tdata  = out0_data;
  assign m_axis_tkeep  = {DATA_WIDTH/8{1'b1}};
  assign m_axis_tstrb  = {DATA_WIDTH/8{1'b1}};
  assign m_axis_tlast  = 1'b0;
  // local_resetn is deliberately a clocked command, not an async guard path.
  // flush still masks the selected cycle so an old word cannot be transferred.
  assign local_resetn = resetn_sync[1];
  assign m_axis_tvalid = out0_valid & ~flush_sync & ~flush_cmd & local_resetn;
  assign pop_output = m_axis_tvalid & m_axis_tready;

  // At most three words are either visible in the output skid or in flight
  // from XPM.  That credit is wholly local state: downstream TREADY is not a
  // term of rd_issue, so a guard-gated READY cannot control RAM ENB.
  assign rd_issue = local_resetn && !flush_cmd && (ram_count != 0) &&
                    ((out_count + rd_pending) < 3);
  assign rd_complete = rd_pending;
  assign can_accept_write = (ram_count < RAM_USABLE_WORDS) || rd_issue;
  // On a flush, cancel an old pending write; retain a simultaneous producer
  // word in the boundary for slot zero on the next cycle. This preserves the
  // public flush-simultaneous-write contract without a raw guard->RAM path.
  assign write_to_ram = wr0_valid && !flush_cmd && can_accept_write;
  assign wr_retire = write_to_ram;
  // A third boundary word absorbs the exact full/pop/push edge.  Whether that
  // local slot represents a real system credit is decided independently by
  // total_count below; internal RAM movement never manufactures a credit.
  assign local_boundary_ready = !wr2_valid || wr_retire;
  assign wr_ready = local_boundary_ready;
  // This raw producer level is admitted only into DONT_TOUCH boundary
  // registers.  It never forms a RAM EN/WE cone.
  assign producer_valid = packed_fifo_wr_en && resetn;
  assign flush_edge = flush_sync && !flush_level_q;
  // A high flush is one epoch boundary, not a level-sensitive rewrite command:
  // only its first cycle can admit a producer word. Later held-high beats are
  // reported as drops, avoiding repeated slot-zero overwrites.
  assign global_credit = flush_edge || (total_count < DEPTH) || pop_output;
  assign admit_producer = producer_valid && (flush_edge ||
                          (!flush_sync && global_credit && local_boundary_ready));
  assign accepted_write = admit_producer;
  assign ingress_drop = producer_valid && !admit_producer;
  assign mem_waddr = write_ptr;
  assign mem_raddr = read_ptr;
  assign mem_wren = local_resetn && write_to_ram;

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
    .dina(wr0_data),
    .injectsbiterra(1'b0),
    .injectdbiterra(1'b0),
    .clkb(clk),
    .rstb(1'b0),
    .enb(rd_issue),
    .regceb(1'b1),
    .addrb(mem_raddr),
    .doutb(ram_q),
    .sbiterrb(),
    .dbiterrb(),
    .sleep(1'b0)
  );

  always @(posedge clk) begin
    resetn_sync <= {resetn_sync[0], resetn};
    if (!resetn) begin
      flush_level_q <= 1'b0;
      flush_cmd <= 1'b0;
    end else begin
      flush_level_q <= flush_sync;
      flush_cmd <= flush_edge;
    end
    if (!local_resetn) flush_ack <= 1'b0;
    else flush_ack <= flush_cmd;
  end

  always @(posedge clk) begin
    if (!local_resetn) begin
      write_ptr <= {ADDR_WIDTH{1'b0}};
      read_ptr <= {ADDR_WIDTH{1'b0}};
      ram_count <= {(ADDR_WIDTH+1){1'b0}};
      // Output skid/read token reset is owned by the separate local-output
      // process below.
      packed_fifo_wr_overflow <= 1'b0;
    end else if (flush_cmd) begin
      // The raw flush edge has already rebased the write boundary.  Apply its
      // registered command to RAM now; no raw flush/reset level controls a
      // BRAM pin or pointer.
      write_ptr <= {ADDR_WIDTH{1'b0}};
      read_ptr <= {ADDR_WIDTH{1'b0}};
      ram_count <= {(ADDR_WIDTH+1){1'b0}};
      // A producer held during a multi-cycle flush level is deliberately not
      // reinterpreted as another epoch head; report that loss even while the
      // registered flush command is clearing old RAM state.
      packed_fifo_wr_overflow <= ingress_drop;
    end else begin
      packed_fifo_wr_overflow <= ingress_drop;

      if (rd_issue)
        read_ptr <= read_ptr + 1'b1;

      if (write_to_ram) begin
        write_ptr <= write_ptr + 1'b1;
      end

      case ({write_to_ram, rd_issue})
        2'b10: ram_count <= ram_count + 1'b1;
        2'b01: ram_count <= ram_count - 1'b1;
        default: ram_count <= ram_count;
      endcase
    end
  end

  // Preserve the exact global 1024-word capacity across RAM, read/output
  // staging and the no-READY write boundary.  In particular, a full same-edge
  // pop/push leaves this count unchanged; a later stalled consumer cannot
  // turn an internal RAM retire into another producer credit.
  always @(posedge clk) begin
    if (!resetn) begin
      total_count <= {(ADDR_WIDTH+1){1'b0}};
    end else if (flush_edge) begin
      total_count <= producer_valid ? {{ADDR_WIDTH{1'b0}},1'b1} : {(ADDR_WIDTH+1){1'b0}};
    end else begin
      case ({admit_producer,pop_output})
        2'b10: total_count <= total_count + 1'b1;
        2'b01: total_count <= total_count - 1'b1;
        default: total_count <= total_count;
      endcase
    end
  end

  // No READY is returned to CPack. Retiring and capturing in the same cycle
  // keeps one packed word/clk sustained throughput.  The two boundary slots
  // also retain the first two producer words while the local reset command is
  // synchronously releasing, rather than silently losing them.
  always @(posedge clk) begin
    if (!resetn) begin
      wr0_valid <= 1'b0;
      wr1_valid <= 1'b0;
      wr2_valid <= 1'b0;
      wr0_data <= {DATA_WIDTH{1'b0}};
      wr1_data <= {DATA_WIDTH{1'b0}};
      wr2_data <= {DATA_WIDTH{1'b0}};
    end else if (flush_edge) begin
      // Rebase solely at the public flush edge. The simultaneous beat becomes
      // the new epoch's boundary head; held-high flush cycles are rejected.
      wr0_valid <= producer_valid;
      wr1_valid <= 1'b0;
      wr2_valid <= 1'b0;
      if (producer_valid) wr0_data <= packed_fifo_wr_data;
    end else if (!local_resetn) begin
      if (admit_producer) begin
        if (!wr0_valid) begin
          wr0_valid <= 1'b1;
          wr0_data <= packed_fifo_wr_data;
        end else if (!wr1_valid) begin
          wr1_valid <= 1'b1;
          wr1_data <= packed_fifo_wr_data;
        end else if (!wr2_valid) begin
          wr2_valid <= 1'b1;
          wr2_data <= packed_fifo_wr_data;
        end
      end
    end else if (wr_retire) begin
      if (wr1_valid) begin
        wr0_valid <= 1'b1;
        wr0_data <= wr1_data;
        if (wr2_valid) begin
          wr1_valid <= 1'b1;
          wr1_data <= wr2_data;
          if (admit_producer) begin
            wr2_valid <= 1'b1;
            wr2_data <= packed_fifo_wr_data;
          end else begin
            wr2_valid <= 1'b0;
          end
        end else if (admit_producer) begin
          wr1_valid <= 1'b1;
          wr1_data <= packed_fifo_wr_data;
          wr2_valid <= 1'b0;
        end else begin
          wr1_valid <= 1'b0;
          wr2_valid <= 1'b0;
        end
      end else if (admit_producer) begin
        wr0_valid <= 1'b1;
        wr0_data <= packed_fifo_wr_data;
        wr1_valid <= 1'b0;
        wr2_valid <= 1'b0;
      end else begin
        wr0_valid <= 1'b0;
        wr1_valid <= 1'b0;
        wr2_valid <= 1'b0;
      end
    end else if (admit_producer && !wr2_valid) begin
      if (!wr0_valid) begin
        wr0_valid <= 1'b1;
        wr0_data <= packed_fifo_wr_data;
      end else if (!wr1_valid) begin
        wr1_valid <= 1'b1;
        wr1_data <= packed_fifo_wr_data;
      end else begin
        wr2_valid <= 1'b1;
        wr2_data <= packed_fifo_wr_data;
      end
    end
  end

  // XPM has one registered read-latency.  rd_pending labels its response;
  // out0..out2 form a three-word AXIS skid.  RAM issuance sees only these
  // registered occupancy tokens, never m_axis_tready.
  always @(posedge clk) begin
    if (!local_resetn || flush_cmd) begin
      rd_pending <= 1'b0;
      out0_valid <= 1'b0;
      out1_valid <= 1'b0;
      out2_valid <= 1'b0;
      out_count <= 2'd0;
      out0_data <= {DATA_WIDTH{1'b0}};
      out1_data <= {DATA_WIDTH{1'b0}};
      out2_data <= {DATA_WIDTH{1'b0}};
    end else begin
      rd_pending <= rd_issue;
      case ({pop_output, rd_complete})
        2'b10: begin
          out0_valid <= out1_valid;
          out0_data <= out1_data;
          out1_valid <= out2_valid;
          out1_data <= out2_data;
          out2_valid <= 1'b0;
          out_count <= out_count - 1'b1;
        end
        2'b01: begin
          if (out_count == 0) begin
            out0_valid <= 1'b1;
            out0_data <= ram_q;
          end else if (out_count == 1) begin
            out1_valid <= 1'b1;
            out1_data <= ram_q;
          end else begin
            out2_valid <= 1'b1;
            out2_data <= ram_q;
          end
          out_count <= out_count + 1'b1;
        end
        2'b11: begin
          if (out_count == 1) begin
            out0_valid <= 1'b1;
            out0_data <= ram_q;
            out1_valid <= 1'b0;
            out2_valid <= 1'b0;
          end else if (out_count == 2) begin
            out0_valid <= 1'b1;
            out0_data <= out1_data;
            out1_valid <= 1'b1;
            out1_data <= ram_q;
            out2_valid <= 1'b0;
          end else begin
            out0_valid <= 1'b1;
            out0_data <= out1_data;
            out1_valid <= 1'b1;
            out1_data <= out2_data;
            out2_valid <= 1'b1;
            out2_data <= ram_q;
          end
          out_count <= out_count;
        end
        default: begin end
      endcase
    end
  end

endmodule
