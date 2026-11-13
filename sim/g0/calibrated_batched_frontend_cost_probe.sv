`timescale 1ns/1ps
// Unmodified raw signed16 ADC -> PL startup DC -> fixed8 receiver/SOURCE/batches.
// No notch or physical IO/CSR adapter; candidate only.
`timescale 1ns/1ps
// G0 receiver -> complete SOURCE records -> fixed-size DMA transport batches.
// No physical DMA/provider/IO adapter or video decoder is instantiated here.
module calibrated_batched_frontend_cost_probe(
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
 output wire batch_busy,output wire [31:0] batches_completed,batch_records,batch_rejected,batch_input_timeouts,
 output wire calibration_ready,output wire [31:0] calibration_faults,calibration_range_drops
);
 wire dc_valid,dc_loss;wire [63:0] dc_tag,dc_data;
 adc_dc_calibration_cost_probe calibration(
 .clk(clk),.rst(rst),.enable(1'b1),.raw_valid(raw_valid),.upstream_loss(upstream_loss),.raw_tag(raw_tag),.raw_data(raw_data),
 .out_valid(dc_valid),.out_loss(dc_loss),.out_tag(dc_tag),.out_data(dc_data),.calibrated(calibration_ready),
 .calibration_faults(calibration_faults),.range_drops(calibration_range_drops),.first_tag());
 batched_acquiring_frontend_cost_probe core(
 .clk(clk),
 .rst(rst),
 .cancel(cancel),
 .rearm(rearm),
 .run(run),
 .raw_valid(dc_valid),
 .upstream_loss(dc_loss),
 .raw_tag(dc_tag),
 .raw_data(dc_data),
 .search_valid(search_valid),
 .search_ready(search_ready),
 .search_id(search_id),
 .search_tag(search_tag),
 .phase_origin(phase_origin),
 .ingress_running(ingress_running),
 .ingress_faults(ingress_faults),
 .local_reacquisitions(local_reacquisitions),
 .m_axis_tdata(m_axis_tdata),
 .m_axis_tkeep(m_axis_tkeep),
 .m_axis_tvalid(m_axis_tvalid),
 .m_axis_tready(m_axis_tready),
 .m_axis_tlast(m_axis_tlast),
 .packets_accepted(packets_accepted),
 .packets_erased(packets_erased),
 .packets_unmapped(packets_unmapped),
 .descriptors_accepted(descriptors_accepted),
 .descriptors_rejected(descriptors_rejected),
 .range_busy(range_busy),
 .fec_events_dropped(fec_events_dropped),
 .expired_blocks(expired_blocks),
 .sources_committed(sources_committed),
 .sources_dropped(sources_dropped),
 .sources_bad(sources_bad),
 .sources_aborted(sources_aborted),
 .sources_late_or_duplicate(sources_late_or_duplicate),
 .events_dropped(events_dropped),
 .records_completed(records_completed),
 .batch_busy(batch_busy),
 .batches_completed(batches_completed),
 .batch_records(batch_records),
 .batch_rejected(batch_rejected),
 .batch_input_timeouts(batch_input_timeouts));
endmodule
