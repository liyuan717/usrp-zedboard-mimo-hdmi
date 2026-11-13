`timescale 1ns/1ps
module tb_fec_deadline_clock;
 parameter integer DEADLINE=15;
 localparam integer PERIOD=1 << $clog2(DEADLINE+1);
 reg clk=0;
 always #10 clk=~clk;
 reg rst=1, open_valid=0, seal_valid=0, cancel_valid=0;
 reg [2:0] open_bank=0, seal_bank=0;
 reg [31:0] open_job=0, seal_job=1;
 wire open_ready, seal_ready;
 wire [31:0] blocks_expired;
 fec_stream_recovery_cost_probe #(.BLOCK_DEADLINE_CYCLES(DEADLINE)) dut(
  .clk(clk),.rst(rst),.open_valid(open_valid),.open_ready(open_ready),
  .open_bank(open_bank),.open_job(open_job),
  .seal_valid(seal_valid),.seal_ready(seal_ready),.seal_bank(seal_bank),.seal_job(seal_job),
  .release_valid(1'b0),.release_bank(3'd0),.release_job(32'd0),
  .cancel_valid(cancel_valid),.cancel_bank(3'd2),.cancel_job(32'd3),
  .packet_valid(1'b0),.packet_bank(3'd0),.packet_job(32'd0),.packet_index(5'd0),
  .byte_valid(1'b0),.byte_data(8'd0),.byte_last(1'b0),.crc_good(1'b0),
  .source_ready(1'b0),.status_ready(1'b0),.packet_abort(1'b0),
  .blocks_expired(blocks_expired)
 );

 integer cycle=0, expiry_count=0, wrap_matches=0, queued_matches=0, bank;
 integer opened[0:7];
 reg expected_expiry, live;
 always @(posedge clk) begin
  if (rst) begin
   cycle=0;
   expiry_count=0;
   for (integer n=0;n<8;n=n+1) opened[n]=-1;
  end else begin
   if (blocks_expired!==32'(expiry_count)) $fatal(1,"expiry count");
   for (integer n=0;n<8;n=n+1) begin
    live=dut.bank_state[n]==1 || dut.bank_state[n]==2 || dut.bank_state[n]==3;
    expected_expiry=live && opened[n]>=0 && cycle-opened[n]==DEADLINE;
    if (dut.expiring[n]!==expected_expiry)
     $fatal(1,"expiry edge bank=%0d open=%0d cycle=%0d",n,opened[n],cycle);
    if (expected_expiry) begin
     expiry_count=expiry_count+1;
     if (opened[n]/PERIOD!=cycle/PERIOD) wrap_matches=wrap_matches+1;
     if (dut.bank_state[n]==2) queued_matches=queued_matches+1;
    end
   end
   if (open_valid && open_ready) opened[open_bank]=cycle;
   if (cancel_valid) opened[2]=-1;
   cycle=cycle+1;
  end
 end

 task open_owner(input integer slot,input integer job);
  begin
   open_bank=3'(slot);open_job=32'(job);open_valid=1;
   @(posedge clk);
   if (!open_ready) $fatal(1,"open owner");
   @(negedge clk);
   open_valid=0;
  end
 endtask

 initial begin
  repeat (3) @(negedge clk);
  rst=0;
  repeat (PERIOD==2 ? 1 : PERIOD-2) @(negedge clk);
  for (bank=0;bank<8;bank=bank+1) begin
   open_owner(bank,bank+1);
   if (bank==1 && DEADLINE>=8) begin
    seal_valid=1;
    @(posedge clk);
    if (!seal_ready) $fatal(1,"seal owner");
    @(negedge clk);
    seal_valid=0;
   end
   if (bank==2 && DEADLINE>=8) begin
    cancel_valid=1;
    @(negedge clk);
    cancel_valid=0;
    open_owner(2,22);
   end
  end
  if (DEADLINE>=15) begin
   // The held status from bank 0 prevents bank 7 from reaching the core.
   // Seal that owner so it expires queued rather than while loading.
   seal_bank=7;
   seal_job=8;
   seal_valid=1;
   @(posedge clk);
   if (!seal_ready) $fatal(1,"seal queued owner");
   @(negedge clk);
   seal_valid=0;
  end
  repeat (DEADLINE+PERIOD) @(negedge clk);
  if (blocks_expired!=8) $fatal(1,"expiry owner census");
  if (DEADLINE>=15 && queued_matches!=1) $fatal(1,"queued expiry coverage");
  rst=1;
  repeat (2) @(negedge clk);
  rst=0;
  open_owner(2,33);
  repeat (DEADLINE+PERIOD) @(negedge clk);
  if (blocks_expired!=1 || wrap_matches==0) $fatal(1,"reset/wrap coverage");
  $display("FEC_SHARED_DEADLINE_PASS deadline=%0d wrap_matches=%0d queued_matches=%0d",DEADLINE,wrap_matches,queued_matches);
  $finish;
 end
 initial begin #100000; $fatal(1,"deadline test timeout"); end
endmodule
