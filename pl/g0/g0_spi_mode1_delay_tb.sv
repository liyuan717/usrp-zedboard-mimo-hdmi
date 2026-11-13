`timescale 1ns/1ps
module g0_spi_mode1_delay_tb;
  localparam [6:0] CR = 'h60, SR = 'h64, DTR = 'h68, DRR = 'h6c, SSR = 'h70;
  localparam [31:0] RUN = 'h096, INHIBIT = 'h196;
  reg clk = 0;
  always #10 clk = ~clk;
  reg reset_n = 0;
  reg [6:0] awaddr = 0, araddr = 0;
  reg [31:0] wdata = 0;
  reg awvalid = 0, wvalid = 0, arvalid = 0;
  wire awready, wready, bvalid, arready, rvalid;
  wire [1:0] bresp, rresp;
  wire [31:0] rdata;
  wire sck, mosi, csn;
  reg miso = 0;
`ifdef R17_ROUTED
  receiver_v2_rf_spi_routed dut (
`else
  receiver_v2_rf_spi_0 dut (
`endif
    .ext_spi_clk(clk), .s_axi_aclk(clk), .s_axi_aresetn(reset_n),
    .s_axi_awaddr(awaddr), .s_axi_awvalid(awvalid), .s_axi_awready(awready),
    .s_axi_wdata(wdata), .s_axi_wstrb(4'hf), .s_axi_wvalid(wvalid), .s_axi_wready(wready),
    .s_axi_bresp(bresp), .s_axi_bvalid(bvalid), .s_axi_bready(1'b1),
    .s_axi_araddr(araddr), .s_axi_arvalid(arvalid), .s_axi_arready(arready),
    .s_axi_rdata(rdata), .s_axi_rresp(rresp), .s_axi_rvalid(rvalid), .s_axi_rready(1'b1),
    .io0_i(1'b0), .io0_o(mosi), .io1_i(miso), .sck_i(1'b0), .sck_o(sck),
    .ss_i(1'b1), .ss_o(csn)
  );

  task automatic wr(input [6:0] addr, input [31:0] value);
    @(negedge clk);
    awaddr = addr; wdata = value; awvalid = 1; wvalid = 1;
    fork
      begin do @(posedge clk); while (!awready); @(negedge clk); awvalid = 0; end
      begin do @(posedge clk); while (!wready); @(negedge clk); wvalid = 0; end
      begin do @(posedge clk); while (!bvalid); if (bresp !== 0) $fatal(1, "AXI write response"); end
    join
  endtask

  task automatic rd(input [6:0] addr, output reg [31:0] value);
    @(negedge clk);
    araddr = addr; arvalid = 1;
    fork
      begin do @(posedge clk); while (!arready); @(negedge clk); arvalid = 0; end
      begin do @(posedge clk); while (!rvalid); if (rresp !== 0) $fatal(1, "AXI read response"); value = rdata; end
    join
  endtask

  reg [23:0] tx_word, response, observed;
  integer launches = 0, captures = 0;
  real latency;
  bit poison_early = 0;
  time last_rise;
  integer short_sck_intervals = 0;
  realtime cs_start, sck_rise, sck_fall, mosi_change;
  realtime min_high = 1000, min_low = 1000;
  realtime min_cs_setup = 1000, min_cs_hold = -1;
  realtime min_mosi_setup = 1000, min_mosi_hold = 1000;

  always @(mosi) begin
    mosi_change = $realtime;
    if (reset_n && !csn && captures && mosi_change-sck_fall < min_mosi_hold)
      min_mosi_hold = mosi_change-sck_fall;
  end
  always @(posedge csn) if (reset_n && captures == 24) begin
    if (min_cs_hold < 0 || $realtime-sck_fall < min_cs_hold) min_cs_hold = $realtime-sck_fall;
  end

  always @(negedge csn) begin
    cs_start = $realtime;
    launches = 0;
    captures = 0;
    observed = 0;
  end
  always @(posedge sck) if (reset_n && !csn) begin
    sck_rise = $realtime;
    if (!launches && sck_rise-cs_start < min_cs_setup) min_cs_setup = sck_rise-cs_start;
    if (captures && sck_rise-sck_fall < min_low) min_low = sck_rise-sck_fall;
    if (launches && ($time - last_rise) < 320) short_sck_intervals++;
    last_rise = $time;
    if (launches >= 24) $fatal(1, "Unexpected SCK rise");
    if (poison_early) begin
      miso <= 1'bx;
      miso <= #(latency + ((launches % 4) * 4.0)) response[23-launches];
    end else begin
      miso <= #(latency) response[23-launches];
    end
    launches++;
  end
  always @(negedge sck) if (reset_n && !csn) begin
    sck_fall = $realtime;
    if (sck_fall-sck_rise < min_high) min_high = sck_fall-sck_rise;
    if (sck_fall-mosi_change < min_mosi_setup) min_mosi_setup = sck_fall-mosi_change;
    captures++;
    observed = {observed[22:0], mosi};
  end

  // Verify that injected X reaches the actual first capture flop on every bit.
`ifdef R17_ROUTED
  integer early_unknown_samples = 0;
  wire first_capture = dut.U0.\NO_DUAL_QUAD_MODE.QSPI_NORMAL .IO1_I_REG.Q;
  always @(posedge sck) if (reset_n && !csn && poison_early) begin
    @(posedge clk);
    #1;
    if (first_capture !== 1'bx) $fatal(1, "First MISO capture did not receive injected X");
    early_unknown_samples++;
  end
`endif

  task automatic transfer(input real delay_ns, input [23:0] data, input [23:0] reply,
                          output reg [23:0] result);
    reg [31:0] status, value;
    latency = delay_ns; tx_word = data; response = reply;
    wr(SSR, 0);
    wr(DTR, data[23:16]); wr(DTR, data[15:8]); wr(DTR, data[7:0]);
    wr(CR, RUN);
    result = 0;
    for (int b = 0; b < 3; b++) begin
      do rd(SR, status); while (status[0] !== 1'b0);
      rd(DRR, value);
      if ($isunknown(value[7:0])) $fatal(1, "Unknown RX at byte %d", b);
      result = {result[15:0], value[7:0]};
    end
    wr(CR, INHIBIT);
    wr(SSR, 1);
    if (launches != 24 || captures != 24 || observed !== tx_word)
      $fatal(1, "MOSI/clock mismatch rise=%d fall=%d actual=%h expected=%h",
             launches, captures, observed, tx_word);
  endtask

  reg [23:0] result;
  real delays[10] = '{0.25, 14.25, 38.25, 59.25, 79.25, 99.25, 119.25, 139.25, 159.25, 180.25};
  integer mismatches;
  reg [23:0] test_reply;
  initial begin
    repeat (12) @(negedge clk);
    reset_n = 1;
    repeat (15) @(negedge clk);
    wr(CR, INHIBIT | 'h60);
    wr(CR, INHIBIT);
    wr(SSR, 1);
    for (int i = 0; i < 10; i++) begin
      mismatches = 0;
      for (int trial = 0; trial < 4; trial++) begin
        transfer(delays[i], 24'h00170f ^ (trial << 8), 24'h4ae193 ^ (trial * 24'h00ff77), result);
        if (result !== (24'h4ae193 ^ (trial * 24'h00ff77))) mismatches++;
        $display("READ delay=%0.2f trial=%0d got=%06h expected=%06h", delays[i], trial,
                 result, (24'h4ae193 ^ (trial * 24'h00ff77)));
      end
      $display("SWEEP delay=%0.2f mismatch=%0d", delays[i], mismatches);
      if (i < 9 && mismatches != 0) $fatal(1, "Required modeled delay failed");
      if (i == 9 && mismatches != 4) $fatal(1, "Late-data control undetected");
    end
    poison_early = 1;
    for (int trial = 0; trial < 48; trial++) begin
      test_reply = 24'b1 << (trial % 24);
      if (trial >= 24) test_reply = ~test_reply;
      transfer(38.25, 24'h001700 | trial, test_reply, result);
      if (result !== test_reply)
        $fatal(1, "Unsettled MISO affected a consumed byte at trial %0d", trial);
    end
    $display("EARLY_X_SWEEP transactions=48 bits=1152 delay_min=38.25 delay_max=50.25");
`ifdef R17_ROUTED
    if (early_unknown_samples != 1152) $fatal(1, "Missing first-capture observations");
    $display("ROUTED_FIRST_CAPTURE_X samples=%0d", early_unknown_samples);
`endif
    if (short_sck_intervals) $fatal(1, "Short SCK periods %0d", short_sck_intervals);
    $display("SPI_CONTROLLER_EDGE_NS high=%0.3f low=%0.3f cs_setup=%0.3f cs_hold=%0.3f mosi_setup=%0.3f mosi_hold=%0.3f",
             min_high, min_low, min_cs_setup, min_cs_hold, min_mosi_setup, min_mosi_hold);
    if (min_high < 150 || min_high >= 1000 || min_low < 150 || min_low >= 1000 ||
        min_cs_setup < 1 || min_cs_setup >= 1000 || min_cs_hold < 0 ||
        min_mosi_setup < 2 || min_mosi_setup >= 1000 || min_mosi_hold < 1 || min_mosi_hold >= 1000)
      $fatal(1, "Mode-1 controller-boundary edge spacing invalid");
    $display("R17_SPI_MODE1_DELAY_SWEEP_PASS");
    $finish;
  end
  initial #2_000_000 $fatal(1, "Simulation watchdog");
endmodule
