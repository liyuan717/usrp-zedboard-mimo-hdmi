`timescale 1ns/1ps
// G0 candidate: opaque complete V3RX records -> fixed 32KiB DMA batches.
// Two header beats only, no whole-batch RAM. A trailer commits a contiguous
// valid prefix. Faulty/incomplete records never enter that prefix; subsequent
// batches resume. Local run=0 finishes one active batch and drains/discards an
// unstarted carry record without creating a second DMA batch. Global reset
// requires a coordinated DMA-owner reset, never a per-radio-packet action.
module dma_batch_stream_cost_probe #(
 parameter integer BATCH_BYTES=32768,BATCH_TRIGGER_CYCLES=50000,INPUT_TIMEOUT_CYCLES=50000
)(
 input wire clk,rst,run,
 input wire [63:0] s_axis_tdata,input wire [7:0] s_axis_tkeep,
 input wire s_axis_tvalid,output wire s_axis_tready,input wire s_axis_tlast,
 output reg [63:0] m_axis_tdata,output wire [7:0] m_axis_tkeep,
 output wire m_axis_tvalid,input wire m_axis_tready,output wire m_axis_tlast,
 output wire busy,output reg [31:0] batches_completed,records_forwarded,records_rejected,input_timeouts
);
 localparam WORDS=BATCH_BYTES/8,TRAILER_START=WORDS-4;
 localparam PW=$clog2(WORDS+1),TW=$clog2(BATCH_TRIGGER_CYCLES+1),IW=$clog2(INPUT_TIMEOUT_CYCLES+1);
 // The valid prefix excludes four header and four trailer words. Every
 // committed record consumes at least eight words, even without a payload.
 localparam VW=$clog2(WORDS),CW=$clog2((WORDS-8)/8+1);
 localparam IDLE=0,HEADER=1,WAIT_RECORD=2,HEADER2=3,RECORD0=4,RECORD1=5,BODY=6,PAD=7,TRAILER=8,DROP=9,DROP_ONLY=10;
 reg [3:0] state;reg [1:0] meta_beat;
 reg [PW-1:0] position;
 reg [TW-1:0] batch_age;
 reg [IW-1:0] input_age;
 reg [63:0] first_word,second_word;
 reg [6:0] record_words,record_word;
 reg [VW-1:0] valid_words;
 reg [CW-1:0] record_count;
 reg [2:0] batch_flags;
 reg [63:0] sequence_number;
 reg carry_record;
 wire batch_due=batch_age>=BATCH_TRIGGER_CYCLES-1;
 wire input_due=input_age>=INPUT_TIMEOUT_CYCLES-1;
 wire [31:0] declared_bytes=s_axis_tdata[31:0];
 wire header_shape=first_word[31:0]==32'h58523356 && first_word[47:32]==1 && first_word[63:48]==64 &&
  s_axis_tkeep==8'hff && !s_axis_tlast && declared_bytes>=64 && declared_bytes<=576 && declared_bytes[2:0]==0;
 wire [PW-1:0] space_words=PW'(TRAILER_START)-position;
 assign busy=state!=IDLE || carry_record;
 assign s_axis_tready=!rst &&
  ((state==WAIT_RECORD && run && !batch_due && space_words>=8) || state==HEADER2 ||
   (state==BODY && m_axis_tready) || state==DROP || state==DROP_ONLY);
 assign m_axis_tkeep=8'hff;
 assign m_axis_tvalid=!rst && (state==HEADER || state==RECORD0 || state==RECORD1 ||
  (state==BODY && s_axis_tvalid) || (state==PAD && position<TRAILER_START) || state==TRAILER);
 assign m_axis_tlast=state==TRAILER && meta_beat==3;
 wire take=s_axis_tvalid && s_axis_tready;
 wire send=m_axis_tvalid && m_axis_tready;
 always @* begin
  m_axis_tdata=0;
  case(state)
   HEADER:case(meta_beat)
    0:m_axis_tdata=64'h0020000142443356; // "V3DB",major1,32B header
    1:m_axis_tdata={32'd0,32'(BATCH_BYTES)};
    2:m_axis_tdata=sequence_number;
    3:m_axis_tdata=0;
   endcase
   RECORD0:m_axis_tdata=first_word;
   RECORD1:m_axis_tdata=second_word;
   BODY:m_axis_tdata=s_axis_tdata;
   TRAILER:case(meta_beat)
    0:m_axis_tdata={32'({valid_words,3'b000}),32'h45443356}; // "V3DE",valid prefix bytes
    1:m_axis_tdata={29'd0,batch_flags,32'(record_count)};
    2:m_axis_tdata=sequence_number;
    3:m_axis_tdata={records_rejected,32'd0};
   endcase
   default:m_axis_tdata=0;
  endcase
 end
 always @(posedge clk)begin
  if(rst)begin
   state<=IDLE;meta_beat<=0;position<=0;batch_age<=0;input_age<=0;
   first_word<=0;second_word<=0;record_words<=0;record_word<=0;
   valid_words<=0;record_count<=0;batch_flags<=0;sequence_number<=0;carry_record<=0;
   batches_completed<=0;records_forwarded<=0;records_rejected<=0;input_timeouts<=0;
  end else begin
   if(send)position<=position+1'b1;
   if(state!=IDLE && batch_age<BATCH_TRIGGER_CYCLES)batch_age<=batch_age+1'b1;
   if((state==HEADER2 || state==RECORD0 || state==RECORD1 || state==BODY ||
       state==DROP || state==DROP_ONLY) && input_age<INPUT_TIMEOUT_CYCLES)input_age<=input_age+1'b1;
   case(state)
    IDLE:begin
     if(carry_record && !run)begin
      records_rejected<=records_rejected+1'b1;carry_record<=0;input_age<=0;state<=DROP_ONLY;
     end else if(run || carry_record)begin
      state<=HEADER;meta_beat<=0;position<=0;batch_age<=0;valid_words<=0;record_count<=0;batch_flags<=0;
     end
    end
    HEADER:if(send)begin
     if(meta_beat==3)begin
      if(carry_record)begin
       carry_record<=0;input_age<=0;
       if(run)state<=RECORD0;
       else begin records_rejected<=records_rejected+1'b1;batch_flags<=5;state<=DROP;end
      end else state<=WAIT_RECORD;
     end else meta_beat<=meta_beat+1'b1;
    end
    WAIT_RECORD:begin
     if(!run || batch_due || space_words<8)begin state<=PAD;if(!run)batch_flags<=batch_flags|4;else if(batch_due)batch_flags<=batch_flags|2;end
     else if(take)begin
      first_word<=s_axis_tdata;input_age<=0;
      if(s_axis_tkeep!=8'hff || s_axis_tlast)begin
       records_rejected<=records_rejected+1'b1;batch_flags<=batch_flags|1;state<=s_axis_tlast?PAD:DROP;
      end else state<=HEADER2;
     end
    end
    HEADER2:begin
     if(input_due && !s_axis_tvalid)begin
      records_rejected<=records_rejected+1'b1;input_timeouts<=input_timeouts+1'b1;batch_flags<=batch_flags|3;state<=PAD;
     end else if(take)begin
      second_word<=s_axis_tdata;
      if(!header_shape)begin
       records_rejected<=records_rejected+1'b1;batch_flags<=batch_flags|1;input_age<=0;state<=s_axis_tlast?PAD:DROP;
      end else begin
       record_words<=declared_bytes[9:3];record_word<=2;
       if(!run)begin records_rejected<=records_rejected+1'b1;batch_flags<=batch_flags|5;input_age<=0;state<=DROP;end
       else if(declared_bytes[9:3]>space_words)begin carry_record<=1;state<=PAD;end
       else state<=RECORD0;
      end
     end
    end
    RECORD0:if(send)state<=RECORD1;
    RECORD1:if(send)state<=BODY;
    BODY:begin
     if(input_due && !s_axis_tvalid)begin
      records_rejected<=records_rejected+1'b1;input_timeouts<=input_timeouts+1'b1;batch_flags<=batch_flags|3;state<=PAD;
     end else if(send)begin
      if(s_axis_tkeep!=8'hff || s_axis_tlast!=(record_word==record_words-1'b1) || (input_due && !s_axis_tlast))begin
       records_rejected<=records_rejected+1'b1;batch_flags<=batch_flags|1;input_age<=0;state<=s_axis_tlast?PAD:DROP;
       if(input_due)begin input_timeouts<=input_timeouts+1'b1;batch_flags<=batch_flags|3;end
      end else if(s_axis_tlast)begin
       valid_words<=valid_words+VW'(record_words);record_count<=record_count+1'b1;records_forwarded<=records_forwarded+1'b1;state<=WAIT_RECORD;
      end else record_word<=record_word+1'b1;
     end
    end
    DROP,DROP_ONLY:begin
     if((take && s_axis_tlast) || input_due)begin
      state<=state==DROP?PAD:IDLE;
      if(input_due)begin input_timeouts<=input_timeouts+1'b1;if(state==DROP)batch_flags<=batch_flags|2;end
     end
    end
    PAD:begin
     if(position>=TRAILER_START || (send && position==TRAILER_START-1))begin meta_beat<=0;state<=TRAILER;end
    end
    TRAILER:if(send)begin
     if(meta_beat==3)begin state<=IDLE;batches_completed<=batches_completed+1'b1;sequence_number<=sequence_number+1'b1;end
     else meta_beat<=meta_beat+1'b1;
    end
    default:state<=IDLE;
   endcase
  end
 end
endmodule
