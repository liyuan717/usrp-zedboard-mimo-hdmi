`timescale 1ns/1ps
// SPDX-License-Identifier: Apache-2.0
//
// Sticky request/acknowledge CDC for RX ADC/FIFO overflow faults.
//
// Unlike a toggle event bridge, src_pending remains asserted until the
// destination has observed the fault and the source event has gone away.
// Thus a held overflow, or several source-clock pulses before the 50-MHz
// domain can react, cannot be cancelled by an even number of toggles.
//
// Reset contract:
// * src_reset and dst_reset are synchronous to their respective clocks and
//   must each be asserted for at least one local active clock edge.
// * A source reset intentionally discards a pending source fault: the caller
//   must treat it as a new acquisition epoch and flush destination state.
// * A destination reset does not clear src_pending.  If a source fault is
//   pending, dst_fault reasserts after the destination synchronizer latency.
// * Release either reset only through its local reset-release discipline.
module rf_rx_overflow_fault_cdc (
  input  wire src_clk,
  input  wire src_reset,
  input  wire src_event,
  input  wire dst_clk,
  input  wire dst_reset,
  output wire dst_fault
);
  // Source-domain request.  A new source event wins over an acknowledge so
  // that an event on the same edge as acknowledgement cannot be dropped.
  reg src_pending;

  // dst_fault is asynchronous to src_clk.  These are the destination FFs of
  // the acknowledge CDC, therefore retain them as a synchronizer chain.
  (* ASYNC_REG = "TRUE", SHREG_EXTRACT = "NO" *) reg src_ack_sync_0;
  (* ASYNC_REG = "TRUE", SHREG_EXTRACT = "NO" *) reg src_ack_sync_1;

  always @(posedge src_clk) begin
    if (src_reset) begin
      src_pending    <= 1'b0;
      src_ack_sync_0 <= 1'b0;
      src_ack_sync_1 <= 1'b0;
    end else begin
      src_ack_sync_0 <= dst_fault;
      src_ack_sync_1 <= src_ack_sync_0;

      if (src_event)
        src_pending <= 1'b1;
      else if (src_ack_sync_1)
        src_pending <= 1'b0;
    end
  end

  // src_pending is asynchronous to dst_clk.  All three registers are kept
  // explicit so stage 0 is the metastability catcher and dst_fault uses only
  // the third stage.
  (* ASYNC_REG = "TRUE", SHREG_EXTRACT = "NO" *) reg dst_pending_sync_0;
  (* ASYNC_REG = "TRUE", SHREG_EXTRACT = "NO" *) reg dst_pending_sync_1;
  (* ASYNC_REG = "TRUE", SHREG_EXTRACT = "NO" *) reg dst_pending_sync_2;

  always @(posedge dst_clk) begin
    if (dst_reset) begin
      dst_pending_sync_0 <= 1'b0;
      dst_pending_sync_1 <= 1'b0;
      dst_pending_sync_2 <= 1'b0;
    end else begin
      dst_pending_sync_0 <= src_pending;
      dst_pending_sync_1 <= dst_pending_sync_0;
      dst_pending_sync_2 <= dst_pending_sync_1;
    end
  end

  assign dst_fault = dst_pending_sync_2;
endmodule
