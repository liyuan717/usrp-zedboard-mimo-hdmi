`timescale 1ns/1ps
// 50MHz CPACK-domain boundary: no ready toward the physical producer.
// Compact common-clock XPM boundary; original platform FIFO is unchanged.
// Raw upstream loss/fence must already be synchronous to this domain.
module continuous_adc_ingress_cost_probe #(
 parameter DEPTH=128,ADDR_WIDTH=7
)(
 input wire clk,rst,rearm,
 input wire raw_valid,input wire [63:0] raw_tag,raw_data,
 input wire upstream_loss,
 output wire out_valid,input wire out_ready,
 output wire [63:0] out_tag,out_data,output wire out_start,
 output wire receiver_cancel,output wire running,
 output reg [31:0] generation,fault_count,
 output reg [2:0] last_fault,
 output reg [ADDR_WIDTH:0] occupancy,high_water,
 output wire admitted,output wire dropped
);
 localparam HALTED=0,FLUSH=1,DRAIN=2,RUN=3;
 reg [1:0] state;
 reg has_tag,first_pending,overflow_pending;
 reg [63:0] expected_tag;
 wire tag_bad=has_tag && raw_valid && raw_tag!=expected_tag;
 wire fifo_valid,fifo_ready,flush_ack,fifo_drop,fifo_overflow;
 wire [127:0] fifo_data;
 wire fifo_flush=state==FLUSH;
 wire push=state==RUN && raw_valid && !upstream_loss && !tag_bad && !overflow_pending && !rst;
 wire fault=state==RUN && (upstream_loss || tag_bad || overflow_pending);
 assign running=!rst && state==RUN;
 assign receiver_cancel=!running || fault;
 assign out_valid=running && !fault && fifo_valid;
 // Do not feed FIFO ingress_drop back into its own full/pop credit decision.
 // A fault revokes the consumer transaction via receiver_cancel/out_valid.
 assign fifo_ready=out_ready && running && !upstream_loss && !tag_bad;
 assign out_tag=fifo_data[127:64];assign out_data=fifo_data[63:0];
 assign out_start=first_pending;
 assign dropped=running && raw_valid && (upstream_loss || tag_bad || fifo_drop);
 compact_stream_fifo_cost_probe #(.DATA_WIDTH(128),.DEPTH(DEPTH),.ADDR_WIDTH(ADDR_WIDTH)) elastic(
  .clk(clk),.resetn(!rst),.flush_sync(fifo_flush),.flush_ack(flush_ack),
  .packed_fifo_wr_en(push),.packed_fifo_wr_data({raw_tag,raw_data}),
  .packed_fifo_wr_overflow(fifo_overflow),.accepted_write(admitted),.ingress_drop(fifo_drop),
  .m_axis_tdata(fifo_data),.m_axis_tkeep(),.m_axis_tstrb(),.m_axis_tvalid(fifo_valid),
  .m_axis_tready(fifo_ready),.m_axis_tlast());
 always @(posedge clk) begin
  if(rst) begin
   overflow_pending<=0;state<=HALTED;has_tag<=0;first_pending<=1;expected_tag<=0;
   generation<=0;fault_count<=0;last_fault<=0;occupancy<=0;high_water<=0;
  end else begin
   // Register loss before it resets the downstream READY producer, avoiding
   // a combinational loop through full/pop credit. Older FIFO words remain
   // valid on the drop edge; all later consumer transfers are revoked.
   overflow_pending<=state==RUN && fifo_drop;
   if(state!=RUN) occupancy<=0;
   else begin
    case({admitted,out_valid && out_ready})
     2'b10: begin occupancy<=occupancy+1'b1;if(occupancy+1'b1>high_water) high_water<=occupancy+1'b1;end
     2'b01: occupancy<=occupancy-1'b1;
    endcase
    if(raw_valid && !tag_bad && !upstream_loss)
      begin has_tag<=1;expected_tag<={raw_tag[63:48],raw_tag[47:0]+48'd1};end
    if(out_valid && out_ready) first_pending<=0;
   end
   case(state)
    HALTED: if(rearm) state<=FLUSH;
    FLUSH: if(flush_ack) state<=DRAIN;
    DRAIN: if(!flush_ack && !upstream_loss) begin
     state<=RUN;has_tag<=0;first_pending<=1;generation<=generation+1'b1;
    end
    RUN: if(fault) begin
     state<=HALTED;fault_count<=fault_count+1'b1;
     last_fault<={tag_bad,overflow_pending,upstream_loss};has_tag<=0;
    end
   endcase
  end
 end
endmodule
