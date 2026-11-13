`timescale 1ns/1ps
module tb;
  reg din_clk = 0, dout_clk = 0;
  reg run_din = 0, run_dout = 0;
  always #31.25 din_clk = run_din ? ~din_clk : 1'b0;
  always #10 dout_clk = run_dout ? ~dout_clk : 1'b0;
  // Active-high normalized external faults for the four-source matrix.
  reg [3:0] faults = 4'b0001;
  reg elastic_flush_ack = 0;
  wire wfifo_din_rst, wfifo_dout_rstn, cpack_reset, elastic_resetn;
  wire source_write_enable, cpack_write_enable, reader_data_enable;
  wire capture_epoch_fence, capture_ready, source_request, source_fresh, destination_fenced;

`ifdef ROUTED
  capture_guard_routed dut (
`else
  rfv2_capture_reset_guard dut (
`endif
    .din_clk(din_clk), .dout_clk(dout_clk),
    .din_raw_reset(faults[0]), .dout_raw_reset(faults[1]),
    .pll_locked(!faults[2]), .source_ready(!faults[3]),
    .elastic_flush_ack(elastic_flush_ack), .wfifo_din_rst(wfifo_din_rst),
    .wfifo_dout_rstn(wfifo_dout_rstn), .cpack_reset(cpack_reset),
    .elastic_resetn(elastic_resetn), .source_write_enable(source_write_enable),
    .capture_epoch_fence(capture_epoch_fence), .capture_ready(capture_ready)
`ifndef ROUTED
    , .cpack_write_enable(cpack_write_enable), .reader_data_enable(reader_data_enable)
    , .source_request(source_request), .source_fresh(source_fresh)
    , .destination_fenced(destination_fenced)
`endif
  );

  integer fences = 0;
  always @(posedge capture_epoch_fence) fences++;
  integer ready_edges = 0;
  always @(posedge capture_ready) ready_edges++;

  task automatic assert_inactive;
    if ({capture_ready, source_write_enable, wfifo_din_rst} !== 3'b001)
      $fatal(1, "Public traffic gates opened before recovery at %0t", $time);
`ifndef ROUTED
    if ({cpack_write_enable, reader_data_enable, wfifo_dout_rstn} !== 3'b000)
      $fatal(1, "Traffic escaped asserted or incomplete reset at %0t", $time);
`endif
  endtask

  task automatic complete_recovery(input integer old_fences, input integer old_ready);
    // Before clocks run, a stale/unsolicited ACK pulse cannot advance the epoch.
    elastic_flush_ack = 1;
    #28;
    elastic_flush_ack = 0;
    assert_inactive();
    if (fences != old_fences || ready_edges != old_ready)
      $fatal(1, "stale acknowledgement released a stopped domain");
    run_din = 1;
    run_dout = 1;
    fork : fence_timeout
      begin
        @(posedge capture_epoch_fence);
      end
      begin
        #2000;
        $fatal(1, "missing recovery fence");
      end
    join_any
    disable fence_timeout;
    #500;
    assert_inactive();
    if (fences != old_fences+1 || ready_edges != old_ready ||
        {cpack_reset, elastic_resetn} !== 2'b01)
      $fatal(1, "fence repeated, or control not waiting for fresh FIFO ACK");
`ifndef ROUTED
    if (destination_fenced !== 1'b0)
      $fatal(1, "destination acknowledged before a fresh FIFO ACK");
`endif
    @(negedge dout_clk);
    elastic_flush_ack = 1;
    @(negedge dout_clk);
    elastic_flush_ack = 0;
    fork : ready_timeout
      begin
        @(posedge capture_ready);
      end
      begin
        #2000;
        $fatal(1, "fresh ACK did not restore capture");
      end
    join_any
    disable ready_timeout;
    #40;
`ifndef ROUTED
    if ({cpack_write_enable, reader_data_enable, destination_fenced, wfifo_dout_rstn} !== 4'b1111)
      $fatal(1, "RTL traffic gates inconsistent after recovery");
`endif
    if ({capture_ready, source_write_enable, wfifo_din_rst,
         cpack_reset, elastic_resetn} !== 5'b11001 ||
        fences != old_fences+1 || ready_edges != old_ready+1)
      $fatal(1, "capture gates inconsistent after recovery");
  endtask

  integer prev_fence, prev_ready;
  initial begin
    #160;
    assert_inactive();
    faults=0;
    complete_recovery(0, 0);
    for (integer repetition=0; repetition<4; repetition++) begin
      for (integer index=0; index<4; index++) begin
        prev_fence = fences;
        prev_ready = ready_edges;
        run_din = 0;
        run_dout = 0;
        #97;
        if (capture_ready !== 1'b1) $fatal(1, "stopping test clocks alone changed the guard");
        #(repetition*2.5+index*1.25);
        faults[index] = 1;
        #35;
        assert_inactive();
        if ({cpack_reset, elastic_resetn} !== 2'b10)
          $fatal(1, "asynchronous assertion failed with both clocks stopped");
        // Releasing the first root while a different fault is held must not release capture.
        faults[(index+1)%4] = 1;
        #43;
        faults[index] = 0;
        #43;
        assert_inactive();
        faults = 0;
        // Resume just one clock before the other to exercise separate release domains.
        run_din = index%2 == 0;
        run_dout = index%2 == 1;
        #303;
        assert_inactive();
        if (fences != prev_fence || ready_edges != prev_ready)
          $fatal(1, "one local clock sufficed to open capture");
        complete_recovery(prev_fence, prev_ready);
      end
    end
    if (fences != 17 || ready_edges != 17) $fatal(1, "reset case census");
    $display("R17_CAPTURE_RESET_GUARD_PASS fault_cases=16 fences=17 capture_recoveries=17");
    $finish;
  end
  initial #90000 $fatal(1, "test watchdog");
endmodule
