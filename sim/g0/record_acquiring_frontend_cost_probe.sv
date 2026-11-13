`timescale 1ns/1ps
// G0 candidate joins the unchanged 48RTL blind ADC receiver/FEC to atomic
// records. Wide diagnostics are not exported by this data-path wrapper;
// required counters remain. Physical RF IO, DMA owner and PS are not present.
module record_acquiring_frontend_cost_probe(
 input wire clk,rst,cancel,rearm,raw_valid,upstream_loss,
 input wire [63:0] raw_tag,raw_data,
 input wire search_valid,output wire search_ready,input wire [31:0] search_id,
 input wire [63:0] search_tag,input wire [47:0] phase_origin,
 output wire ingress_running,output wire [31:0] ingress_faults,local_reacquisitions,
 output wire [63:0] m_axis_tdata,output wire [7:0] m_axis_tkeep,
 output wire m_axis_tvalid,input wire m_axis_tready,output wire m_axis_tlast,
 output wire [31:0] packets_accepted,packets_erased,packets_unmapped,
 output wire [31:0] descriptors_accepted,descriptors_rejected,range_busy,fec_events_dropped,expired_blocks,
 output wire [31:0] sources_committed,sources_dropped,sources_bad,sources_aborted,
 output wire [31:0] sources_late_or_duplicate,events_dropped,records_completed
);
 wire sv,sr,sl,srec,sa,ev,er;wire [7:0] sd;wire [31:0] ss,sb,es,eb;
 wire [4:0] si;wire [8:0] so;wire [2:0] ec;wire [19:0] em,ed;
 wire cv,cr,cl;wire [63:0] cd;wire [1:0] cw;wire [31:0] cs,cf;wire [95:0] cp;
 reg [31:0] delivery_epoch=1;reg fence_previous;
 always @(posedge clk)begin
  if(rst)begin delivery_epoch<=1;fence_previous<=0;end
  else begin fence_previous<=cancel||sa;if((cancel||sa)&&!fence_previous)delivery_epoch<=delivery_epoch+1'b1;end
 end
 frame_acquiring_frontend_cost_probe #(.ENABLE_FEC(1)) core(
  .clk(clk),.rst(rst),.cancel(cancel),.rearm(rearm),.raw_valid(raw_valid),.upstream_loss(upstream_loss),.raw_tag(raw_tag),.raw_data(raw_data),
  .search_valid(search_valid),.search_ready(search_ready),.search_id(search_id),.search_tag(search_tag),.phase_origin(phase_origin),
  .result_ready(1'b1),.normal_ready(1'b1),.header_ready(1'b0),.phase_ready(1'b0),.status_ready(1'b1),.payload_ready(1'b0),.diagnostic_ready(1'b0),
  .packet_ready(1'b0),.packet_data_ready(1'b0),.ingress_running(ingress_running),.ingress_faults(ingress_faults),.local_reacquisitions(local_reacquisitions),
  .fec_source_valid(sv),.fec_source_ready(sr),.fec_source_data(sd),.fec_source_session(ss),.fec_source_block(sb),.fec_source_index(si),
  .fec_source_offset(so),.fec_source_last(sl),.fec_source_recovered(srec),.fec_source_abort(sa),
  .fec_event_valid(ev),.fec_event_ready(er),.fec_event_session(es),.fec_event_block(eb),.fec_event_code(ec),.fec_event_missing(em),.fec_event_delivered(ed),
  .control_valid(cv),.control_ready(cr),.control_data(cd),.control_word(cw),.control_last(cl),
  .control_session(cs),.control_superframe(cf),.control_position(cp),
  .fec_packets_accepted(packets_accepted),.fec_packets_erased(packets_erased),.fec_packets_unmapped(packets_unmapped),
  .fec_descriptors_accepted(descriptors_accepted),.fec_descriptors_rejected(descriptors_rejected),.fec_range_busy(range_busy),.fec_events_dropped(fec_events_dropped),.fec_expired_blocks(expired_blocks));
 source_record_bridge_cost_probe delivery(
  .clk(clk),.rst(rst),.fence(cancel),.epoch(delivery_epoch),
  .source_valid(sv),.source_ready(sr),.source_data(sd),.source_session(ss),.source_block(sb),.source_index(si),.source_offset(so),
  .source_last(sl),.source_recovered(srec),.source_abort(sa),
  .event_valid(ev),.event_ready(er),.event_session(es),.event_block(eb),.event_code(ec),.event_missing(em),.event_delivered(ed),
  .control_valid(cv),.control_ready(cr),.control_data(cd),.control_word(cw),.control_last(cl),
  .control_session(cs),.control_superframe(cf),.control_position(cp),
  .m_axis_tdata(m_axis_tdata),.m_axis_tkeep(m_axis_tkeep),.m_axis_tvalid(m_axis_tvalid),.m_axis_tready(m_axis_tready),.m_axis_tlast(m_axis_tlast),
  .sources_committed(sources_committed),.sources_dropped(sources_dropped),.sources_bad(sources_bad),.sources_aborted(sources_aborted),
  .sources_late_or_duplicate(sources_late_or_duplicate),.events_dropped(events_dropped),.records_completed(records_completed));
endmodule
