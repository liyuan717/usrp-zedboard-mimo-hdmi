`timescale 1ns/1ps
// 512-scan, single-clock elasticity FIFO.  It prevents a cold SFD search from
// backpressuring the DSP's 129-scan output buffer.  Payload is Q15 DSP output
// plus exact coordinate/epoch/discontinuity provenance.
module rxv2_scan_elastic_fifo #(
  parameter integer DEPTH=512
) (
 input wire clk,input wire resetn,input wire flush,
 input wire [127:0] s_data,input wire [31:0] s_epoch,input wire [63:0] s_sample,input wire s_disc,input wire s_valid,output wire s_ready,
 output reg [127:0] m_data,output reg [31:0] m_epoch,output reg [63:0] m_sample,output reg m_disc,output reg m_valid,input wire m_ready,
 output reg overflow_sticky
);
 localparam AW=$clog2(DEPTH); localparam W=225;
 (* ram_style="block" *) reg [W-1:0] mem[0:DEPTH-1];
 reg [AW-1:0] wp,rp,read_address; reg [AW:0] count; reg rd_pending;
 assign s_ready=resetn && !flush && (count<DEPTH-1);
 wire push=s_valid&&s_ready;
 wire issue_read=!m_valid&&!rd_pending&&(count!=0);
 // Dedicated synchronous RAM ports, with an explicitly width-bounded read
 // address. Do not express the port as mem[rp-1]: expression widening and
 // the reset/control mux formerly prevented the requested BRAM inference.
 // Storage need not reset: count/m_valid fence all previous-generation data.
 always @(posedge clk) begin
   if(push) mem[wp]<={s_disc,s_sample,s_epoch,s_data};
   if(resetn&&!flush&&rd_pending) {m_disc,m_sample,m_epoch,m_data}<=mem[read_address];
 end
 always @(posedge clk) begin
  if(!resetn || flush) begin wp<=0;rp<=0;read_address<=0;count<=0;m_valid<=0;rd_pending<=0;overflow_sticky<=0;end
  else begin
   // AXI valid while ready is low is ordinary backpressure; it is not an
   // overflow and must not manufacture a discontinuity.  A non-backpressure
   // ADI/DMA loss is reported separately by the integration boundary.
   if(push) wp<=wp+1'b1;
   if(m_valid && m_ready) m_valid<=0;
   if(issue_read) begin rd_pending<=1;read_address<=rp;rp<=rp+1'b1;end
   if(rd_pending) begin m_valid<=1;rd_pending<=0;end
   case({push,issue_read})
    2'b10:count<=count+1'b1;
    2'b01:count<=count-1'b1;
    default:count<=count;
   endcase
  end
 end
endmodule
