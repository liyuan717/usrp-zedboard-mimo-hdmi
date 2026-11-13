`timescale 1ns/1ps
// G0 receiver -> complete SOURCE records -> fixed-size DMA transport batches.
// No physical DMA/provider/IO adapter or video decoder is instantiated here.
module batched_acquiring_frontend_cost_probe #(parameter CHECK_INPUT_TAG=1)(
 input wire clk,rst,cancel,rearm,run,raw_valid,upstream_loss,
 input wire [63:0] raw_tag,raw_data,
 input wire search_valid,output wire search_ready,input wire [31:0] search_id,
 input wire [63:0] search_tag,input wire [47:0] phase_origin,
 output wire ingress_running,output wire [31:0] ingress_faults,local_reacquisitions,
 output wire [63:0] m_axis_tdata,output wire [7:0] m_axis_tkeep,
 output wire m_axis_tvalid,input wire m_axis_tready,output wire m_axis_tlast,
 output wire [31:0] packets_accepted,packets_erased,packets_unmapped,
 output wire [31:0] descriptors_accepted,descriptors_rejected,range_busy,fec_events_dropped,expired_blocks,
 output wire [31:0] sources_committed,sources_dropped,sources_bad,sources_aborted,
 output wire [31:0] sources_late_or_duplicate,events_dropped,records_completed,
 output wire batch_busy,output wire [31:0] batches_completed,batch_records,batch_rejected,batch_input_timeouts
);
 wire [63:0] record_data;wire [7:0] record_keep;wire record_valid,record_ready,record_last;
 record_acquiring_frontend_cost_probe #(.CHECK_INPUT_TAG(CHECK_INPUT_TAG)) core(
 .clk(clk),.rst(rst),.cancel(cancel),.rearm(rearm),.raw_valid(raw_valid),.upstream_loss(upstream_loss),.raw_tag(raw_tag),.raw_data(raw_data),
 .search_valid(search_valid),.search_ready(search_ready),.search_id(search_id),.search_tag(search_tag),.phase_origin(phase_origin),
 .ingress_running(ingress_running),.ingress_faults(ingress_faults),.local_reacquisitions(local_reacquisitions),
 .m_axis_tdata(record_data),.m_axis_tkeep(record_keep),.m_axis_tvalid(record_valid),.m_axis_tready(record_ready),.m_axis_tlast(record_last),
 .packets_accepted(packets_accepted),.packets_erased(packets_erased),.packets_unmapped(packets_unmapped),
 .descriptors_accepted(descriptors_accepted),.descriptors_rejected(descriptors_rejected),.range_busy(range_busy),.fec_events_dropped(fec_events_dropped),.expired_blocks(expired_blocks),
 .sources_committed(sources_committed),.sources_dropped(sources_dropped),.sources_bad(sources_bad),.sources_aborted(sources_aborted),
 .sources_late_or_duplicate(sources_late_or_duplicate),.events_dropped(events_dropped),.records_completed(records_completed));
 dma_batch_stream_cost_probe batch(
 .clk(clk),.rst(rst),.run(run),.s_axis_tdata(record_data),.s_axis_tkeep(record_keep),.s_axis_tvalid(record_valid),.s_axis_tready(record_ready),.s_axis_tlast(record_last),
 .m_axis_tdata(m_axis_tdata),.m_axis_tkeep(m_axis_tkeep),.m_axis_tvalid(m_axis_tvalid),.m_axis_tready(m_axis_tready),.m_axis_tlast(m_axis_tlast),
 .busy(batch_busy),.batches_completed(batches_completed),.records_forwarded(batch_records),.records_rejected(batch_rejected),.input_timeouts(batch_input_timeouts));
endmodule
