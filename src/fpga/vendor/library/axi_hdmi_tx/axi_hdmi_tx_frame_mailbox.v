// Derived copy of pl/vendor_patches/axi_hdmi_tx_frame_mailbox.v.
`timescale 1ns/100ps
module axi_hdmi_tx_frame_mailbox (
 input src_clk,input src_rst,input src_commit,input [8:0] src_addr,
 input dst_clk,input dst_rst,output reg [8:0] dst_addr,output reg dst_update_toggle);
 localparam [2:0] S_FLUSH_HI=0,S_FLUSH_LO=1,S_WAIT_ACK_LO=2,S_IDLE=3,S_WAIT_ACK_HI=4;
 reg [2:0] src_state=S_FLUSH_HI; reg src_flush=1'b1,src_valid=1'b0;
 reg [8:0] src_payload_hold=0; reg src_pending_valid=0; reg [8:0] src_pending_addr=0;
 (* ASYNC_REG="TRUE" *) reg src_ack_m1=0,src_ack_m2=0,src_ack_m3=0;
 (* ASYNC_REG="TRUE" *) reg src_flush_ack_m1=0,src_flush_ack_m2=0,src_flush_ack_m3=0;
 reg [2:0] src_ack_sync_ready=0; reg dst_ack=0,dst_flush_ack=0;
 (* ASYNC_REG="TRUE" *) reg dst_valid_m1=0,dst_valid_m2=0,dst_valid_m3=0;
 (* ASYNC_REG="TRUE" *) reg dst_flush_m1=1,dst_flush_m2=1,dst_flush_m3=1;
 task keep_pending; input [8:0] a; begin src_pending_valid<=1'b1; src_pending_addr<=a; end endtask
 always @(posedge src_clk) begin
  if(src_rst) begin src_state<=S_FLUSH_HI;src_flush<=1;src_valid<=0;src_pending_valid<=0;src_pending_addr<=0;
   src_ack_m1<=0;src_ack_m2<=0;src_ack_m3<=0;src_flush_ack_m1<=0;src_flush_ack_m2<=0;src_flush_ack_m3<=0;src_ack_sync_ready<=0;
  end else begin
   src_ack_m1<=dst_ack;src_ack_m2<=src_ack_m1;src_ack_m3<=src_ack_m2;
   src_flush_ack_m1<=dst_flush_ack;src_flush_ack_m2<=src_flush_ack_m1;src_flush_ack_m3<=src_flush_ack_m2;
   src_ack_sync_ready<={src_ack_sync_ready[1:0],1'b1};
   case(src_state)
    S_FLUSH_HI: begin src_valid<=0;if(src_commit)keep_pending(src_addr);if(src_flush_ack_m3)begin src_flush<=0;src_state<=S_FLUSH_LO;end end
    S_FLUSH_LO: begin src_valid<=0;if(src_commit)keep_pending(src_addr);if(!src_flush_ack_m3)src_state<=S_WAIT_ACK_LO;end
    S_WAIT_ACK_LO: begin src_valid<=0;if(src_commit)keep_pending(src_addr);if(src_ack_sync_ready==3'b111&&!src_ack_m3)begin
     if(src_pending_valid)begin src_payload_hold<=src_pending_addr;src_valid<=1;src_state<=S_WAIT_ACK_HI;src_pending_valid<=src_commit;if(src_commit)src_pending_addr<=src_addr;end
     else if(src_commit)begin src_payload_hold<=src_addr;src_valid<=1;src_pending_valid<=0;src_state<=S_WAIT_ACK_HI;end else src_state<=S_IDLE;end end
    S_IDLE: if(src_pending_valid)begin src_payload_hold<=src_pending_addr;src_valid<=1;src_state<=S_WAIT_ACK_HI;src_pending_valid<=src_commit;if(src_commit)src_pending_addr<=src_addr;end else if(src_commit)begin src_payload_hold<=src_addr;src_valid<=1;src_state<=S_WAIT_ACK_HI;end
    default: begin if(src_commit)keep_pending(src_addr);if(src_ack_m3)begin src_valid<=0;src_state<=S_WAIT_ACK_LO;end end
   endcase
  end
 end
 always @(posedge dst_clk) begin
  if(dst_rst)begin dst_ack<=0;dst_flush_ack<=0;dst_addr<=0;dst_update_toggle<=0;dst_valid_m1<=0;dst_valid_m2<=0;dst_valid_m3<=0;dst_flush_m1<=1;dst_flush_m2<=1;dst_flush_m3<=1;end
  else begin
   dst_flush_m1<=src_flush;dst_flush_m2<=dst_flush_m1;dst_flush_m3<=dst_flush_m2;
   if(dst_flush_m3)begin dst_flush_ack<=1;dst_ack<=0;dst_valid_m1<=0;dst_valid_m2<=0;dst_valid_m3<=0;end
   else begin dst_flush_ack<=0;dst_valid_m1<=src_valid;dst_valid_m2<=dst_valid_m1;dst_valid_m3<=dst_valid_m2;
    if(dst_valid_m3&&!dst_ack)begin dst_addr<=src_payload_hold;dst_ack<=1;dst_update_toggle<=~dst_update_toggle;end else if(!dst_valid_m3&&dst_ack)dst_ack<=0;
   end
  end
 end
endmodule
