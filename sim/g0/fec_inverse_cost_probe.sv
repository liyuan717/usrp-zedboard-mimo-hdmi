`timescale 1ns/1ps
// Unfrozen G0 RS(20,32) coefficient-generator cost probe, NOT block scheduling.
// Twenty accepted basis indices select rows of the systematic generator ROM.
// Single-port scratch stores [G_selected | I]; Gauss-Jordan emits its inverse.
// Cancel/reset revoke the job and its outputs; scratch RAM is never reset.
module fec_inverse_cost_probe #(
 parameter GENERATOR_FILE="generator.hex", INVERSE_FILE="inverse.hex"
)(
 input wire clk, rst, cancel,
 input wire start_valid, output wire start_ready, input wire [15:0] job_id,
 input wire index_valid, output wire index_ready, input wire [5:0] index_data,
 output wire coeff_valid, input wire coeff_ready, output wire [7:0] coeff_data,
 output wire [4:0] coeff_row, coeff_col, output wire coeff_last,
 output wire done_valid, input wire done_ready, output reg [1:0] done_code,
 output reg [15:0] active_job, output reg [31:0] elapsed_cycles
);
 localparam IDLE=0, INDICES=1, INIT=2, SEARCH_READ=3, SEARCH_CHECK=4,
  SWAP_A_READ=5, SWAP_A_SAVE=6, SWAP_B_READ=7, SWAP_B_SAVE=8,
  SWAP_A_WRITE=9, SWAP_B_WRITE=10, NORM_READ=11, NORM_WRITE=12,
  ROW_NEXT=13, FACTOR_READ=14, FACTOR_CHECK=15, ELIM_READ=16,
  ELIM_WRITE=17, EMIT_READ=18, EMIT_HOLD=19, DONE=20;
 reg [4:0] state=IDLE;
 reg [4:0] ids[0:19];
 reg [31:0] seen;
 reg bad_indices;
 reg [4:0] input_count, row, pivot, column_pivot;
 reg [5:0] col;
 reg [7:0] inv_pivot, factor, swap_a, swap_b;
 reg [7:0] pivot_row[0:39];
 (* ram_style="block" *) reg [7:0] scratch[0:799];
 reg [7:0] memory_q;
 reg mem_read, mem_write;
 reg [9:0] address;
 reg [7:0] write_data;
 reg [7:0] generator_rom[0:1023];
 reg [7:0] inverse_rom[0:255];
 initial begin
  $readmemh(GENERATOR_FILE,generator_rom);
  $readmemh(INVERSE_FILE,inverse_rom);
 end
 function automatic [7:0] gf_mul(input [7:0] a,input [7:0] b);
  reg [7:0] x,y,z;
  integer i;
  begin
   x=a;y=b;z=0;
   for(i=0;i<8;i=i+1) begin
    if(y[0]) z=z^x;
    x=(x<<1)^(x[7]?8'h1d:8'h00);y=y>>1;
   end
   gf_mul=z;
  end
 endfunction
 // One shared combinational GF multiplier for normalize and elimination.
 wire [7:0] product=gf_mul(state==NORM_WRITE?memory_q:pivot_row[col],
                           state==NORM_WRITE?inv_pivot:factor);
 wire enabled=!rst && !cancel;
 assign start_ready=enabled && state==IDLE;
 assign index_ready=enabled && state==INDICES;
 assign coeff_valid=enabled && state==EMIT_HOLD;
 assign coeff_data=memory_q;
 assign coeff_row=row;
 assign coeff_col=col-20;
 assign coeff_last=row==19 && col==39;
 assign done_valid=enabled && state==DONE;

 always @* begin
  mem_read=0;mem_write=0;address=row*40+col;write_data=0;
  case(state)
   INIT: begin
    mem_write=1;
    write_data=col<20?generator_rom[{ids[row],5'b00000}+col]:
                        (row==col-20?8'h01:8'h00);
   end
   SEARCH_READ:begin address=pivot*40+column_pivot;mem_read=1;end
   SWAP_A_READ:begin address=column_pivot*40+col;mem_read=1;end
   SWAP_B_READ:begin address=pivot*40+col;mem_read=1;end
   SWAP_A_WRITE:begin address=column_pivot*40+col;mem_write=1;write_data=swap_b;end
   SWAP_B_WRITE:begin address=pivot*40+col;mem_write=1;write_data=swap_a;end
   NORM_READ:begin address=column_pivot*40+col;mem_read=1;end
   NORM_WRITE:begin address=column_pivot*40+col;mem_write=1;write_data=product;end
   FACTOR_READ:begin address=row*40+column_pivot;mem_read=1;end
   ELIM_READ,EMIT_READ:mem_read=1;
   ELIM_WRITE:begin mem_write=1;write_data=memory_q^product;end
   default:begin end
  endcase
 end
 // Single-port NO_CHANGE template: the read register holds during writes.
 always @(posedge clk) if(enabled && (mem_write || mem_read)) begin
  if(mem_write) scratch[address]<=write_data;
  else memory_q<=scratch[address];
 end

 always @(posedge clk) begin
  if(rst || cancel) begin
   state<=IDLE;seen<=0;bad_indices<=0;input_count<=0;
   row<=0;col<=0;pivot<=0;column_pivot<=0;done_code<=0;
   active_job<=0;elapsed_cycles<=0;inv_pivot<=0;factor<=0;
   swap_a<=0;swap_b<=0;
  end else begin
   if(state!=IDLE && state!=DONE) elapsed_cycles<=elapsed_cycles+1;
   case(state)
    IDLE:if(start_valid) begin
     active_job<=job_id;elapsed_cycles<=0;seen<=0;bad_indices<=0;
     input_count<=0;done_code<=0;state<=INDICES;
    end
    INDICES:if(index_valid) begin
     ids[input_count]<=index_data[4:0];
     if(index_data>=32 || seen[index_data[4:0]]) bad_indices<=1;
     seen[index_data[4:0]]<=1;
     if(input_count==19) begin
      if(bad_indices || index_data>=32 || seen[index_data[4:0]]) begin
       done_code<=1;state<=DONE;
      end else begin row<=0;col<=0;state<=INIT;end
     end else input_count<=input_count+1;
    end
    INIT:if(col==39) begin
     col<=0;
     if(row==19) begin column_pivot<=0;pivot<=0;state<=SEARCH_READ;end
     else row<=row+1;
    end else col<=col+1;
    SEARCH_READ:state<=SEARCH_CHECK;
    SEARCH_CHECK:if(memory_q!=0) begin
     inv_pivot<=inverse_rom[memory_q];col<=0;
     state<=pivot==column_pivot?NORM_READ:SWAP_A_READ;
    end else if(pivot==19) begin done_code<=2;state<=DONE;end
    else begin pivot<=pivot+1;state<=SEARCH_READ;end
    SWAP_A_READ:state<=SWAP_A_SAVE;
    SWAP_A_SAVE:begin swap_a<=memory_q;state<=SWAP_B_READ;end
    SWAP_B_READ:state<=SWAP_B_SAVE;
    SWAP_B_SAVE:begin swap_b<=memory_q;state<=SWAP_A_WRITE;end
    SWAP_A_WRITE:state<=SWAP_B_WRITE;
    SWAP_B_WRITE:if(col==39) begin col<=0;state<=NORM_READ;end
                else begin col<=col+1;state<=SWAP_A_READ;end
    NORM_READ:state<=NORM_WRITE;
    NORM_WRITE:begin
     pivot_row[col]<=product;
     if(col==39) begin row<=0;state<=ROW_NEXT;end
     else begin col<=col+1;state<=NORM_READ;end
    end
    ROW_NEXT:if(row==column_pivot) begin
     if(row==19) begin row<=0;col<=20;state<=EMIT_READ;end
     else begin row<=row+1;state<=FACTOR_READ;end
    end else state<=FACTOR_READ;
    FACTOR_READ:state<=FACTOR_CHECK;
    FACTOR_CHECK:begin
     factor<=memory_q;col<=0;
     if(memory_q!=0) state<=ELIM_READ;
     else if(row==19) begin
      column_pivot<=column_pivot+1;pivot<=column_pivot+1;state<=SEARCH_READ;
     end else begin row<=row+1;state<=ROW_NEXT;end
    end
    ELIM_READ:state<=ELIM_WRITE;
    ELIM_WRITE:if(col==39) begin
     if(row==19) begin
      column_pivot<=column_pivot+1;pivot<=column_pivot+1;state<=SEARCH_READ;
     end else begin row<=row+1;state<=ROW_NEXT;end
    end else begin col<=col+1;state<=ELIM_READ;end
    EMIT_READ:state<=EMIT_HOLD;
    EMIT_HOLD:if(coeff_ready) begin
     if(col==39) begin
      col<=20;
      if(row==19) begin done_code<=0;state<=DONE;end
      else begin row<=row+1;state<=EMIT_READ;end
     end else begin col<=col+1;state<=EMIT_READ;end
    end
    DONE:if(done_ready) state<=IDLE;
    default:state<=IDLE;
   endcase
  end
 end
endmodule
