`timescale 1ns/1ps
module tb_source_bridge_admission;
 parameter integer ADMISSION=8192;
 localparam [31:0] SESSION=32'h34567891;
 reg clk=0;
 always #10 clk=~clk;
 reg rst=1, source_valid=0, source_last=0, axis_ready=0;
 reg [4:0] source_index=0;
 reg [8:0] source_offset=0;
 reg [7:0] source_data=0;
 wire source_ready, axis_valid, axis_last;
 wire [63:0] axis_data;
 wire [7:0] axis_keep;
 wire [31:0] committed,dropped,bad,aborted;
 source_record_bridge_cost_probe #(.ADMISSION_WAIT_CYCLES(ADMISSION)) dut(
  .clk(clk),.rst(rst),.fence(1'b0),.epoch(32'd1),
  .source_valid(source_valid),.source_ready(source_ready),.source_data(source_data),
  .source_session(SESSION),.source_block(32'd0),.source_index(source_index),
  .source_offset(source_offset),.source_last(source_last),.source_recovered(1'b0),.source_abort(1'b0),
  .event_valid(1'b0),.event_session(32'd0),.event_block(32'd0),.event_code(3'd0),
  .event_missing(20'd0),.event_delivered(20'd0),
  .m_axis_tdata(axis_data),.m_axis_tkeep(axis_keep),.m_axis_tvalid(axis_valid),
  .m_axis_tready(axis_ready),.m_axis_tlast(axis_last),
  .sources_committed(committed),.sources_dropped(dropped),.sources_bad(bad),.sources_aborted(aborted),
  .control_valid(1'b0),.control_data(64'd0),.control_word(2'd0),.control_last(1'b0),
  .control_session(32'd0),.control_superframe(32'd0),.control_position(96'd0));

 integer blocked_cycles, beat, kind, index, offset, checked, gap_count, cycle;
 reg [3:0] delivered;
 reg held=0;
 reg [72:0] held_beat;

 function automatic [7:0] pattern(input integer symbol,position);
  return 8'(position*17 + symbol*71 + (position>>2));
 endfunction

 always @(posedge clk) begin
  if (rst) begin
   cycle=0; held=0; beat=0; delivered=0; checked=0; gap_count=0;
  end else begin
   cycle=cycle+1;
   if (held && (!axis_valid || {axis_last,axis_keep,axis_data}!==held_beat))
    $fatal(1,"stalled AXIS changed");
   if (axis_valid && axis_ready) begin
    if (axis_keep!==8'hff) $fatal(1,"AXIS byte keep");
    if (beat==0 && axis_data[31:0]!==32'h58523356) $fatal(1,"record magic");
    if (beat==1) begin
     kind=int'(axis_data[47:32]);
     if (kind!=1 && kind!=4) $fatal(1,"unexpected record kind");
    end
    if (beat==6 && kind==1) index=int'(axis_data[47:32]);
    if (beat>=8 && kind==1) begin
     offset=(beat-8)*8;
     for (integer byte_lane=0;byte_lane<8;byte_lane=byte_lane+1)
      if (axis_data[byte_lane*8+:8]!==pattern(index,offset+byte_lane))
       $fatal(1,"SOURCE payload mismatch");
     checked=checked+8;
    end
    if (axis_last) begin
     if (kind==1) begin
      if (beat!=71 || index>3 || delivered[index]) $fatal(1,"SOURCE identity or boundary");
      delivered[index]=1;
     end else begin
      if (beat!=9) $fatal(1,"GAP record boundary");
      gap_count=gap_count+1;
     end
     beat=0;
    end else beat=beat+1;
   end
   held=axis_valid&&!axis_ready;
   if (held) held_beat={axis_last,axis_keep,axis_data};
  end
 end

 task send(input integer symbol,output integer waited);
  begin
   waited=0;
   source_index=5'(symbol);
   for (integer j=0;j<512;j=j+1) begin
    source_offset=9'(j);source_data=pattern(symbol,j);source_last=j==511;source_valid=1;
    @(posedge clk);
    while (!source_ready) begin
     if (j!=0) $fatal(1,"accepted SOURCE stalled mid-payload");
     waited=waited+1;
     if (waited>=ADMISSION) $fatal(1,"unbounded SOURCE admission");
     @(posedge clk);
    end
    @(negedge clk);
   end
   source_valid=0;
  end
 endtask

 task exercise(input integer pause_cycles);
  integer ignored;
  bit expect_drop;
  begin
   rst=1;axis_ready=0;source_valid=0;
   repeat (3) @(negedge clk);
   rst=0;
   send(0,ignored);send(1,ignored);
   while (!axis_valid) @(negedge clk);
   fork
    send(2,blocked_cycles);
    begin
     repeat (pause_cycles) @(negedge clk);
     axis_ready=1;
    end
   join
   send(3,ignored);
   repeat (6000) @(negedge clk);
   expect_drop=pause_cycles>=ADMISSION;
   if (delivered!==(expect_drop ? 4'b1011 : 4'b1111) ||
       committed!==(expect_drop ? 32'd3 : 32'd4) ||
       dropped!==32'(expect_drop) || gap_count!=int'(expect_drop) ||
       checked!=(expect_drop ? 1536 : 2048) || bad!=0 || aborted!=0 || beat!=0)
    $fatal(1,"bounded admission delivery census");
   if (expect_drop && blocked_cycles!=ADMISSION-1)
    $fatal(1,"admission limit changed");
   if (!expect_drop && (blocked_cycles<pause_cycles || blocked_cycles>=ADMISSION-1))
    $fatal(1,"resumed before expected slot release");
   $display("BRIDGE_ADMISSION_CASE limit=%0d pause=%0d waited=%0d dropped=%0d bytes=%0d",
            ADMISSION,pause_cycles,blocked_cycles,dropped,checked);
  end
 endtask

 initial begin
  exercise(6000);
  exercise(12000);
  exercise(26000);
  exercise(35000);
  $display("BRIDGE_ADMISSION_PASS limit=%0d",ADMISSION);
  $finish;
 end
 initial begin #5000000; $fatal(1,"bridge admission test timeout"); end
endmodule
