`timescale 1ns/1ps
// A descriptor belongs to this PL domain after input acceptance. Source fences
// suppress PUBLIC transfers immediately; registered clear resets internal work
// on the following edge. Internal issue on the fence edge is canceled, never
// published. No new input is admitted until clear has completed.
module frame_domain_cost_probe #(parameter WIDTH=499)(
 input wire clk,rst,fence,in_valid,output wire in_ready,input wire [WIDTH-1:0] in_data,
 output wire cfg_valid,input wire cfg_ready,output reg [WIDTH-1:0] cfg_data,
 output reg clear,output wire live,output reg pending
);
 assign live=!rst && !fence && !clear;
 assign in_ready=live && !pending && cfg_ready;
 assign cfg_valid=!rst && !clear && pending;
 always @(posedge clk)begin
  if(rst)begin clear<=1;pending<=0;cfg_data<=0;end
  else begin
   clear<=fence;
   if(fence)pending<=0;
   else begin
    if(cfg_valid && cfg_ready)pending<=0;
    if(in_valid && in_ready)begin pending<=1;cfg_data<=in_data;end
   end
  end
 end
endmodule
