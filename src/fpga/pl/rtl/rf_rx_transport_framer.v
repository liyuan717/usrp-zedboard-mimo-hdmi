`timescale 1ns/1ps
// SPDX-License-Identifier: Apache-2.0
//
// Local RX-DMA transport framer.
//
// This block is deliberately downstream of the ADC-clock metadata producer:
// it never derives continuity from an IIO sequence number or an RF payload.
// Each emitted transport block is exactly 32 + BLOCK_SAMPLES*8 bytes.  The
// first four AXIS beats are the little-endian RXTH v1 header, followed by
// exactly BLOCK_SAMPLES 64-bit payload beats.  An invalid/partial metadata
// record still occupies a fixed-size DMA block; raw beats above valid_count
// are replaced by zero padding and consumers must discard all its IQ.
//
// An RXTH block has a strict ownership rule: raw words are first collected
// into this block-local RAM, and only a *complete immutable block* is exposed
// on M_AXIS.  This is deliberate rather than an avoidable latency.  A source
// FIFO epoch flush during collection drops the uncommitted block; a flush
// during emission cannot splice next-epoch raw words into its header because
// emission reads only this RAM.  At the nominal 4 MSPS capture rate, a
// single-bank 1024-word collector still has more than enough time to emit its
// 1028 AXIS beats at 50 MHz before the next block closes.
module rf_rx_transport_framer #(
  parameter integer BLOCK_SAMPLES = 1024,
  parameter integer COUNT_WIDTH = 11
) (
  input  wire                  clk,
  input  wire                  resetn,
  // Synchronous epoch boundary.  It is intentionally *not* a reset for an
  // already committed output block: collect is discarded, emit completes.
  input  wire                  transport_flush,

  // Metadata arrived through the dedicated CDC FIFO.  `meta_valid` denotes a
  // complete, atomically captured record; it is not a Gray-synchronized bus.
  input  wire                  meta_valid,
  output wire                  meta_ready,
  input  wire [15:0]           meta_flags,
  input  wire [31:0]           meta_epoch,
  input  wire [31:0]           meta_block_seq,
  input  wire [63:0]           meta_first_adc_sample,
  input  wire [31:0]           meta_valid_sample_count,

  input  wire [63:0]           s_axis_tdata,
  input  wire [7:0]            s_axis_tkeep,
  input  wire                  s_axis_tvalid,
  output wire                  s_axis_tready,

  output reg  [63:0]           m_axis_tdata,
  output reg  [7:0]            m_axis_tkeep,
  output reg                   m_axis_tvalid,
  input  wire                  m_axis_tready,
  output reg                   m_axis_tlast
);

  localparam [15:0] FLAG_VALID                  = 16'h0001;
  localparam [15:0] FLAG_META_SEQUENCE_MISMATCH = 16'h0100;
  localparam [2:0] ST_IDLE = 3'd0;
  localparam [2:0] ST_COLLECT = 3'd1;
  localparam [2:0] ST_HDR0 = 3'd2;
  localparam [2:0] ST_HDR1 = 3'd3;
  localparam [2:0] ST_HDR2 = 3'd4;
  localparam [2:0] ST_HDR3 = 3'd5;
  localparam [2:0] ST_PAYLOAD = 3'd6;

  reg [2:0] state;
  reg [15:0] active_flags;
  reg [31:0] active_epoch;
  reg [31:0] active_seq;
  reg [63:0] active_first;
  reg [31:0] active_count;
  reg [31:0] active_crc;
  reg [COUNT_WIDTH-1:0] collect_index;
  reg [COUNT_WIDTH-1:0] payload_index;
  // One sample vector per entry.  Vivado infers block RAM at the production
  // 1024x64 configuration; it must not be shared with the source elastic FIFO.
  reg [63:0] payload_ram [0:BLOCK_SAMPLES-1];

  // Reflected IEEE 802.3 CRC-32: initial FFFFFFFF, polynomial EDB88320,
  // complemented after the final byte.  Byte index zero is AXIS bits [7:0].
  function automatic [31:0] crc32_byte;
    input [31:0] crc_in;
    input [7:0] byte_in;
    integer bit_index;
    reg [31:0] crc;
    begin
      crc = crc_in ^ byte_in;
      for (bit_index = 0; bit_index < 8; bit_index = bit_index + 1)
        if (crc[0]) crc = (crc >> 1) ^ 32'hEDB88320;
        else        crc = (crc >> 1);
      crc32_byte = crc;
    end
  endfunction

  function automatic [31:0] header_crc32;
    input [15:0] flags;
    input [31:0] epoch;
    input [31:0] seq_number;
    input [63:0] first_sample;
    input [31:0] valid_count;
    integer index;
    reg [223:0] header_prefix;
    reg [31:0] crc;
    begin
      // The packed vector is indexed by byte: [7:0] is wire byte offset 0.
      header_prefix = {valid_count, first_sample, seq_number, epoch, flags,
                       8'd32, 8'd1, 32'h48545852};
      crc = 32'hFFFF_FFFF;
      for (index = 0; index < 28; index = index + 1)
        crc = crc32_byte(crc, header_prefix[index*8 +: 8]);
      header_crc32 = crc ^ 32'hFFFF_FFFF;
    end
  endfunction

  wire count_in_range = (meta_valid_sample_count <= BLOCK_SAMPLES);
  wire [15:0] captured_flags = count_in_range ? meta_flags :
      ((meta_flags | FLAG_META_SEQUENCE_MISMATCH) & ~FLAG_VALID);
  wire [31:0] captured_count = count_in_range ? meta_valid_sample_count : 32'd0;
  // `resetn` is the ownership boundary as well as the RF-reset boundary.  In
  // the integrated design it is `reset_rf/peripheral_aresetn &&
  // dma_enabled`.  Thus an observed RXTH-DMAC disable makes a previously
  // committed block inaccessible immediately; software must abort/reclaim its
  // descriptor rather than mistake a truncated block for a valid RXTH record.
  // A source-fault epoch flush does *not* lower resetn and therefore retains
  // the established commit-immutable behavior during emission.
  wire output_handshake = resetn && m_axis_tvalid && m_axis_tready;
  wire payload_is_captured = (payload_index < active_count);
  wire collect_uses_input = resetn && (state == ST_COLLECT) &&
      (collect_index < active_count) && !transport_flush;

  // Gate every handshake-producing output with resetn.  This is deliberately
  // combinational so DMA-owner disable cannot leave one stale AXIS beat or an
  // accepted metadata/raw word in the interval before the next clk edge.
  assign meta_ready = resetn && (state == ST_IDLE) && !transport_flush;
  // Never read source FIFO while emitting.  Thus a source epoch flush can
  // discard/reset the raw FIFO without changing an already committed block.
  assign s_axis_tready = collect_uses_input;

  always @(*) begin
    m_axis_tdata = 64'd0;
    m_axis_tkeep = 8'hFF;
    m_axis_tvalid = 1'b0;
    m_axis_tlast = 1'b0;
    if (resetn) begin
      case (state)
        ST_HDR0: begin
          m_axis_tdata = {active_flags, 8'd32, 8'd1, 32'h48545852};
          m_axis_tvalid = 1'b1;
        end
        ST_HDR1: begin
          m_axis_tdata = {active_seq, active_epoch};
          m_axis_tvalid = 1'b1;
        end
        ST_HDR2: begin
          m_axis_tdata = active_first;
          m_axis_tvalid = 1'b1;
        end
        ST_HDR3: begin
          m_axis_tdata = {active_crc, active_count};
          m_axis_tvalid = 1'b1;
        end
        ST_PAYLOAD: begin
          // Fixed block size: past valid_count the physically reserved payload
          // is deterministic zero padding, not a borrowed word from next block.
          m_axis_tdata = payload_is_captured ? payload_ram[payload_index] : 64'd0;
          m_axis_tkeep = 8'hFF;
          m_axis_tvalid = 1'b1;
          m_axis_tlast = (payload_index == BLOCK_SAMPLES-1);
        end
        default: begin end
      endcase
    end
  end

  always @(posedge clk) begin
    if (!resetn) begin
      state <= ST_IDLE;
      active_flags <= 16'd0;
      active_epoch <= 32'd0;
      active_seq <= 32'd0;
      active_first <= 64'd0;
      active_count <= 32'd0;
      active_crc <= 32'd0;
      collect_index <= {COUNT_WIDTH{1'b0}};
      payload_index <= {COUNT_WIDTH{1'b0}};
    end else begin
      case (state)
        ST_IDLE: if (meta_valid && !transport_flush) begin
          active_flags <= captured_flags;
          active_epoch <= meta_epoch;
          active_seq <= meta_block_seq;
          active_first <= meta_first_adc_sample;
          active_count <= captured_count;
          active_crc <= header_crc32(captured_flags, meta_epoch, meta_block_seq,
                                     meta_first_adc_sample, captured_count);
          collect_index <= {COUNT_WIDTH{1'b0}};
          payload_index <= {COUNT_WIDTH{1'b0}};
          state <= ST_COLLECT;
        end
        ST_COLLECT: begin
          // The metadata FIFO is flushed in the same transport epoch.  The
          // raw FIFO branch is also flushed, so discarding this local partial
          // buffer makes the next accepted metadata/raw pair a new epoch.
          if (transport_flush) begin
            collect_index <= {COUNT_WIDTH{1'b0}};
            state <= ST_IDLE;
          end else if (active_count == 0) begin
            state <= ST_HDR0;
          end else if (s_axis_tvalid && s_axis_tready) begin
            payload_ram[collect_index] <= s_axis_tdata;
            if (collect_index == active_count - 1'b1) begin
              collect_index <= {COUNT_WIDTH{1'b0}};
              state <= ST_HDR0;
            end else begin
              collect_index <= collect_index + 1'b1;
            end
          end
        end
        ST_HDR0: if (output_handshake) state <= ST_HDR1;
        ST_HDR1: if (output_handshake) state <= ST_HDR2;
        ST_HDR2: if (output_handshake) state <= ST_HDR3;
        ST_HDR3: if (output_handshake) state <= ST_PAYLOAD;
        ST_PAYLOAD: if (output_handshake) begin
          if (payload_index == BLOCK_SAMPLES-1) begin
            payload_index <= {COUNT_WIDTH{1'b0}};
            state <= ST_IDLE;
          end else begin
            payload_index <= payload_index + 1'b1;
          end
        end
        default: state <= ST_IDLE;
      endcase
    end
  end
endmodule
