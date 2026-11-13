`timescale 1ns/1ps
// Configured Q32 position accumulator and Q16 request producer only.
// Upstream supplies the first RRC-center position; this block estimates no timing.
module timing_request_cost_probe(
 input wire clk,rst,cancel,
 input wire cfg_valid,output wire cfg_ready,
 input wire [31:0] cfg_generation,input wire [15:0] cfg_epoch,
 input wire [79:0] cfg_first_position,input wire [34:0] cfg_step,
 input wire [15:0] cfg_count,input wire [31:0] cfg_first_id,
 output wire req_valid,input wire req_ready,
 output wire [31:0] req_generation,req_id,output wire [63:0] req_tag,
 output wire [15:0] req_mu,
 output reg done,output reg [1:0] cfg_error,output reg [15:0] emitted_count
);
 // nearest-Q32(4*(1 +/- 200ppm)), computed as exact rational integers.
 localparam [34:0] MIN_STEP=35'd17176433210,MAX_STEP=35'd17183305158;
 localparam [15:0] MAX_COUNT=16'd20000;
 reg active;
 reg [79:0] position;
 reg [34:0] step;
 reg [31:0] generation,identifier;
 reg [15:0] epoch,remaining;
 wire [80:0] rounded_position={1'b0,position}+81'd32768;
 assign cfg_ready=!rst && !cancel && !active;
 assign req_valid=!rst && !cancel && active;
 assign req_generation=generation;
 assign req_id=identifier;
 assign req_tag={epoch,rounded_position[79:32]};
 assign req_mu=rounded_position[31:16];
 always @(posedge clk) begin
  if(rst) begin
   active<=0;position<=0;step<=0;generation<=0;identifier<=0;
   epoch<=0;remaining<=0;done<=0;cfg_error<=0;emitted_count<=0;
  end else if(cancel) begin
   active<=0;remaining<=0;done<=0;cfg_error<=0;emitted_count<=0;
  end else begin
   done<=0;cfg_error<=0;
   if(cfg_valid && cfg_ready) begin
    emitted_count<=0;
    if(cfg_count==0 || cfg_count>MAX_COUNT) cfg_error<=2'd1;
    else if(cfg_step<MIN_STEP || cfg_step>MAX_STEP) cfg_error<=2'd2;
    else begin
     active<=1;position<=cfg_first_position;step<=cfg_step;
     generation<=cfg_generation;epoch<=cfg_epoch;identifier<=cfg_first_id;
     remaining<=cfg_count;
    end
   end else if(req_valid && req_ready) begin
    position<=position+{{45{1'b0}},step};identifier<=identifier+32'd1;
    remaining<=remaining-16'd1;emitted_count<=emitted_count+16'd1;
    if(remaining==16'd1) begin active<=0;done<=1;end
   end
  end
 end
endmodule
