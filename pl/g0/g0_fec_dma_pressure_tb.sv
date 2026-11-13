`timescale 1ns/1ps
module tb;
localparam [31:0] BASE = 32'hfffffffc;
localparam [31:0] SESSION = 32'h56334601;
reg clk=0; always #10 clk=~clk;
reg rst=1, run=0, cfg=0, valid=0, allow_dma=0;
reg [7:0] data=0;
wire cfg_ready, in_ready, source_valid, source_ready, source_last, recovered, abort_source;
wire [7:0] source_data;
wire [31:0] source_session, source_block, event_session, event_block;
wire [4:0] source_index;
wire [8:0] source_offset;
wire event_valid, event_ready;
wire [2:0] event_code;
wire [19:0] event_missing, event_delivered;
wire [31:0] accepted, erased, unmapped, descriptions, rejected_desc, range_busy, events_lost, expired;
wire [31:0] committed, dropped, bad, aborted, late, bridge_events_lost, bridge_records;
wire [63:0] record_data, dma_data;
wire [7:0] record_keep, dma_keep;
wire record_valid, record_ready, record_last, dma_valid, dma_last, batch_busy;
reg dma_ready=0;
wire [31:0] batches, forwarded, rejected, timeouts;

crc_fec_pipeline_cost_probe fec(
 .clk(clk),.rst(rst),.packet_abort(1'b0),.cfg_valid(cfg),.cfg_ready(cfg_ready),
 .cfg_session(SESSION),.cfg_superframe(32'd31),.cfg_packet_bytes(16'd548),
 .in_valid(valid),.in_ready(in_ready),.in_data(data),
 .source_valid(source_valid),.source_ready(source_ready),.source_data(source_data),
 .source_session(source_session),.source_block(source_block),.source_index(source_index),
 .source_offset(source_offset),.source_last(source_last),.source_recovered(recovered),.source_abort(abort_source),
 .event_valid(event_valid),.event_ready(event_ready),.event_session(event_session),.event_block(event_block),
 .event_code(event_code),.event_missing(event_missing),.event_delivered(event_delivered),
 .packets_accepted(accepted),.packets_erased(erased),.packets_unmapped(unmapped),
 .descriptors_accepted(descriptions),.descriptors_rejected(rejected_desc),.range_busy(range_busy),
 .events_dropped(events_lost),.expired_blocks(expired)
);
source_record_bridge_cost_probe bridge(
 .clk(clk),.rst(rst),.fence(1'b0),.epoch(32'd1),
 .source_valid(source_valid),.source_ready(source_ready),.source_data(source_data),
 .source_session(source_session),.source_block(source_block),.source_index(source_index),
 .source_offset(source_offset),.source_last(source_last),.source_recovered(recovered),.source_abort(abort_source),
 .event_valid(event_valid),.event_ready(event_ready),.event_session(event_session),.event_block(event_block),
 .event_code(event_code),.event_missing(event_missing),.event_delivered(event_delivered),
 .m_axis_tdata(record_data),.m_axis_tkeep(record_keep),.m_axis_tvalid(record_valid),
 .m_axis_tready(record_ready),.m_axis_tlast(record_last),
 .sources_committed(committed),.sources_dropped(dropped),.sources_bad(bad),.sources_aborted(aborted),
 .sources_late_or_duplicate(late),.events_dropped(bridge_events_lost),.records_completed(bridge_records),
 .control_valid(1'b0),.control_data(64'd0),.control_word(2'd0),.control_last(1'b0),
 .control_session(32'd0),.control_superframe(32'd0),.control_position(96'd0)
);
dma_batch_stream_cost_probe batch(
 .clk(clk),.rst(rst),.run(run),.s_axis_tdata(record_data),.s_axis_tkeep(record_keep),
 .s_axis_tvalid(record_valid),.s_axis_tready(record_ready),.s_axis_tlast(record_last),
 .m_axis_tdata(dma_data),.m_axis_tkeep(dma_keep),.m_axis_tvalid(dma_valid),.m_axis_tready(dma_ready),
 .m_axis_tlast(dma_last),.busy(batch_busy),.batches_completed(batches),.records_forwarded(forwarded),
 .records_rejected(rejected),.input_timeouts(timeouts)
);

`ifdef CRC_RECOVERY
localparam integer PACKETS=183, EXPECTED_ERASURES=1;
`else
localparam integer PACKETS=182, EXPECTED_ERASURES=0;
`endif
reg [7:0] bytes[0:PACKETS*548-1];
wire bridge_empty=bridge.slot_state[0]==0 && bridge.slot_state[1]==0 && bridge.slot_state[2]==0 &&
                  !bridge.event_pending && bridge.state==0 &&
                  bridge.loss_generation==bridge.reported_generation;
