`timescale 1ns/1ps
// SPDX-License-Identifier: Apache-2.0
//
// Source-clock-side RX transport metadata producer.
//
// `adc_sample_valid` names one complete packed 64-bit IQ sample-vector event
// (not an individual I/Q scalar) in the post-util_wfifo / CPack 50-MHz
// capture domain.  The legacy wire and header field retain the name
// `adc_sample`, but this is deliberately *not* an absolute AD9361 sample
// count: it is only a monotonically increasing capture-domain position while
// that epoch is healthy.  A source overflow begins a new epoch, so no client
// may infer a physical sample index across that boundary.
//
// metadata_* must be connected to an async FIFO; this source has no right to
// cross the data bus with a naked multi-bit synchronizer.  Any fault requests
// a matching raw-data flush.  The integration must apply that flush to the
// raw FIFO and metadata FIFO as one transport-epoch operation.
module rf_rx_transport_metadata_source #(
  parameter integer BLOCK_SAMPLES = 1024,
  parameter integer COUNT_WIDTH = 11,
  parameter integer EPOCH_WIDTH = 32,
  parameter integer SEQ_WIDTH = 32
) (
  input  wire                     src_clk,
  input  wire                     src_resetn,
  input  wire                     adc_sample_valid,
  input  wire                     sample_written,
  input  wire                     adc_overflow,
  input  wire                     source_fifo_overflow,
  input  wire                     transport_flush,

  output reg                      metadata_valid,
  input  wire                     metadata_ready,
  output reg [15:0]               metadata_flags,
  output reg [EPOCH_WIDTH-1:0]    metadata_epoch,
  output reg [SEQ_WIDTH-1:0]      metadata_block_seq,
  output reg [63:0]               metadata_first_adc_sample,
  output reg [31:0]               metadata_valid_sample_count,
  output reg                      raw_flush_request,
  output reg                      fatal_counter_wrap
);

  localparam [15:0] FLAG_VALID                 = 16'h0001;
  localparam [15:0] FLAG_ADC_OVERFLOW          = 16'h0002;
  localparam [15:0] FLAG_SOURCE_FIFO_OVERFLOW  = 16'h0004;
  localparam [15:0] FLAG_FLUSH                 = 16'h0008;
  localparam [15:0] FLAG_RESET_EPOCH           = 16'h0010;
  localparam [15:0] FLAG_META_OVERFLOW         = 16'h0040;
  localparam [15:0] FLAG_META_SEQUENCE_MISMATCH = 16'h0100;

  reg [63:0] adc_sample_counter;
  // Current epoch/sequence are distinct from the staged CDC record.  The
  // latter must remain immutable until metadata_ready, otherwise an increment
  // at block close can relabel a record already presented to the async FIFO.
  reg [EPOCH_WIDTH-1:0] epoch_counter;
  reg [SEQ_WIDTH-1:0] sequence_counter;
  reg [COUNT_WIDTH-1:0] accepted_count;
  reg [63:0] first_sample_pending;
  reg reset_epoch_pending;
  // External fault sources are level signals.  In particular transport_flush
  // remains high for the whole interval in which the RXTH DMAC CTRL.ENABLE
  // bit is clear.  They therefore define a *fault episode*, not one new
  // epoch per source-clock edge.  Keeping the episode state here prevents a
  // normal disabled interval from exhausting the 32-bit epoch counter.
  reg external_fault_active;
  // A flush cannot send a standalone record: its raw partner was discarded.
  // Carry the cause into the next COMPLETE block so software can reject that
  // recovery block and observe why acquisition restarted.  Subsequent clean
  // blocks are usable; fault bits are not a permanent poison latch.
  reg [15:0] pending_fault_flags;
  localparam [15:0] FAULT_MASK = 16'h014e;

  wire sample_counter_would_wrap = adc_sample_valid && (&adc_sample_counter);
  wire sequence_exhausted_on_new_block = sample_written &&
      (accepted_count == {COUNT_WIDTH{1'b0}}) && (&sequence_counter);
  wire protocol_error = sample_written && !adc_sample_valid;
  wire external_fault = adc_overflow || source_fifo_overflow || transport_flush;
  wire external_fault_start = external_fault && !external_fault_active;
  wire internal_fault = protocol_error || sample_counter_would_wrap ||
      sequence_exhausted_on_new_block;

  function automatic [15:0] fault_flags;
    input ignored;
    begin
      fault_flags = 16'd0;
      if (adc_overflow) fault_flags = fault_flags | FLAG_ADC_OVERFLOW;
      if (source_fifo_overflow) fault_flags = fault_flags | FLAG_SOURCE_FIFO_OVERFLOW;
      if (transport_flush) fault_flags = fault_flags | FLAG_FLUSH;
      if (metadata_valid && !metadata_ready)
        fault_flags = fault_flags | FLAG_META_OVERFLOW;
      if (protocol_error || sample_counter_would_wrap || sequence_exhausted_on_new_block)
        fault_flags = fault_flags | FLAG_META_SEQUENCE_MISMATCH;
    end
  endfunction

  always @(posedge src_clk) begin
    if (!src_resetn) begin
      metadata_valid <= 1'b0;
      metadata_flags <= 16'd0;
      metadata_epoch <= {EPOCH_WIDTH{1'b0}};
      metadata_block_seq <= {SEQ_WIDTH{1'b0}};
      metadata_first_adc_sample <= 64'd0;
      metadata_valid_sample_count <= 32'd0;
      adc_sample_counter <= 64'd0;
      epoch_counter <= {{(EPOCH_WIDTH-1){1'b0}},1'b1};
      sequence_counter <= {SEQ_WIDTH{1'b0}};
      accepted_count <= {COUNT_WIDTH{1'b0}};
      first_sample_pending <= 64'd0;
      reset_epoch_pending <= 1'b1;
      external_fault_active <= 1'b0;
      pending_fault_flags <= 16'd0;
      raw_flush_request <= 1'b0;
      fatal_counter_wrap <= 1'b0;
    end else begin
      raw_flush_request <= 1'b0;
      external_fault_active <= external_fault;
      if (metadata_valid && metadata_ready)
        metadata_valid <= 1'b0;

      if (adc_sample_valid)
        adc_sample_counter <= adc_sample_counter + 1'b1;

      if (fatal_counter_wrap) begin
        metadata_valid <= 1'b0;
      end else if (external_fault) begin
        // A held transport_flush/ADC-overflow/source-overflow continues to
        // suppress partial metadata and request raw flushing.  Its cause bits
        // are accumulated for the next complete recovery block, but only the
        // rising edge of the whole external-fault episode may advance epoch.
        metadata_valid <= 1'b0;
        accepted_count <= {COUNT_WIDTH{1'b0}};
        reset_epoch_pending <= 1'b1;
        raw_flush_request <= 1'b1;
        pending_fault_flags <= pending_fault_flags | fault_flags(1'b0) |
            (metadata_valid ? (metadata_flags & FAULT_MASK) : 16'd0);
        metadata_flags <= fault_flags(1'b0);
        metadata_valid_sample_count <= accepted_count;
        if (external_fault_start) begin
          if (&epoch_counter) begin
            fatal_counter_wrap <= 1'b1;
          end else begin
            epoch_counter <= epoch_counter + 1'b1;
            sequence_counter <= {SEQ_WIDTH{1'b0}};
          end
        end
      end else if (internal_fault) begin
        // Do not emit a header for data whose matching raw FIFO epoch is being
        // discarded.  The next accepted word starts a fresh valid block.
        metadata_valid <= 1'b0;
        accepted_count <= {COUNT_WIDTH{1'b0}};
        reset_epoch_pending <= 1'b1;
        raw_flush_request <= 1'b1;
        if (&epoch_counter) begin
          fatal_counter_wrap <= 1'b1;
        end else begin
          epoch_counter <= epoch_counter + 1'b1;
          sequence_counter <= {SEQ_WIDTH{1'b0}};
        end
        pending_fault_flags <= pending_fault_flags | fault_flags(1'b0) |
            (metadata_valid ? (metadata_flags & FAULT_MASK) : 16'd0);
        // Staging alone is not observable on the wire.  pending_fault_flags
        // carries this provenance to the next fixed-size recovery record.
        metadata_flags <= fault_flags(1'b0);
        metadata_valid_sample_count <= accepted_count;
      end else if (sample_written) begin
        if (accepted_count == {COUNT_WIDTH{1'b0}})
          first_sample_pending <= adc_sample_counter;

        if (accepted_count == BLOCK_SAMPLES-1) begin
          // A previous record must drain within one block period.  Otherwise
          // exact data/metadata binding is lost, so flush rather than guess.
          if (metadata_valid && !metadata_ready) begin
            metadata_valid <= 1'b0;
            accepted_count <= {COUNT_WIDTH{1'b0}};
            reset_epoch_pending <= 1'b1;
            raw_flush_request <= 1'b1;
            if (&epoch_counter)
              fatal_counter_wrap <= 1'b1;
            else begin
              epoch_counter <= epoch_counter + 1'b1;
              sequence_counter <= {SEQ_WIDTH{1'b0}};
            end
            metadata_flags <= FLAG_META_OVERFLOW;
            pending_fault_flags <= pending_fault_flags | FLAG_META_OVERFLOW |
                (metadata_flags & FAULT_MASK);
            metadata_valid_sample_count <= BLOCK_SAMPLES;
          end else begin
            metadata_valid <= 1'b1;
            metadata_flags <= FLAG_VALID | pending_fault_flags |
                (reset_epoch_pending ? FLAG_RESET_EPOCH : 16'd0);
            pending_fault_flags <= 16'd0;
            metadata_epoch <= epoch_counter;
            metadata_block_seq <= sequence_counter;
            metadata_first_adc_sample <= (accepted_count == 0) ? adc_sample_counter : first_sample_pending;
            metadata_valid_sample_count <= BLOCK_SAMPLES;
            accepted_count <= {COUNT_WIDTH{1'b0}};
            reset_epoch_pending <= 1'b0;
            sequence_counter <= sequence_counter + 1'b1;
          end
        end else begin
          accepted_count <= accepted_count + 1'b1;
        end
      end
    end
  end
endmodule
