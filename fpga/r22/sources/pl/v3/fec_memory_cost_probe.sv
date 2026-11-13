`timescale 1ns/1ps
// G0 storage/port/GF-MAC prototype ONLY, not a complete FEC decoder.
// Matrix inversion, block control, deadlines and DMA are intentionally not
// part of this cost measurement and must retain separate resource budgets.
module fec_memory_cost_probe #(
 parameter integer BANKS=8,
 parameter integer AW=$clog2(BANKS*32*512)
)(
 input wire clk, rst,
 input wire load_we,
 input wire [AW-1:0] load_addr,
 input wire [7:0] load_data,
 input wire op_valid,
 output wire op_ready,
 input wire op_write,
 input wire [AW-1:0] op_addr,
 input wire [7:0] op_data, coefficient,
 input wire first, last,
 output reg out_valid,
 input wire out_ready,
 output reg [7:0] out_data
);
 localparam integer BYTES=BANKS*32*512;
 (* ram_style="block" *) reg [7:0] memory [0:BYTES-1];
 reg [7:0] read_q, coefficient_q, accumulator;
 reg first_q, last_q, pending;
 wire conflict=load_we && load_addr==op_addr;
 assign op_ready=!rst && !pending && (!out_valid || out_ready) && !conflict;
 wire fire=op_valid && op_ready;

 function automatic [7:0] gf_mul(input [7:0] a,input [7:0] b);
  reg [7:0] x,y,z;
  integer i;
  begin
   x=a; y=b; z=0;
   for(i=0;i<8;i=i+1) begin
    if(y[0]) z=z^x;
    x=(x<<1)^(x[7]?8'h1d:8'h00); y=y>>1;
   end
   gf_mul=z;
  end
 endfunction
 wire [7:0] product=gf_mul(read_q,coefficient_q);
 wire [7:0] sum=first_q?product:(accumulator^product);

 // Two true ports, one incoming write and one decoder read OR writeback.
 // No RAM reset. Ownership prevents same-byte collisions; this prototype
 // also stalls a conflicting decoder request rather than using undefined
 // read-during-write data. Output stalls do not stop the incoming port.
 always @(posedge clk) if(load_we) memory[load_addr]<=load_data;
 always @(posedge clk) if(fire) begin
  if(op_write) memory[op_addr]<=op_data;
  else read_q<=memory[op_addr];
 end

 always @(posedge clk) begin
  if(rst) begin
   pending<=0;out_valid<=0;accumulator<=0;
   coefficient_q<=0;first_q<=0;last_q<=0;out_data<=0;
  end else begin
   if(out_valid && out_ready) out_valid<=0;
   if(fire && !op_write) begin
    coefficient_q<=coefficient;first_q<=first;last_q<=last;pending<=1;
   end
   if(pending) begin
    pending<=0;accumulator<=sum;
    if(last_q) begin out_data<=sum;out_valid<=1;end
   end
  end
 end
endmodule
