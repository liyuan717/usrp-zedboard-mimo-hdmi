`timescale 1ns/1ps
// Exact calibrated signed16 ADC -> lag4 products + aligned127-point C/E.
// E metadata accompanies each complete correlation through a bounded queue.
module differential_frontend_cost_probe(
 input wire clk,rst,cancel,
 input wire in_valid,output wire in_ready,input wire in_start,
 input wire [63:0] in_tag,in_data,
 output wire out_valid,input wire out_ready,
 output wire [391:0] out_c,output wire [69:0] out_energy,
 output wire [63:0] out_tag,output wire [31:0] out_generation,
 output wire [31:0] active_generation,output wire history_restart,
 output wire [31:0] input_fences,correlation_fences
);
 wire z_valid,z_ready,z_start,window_valid;
 wire [131:0] z;wire [69:0] energy;
 wire [63:0] endpoint;wire [31:0] generation;
 wire c_valid,c_ready,correlation_ready;
 wire [391:0] c;wire [63:0] c_tag;
 reg [165:0] metadata[0:7];
 reg [2:0] head,tail;reg [3:0] count;
 wire pop=out_valid && out_ready;
 wire space=count<8 || pop;
 wire push=z_valid && z_ready && window_valid;
 wire feed=z_valid && (!window_valid || space);
 assign z_ready=correlation_ready && (!window_valid || space);
 assign out_valid=!rst && !cancel && c_valid && count!=0;
 assign c_ready=!rst && !cancel && count!=0 && out_ready;
 assign out_c=c;assign out_tag=c_tag;
 assign out_energy=metadata[head][69:0];
 assign out_generation=metadata[head][101:70];
 // Metadata top64 is an independent expected correlation tag for verification.
 always @(posedge clk) begin
  if(rst || cancel) begin head<=0;tail<=0;count<=0;end
  else begin
   if(push) begin
    metadata[tail]<={{endpoint[63:48],endpoint[47:0]-48'd508},generation,energy};
    tail<=tail+1'b1;
   end
   if(pop) head<=head+1'b1;
   case({push,pop}) 2'b10:count<=count+1'b1;2'b01:count<=count-1'b1;default:count<=count;endcase
  end
 end
 differential_input_cost_probe input_stage(
  .clk(clk),.rst(rst),.cancel(cancel),.in_valid(in_valid),.in_ready(in_ready),
  .in_start(in_start),.in_tag(in_tag),.in_data(in_data),
  .out_valid(z_valid),.out_ready(z_ready),.out_start(z_start),.out_tag(endpoint),
  .out_generation(generation),.out_z(z),.out_energy(energy),.out_window_valid(window_valid),
  .fences(input_fences),.active_generation(active_generation),.history_restart(history_restart));
 differential_corr_cost_probe #(.W(33)) correlation(
  .clk(clk),.rst(rst || cancel),.in_valid(feed),.in_ready(correlation_ready),
  .in_z(z),.in_tag(endpoint),.in_start(z_start),.out_valid(c_valid),.out_ready(c_ready),
  .out_c(c),.out_tag(c_tag),.fences(correlation_fences));
endmodule
