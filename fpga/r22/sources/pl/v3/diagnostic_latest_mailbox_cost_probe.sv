`timescale 1ns/1ps
// No producer backpressure. Held head is immutable; a full pending tail is
// replaced by the newest event and counted, so reception never waits for UI.
module diagnostic_latest_mailbox_cost_probe #(parameter WIDTH=134)(
 input wire clk,rst,clear,input wire in_valid,input wire [WIDTH-1:0] in_data,
 output reg out_valid,input wire out_ready,output reg [WIDTH-1:0] out_data,
 output reg [31:0] replaced_count
);
 reg tail_valid;reg [WIDTH-1:0] tail;
 always @(posedge clk) begin
  if(rst || clear) begin out_valid<=0;out_data<=0;tail_valid<=0;tail<=0;replaced_count<=0;end
  else if(!out_valid || out_ready) begin
   if(tail_valid) begin
    out_valid<=1;out_data<=tail;tail_valid<=in_valid;
    if(in_valid) tail<=in_data;
   end else begin out_valid<=in_valid;if(in_valid)out_data<=in_data;end
  end else if(in_valid) begin
   if(tail_valid) replaced_count<=replaced_count+1'b1;
   tail<=in_data;tail_valid<=1;
  end
 end
endmodule
