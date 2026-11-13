`timescale 1ns/1ps
// Local Stage1 recovery only. The ingress owner performs the actual flush.
// An explicit search starts a session; reset/cancel stops it. No RF messages.
module ingress_recovery_control_cost_probe #(parameter ENABLE=0)(
 input wire clk,rst,cancel,
 input wire requested_search_fire,requested_search_valid,
 input wire [31:0] active_attempt_id,
 input wire fault_event,ingress_running,ingress_healthy,dispatch_ready,raw_valid,
 input wire [63:0] raw_tag,
 output wire rearm_request,recovery_fire,
 output wire [31:0] recovery_id,
 output wire [63:0] recovery_tag,
 output wire [47:0] recovery_origin,
 output wire waiting,output wire [31:0] recovery_count
);
 generate if(ENABLE) begin: enabled
  reg armed,pending;reg [31:0] next_id,recovered;
  assign rearm_request=armed && !ingress_running && !rst && !cancel;
  assign recovery_fire=armed && pending && ingress_running && ingress_healthy &&
    dispatch_ready && raw_valid && !requested_search_valid && !rst && !cancel;
  assign recovery_id=next_id;
  // Bind a fresh phase basis only at the accepted new-generation search.
  // No old epoch/index or old phase origin survives the discontinuity.
  assign recovery_tag={raw_tag[63:48],raw_tag[47:0]+48'd1};
  assign recovery_origin=raw_tag[47:0];
  assign waiting=pending;
  assign recovery_count=recovered;
  always @(posedge clk) begin
   if(rst || cancel) begin armed<=0;pending<=0;next_id<=0;recovered<=0;end
   else if(requested_search_fire) begin armed<=1;pending<=0;recovered<=0;end
   else if(fault_event && armed) begin
    pending<=1;
    if(!pending) next_id<=active_attempt_id+32'd1;
   end else if(recovery_fire) begin pending<=0;recovered<=recovered+1'b1;end
  end
 end else begin: disabled
  assign rearm_request=0;assign recovery_fire=0;assign recovery_id=0;
  assign recovery_tag=0;assign recovery_origin=0;assign waiting=0;assign recovery_count=0;
 end endgenerate
endmodule
