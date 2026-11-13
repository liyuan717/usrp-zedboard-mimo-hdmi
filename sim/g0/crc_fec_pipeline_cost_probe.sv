`timescale 1ns/1ps
// Actual byte/CRC/RAM -> periodic descriptor admission -> fixed8 stream FEC.
// cfg_session/superframe/length are the existing protected PHY-header contract.
module crc_fec_pipeline_cost_probe #(
 parameter GENERATOR_FILE="generator.hex",INVERSE_FILE="inverse.hex"
)(
 input wire clk,rst,packet_abort,cfg_valid,output wire cfg_ready,
 input wire [31:0] cfg_session,cfg_superframe,input wire [15:0] cfg_packet_bytes,
 input wire in_valid,output wire in_ready,input wire [7:0] in_data,
 output wire source_valid,input wire source_ready,output wire [7:0] source_data,
 output wire [31:0] source_session,source_block,output wire [4:0] source_index,
 output wire [8:0] source_offset,output wire source_last,source_recovered,source_abort,
 output wire event_valid,input wire event_ready,output wire [31:0] event_session,event_block,
 output wire [2:0] event_code,output wire [19:0] event_missing,event_delivered,
 output wire [31:0] packets_accepted,packets_erased,packets_unmapped,
 output wire [31:0] descriptors_accepted,descriptors_rejected,range_busy,events_dropped,expired_blocks
);
 wire rv,rr,pv,pr,pl;wire [2:0] error;wire [255:0] header;wire [15:0] size,index;wire [7:0] data;
 link_packet_cost_probe packet(.clk(clk),.rst(rst),.cancel(packet_abort),.cfg_valid(cfg_valid),.cfg_ready(cfg_ready),
  .cfg_session(cfg_session),.cfg_superframe(cfg_superframe),.cfg_packet_bytes(cfg_packet_bytes),
  .in_valid(in_valid),.in_ready(in_ready),.in_data(in_data),
  .result_valid(rv),.result_ready(rr),.result_error(error),.result_header(header),.result_payload_bytes(size),
  .out_valid(pv),.out_ready(pr),.out_data(data),.out_index(index),.out_last(pl),.active());
 link_fec_ingress_cost_probe #(.GENERATOR_FILE(GENERATOR_FILE),.INVERSE_FILE(INVERSE_FILE)) ingress(
  .clk(clk),.rst(rst),.packet_abort(packet_abort),.result_valid(rv),.result_ready(rr),.result_error(error),
  .result_header(header),.result_payload_bytes(size),.payload_valid(pv),.payload_ready(pr),
  .reference_valid(1'b0),.reference_position(96'd0),.control_ready(1'b0),
  .payload_data(data),.payload_index(index),.payload_last(pl),
  .source_valid(source_valid),.source_ready(source_ready),.source_data(source_data),
  .source_session(source_session),.source_block(source_block),.source_index(source_index),.source_offset(source_offset),
  .source_last(source_last),.source_recovered(source_recovered),.source_abort(source_abort),
  .event_valid(event_valid),.event_ready(event_ready),.event_session(event_session),.event_block(event_block),
  .event_code(event_code),.event_missing(event_missing),.event_delivered(event_delivered),
  .packets_accepted(packets_accepted),.packets_erased(packets_erased),.packets_unmapped(packets_unmapped),
  .descriptors_accepted(descriptors_accepted),.descriptors_rejected(descriptors_rejected),
  .range_busy(range_busy),.events_dropped(events_dropped),.expired_blocks(expired_blocks));
endmodule
