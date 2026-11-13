`timescale 1ns/1ps
// Candidate CRC-result -> fixed8 FEC ingress. A CRC-good periodic VRS3 session
// descriptor declares up to eight contiguous blocks. Bad headers never locate
// erasures. The parser is packet-local; a PHY abort preserves committed banks.
// This is not R09 ordering or atomic DMA. The one-entry terminal mailbox drops
// on overflow with an explicit counter instead of blocking the FEC engine.
// A CRC-good replacement range closes old out-of-range block admission.
// Seal loading jobs, emit their complete sources and missing-row status, then
// release normally. Queued/working owners finish; never overwrite a live bank.
module link_fec_ingress_cost_probe #(
 parameter integer MAX_JOB_CYCLES=1000000,BLOCK_DEADLINE_CYCLES=25000000,
 parameter GENERATOR_FILE="generator.hex",INVERSE_FILE="inverse.hex"
)(
 input wire clk,rst,packet_abort,
 input wire result_valid,output wire result_ready,input wire [2:0] result_error,
 input wire [255:0] result_header,input wire [15:0] result_payload_bytes,
 input wire payload_valid,output wire payload_ready,input wire [7:0] payload_data,
 input wire [15:0] payload_index,input wire payload_last,
 output wire source_valid,input wire source_ready,output wire [7:0] source_data,
 output wire [31:0] source_session,source_block,output wire [4:0] source_index,
 output wire [8:0] source_offset,output wire source_last,source_recovered,source_abort,
 output wire event_valid,input wire event_ready,output reg [31:0] event_session,event_block,
 output reg [2:0] event_code,output reg [19:0] event_missing,event_delivered,
 output reg [31:0] packets_accepted,packets_erased,packets_unmapped,
 output reg [31:0] descriptors_accepted,descriptors_rejected,range_busy,events_dropped,
 output wire [31:0] expired_blocks,
 input wire reference_valid,input wire [95:0] reference_position,
 output wire control_valid,input wire control_ready,output wire [63:0] control_data,
 output wire [1:0] control_word,output wire control_last,
 output wire [31:0] control_session,control_superframe,output wire [95:0] control_position,
 output reg [31:0] controls_emitted,controls_dropped
);
 localparam IDLE=0,START=1,DATA=2,DROP=3,DESC=4,APPLY=5,DECLARE=6,META=7;
 reg [2:0] state;
 reg event_pending;
 reg session_active,range_seen,retired_valid;
 reg [31:0] session_id,retired_session,range_first,range_serial;
 reg [3:0] range_count,declare_index;
 reg [7:0] bank_live,bank_known;
 reg [31:0] bank_block[0:7];
 reg [2:0] retire_bank;
 reg [255:0] descriptor;
 reg [31:0] meta_session,rx_block;
 reg [4:0] rx_index;
 reg [2:0] rx_bank;
 reg [15:0] meta_size,count;
 reg descriptor_bad;
 reg timebase_packet,meta_reference_valid;
 reg [95:0] meta_reference;
 reg [31:0] meta_superframe;
 reg [1:0] meta_word;
 reg return_declare;
 wire [7:0] kind=result_header[47:40];
 wire [31:0] header_session=result_header[95:64],header_block=result_header[223:192];
 wire [15:0] header_index=result_header[239:224];
 wire [2:0] header_bank=header_block[2:0];
 wire [31:0] header_range_offset=header_block-range_first;
 wire header_mapped=session_active && range_seen && header_session==session_id &&
  header_range_offset<{28'd0,range_count} && bank_live[header_bank] && bank_block[header_bank]==header_block;
 wire data_semantic=(kind==1 || kind==2) && result_payload_bytes==512 && header_index<32 &&
  (kind==1 ? header_index<20 : header_index>=20);
 wire descriptor_shape=!descriptor_bad && descriptor[31:0]==32'h33535256 &&
  descriptor[39:32]==1 && descriptor[47:40]==8'hf0 && !descriptor[48] &&
  descriptor[71:64]==20 && descriptor[79:72]==32 && descriptor[95:80]==512 &&
  descriptor[111:96]==1024 && descriptor[127:112]==600 &&
  descriptor[143:128]==20 && descriptor[159:144]==1 &&
  descriptor[199:192]>=1 && descriptor[199:192]<=8 && descriptor[207:200]==0;
 wire [31:0] new_serial=descriptor[255:224];
 wire [31:0] serial_delta=new_serial-range_serial;
 wire same_session=session_active && meta_session==session_id;
 wire serial_ok=!same_session || !range_seen ||
  (new_serial==range_serial && descriptor[191:160]==range_first && descriptor[199:192]=={4'd0,range_count}) ||
  (serial_delta!=0 && !serial_delta[31]);
 wire retired=retired_valid && meta_session==retired_session && !same_session;
 wire descriptor_ok=descriptor_shape && serial_ok && !retired;
 // VRT3 is a periodic forward control, separately protected by link CRC.
 // It never opens a bank or changes the active VRS3 profile/range/session.
 wire timebase_ok=!descriptor_bad && descriptor[31:0]==32'h33545256 &&
  descriptor[39:32]==1 && descriptor[47:40]==8'hf0 && descriptor[63:48]==1 &&
  $signed(descriptor[191:128])<=$signed(descriptor[255:192]) &&
  same_session && !retired;
 assign control_valid=!rst && state==META;
 assign control_data=descriptor[meta_word*64+:64];
 assign control_word=meta_word;assign control_last=meta_word==3;
 assign control_session=meta_session;assign control_superframe=meta_superframe;
 assign control_position=meta_reference;
 wire session_switch=state==APPLY && descriptor_ok && !same_session;
 wire core_rst=rst || session_switch;
 wire [31:0] declare_block=range_first+{28'd0,declare_index};
 wire [2:0] declare_bank=declare_block[2:0];
 wire [31:0] block_delta=declare_block-bank_block[declare_bank];
 wire declare_new=!bank_known[declare_bank] || (block_delta!=0 && !block_delta[31]);
 wire open_valid=state==DECLARE && declare_new && !bank_live[declare_bank];
 wire open_ready;
 wire capacity_miss=state==DECLARE && declare_new && (bank_live[declare_bank] || !open_ready);
 wire byte_shape=payload_index==count && payload_last==(count==511);
 wire bad_data=state==DATA && payload_valid && !byte_shape;
 wire core_packet_abort=packet_abort || bad_data;
 wire core_packet_ready,core_byte_ready;
 wire [2:0] core_source_bank,core_status_bank,core_code;
 wire [31:0] core_source_job,core_status_job,core_elapsed;
 wire [19:0] core_missing,core_delivered;
 wire core_source_valid,core_source_abort,core_status_valid;
 reg release_pending;reg [2:0] release_bank;reg [31:0] release_job;
 wire release_ready;
 wire seal_ready;
 wire [31:0] retire_offset=bank_block[retire_bank]-range_first;
 wire retire_candidate=session_active && range_seen && bank_live[retire_bank] &&
                       retire_offset>={28'd0,range_count};
 // An ineligible queued/working owner cannot repeatedly block packet headers.
 wire seal_valid=!rst && retire_candidate && seal_ready;
 assign result_ready=!rst && !packet_abort && state==IDLE;
 assign payload_ready=!rst && !packet_abort &&
  ((state==DATA && (bad_data || core_byte_ready)) || state==DROP || state==DESC);
 assign source_valid=core_source_valid;
 assign source_session=session_id;assign source_block=core_source_job;
 assign source_abort=core_source_abort || (!rst && session_switch && session_active);
 assign event_valid=!rst && event_pending;
 fec_stream_recovery_cost_probe #(.MAX_JOB_CYCLES(MAX_JOB_CYCLES),.BLOCK_DEADLINE_CYCLES(BLOCK_DEADLINE_CYCLES),
  .GENERATOR_FILE(GENERATOR_FILE),.INVERSE_FILE(INVERSE_FILE)) fec(
  .clk(clk),.rst(core_rst),.open_valid(open_valid),.open_ready(open_ready),.open_bank(declare_bank),.open_job(declare_block),
  .seal_valid(seal_valid),.seal_ready(seal_ready),.seal_bank(retire_bank),.seal_job(bank_block[retire_bank]),
  .release_valid(release_pending),.release_ready(release_ready),.release_bank(release_bank),.release_job(release_job),
  .cancel_valid(1'b0),.cancel_bank(3'd0),.cancel_job(32'd0),
  .packet_valid(!rst && !packet_abort && state==START),.packet_ready(core_packet_ready),
  .packet_bank(rx_bank),.packet_job(rx_block),.packet_index(rx_index),
  .byte_valid(!rst && !packet_abort && state==DATA && payload_valid && byte_shape),.byte_ready(core_byte_ready),
  .byte_data(payload_data),.byte_last(payload_last),.crc_good(1'b1),.packet_abort(core_packet_abort),
  .source_valid(core_source_valid),.source_ready(source_ready),.source_data(source_data),
  .source_bank(core_source_bank),.source_job(core_source_job),.source_index(source_index),.source_offset(source_offset),
  .source_last(source_last),.source_recovered(source_recovered),.source_abort(core_source_abort),
  .status_valid(core_status_valid),.status_ready(1'b1),.status_bank(core_status_bank),.status_job(core_status_job),
  .status_code(core_code),.missing_sources(core_missing),.delivered_sources(core_delivered),.elapsed_cycles(core_elapsed),
  .packets_committed(),.packets_bad_crc(),.packets_bad_length(),.packets_stale(),.packets_duplicate(),
  .jobs_cancelled(),.packets_aborted(),.blocks_expired(expired_blocks));
 integer b;
 always @(posedge clk)begin
  if(rst)begin
   state<=IDLE;session_active<=0;range_seen<=0;retired_valid<=0;session_id<=0;retired_session<=0;
   range_first<=0;range_serial<=0;range_count<=0;declare_index<=0;bank_live<=0;bank_known<=0;retire_bank<=0;
   for(b=0;b<8;b=b+1)bank_block[b]<=0;
   descriptor<=0;meta_session<=0;rx_block<=0;rx_index<=0;rx_bank<=0;meta_size<=0;count<=0;descriptor_bad<=0;
   timebase_packet<=0;meta_reference_valid<=0;meta_reference<=0;meta_superframe<=0;meta_word<=0;return_declare<=0;
   controls_emitted<=0;controls_dropped<=0;
   release_pending<=0;release_bank<=0;release_job<=0;
   event_pending<=0;event_session<=0;event_block<=0;event_code<=0;event_missing<=0;event_delivered<=0;
   packets_accepted<=0;packets_erased<=0;packets_unmapped<=0;descriptors_accepted<=0;
   descriptors_rejected<=0;range_busy<=0;events_dropped<=0;
  end else begin
   retire_bank<=retire_bank+1'b1;
   if(event_valid && event_ready)event_pending<=0;
   if(core_status_valid || capacity_miss)begin
    if(!event_valid || event_ready)begin
     event_pending<=1;event_session<=session_id;
     event_block<=core_status_valid?core_status_job:declare_block;
     event_code<=core_status_valid?core_code:3'd6;
     event_missing<=core_status_valid?core_missing:20'hfffff;
     event_delivered<=core_status_valid?core_delivered:20'd0;
    end else events_dropped<=events_dropped+1'b1;
    if(core_status_valid && capacity_miss)events_dropped<=events_dropped+((event_valid&&!event_ready)?2:1);
   end
   if(release_pending && release_ready)begin release_pending<=0;bank_live[release_bank]<=0;end
   if(core_status_valid)begin release_pending<=1;release_bank<=core_status_bank;release_job<=core_status_job;end
   case(state)
    IDLE:if(result_valid && result_ready)begin
     count<=0;meta_size<=result_payload_bytes;meta_session<=header_session;
     if(result_error!=0)begin packets_erased<=packets_erased+1'b1;end
     else if((kind==3 || kind==5) && result_payload_bytes>=32 && result_payload_bytes<=512)begin
      descriptor<=0;descriptor_bad<=0;state<=DESC;timebase_packet<=kind==5;
      meta_reference_valid<=reference_valid;meta_reference<=reference_position;
      meta_superframe<=result_header[127:96];
     end else if(data_semantic && header_mapped)begin
      rx_block<=header_block;rx_bank<=header_bank;rx_index<=header_index[4:0];state<=START;
     end else begin
      packets_unmapped<=packets_unmapped+1'b1;
      if(result_payload_bytes!=0)state<=DROP;
     end
    end
    START:if(core_packet_ready && !packet_abort)state<=DATA;
    DATA:if(payload_valid && payload_ready)begin
     if(bad_data)begin packets_unmapped<=packets_unmapped+1'b1;state<=payload_last?IDLE:DROP;end
     else if(payload_last)begin state<=IDLE;packets_accepted<=packets_accepted+1'b1;end
     else count<=count+1'b1;
    end
    DROP:if(payload_valid && payload_ready && payload_last)state<=IDLE;
    DESC:if(payload_valid && payload_ready)begin
     if(payload_index!=count || payload_last!=(count==meta_size-1'b1))descriptor_bad<=1;
     // Valid descriptors deliver the first 32 bytes in order. One shared
     // enable replaces a separate decoded write enable for every byte.
     if(count<32)descriptor<={payload_data,descriptor[255:8]};
     if(payload_last)begin
      if(payload_index!=count || count!=meta_size-1'b1)begin state<=IDLE;descriptors_rejected<=descriptors_rejected+1'b1;end
      else state<=APPLY;
     end else count<=count+1'b1;
    end
    APPLY:begin
     if(timebase_packet)begin
      state<=IDLE;
      if(timebase_ok && meta_reference_valid)begin
       meta_word<=0;return_declare<=0;state<=META;
      end else if(descriptor[31:0]==32'h33545256)controls_dropped<=controls_dropped+1'b1;
      else packets_unmapped<=packets_unmapped+1'b1;
     end else if(!descriptor_ok)begin state<=IDLE;descriptors_rejected<=descriptors_rejected+1'b1;end
     else begin
      if(!same_session)begin
       if(session_active)begin retired_session<=session_id;retired_valid<=1;end
       session_id<=meta_session;session_active<=1;bank_live<=0;bank_known<=0;release_pending<=0;
      end
      range_first<=descriptor[191:160];range_count<=descriptor[195:192];range_serial<=new_serial;
      range_seen<=1;declare_index<=0;descriptors_accepted<=descriptors_accepted+1'b1;state<=DECLARE;
      // Bit1 advertises this candidate's periodic forward timebase. Legacy
      // descriptors keep byte-identical records and do not add metadata.
      if(descriptor[49] && meta_reference_valid)begin meta_word<=0;return_declare<=1;state<=META;end
     end
    end
    META:begin
     // A busy slot at admission drops metadata, never stalls the RF stream.
     // Once word0 is accepted the bridge reserves one existing slot and
     // guarantees the remaining three words without downstream backpressure.
     if(!control_ready && meta_word==0)begin
      controls_dropped<=controls_dropped+1'b1;state<=return_declare?DECLARE:IDLE;
     end else if(control_ready)begin
      if(meta_word==3)begin controls_emitted<=controls_emitted+1'b1;state<=return_declare?DECLARE:IDLE;end
      else meta_word<=meta_word+1'b1;
     end
    end
    DECLARE:begin
     if(open_valid && open_ready)begin bank_live[declare_bank]<=1;bank_known[declare_bank]<=1;bank_block[declare_bank]<=declare_block;end
     if(capacity_miss)range_busy<=range_busy+1'b1;
     if(declare_index+1>=range_count)state<=IDLE;else declare_index<=declare_index+1'b1;
    end
    default:state<=IDLE;
   endcase
   // Only a partial packet is invalidated by PHY loss; previously committed
   // sources and the complete descriptor remain useful to autonomous recovery.
   if(packet_abort && (state==START || state==DATA || state==DROP || state==DESC))state<=IDLE;
  end
 end
endmodule
