`timescale 1ns/1ps
// One bounded normal-job descriptor. No diagnostic ready in this interface.
// Expiry/identity faults request LOCAL reacquisition, never RF feedback.
module tracking_job_dispatch_cost_probe #(parameter WAIT_LIMIT=2048)(
 input wire clk,rst,cancel,
 input wire event_valid,input wire [31:0] event_id,event_context,event_generation,
 input wire [15:0] event_epoch,input wire [79:0] event_position,input wire [34:0] event_step,
 input wire window_ready,input wire [31:0] current_context,current_generation,input wire [63:0] latest_tag,
 output wire job_valid,input wire job_ready,
 output reg [31:0] job_id,job_context,job_generation,output reg [15:0] job_epoch,
 output reg [79:0] job_position,output reg [34:0] job_step,
 output reg fault,output reg [2:0] fault_reason,output reg [31:0] expired_count,
 output wire pending
);
 localparam WAIT_BITS=$clog2(WAIT_LIMIT+1)>0?$clog2(WAIT_LIMIT+1):1;
 reg valid;reg [WAIT_BITS-1:0] waited;
 wire identity=job_context==current_context && job_generation==current_generation && job_epoch==latest_tag[63:48];
 assign job_valid=valid && identity && window_ready && !rst && !cancel;
 assign pending=valid;
 wire fire=job_valid && job_ready;
 task reject(input [2:0] reason);
  begin valid<=0;fault<=1;fault_reason<=reason;expired_count<=expired_count+1'b1;end
 endtask
 always @(posedge clk)begin
  if(rst || cancel)begin
   valid<=0;waited<=0;job_id<=0;job_context<=0;job_generation<=0;job_epoch<=0;job_position<=0;job_step<=0;
   fault<=0;fault_reason<=0;expired_count<=0;
  end else begin
   fault<=0;
   if(valid && !identity)reject(1);
   else if(event_valid)begin
    if(valid && !fire)reject(2);
    else if(event_context!=current_context || event_generation!=current_generation || event_epoch!=latest_tag[63:48])reject(1);
    else if(event_step<35'd17176433210 || event_step>35'd17183305158)reject(4);
    else begin
     valid<=1;waited<=0;job_id<=event_id;job_context<=event_context;job_generation<=event_generation;
     job_epoch<=event_epoch;job_position<=event_position;job_step<=event_step;
    end
   end else if(fire)begin valid<=0;waited<=0;end
   else if(valid)begin
    if(waited==WAIT_LIMIT-1)reject(3);else waited<=waited+1'b1;
   end
  end
 end
endmodule