integer cycle=0, fd, held=0, wait_cycles=0, max_wait=0, terminals=0, start, drops_at_release;
reg was_held=0;
reg [72:0] held_dma;
always @(negedge clk) dma_ready=allow_dma && cycle%19>4;
always @(posedge clk) begin
 cycle=cycle+1;
 if(!rst) begin
  if(abort_source) $fatal(1,"unexpected FEC abort");
  if(was_held && (!dma_valid || {dma_data,dma_keep,dma_last}!==held_dma))
   $fatal(1,"DMA beat changed during backpressure");
  was_held=dma_valid && !dma_ready;
  if(was_held) begin held_dma={dma_data,dma_keep,dma_last}; held=held+1; end
  if(dma_valid && dma_ready) begin
   if(dma_keep!=8'hff) $fatal(1,"bad DMA keep");
   $fwrite(fd,"%016h %0d\n",dma_data,dma_last);
  end
  if(source_valid && !source_ready) begin
   wait_cycles=wait_cycles+1;
   if(wait_cycles>max_wait)max_wait=wait_cycles;
   if(wait_cycles>=32768)$fatal(1,"SOURCE admission exceeded R17 bound");
  end else wait_cycles=0;
  if(fec.ingress.core_status_valid) begin
   if(fec.ingress.core_code!=0 || fec.ingress.core_missing ||
      fec.ingress.core_delivered!=20'hfffff) $fatal(1,"FEC failed to finish an offered block");
   terminals=terminals+1;
  end
 end
end

task send(input integer packet);
 begin
  while(!cfg_ready)@(negedge clk);
  cfg=1;@(negedge clk);cfg=0;
  for(integer p=0;p<548;p=p+1) begin
   data=bytes[packet*548+p];valid=1;
   @(posedge clk);while(!in_ready)@(posedge clk);@(negedge clk);valid=0;
  end
  while(!cfg_ready || fec.ingress.state!=0)@(negedge clk);
  repeat(2)@(negedge clk);
 end
endtask

task check_owners;
 begin
  if(fec.ingress.bank_live!=8'hff)$fatal(1,"eight old banks were not held");
  for(integer b=0;b<8;b=b+1) begin
   if(fec.ingress.bank_block[(b+4)%8]!==BASE+32'(b) ||
      fec.ingress.fec.bank_job[(b+4)%8]!==BASE+32'(b))
    $fatal(1,"an old bank owner was overwritten");
  end
 end
endtask

initial begin
 $readmemh("bytes.hex",bytes);
 fd=$fopen("batches.txt","w");
 repeat(5)@(negedge clk); rst=0; run=1;
 send(0);
 for(integer j=1;j<=160;j=j+1)send(j);
 check_owners();
 send(161); check_owners(); send(161); check_owners();
 send(162); check_owners();
 if(range_busy!=16 || unmapped!=1)$fatal(1,"ninth bank was not rejected as expected");
 repeat(80000)@(negedge clk);check_owners();
 drops_at_release=dropped;
 if(drops_at_release==0 || max_wait!=32767)$fatal(1,"no bounded whole-SOURCE drop during stall");
 allow_dma=1;start=cycle;
 while(terminals!=8 || fec.ingress.bank_live!=0) begin
  @(negedge clk);if(cycle-start>600000)$fatal(1,"eight-bank drain bound");
 end
 send(161);
 for(integer j=162;j<PACKETS;j=j+1)send(j);
 start=cycle;
 while(terminals!=9 || !bridge_empty || forwarded!=bridge_records) begin
  @(negedge clk);if(cycle-start>120000)$fatal(1,"ninth block did not resume");
 end
 run=0;start=cycle;
 while(batch_busy)begin
  @(negedge clk);if(cycle-start>10000)$fatal(1,"DMA did not quiesce");
 end
 if(unmapped!=1 || dropped!=drops_at_release || committed+dropped!=180 ||
    rejected || timeouts || bad || aborted || late!=EXPECTED_ERASURES || expired ||
    erased!=EXPECTED_ERASURES || rejected_desc)
  $fatal(1,"conservation: admitted=%0d dropped=%0d late=%0d erased=%0d expired=%0d bad=%0d",
         committed,dropped,late,erased,expired,bad);
 $fclose(fd);
 $display("R17_FEC_DMA_PRESSURE_PASS terminals=%0d admitted=%0d dropped=%0d max_wait=%0d dma_held=%0d CRC_erasures=%0d late_or_duplicate=%0d batch_count=%0d events_lost=%0d bridge_events_lost=%0d",terminals,committed,dropped,max_wait,held,erased,late,batches,events_lost,bridge_events_lost);
 $finish;
end
initial begin #40000000;$fatal(1,"global pressure test bound");end
endmodule
