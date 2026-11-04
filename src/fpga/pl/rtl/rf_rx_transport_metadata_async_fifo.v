`timescale 1ns/1ps
// SPDX-License-Identifier: Apache-2.0
//
// Exact-record CDC boundary between the AD9361 packed-sample clock and the
// 50-MHz RX-DMA/framer clock.  This is the only permitted path for RXTH
// metadata: do not replace it with per-bit synchronizers or a Gray snapshot.
//
// Reset contract: transport_reset is one coordinated epoch reset presented to
// both domains.  Independent src/dst reset release is deliberately unsupported
// because it could leave raw IQ and metadata in different epochs.  The system
// integration must combine reset/flush requests before driving this port.
module rf_rx_transport_metadata_async_fifo #(
  parameter integer FIFO_WRITE_DEPTH = 16
) (
  input  wire          src_clk,
  input  wire          dst_clk,
  input  wire          transport_reset,

  input  wire          src_valid,
  output wire          src_ready,
  input  wire [15:0]   src_flags,
  input  wire [31:0]   src_epoch,
  input  wire [31:0]   src_block_seq,
  input  wire [63:0]   src_first_adc_sample,
  input  wire [31:0]   src_valid_sample_count,
  output wire          src_overflow,

  output wire          dst_valid,
  input  wire          dst_ready,
  output wire [15:0]   dst_flags,
  output wire [31:0]   dst_epoch,
  output wire [31:0]   dst_block_seq,
  output wire [63:0]   dst_first_adc_sample,
  output wire [31:0]   dst_valid_sample_count,
  output wire          dst_underflow
);
  localparam integer RECORD_WIDTH = 176;
  wire [RECORD_WIDTH-1:0] din = {src_valid_sample_count, src_first_adc_sample,
                                  src_block_seq, src_epoch, src_flags};
  wire [RECORD_WIDTH-1:0] dout;
  wire full, empty, overflow, underflow, wr_rst_busy, rd_rst_busy;
  wire data_valid;

  assign src_ready = !transport_reset && !wr_rst_busy && !full;
  assign src_overflow = overflow;
  assign dst_valid = !transport_reset && !rd_rst_busy && !empty && data_valid;
  assign dst_underflow = underflow;
  assign {dst_valid_sample_count, dst_first_adc_sample, dst_block_seq,
          dst_epoch, dst_flags} = dout;

  xpm_fifo_async #(
    .CDC_SYNC_STAGES(3),
    .DOUT_RESET_VALUE("0"),
    .ECC_MODE("no_ecc"),
    .FIFO_MEMORY_TYPE("auto"),
    .FIFO_READ_LATENCY(0),
    .FIFO_WRITE_DEPTH(FIFO_WRITE_DEPTH),
    .FULL_RESET_VALUE(1),
    .PROG_EMPTY_THRESH(10),
    // At depth16/FWFT/3 CDC stages the legal full threshold is 8..11.
    // These flags are unused, but keep their configuration legal as well.
    .PROG_FULL_THRESH(FIFO_WRITE_DEPTH-6),
    .RD_DATA_COUNT_WIDTH($clog2(FIFO_WRITE_DEPTH)+1),
    .READ_DATA_WIDTH(RECORD_WIDTH),
    .READ_MODE("fwft"),
    .RELATED_CLOCKS(0),
    .SIM_ASSERT_CHK(1),
    // bit12=data_valid, bit8=underflow, bit0=overflow. In particular 0707
    // does NOT enable data_valid, which would hold dst_valid low forever.
    .USE_ADV_FEATURES("1101"),
    // XPM FIFO uses integer0/2, unlike XPM MEMORY's string sleep selector.
    .WAKEUP_TIME(0),
    .WRITE_DATA_WIDTH(RECORD_WIDTH),
    .WR_DATA_COUNT_WIDTH($clog2(FIFO_WRITE_DEPTH)+1)
  ) metadata_fifo_i (
    .rst(transport_reset),
    .wr_clk(src_clk), .wr_en(src_valid && src_ready), .din(din),
    .full(full), .overflow(overflow), .wr_ack(), .almost_full(), .prog_full(),
    .wr_data_count(), .injectsbiterr(1'b0), .injectdbiterr(1'b0),
    .rd_clk(dst_clk), .rd_en(dst_valid && dst_ready), .dout(dout),
    .empty(empty), .underflow(underflow), .rd_rst_busy(rd_rst_busy),
    .wr_rst_busy(wr_rst_busy), .data_valid(data_valid), .almost_empty(),
    .prog_empty(), .rd_data_count(), .sbiterr(), .dbiterr(), .sleep(1'b0)
  );
endmodule
