`timescale 1ns/1ps
// G0 R09/R10 candidate, not the final schema or DMA owner. Three whole-source
// slots provide bounded lookahead ordering, never whole-AU completeness.
// Admission waits at most 32768 cycles; overflow then drains/drops a whole
// SOURCE and emits a counted gap, rather than stalling FEC indefinitely.
// Only a complete, consistent 512B source becomes visible on AXIS. Once a
// record starts, fence/abort never revoke it or change a held AXIS beat.
module source_record_bridge_cost_probe #(
 parameter integer REORDER_CYCLES=4096,SOURCE_TIMEOUT_CYCLES=50000,ADMISSION_WAIT_CYCLES=32768
)(
 input wire clk,rst,fence,input wire [31:0] epoch,
 input wire source_valid,output wire source_ready,input wire [7:0] source_data,
 input wire [31:0] source_session,source_block,input wire [4:0] source_index,
 input wire [8:0] source_offset,input wire source_last,source_recovered,source_abort,
 input wire event_valid,output wire event_ready,input wire [31:0] event_session,event_block,
 input wire [2:0] event_code,input wire [19:0] event_missing,event_delivered,
 output wire [63:0] m_axis_tdata,output wire [7:0] m_axis_tkeep,
 output wire m_axis_tvalid,input wire m_axis_tready,output wire m_axis_tlast,
 output reg [31:0] sources_committed,sources_dropped,sources_bad,sources_aborted,
 output reg [31:0] sources_late_or_duplicate,events_dropped,records_completed,
 input wire control_valid,output wire control_ready,input wire [63:0] control_data,
 input wire [1:0] control_word,input wire control_last,
 input wire [31:0] control_session,control_superframe,input wire [95:0] control_position,
 output reg [31:0] controls_committed,controls_dropped
);
 localparam SLOT_COUNT=3;
 localparam FREE=0,COLLECT=1,COMMITTED=2,SENDING=3;
 localparam IDLE=0,HEADER=1,READ=2,PAYLOAD=3,GAP=4;
 localparam AGE_W=$clog2(REORDER_CYCLES+1)>0?$clog2(REORDER_CYCLES+1):1;
 localparam RX_W=$clog2(SOURCE_TIMEOUT_CYCLES+1)>0?$clog2(SOURCE_TIMEOUT_CYCLES+1):1;
 localparam WAIT_W=$clog2(ADMISSION_WAIT_CYCLES+1)>0?$clog2(ADMISSION_WAIT_CYCLES+1):1;
 reg [1:0] slot_state[0:SLOT_COUNT-1];
 reg [31:0] slot_session[0:SLOT_COUNT-1],slot_block[0:SLOT_COUNT-1],slot_epoch[0:SLOT_COUNT-1];
 reg [4:0] slot_index[0:SLOT_COUNT-1];reg slot_recovered[0:SLOT_COUNT-1];
 reg slot_control[0:SLOT_COUNT-1];
 // Admission permits a single live control through its last payload beat.
 // Its coordinate has the same lifetime regardless of the borrowed slot.
 reg [95:0] control_position_saved;
 reg control_busy;reg [1:0] control_slot;reg [1:0] control_next;
 reg [AGE_W-1:0] slot_age[0:SLOT_COUNT-1];
 (* ram_style="block" *) reg [63:0] payload_memory[0:SLOT_COUNT*64-1];
 reg [63:0] pack_word,read_word;
 reg rx_busy,rx_drop,rx_ignore;reg [1:0] rx_slot;
 reg [8:0] rx_next;
 reg [RX_W-1:0] rx_age;
 reg [WAIT_W-1:0] admission_age;
 reg wm_valid;reg [31:0] wm_epoch,wm_session,wm_block;reg [4:0] wm_index;
 reg event_pending;reg [31:0] es,eb,ee;reg [2:0] ec;reg [19:0] em;
 reg [31:0] loss_generation,reported_generation;
 reg [2:0] state;reg [3:0] header_beat;reg [5:0] payload_beat;
 reg [1:0] send_slot;reg [15:0] send_type;reg [511:0] header;
 reg event_turn,last_gap;
 reg [127:0] gap_payload;reg [63:0] sequence_number;
 wire [SLOT_COUNT-1:0] slot_free,slot_committed,slot_matches_source,slot_matches_event,slot_has_control;
 wire [69:0] slot_order_key[0:SLOT_COUNT-1];
 for(genvar i=0;i<SLOT_COUNT;i=i+1)begin: slot_flags
  assign slot_free[i]=slot_state[i]==FREE;
  assign slot_committed[i]=slot_state[i]==COMMITTED;
  assign slot_has_control[i]=!slot_free[i] && slot_control[i];
  assign slot_order_key[i]={slot_control[i],slot_epoch[i],slot_block[i],slot_index[i]};
  assign slot_matches_source[i]=!slot_free[i] && !slot_control[i] && slot_epoch[i]==epoch &&
   slot_session[i]==source_session && slot_block[i]==source_block && slot_index[i]==source_index;
  assign slot_matches_event[i]=!slot_free[i] && slot_epoch[i]==ee && slot_session[i]==es && slot_block[i]==eb;
 end
 wire have_free=|slot_free;
 wire [1:0] free_slot=slot_free[0]?2'd0:slot_free[1]?2'd1:2'd2;
 function automatic older(input [31:0] ae,ab,input [4:0] ai,
                           input [31:0] be,bb,input [4:0] bi);
  reg [31:0] d;begin
   d=ae-be;
   if(ae!=be)older=d[31];
   else if(ab!=bb)begin d=ab-bb;older=d[31];end
   else older=ai<bi;
  end
 endfunction
 function automatic precedes(input [69:0] a,b);
  begin
   if(a[69]!=b[69])precedes=!a[69];
   else precedes=older(a[68:37],a[36:5],a[4:0],b[68:37],b[36:5],b[4:0]);
  end
 endfunction
 // Only one slot can collect at once. Snapshot the two ordering
 // comparisons involving the new slot while its payload arrives.
 reg [1:0] compare_slot,compare_step;
 reg precedes_10,precedes_20,precedes_21;
 wire [1:0] compare_lower=compare_slot==0?2'd0:compare_step==1?2'd0:2'd1;
 wire [1:0] compare_upper=compare_slot==2 || compare_step==2?2'd2:2'd1;
 wire compare_precedes=precedes(slot_order_key[compare_upper],slot_order_key[compare_lower]);
 wire [1:0] older_pair=slot_committed[1] && (!slot_committed[0] || precedes_10)?2'd1:2'd0;
 wire third_before_pair=older_pair[0]?precedes_21:precedes_20;
 wire [1:0] first_slot=slot_committed[2] && (!(|slot_committed[1:0]) || third_before_pair)?2'd2:older_pair;
 wire multiple_committed=(&slot_committed[1:0]) || (slot_committed[2] && (|slot_committed[1:0]));
 wire source_due=(|slot_committed) && (multiple_committed || slot_age[first_slot]>=REORDER_CYCLES-1);
 wire input_duplicate=(wm_valid && epoch==wm_epoch && source_session==wm_session &&
  source_block==wm_block && source_index==wm_index) || (|slot_matches_source);
 wire selected_late=!slot_control[first_slot] && wm_valid && slot_epoch[first_slot]==wm_epoch && slot_session[first_slot]==wm_session &&
  older(slot_epoch[first_slot],slot_block[first_slot],slot_index[first_slot],wm_epoch,wm_block,wm_index);
 // COLLECT owns the recorded identity until this SOURCE commits or aborts.
 // Reuse the per-slot identity match already needed for duplicate detection.
 wire byte_shape=source_offset==rx_next && source_last==(rx_next==511) &&
  slot_matches_source[rx_slot] && source_recovered==slot_recovered[rx_slot];
 wire abort_rx=(fence || source_abort || (rx_busy && rx_age>=SOURCE_TIMEOUT_CYCLES-1));
 wire source_loss=rx_busy && !rx_drop && !rx_ignore &&
  (abort_rx || (source_valid && source_ready && !byte_shape));
 wire start_loss=source_valid && source_ready && !rx_busy &&
  (source_offset!=0 || source_index>=20 || source_last || (!input_duplicate && !have_free));
 wire event_loss=event_valid && event_pending;
 wire control_live=|slot_has_control;
 // At most one borrowed metadata slot: retain a slot for useful SOURCE.
 assign control_ready=!rst && !fence && (control_busy || (!rx_busy && have_free && !control_live));
 wire control_take=control_valid && control_ready;
 wire control_abort=fence || (control_busy && rx_age>=SOURCE_TIMEOUT_CYCLES-1);
 wire control_shape=control_busy?
  (control_word==control_next && control_last==(control_next==3) &&
   control_session==slot_session[control_slot] && control_superframe==slot_block[control_slot] &&
   control_position==control_position_saved):
  (control_word==0 && !control_last);
 assign source_ready=!rst && !abort_rx && !control_busy && !control_take && (rx_busy || have_free || input_duplicate ||
  source_offset!=0 || source_index>=20 || source_last || admission_age>=ADMISSION_WAIT_CYCLES-1);
 wire alloc_control=control_take && !control_abort && control_shape && !control_busy;
 wire alloc_source=source_valid && source_ready && !rx_busy && source_offset==0 &&
  source_index<20 && !source_last && !input_duplicate && have_free;
 assign event_ready=!rst;
 assign m_axis_tvalid=!rst && (state==HEADER || state==PAYLOAD || state==GAP);
 assign m_axis_tdata=state==HEADER?header[header_beat*64+:64]:state==PAYLOAD?read_word:gap_payload[payload_beat[0]*64+:64];
 assign m_axis_tkeep=8'hff;
 assign m_axis_tlast=(state==HEADER && header_beat==7 && send_type==2) ||
  (state==PAYLOAD && (payload_beat==63 || (send_type==3 && payload_beat==3))) || (state==GAP && payload_beat==1);
 wire sent=m_axis_tvalid && m_axis_tready;
 wire event_due=event_pending && !(|slot_matches_event);
 // Header ABI from the plan. Flags bit0=recovered, bit1=no sample coordinate,
 // bit2=no capture-time metadata, bit3=older than bounded reorder watermark.
 // Valid late repairs still deliver; FEC owns long-lived block deduplication.
 // epoch is the caller's delivery epoch; no
 // invented ADC/sample/capture coordinates. PS reads video time from SOURCE.
 function automatic [511:0] make_header(input [15:0] kind,flags,input [31:0] session,ep,block,
  input [15:0] index,bytes,input [31:0] status,input [63:0] seq,input [63:0] sample_tag);
  reg [511:0] h;begin h=0;
   h[31:0]=32'h58523356;h[47:32]=1;h[63:48]=64;
   h[95:64]=64+bytes;h[111:96]=kind;h[127:112]=flags;
   h[159:128]=session;h[191:160]=ep;h[255:192]=seq;
   h[319:256]=sample_tag;
   h[415:384]=block;h[431:416]=index;h[447:432]=bytes;h[479:448]=status;
   make_header=h;end
 endfunction
 integer n;
 // One shared write port and one synchronous read port. The third slot uses
 // another 512B in the same block RAM. SESSION controls borrow one slot.
 wire source_write=source_valid && source_ready && rx_busy && !abort_rx &&
  !rx_drop && !rx_ignore && byte_shape && rx_next[2:0]==7;
 wire control_write=control_take && !control_abort && control_shape;
 wire [7:0] write_address=control_write?
  {control_busy?control_slot:free_slot,4'd0,control_word}:{rx_slot,rx_next[8:3]};
 wire [63:0] write_data=control_write?control_data:{source_data,pack_word[55:0]};
 always @(posedge clk)begin
  if(control_write || source_write)payload_memory[write_address]<=write_data;
 end
 always @(posedge clk)begin
  if(rst)begin
   for(n=0;n<SLOT_COUNT;n=n+1)begin slot_state[n]<=FREE;slot_session[n]<=0;slot_block[n]<=0;slot_epoch[n]<=0;slot_index[n]<=0;slot_recovered[n]<=0;slot_age[n]<=0;slot_control[n]<=0;end
   control_busy<=0;control_slot<=0;control_next<=0;controls_committed<=0;controls_dropped<=0;
   compare_slot<=0;compare_step<=0;precedes_10<=0;precedes_20<=0;precedes_21<=0;
   control_position_saved<=0;
   pack_word<=0;read_word<=0;rx_busy<=0;rx_drop<=0;rx_ignore<=0;rx_slot<=0;rx_next<=0;rx_age<=0;admission_age<=0;
   wm_valid<=0;wm_epoch<=0;wm_session<=0;wm_block<=0;wm_index<=0;
   event_pending<=0;es<=0;eb<=0;ee<=0;ec<=0;em<=0;
   loss_generation<=0;reported_generation<=0;state<=IDLE;header_beat<=0;payload_beat<=0;send_slot<=0;send_type<=0;
   header<=0;gap_payload<=0;sequence_number<=0;event_turn<=0;last_gap<=0;sources_committed<=0;sources_dropped<=0;
   sources_bad<=0;sources_aborted<=0;sources_late_or_duplicate<=0;events_dropped<=0;records_completed<=0;
  end else begin
   if(alloc_control || alloc_source)begin compare_slot<=free_slot;compare_step<=1;end
   else if(compare_step!=0)begin
    if(compare_lower==1)precedes_21<=compare_precedes;
    else if(compare_upper==1)precedes_10<=compare_precedes;
    else precedes_20<=compare_precedes;
    compare_step<=compare_step==1?2'd2:2'd0;
   end
   if(source_valid && !source_ready && !abort_rx)begin if(admission_age<ADMISSION_WAIT_CYCLES)admission_age<=admission_age+1'b1;end
   else admission_age<=0;
   for(n=0;n<SLOT_COUNT;n=n+1)if(slot_state[n]==COMMITTED && slot_age[n]<REORDER_CYCLES)slot_age[n]<=slot_age[n]+1'b1;
   if(source_loss || start_loss || event_loss)loss_generation<=loss_generation+1'b1;
   if(event_valid)begin
    if(!event_pending)begin event_pending<=1;es<=event_session;eb<=event_block;ee<=epoch;ec<=event_code;em<=event_missing;end
    else events_dropped<=events_dropped+1'b1;
   end
   if((rx_busy && !abort_rx) || (control_busy && !control_abort))rx_age<=rx_age+1'b1;
   if(control_abort)begin
    if(control_busy)begin slot_state[control_slot]<=FREE;controls_dropped<=controls_dropped+1'b1;end
    control_busy<=0;
   end else if(control_take)begin
    if(!control_shape)begin
     if(control_busy)slot_state[control_slot]<=FREE;
     control_busy<=0;controls_dropped<=controls_dropped+1'b1;
    end else if(!control_busy)begin
     control_busy<=1;control_slot<=free_slot;control_next<=1;rx_age<=0;
     slot_state[free_slot]<=COLLECT;slot_session[free_slot]<=control_session;slot_block[free_slot]<=control_superframe;
     slot_epoch[free_slot]<=epoch;slot_index[free_slot]<=0;slot_recovered[free_slot]<=0;
     slot_control[free_slot]<=1;control_position_saved<=control_position;
    end else if(control_last)begin
     control_busy<=0;slot_state[control_slot]<=COMMITTED;slot_age[control_slot]<=0;controls_committed<=controls_committed+1'b1;
    end else control_next<=control_next+1'b1;
   end
   if(abort_rx)begin
    if(rx_busy && !rx_drop && !rx_ignore)begin slot_state[rx_slot]<=FREE;sources_aborted<=sources_aborted+1'b1;end
    rx_busy<=0;
   end else if(source_valid && source_ready)begin
    if(!rx_busy)begin
     rx_busy<=!source_last;rx_next<=1;rx_age<=0;rx_slot<=free_slot;
     rx_ignore<=input_duplicate;
     rx_drop<=source_offset!=0 || source_index>=20 || source_last || !have_free;
     if(source_offset!=0 || source_index>=20 || source_last)sources_bad<=sources_bad+1'b1;
     else if(input_duplicate)sources_late_or_duplicate<=sources_late_or_duplicate+1'b1;
     else if(!have_free)sources_dropped<=sources_dropped+1'b1;
     else begin
      slot_state[free_slot]<=COLLECT;slot_session[free_slot]<=source_session;slot_block[free_slot]<=source_block;
      slot_epoch[free_slot]<=epoch;slot_index[free_slot]<=source_index;slot_recovered[free_slot]<=source_recovered;
      slot_control[free_slot]<=0;
      pack_word[7:0]<=source_data;
     end
    end else if(!rx_drop && !rx_ignore)begin
     if(!byte_shape)begin slot_state[rx_slot]<=FREE;rx_drop<=1;sources_bad<=sources_bad+1'b1;end
     else begin
      pack_word[rx_next[2:0]*8+:8]<=source_data;
      if(source_last)begin slot_state[rx_slot]<=COMMITTED;slot_age[rx_slot]<=0;sources_committed<=sources_committed+1'b1;end
     end
     if(source_last || rx_next==511)rx_busy<=0;else rx_next<=rx_next+1'b1;
    end else if(source_last || source_offset==511)rx_busy<=0;
   end
   case(state)
    IDLE:begin
     header_beat<=0;payload_beat<=0;
     if(loss_generation!=reported_generation && (!last_gap || (!source_due && !event_due)))begin
      header<=make_header(4,6,0,epoch,0,16'hffff,16,loss_generation,sequence_number,0);
      gap_payload<={events_dropped,sources_aborted,sources_bad,sources_dropped};
      reported_generation<=loss_generation;send_type<=4;sequence_number<=sequence_number+1'b1;state<=HEADER;last_gap<=1;
     end else if(event_due && (!source_due || event_turn))begin
      header<=make_header(2,6,es,ee,eb,16'hffff,0,{9'd0,ec,em},sequence_number,0);
      event_pending<=0;send_type<=2;sequence_number<=sequence_number+1'b1;state<=HEADER;event_turn<=0;last_gap<=0;
     end else if(source_due)begin
      send_slot<=first_slot;slot_state[first_slot]<=SENDING;
      if(slot_control[first_slot])
       header<=make_header(3,4,slot_session[first_slot],slot_epoch[first_slot],slot_block[first_slot],16'hffff,32,control_position_saved[31:0],sequence_number,control_position_saved[95:32]);
      else header<=make_header(1,16'd6|{12'd0,selected_late,2'd0,slot_recovered[first_slot]},slot_session[first_slot],slot_epoch[first_slot],slot_block[first_slot],{11'd0,slot_index[first_slot]},512,0,sequence_number,0);
      send_type<=slot_control[first_slot]?3:1;sequence_number<=sequence_number+1'b1;state<=HEADER;event_turn<=1;last_gap<=0;
      if(selected_late)sources_late_or_duplicate<=sources_late_or_duplicate+1'b1;
      else if(!slot_control[first_slot])begin wm_valid<=1;wm_epoch<=slot_epoch[first_slot];wm_session<=slot_session[first_slot];wm_block<=slot_block[first_slot];wm_index<=slot_index[first_slot];end
     end
    end
    HEADER:if(sent)begin
     if(header_beat==7)begin
      if(send_type==2)begin state<=IDLE;records_completed<=records_completed+1'b1;end
      else state<=send_type==4?GAP:READ;
     end else header_beat<=header_beat+1'b1;
    end
    READ:begin read_word<=payload_memory[{send_slot,payload_beat}];state<=PAYLOAD;end
    PAYLOAD:if(sent)begin
     if(payload_beat==63 || (send_type==3 && payload_beat==3))begin slot_state[send_slot]<=FREE;state<=IDLE;records_completed<=records_completed+1'b1;end
     else begin payload_beat<=payload_beat+1'b1;state<=READ;end
    end
    GAP:if(sent)begin
     if(payload_beat==1)begin state<=IDLE;records_completed<=records_completed+1'b1;end
     else payload_beat<=payload_beat+1'b1;
    end
    default:state<=IDLE;
   endcase
  end
 end
 // event_delivered is upstream's FEC delivery bitmap, not a DMA-completion ACK.
 // Do not turn it into a claim that dropped SOURCE records reached software.
endmodule
