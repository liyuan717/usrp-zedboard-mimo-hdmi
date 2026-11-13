`timescale 1ns/1ps
// One initial public window, then PL-predicted windows and causal SFO updates.
// Diagnostic READY never controls progress. timing_update is an internal
// one-cycle state-commit event; normal-job buffering/dispatch is NOT here yet.
module causal_tracking_timing_cost_probe #(parameter EXTERNAL_RESERVATION=0)(
 input wire clk,rst,cancel,
 input wire in_valid,output wire in_ready,input wire in_start,
 input wire [63:0] in_tag,input wire [79:0] in_data,
 input wire external_read_enable,input wire [11:0] external_read_address,output wire [79:0] read_word,
 input wire arm_valid,output wire arm_ready,input wire [31:0] arm_id,arm_generation,input wire [63:0] arm_first_tag,
 output wire timing_update,output wire [31:0] timing_id,timing_generation,output wire [15:0] timing_epoch,
 output wire [79:0] timing_position,training_position,next_position,output wire [34:0] symbol_step,output wire [2:0] peaks_used,
 output wire diagnostic_valid,input wire diagnostic_ready,output wire [121:0] diagnostic_data,
 output wire [31:0] diagnostic_replaced,output reg tracking_locked,
 output reg reacquire_request,output reg [2:0] reacquire_reason,
 output reg [31:0] windows_started,missed_windows,
 output wire [31:0] history_generation,output wire [63:0] latest_tag,
 output wire [12:0] history_count,output wire history_restart,output wire active,input wire external_read_reservation
);
 localparam IDLE=0,ISSUE=1,WAIT_PEAK=2,WAIT_SFO=3;
 localparam [79:0] NOMINAL_PERIOD=80'd80000<<32;
 reg [1:0] state;reg [31:0] owner_generation,window_id;reg [15:0] epoch;
 reg [63:0] window_first_tag;reg [79:0] predicted_center,period_increment,last_peak;
 reg [3:0] consecutive_failures;
 wire cfg_ready,coherent_busy,peak_valid,peak_ready;wire [31:0] peak_id,peak_generation;
 wire [63:0] peak_tag;wire [79:0] peak_position;wire signed [17:0] peak_fraction;
 wire [2:0] peak_error;wire [6:0] peak_point;
 wire sfo_ready,sfo_valid,sfo_locked;wire [2:0] sfo_error;
 wire mailbox_valid;
 wire arm_fire=arm_valid && arm_ready;
 reg sfo_arm_clear;
 // Arm READY depends on SFO READY. Its clear MUST be registered, otherwise
 // arm_fire -> SFO cancel -> SFO READY creates a zero-delay handshake loop.
 always @(posedge clk)begin
  if(rst || cancel) sfo_arm_clear<=0;else sfo_arm_clear<=arm_fire;
 end
 wire identity_valid=history_generation==owner_generation && latest_tag[63:48]==epoch;
 assign active=state!=IDLE;
 assign arm_ready=!rst && !cancel && !history_restart && state==IDLE && cfg_ready && sfo_ready;
 assign peak_ready=state==WAIT_PEAK && peak_error==0?sfo_ready:1'b1;
 wire valid_peak=peak_valid && state==WAIT_PEAK && identity_valid && peak_id==window_id && peak_generation==owner_generation;
 wire valid_sfo=sfo_valid && state==WAIT_SFO && identity_valid && timing_generation==owner_generation && timing_id==window_id && timing_epoch==epoch;
 assign timing_update=valid_sfo && sfo_error==0 && sfo_locked && !rst && !cancel && !history_restart;
 assign diagnostic_valid=mailbox_valid && !rst && !cancel && !history_restart && !arm_fire;
 coherent_timing_cost_probe #(.EXTERNAL_RESERVATION(EXTERNAL_RESERVATION)) coherent(
  .clk(clk),.rst(rst),.cancel(cancel),.in_valid(in_valid),.in_ready(in_ready),.in_start(in_start),.in_tag(in_tag),.in_data(in_data),
  .external_read_enable(external_read_enable),.external_read_address(external_read_address),.read_word(read_word),.external_read_reservation(external_read_reservation),
  .cfg_valid(state==ISSUE && identity_valid && !history_restart),.cfg_ready(cfg_ready),.cfg_id(window_id),.cfg_generation(owner_generation),
  .cfg_first_tag(window_first_tag),.cfg_points(7'd65),
  .out_valid(peak_valid),.out_ready(peak_ready),.out_id(peak_id),.out_generation(peak_generation),.out_tag(peak_tag),
  .out_fraction(peak_fraction),.out_position(peak_position),.out_error(peak_error),.out_point(peak_point),
  .history_generation(history_generation),.latest_tag(latest_tag),.history_count(history_count),.history_restart(history_restart),.busy(coherent_busy));
 causal_sfo_cost_probe sfo(
  .clk(clk),.rst(rst),.cancel(cancel || history_restart || sfo_arm_clear),
  .in_valid(valid_peak && peak_error==0 && !history_restart),.in_ready(sfo_ready),
  .in_id(peak_id),.in_generation(peak_generation),.in_epoch(peak_tag[63:48]),.in_position(peak_position),.in_error(3'd0),
  .out_valid(sfo_valid),.out_ready(1'b1),.out_id(timing_id),.out_generation(timing_generation),.out_epoch(timing_epoch),
  .out_position(timing_position),.out_training_position(training_position),.out_next_position(next_position),
  .out_step(symbol_step),.out_locked(sfo_locked),.out_used(peaks_used),.out_error(sfo_error));
 diagnostic_latest_mailbox_cost_probe #(.WIDTH(122)) diagnostics(
  .clk(clk),.rst(rst),.clear(cancel || history_restart || arm_fire),
  .in_valid(valid_sfo && !history_restart),.in_data({timing_id,timing_generation,timing_epoch,symbol_step,sfo_locked,peaks_used,sfo_error}),
  .out_valid(mailbox_valid),.out_ready(diagnostic_ready),.out_data(diagnostic_data),.replaced_count(diagnostic_replaced));
 task request_acquisition(input [2:0] reason);
  begin state<=IDLE;tracking_locked<=0;reacquire_request<=1;reacquire_reason<=reason;end
 endtask
 always @(posedge clk) begin
  if(rst || cancel) begin
   state<=IDLE;owner_generation<=0;window_id<=0;epoch<=0;window_first_tag<=0;
   predicted_center<=0;period_increment<=0;last_peak<=0;consecutive_failures<=0;
   tracking_locked<=0;reacquire_request<=0;reacquire_reason<=0;windows_started<=0;missed_windows<=0;
  end else if(history_restart) begin
   if(state!=IDLE) request_acquisition(1);
   state<=IDLE;tracking_locked<=0;
  end else case(state)
   IDLE:if(arm_fire) begin
    windows_started<=0;missed_windows<=0;consecutive_failures<=0;tracking_locked<=0;
    owner_generation<=arm_generation;epoch<=arm_first_tag[63:48];window_id<=arm_id;window_first_tag<=arm_first_tag;
    predicted_center<={arm_first_tag[47:0]+48'd32,32'd0};period_increment<=NOMINAL_PERIOD;
    if(arm_generation!=history_generation || arm_first_tag[63:48]!=latest_tag[63:48]) request_acquisition(1);
    else begin state<=ISSUE;reacquire_request<=0;reacquire_reason<=0;end
   end
   ISSUE:if(!identity_valid) request_acquisition(1);
    else if(cfg_ready) begin state<=WAIT_PEAK;windows_started<=windows_started+1'b1;end
   WAIT_PEAK:if(!identity_valid) request_acquisition(1);
    else if(peak_valid && peak_ready) begin
     if(!valid_peak) request_acquisition(1);
     else if(peak_error!=0) begin
      tracking_locked<=0;missed_windows<=missed_windows+1'b1;
      if(consecutive_failures==7) request_acquisition(2);
      else begin
       consecutive_failures<=consecutive_failures+1'b1;predicted_center<=predicted_center+period_increment;
       window_first_tag<={epoch,48'((predicted_center+period_increment)>>32)-48'd32};window_id<=window_id+1'b1;state<=ISSUE;
      end
     end else begin last_peak<=peak_position;state<=WAIT_SFO;end
    end
   WAIT_SFO:if(!identity_valid) request_acquisition(1);
    else if(sfo_valid) begin
     if(!valid_sfo) request_acquisition(1);
     else if(sfo_error==0) begin
      tracking_locked<=sfo_locked;consecutive_failures<=0;predicted_center<=next_position;
      period_increment<=next_position-timing_position;window_first_tag<={epoch,next_position[79:32]-48'd32};
      window_id<=window_id+1'b1;state<=ISSUE;
     end else if(sfo_error==3 && consecutive_failures<7) begin
      tracking_locked<=0;consecutive_failures<=consecutive_failures+1'b1;
      predicted_center<=last_peak+NOMINAL_PERIOD;period_increment<=NOMINAL_PERIOD;
      window_first_tag<={epoch,48'((last_peak+NOMINAL_PERIOD)>>32)-48'd32};window_id<=window_id+1'b1;state<=ISSUE;
     end else request_acquisition(3);
    end
  endcase
 end
endmodule
