`timescale 1ns/1ps
// Bounded VRL3 validator. The outer PHY/profile supplies the packet boundary;
// an untrusted link payload_bytes field never determines framing or allocation.
// Store payload privately, publish metadata/data only after CRC and bounds pass.
module link_packet_cost_probe #(
 parameter SYMBOL_BYTES=512,MAX_PAYLOAD_BYTES=512,FEC_K=20,FEC_N=32
)(
 input wire clk,rst,cancel,cfg_valid,output wire cfg_ready,
 input wire [31:0] cfg_session,cfg_superframe,input wire [15:0] cfg_packet_bytes,
 input wire in_valid,output wire in_ready,input wire [7:0] in_data,
 output wire result_valid,input wire result_ready,output reg [2:0] result_error,
 output wire [255:0] result_header,output wire [15:0] result_payload_bytes,
 output wire out_valid,input wire out_ready,output wire [7:0] out_data,
 output wire [15:0] out_index,output wire out_last,output wire active
);
 localparam IDLE=0,COLLECT=1,RESULT=2,LOAD=3,SEND=4;
 localparam ADDRESS_BITS=$clog2(MAX_PAYLOAD_BYTES)>0?$clog2(MAX_PAYLOAD_BYTES):1;
 reg [2:0] state;reg [15:0] position,packet_size,payload_size,read_index;
 reg [31:0] session,superframe,crc,received_crc;reg [255:0] header;
 (* ram_style="block" *) reg [7:0] memory[0:MAX_PAYLOAD_BYTES-1];
 reg [7:0] read_word;
 wire [15:0] crc_begin=packet_size-16'd4;
 assign cfg_ready=!rst && !cancel && state==IDLE;
 assign in_ready=!rst && !cancel && state==COLLECT;
 wire take=in_valid && in_ready;
 assign active=state!=IDLE;
 assign result_valid=!rst && !cancel && state==RESULT;
 assign result_header=header;
 assign result_payload_bytes=result_error==0?payload_size:16'd0;
 assign out_valid=!rst && !cancel && state==SEND;
 assign out_data=read_word;assign out_index=read_index;
 assign out_last=read_index==payload_size-16'd1;
 function automatic [31:0] crc_byte(input [31:0] c,input [7:0] b);
  reg [31:0] v;integer j;
  begin v=c^{24'd0,b};for(j=0;j<8;j=j+1)v=(v>>1)^(v[0]?32'hedb88320:32'd0);crc_byte=v;end
 endfunction
 wire [7:0] kind=header[47:40];wire [15:0] symbol_index=header[239:224];
 wire data_kind=kind==1 || kind==2;
 wire bad_header=header[31:0]!=32'h334c5256 || header[39:32]!=1 ||
  header[95:64]!=session || header[127:96]!=superframe || header[255:240]!=payload_size ||
  !(data_kind || kind==3 || kind==5) ||
  (data_kind && (payload_size!=SYMBOL_BYTES || symbol_index>=FEC_N ||
                (kind==1?symbol_index>=FEC_K:symbol_index<FEC_K)));
 // Memory is never reset. CRC/descriptor state controls all publication.
 always @(posedge clk)begin
  if(take && position>=32 && position<crc_begin)
   memory[ADDRESS_BITS'(position-16'd32)]<=in_data;
  if(state==LOAD && !rst && !cancel)read_word<=memory[ADDRESS_BITS'(read_index)];
 end
 always @(posedge clk)begin
  if(rst || cancel)begin
   state<=IDLE;position<=0;packet_size<=0;payload_size<=0;read_index<=0;
   session<=0;superframe<=0;crc<=32'hffffffff;received_crc<=0;header<=0;result_error<=0;
  end else begin
   if(cfg_valid && cfg_ready)begin
    session<=cfg_session;superframe<=cfg_superframe;packet_size<=cfg_packet_bytes;
    payload_size<=cfg_packet_bytes-16'd36;position<=0;read_index<=0;
    crc<=32'hffffffff;received_crc<=0;header<=0;result_error<=0;
    if(cfg_packet_bytes<36 || cfg_packet_bytes>MAX_PAYLOAD_BYTES+36)begin
     payload_size<=0;result_error<=3;state<=RESULT;
    end else state<=COLLECT;
   end
   if(take)begin
    position<=position+1'b1;
    if(position<32)header[position[4:0]*8+:8]<=in_data;
    if(position<crc_begin)crc<=crc_byte(crc,in_data);
    else received_crc<={in_data,received_crc[31:8]};
    if(position==packet_size-1'b1)begin
     state<=RESULT;
     if({in_data,received_crc[31:8]}!=~crc)result_error<=1;
     else if(bad_header)result_error<=2;
     else result_error<=0;
    end
   end
   if(result_valid && result_ready)begin
    if(result_error!=0 || payload_size==0)state<=IDLE;
    else begin read_index<=0;state<=LOAD;end
   end
   if(state==LOAD)state<=SEND;
   if(out_valid && out_ready)begin
    if(out_last)state<=IDLE;
    else begin read_index<=read_index+1'b1;state<=LOAD;end
   end
  end
 end
endmodule
