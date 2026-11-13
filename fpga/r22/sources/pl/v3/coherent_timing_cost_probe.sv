`timescale 1ns/1ps
// Actual RRC samples -> public coherent metrics -> fractional center position.
// Search window configured from earlier coarse evidence. Period/SFO fitting,
// actual training dispatch and shared interpolation connection remain separate.
module coherent_timing_cost_probe #(parameter EXTERNAL_RESERVATION=0)(
 input wire clk,rst,cancel,
 input wire in_valid,output wire in_ready,input wire in_start,
 input wire [63:0] in_tag,input wire [79:0] in_data,
 input wire external_read_enable,input wire [11:0] external_read_address,
 output wire [79:0] read_word,
 input wire cfg_valid,output wire cfg_ready,input wire [31:0] cfg_id,cfg_generation,
 input wire [63:0] cfg_first_tag,input wire [6:0] cfg_points,
 output wire out_valid,input wire out_ready,
 output wire [31:0] out_id,out_generation,output wire [63:0] out_tag,
 output wire signed [17:0] out_fraction,output wire [79:0] out_position,
 output wire [2:0] out_error,output wire [6:0] out_point,
 output wire [31:0] history_generation,output wire [63:0] latest_tag,
 output wire [12:0] history_count,output wire history_restart,output wire busy,input wire external_read_reservation
);
 wire window_cfg_ready,window_busy,metric_valid,metric_ready,metric_last;
 wire [31:0] metric_id,metric_generation;
 wire [63:0] metric_tag;wire [6:0] metric_point;wire [2:0] metric_error;
 wire [115:0] metric_c;wire [59:0] metric_n;wire [51:0] metric_e;
 assign cfg_ready=window_cfg_ready && metric_ready;
 assign busy=!rst && !cancel && (window_busy || !metric_ready);
 coherent_window_cost_probe #(.EXTERNAL_RESERVATION(EXTERNAL_RESERVATION)) window(
  .clk(clk),.rst(rst),.cancel(cancel),.in_valid(in_valid),.in_ready(in_ready),.in_start(in_start),.in_tag(in_tag),.in_data(in_data),
  .external_read_enable(external_read_enable),.external_read_address(external_read_address),.read_word(read_word),.external_read_reservation(external_read_reservation),
  .cfg_valid(cfg_valid && metric_ready),.cfg_ready(window_cfg_ready),.cfg_id(cfg_id),.cfg_generation(cfg_generation),.cfg_first_tag(cfg_first_tag),.cfg_points(cfg_points),
  .out_valid(metric_valid),.out_ready(metric_ready),.out_id(metric_id),.out_generation(metric_generation),.out_tag(metric_tag),
  .out_point(metric_point),.out_last(metric_last),.out_error(metric_error),.out_c(metric_c),.out_n(metric_n),.out_e(metric_e),
  .history_generation(history_generation),.latest_tag(latest_tag),.history_count(history_count),.history_restart(history_restart),.busy(window_busy));
 coherent_peak_cost_probe peak(
  .clk(clk),.rst(rst),.cancel(cancel),.in_valid(metric_valid),.in_ready(metric_ready),.in_id(metric_id),.in_generation(metric_generation),.in_tag(metric_tag),
  .in_point(metric_point),.in_last(metric_last),.in_error(metric_error),.in_n(metric_n),.in_e(metric_e),
  .out_valid(out_valid),.out_ready(out_ready),.out_id(out_id),.out_generation(out_generation),.out_tag(out_tag),.out_fraction(out_fraction),
  .out_position(out_position),.out_error(out_error),.out_point(out_point));
endmodule
