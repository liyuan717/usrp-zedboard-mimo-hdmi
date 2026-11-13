`timescale 1ns/1ps
// Diagnostic PHY0xf0 framing:8 independently bounded548-byte packets per TX.
// Real ML sign bits -> MSB-first bytes -> two private CRC validators -> one
// locked packet output. No packet identity is derived from sequence or gold.
module payload_link_cost_probe(
 input wire clk,rst,cancel,cfg_valid,output wire cfg_ready,
 input wire [31:0] cfg_id,cfg_context,cfg_generation,input wire [15:0] cfg_epoch,
 input wire [31:0] cfg_session,cfg_superframe,input wire [15:0] cfg_stream0_bytes,cfg_stream1_bytes,
 input wire in_valid,output wire in_ready,input wire [31:0] in_id,in_context,in_generation,
 input wire [15:0] in_epoch,input wire [14:0] in_symbol,input wire [3:0] in_decision,
 output wire result_valid,input wire result_ready,output wire [31:0] result_id,result_context,result_generation,
 output wire [15:0] result_epoch,output wire result_tx,output wire [2:0] result_slot,result_error,
 output wire [255:0] result_header,output wire [15:0] result_payload_bytes,
 output wire out_valid,input wire out_ready,output wire [7:0] out_data,output wire [15:0] out_index,output wire out_last,
 output wire done,output reg abort,output reg [2:0] detail,output reg [4:0] accepted_packets,erased_packets
);
 reg active,pair_pending,done_q;reg [31:0] owner,context_id,generation,session,superframe;
 reg [15:0] epoch;reg [14:0] symbols;reg [12:0] bytes_sent;
 reg [1:0] dibit;reg [7:0] shift0,shift1,pair0,pair1;
 reg [1:0] armed;reg [3:0] slot0,slot1;
 reg arb_locked,selected,streaming,prefer1;
 wire fence=cancel || abort;
 assign done=done_q && !rst && !fence;
 wire [1:0] cr,ir,rv,dv,dl,engine_active;wire [2:0] er[0:1];wire [255:0] hdr[0:1];wire [15:0] size[0:1],di[0:1];wire [7:0] data[0:1];
 wire [1:0] start={active && !armed[1] && slot1<8,active && !armed[0] && slot0<8};
 wire pair_fire=active && pair_pending && &ir && !rst && !fence;
 assign cfg_ready=!rst && !fence && !active && engine_active==0;
 assign in_ready=!rst && !fence && active && !pair_pending && symbols<17536;
 wire take=in_valid && in_ready;
 wire bad_input=in_id!=owner || in_context!=context_id || in_generation!=generation || in_epoch!=epoch || in_symbol!=symbols;
 assign result_valid=!rst && !fence && active && arb_locked && !streaming && rv[selected];
 assign result_id=owner;assign result_context=context_id;assign result_generation=generation;assign result_epoch=epoch;
 assign result_tx=selected;assign result_slot=selected?slot1[2:0]:slot0[2:0];
 assign result_error=er[selected];assign result_header=hdr[selected];assign result_payload_bytes=size[selected];
 assign out_valid=!rst && !fence && active && arb_locked && streaming && dv[selected];
 assign out_data=data[selected];assign out_index=di[selected];assign out_last=dl[selected];
 wire result_fire=result_valid && result_ready;
 wire payload_last=out_valid && out_ready && out_last;
 wire packet_finished=(result_fire && (result_error!=0 || result_payload_bytes==0)) || payload_last;
 genvar t;generate for(t=0;t<2;t=t+1)begin: packets
  link_packet_cost_probe packet(.clk(clk),.rst(rst),.cancel(fence),.cfg_valid(start[t]),.cfg_ready(cr[t]),
   .cfg_session(session),.cfg_superframe(superframe),.cfg_packet_bytes(16'd548),
   .in_valid(pair_fire),.in_ready(ir[t]),.in_data(t==0?pair0:pair1),
   .result_valid(rv[t]),.result_ready(result_fire && selected==t),.result_error(er[t]),.result_header(hdr[t]),.result_payload_bytes(size[t]),
   .out_valid(dv[t]),.out_ready(!rst && !fence && active && arb_locked && streaming && selected==t && out_ready),
   .out_data(data[t]),.out_index(di[t]),.out_last(dl[t]),.active(engine_active[t]));
 end endgenerate
 always @(posedge clk)begin
  if(rst || cancel)begin
   active<=0;pair_pending<=0;owner<=0;context_id<=0;generation<=0;session<=0;superframe<=0;epoch<=0;
   symbols<=0;bytes_sent<=0;dibit<=0;shift0<=0;shift1<=0;pair0<=0;pair1<=0;armed<=0;slot0<=0;slot1<=0;
   arb_locked<=0;selected<=0;streaming<=0;prefer1<=0;done_q<=0;abort<=0;detail<=0;accepted_packets<=0;erased_packets<=0;
  end else begin
   if(cfg_valid && cfg_ready)begin
    active<=1;owner<=cfg_id;context_id<=cfg_context;generation<=cfg_generation;epoch<=cfg_epoch;session<=cfg_session;superframe<=cfg_superframe;
    symbols<=0;bytes_sent<=0;dibit<=0;shift0<=0;shift1<=0;pair_pending<=0;armed<=0;slot0<=0;slot1<=0;
    arb_locked<=0;selected<=0;streaming<=0;prefer1<=0;done_q<=0;abort<=0;detail<=0;accepted_packets<=0;erased_packets<=0;
    if(cfg_stream0_bytes!=4384 || cfg_stream1_bytes!=4384)begin abort<=1;detail<=2;end
   end
   if(active && !fence)begin
    if(take && bad_input)begin abort<=1;detail<=1;end
    else begin
     if(start[0] && cr[0])armed[0]<=1;
     if(start[1] && cr[1])armed[1]<=1;
     if(take)begin
      symbols<=symbols+1'b1;dibit<=dibit+1'b1;
      shift0<={shift0[5:0],in_decision[0],in_decision[1]};shift1<={shift1[5:0],in_decision[2],in_decision[3]};
      if(dibit==3)begin pair0<={shift0[5:0],in_decision[0],in_decision[1]};pair1<={shift1[5:0],in_decision[2],in_decision[3]};pair_pending<=1;end
     end
     if(pair_fire)begin pair_pending<=0;bytes_sent<=bytes_sent+1'b1;end
     if(!arb_locked && |rv)begin arb_locked<=1;selected<=rv[0] && (!rv[1] || !prefer1)?1'b0:1'b1;streaming<=0;end
     if(result_fire)begin
      if(result_error==0)begin accepted_packets<=accepted_packets+1'b1;if(result_payload_bytes!=0)streaming<=1;end
      else erased_packets<=erased_packets+1'b1;
     end
     if(packet_finished)begin
      arb_locked<=0;streaming<=0;prefer1<=!selected;
      if(selected)begin armed[1]<=0;slot1<=slot1+1'b1;end
      else begin armed[0]<=0;slot0<=slot0+1'b1;end
     end
     if(symbols==17536 && bytes_sent==4384 && !pair_pending && slot0==8 && slot1==8 && !arb_locked && engine_active==0)begin active<=0;done_q<=1;end
    end
   end
  end
 end
endmodule
