`timescale 1ns/1ps
// Streamed RS20/32/S512 candidate. Reuses the measured inverse/RAM/GF engine.
// Whole validated packet commit -> early original sources -> automatic k queue.
// A delivered source bit is set only on its final accepted byte. Recovery omits
// those bits. OPEN/job mapping, range/session admission and atomic DMA remain
// the caller's contracts; partial source records are revoked by source_abort.
module fec_stream_recovery_cost_probe #(
 parameter integer MAX_JOB_CYCLES=1000000, BLOCK_DEADLINE_CYCLES=25000000,
 parameter AUTO_RECOVERY=1,EARLY_SOURCE=1,
 parameter GENERATOR_FILE="generator.hex", INVERSE_FILE="inverse.hex"
)(
 input wire clk,rst,
 input wire open_valid, output wire open_ready,
 input wire [2:0] open_bank, input wire [31:0] open_job,
 input wire seal_valid, output wire seal_ready,
 input wire [2:0] seal_bank, input wire [31:0] seal_job,
 input wire release_valid, output wire release_ready,
 input wire [2:0] release_bank, input wire [31:0] release_job,
 input wire cancel_valid, input wire [2:0] cancel_bank, input wire [31:0] cancel_job,
 input wire packet_valid, output wire packet_ready,
 input wire [2:0] packet_bank, input wire [31:0] packet_job, input wire [4:0] packet_index,
 input wire byte_valid, output wire byte_ready, input wire [7:0] byte_data,
 input wire byte_last, crc_good,
 output wire source_valid, input wire source_ready, output wire [7:0] source_data,
 output wire [2:0] source_bank, output wire [31:0] source_job,
 output wire [4:0] source_index, output wire [8:0] source_offset,
 output wire source_last, source_recovered,
 output wire status_valid, input wire status_ready, output wire [2:0] status_bank,
 output wire [31:0] status_job, output reg [2:0] status_code,
 output reg [19:0] missing_sources, output reg [31:0] elapsed_cycles,
 output reg [31:0] packets_committed, packets_bad_crc, packets_bad_length,
 output reg [31:0] packets_stale, packets_duplicate, jobs_cancelled,
 input wire packet_abort,output wire source_abort,output wire [19:0] delivered_sources,
 output reg [31:0] packets_aborted,blocks_expired
);
 localparam integer BANKS=8,AW=17;
 localparam FREE=0, LOADING=1, QUEUED=2, WORKING=3, TERMINAL=4, EXPIRED=5, REPORTING=6;
 localparam IDLE=0, SELECT=1, INV_START=2, INV_FEED=3, INV_COLLECT=4,
  RESTORE_ROW=5, MAC=6, MAC_RESULT=7, WRITEBACK=8, VERIFY_ROW=9,
  PARITY_READ=10, PARITY_COMPARE=11, OUTPUT_READ=12, OUTPUT_WAIT=13,
  OUTPUT_ROW=14, FINISH=15;
 reg [2:0] bank_state[0:BANKS-1];
 reg [31:0] bank_job[0:BANKS-1], bank_mask[0:BANKS-1];
 localparam DEADLINE_BITS=$clog2(BLOCK_DEADLINE_CYCLES+1)>0?$clog2(BLOCK_DEADLINE_CYCLES+1):1;
 localparam [DEADLINE_BITS-1:0] DEADLINE_INTERVAL=DEADLINE_BITS'(BLOCK_DEADLINE_CYCLES);
 reg [DEADLINE_BITS-1:0] deadline_clock;
 reg [DEADLINE_BITS-1:0] bank_deadline[0:BANKS-1];
 wire [DEADLINE_BITS-1:0] open_deadline=deadline_clock+DEADLINE_INTERVAL;
 reg [19:0] bank_delivered[0:BANKS-1];reg [5:0] bank_count[0:BANKS-1];
 wire [BANKS-1:0] expiring,queued;
 genvar q;generate for(q=0;q<BANKS;q=q+1)begin: expiry
  // Live states are contiguous and expiry consumes the match on that edge.
  // The interval fits in one clock wrap, so equality preserves the exact edge.
  assign expiring[q]=(bank_state[q]==LOADING || bank_state[q]==QUEUED || bank_state[q]==WORKING) && deadline_clock==bank_deadline[q];
  assign queued[q]=bank_state[q]==QUEUED;
 end endgenerate
 reg early_mode;
 reg [3:0] state;
 reg [2:0] active_bank, scan_bank;
 reg [31:0] active_job, received_mask;
 reg [19:0] deliver_mask;
 reg [5:0] scan_index, selected_count, row;
 reg [4:0] selected[0:19], col, feed_index;
 reg [8:0] offset;
 reg verify_phase;
 reg [7:0] result_byte;
 (* ram_style="distributed" *) reg [7:0] coefficients[0:639];
 reg [7:0] generator_rom[0:1023];
 initial $readmemh(GENERATOR_FILE,generator_rom);

 reg packet_active, packet_drop, packet_overlong;
 reg [2:0] rx_bank;
 reg [31:0] rx_job;
 reg [4:0] rx_index;
 reg [8:0] rx_offset;
 wire cancel_match=cancel_valid && bank_state[cancel_bank]!=FREE && bank_job[cancel_bank]==cancel_job;
 wire cancel_core=cancel_match && state!=IDLE && cancel_bank==active_bank && cancel_job==active_job;
 wire watchdog=state!=IDLE && state!=FINISH && elapsed_cycles>=MAX_JOB_CYCLES-1;
 wire active_deadline=state!=IDLE && state!=FINISH && expiring[active_bank];
 wire stop_core=rst || cancel_core || watchdog || active_deadline;
 assign source_abort=!rst && (cancel_core || watchdog || active_deadline);
 assign delivered_sources=bank_delivered[active_bank];
 assign open_ready=!rst && bank_state[open_bank]==FREE && !(cancel_valid && cancel_bank==open_bank);
 assign seal_ready=!rst && bank_state[seal_bank]==LOADING && !expiring[seal_bank] && bank_job[seal_bank]==seal_job &&
                   !(packet_active && rx_bank==seal_bank) && !(cancel_match && cancel_bank==seal_bank);
 assign release_ready=!rst && bank_state[release_bank]==TERMINAL && bank_job[release_bank]==release_job;
 // Seal and packet header are serialized for one cycle; bytes in another bank
 // still arrive while a sealed bank is decoded or its output is backpressured.
 assign packet_ready=!rst && !packet_active && !seal_valid;
 assign byte_ready=!rst && packet_active && !packet_abort;
 // Headers and payload bytes cannot be accepted on the same edge. Share the
 // bank lookup while retaining the saved owner for every payload byte.
 wire [2:0] receive_bank=packet_active?rx_bank:packet_bank;
 wire [31:0] receive_job=packet_active?rx_job:packet_job;
 wire rx_owner=bank_state[receive_bank]==LOADING && bank_job[receive_bank]==receive_job &&
               !(cancel_match && cancel_bank==receive_bank) && !expiring[receive_bank];
 wire commit_packet=byte_valid && byte_ready && byte_last && rx_offset==511 && !packet_overlong && crc_good && !packet_drop && rx_owner;
 wire queue_now=AUTO_RECOVERY && commit_packet && bank_count[rx_bank]==19;
 wire recovery_waiting=|queued || queue_now;
 wire load_we=byte_valid && byte_ready && !packet_drop && !packet_overlong && rx_owner;
 wire [AW-1:0] load_addr={rx_bank,rx_index,rx_offset};

 wire inv_sr,inv_ir,inv_cv,inv_last,inv_dv;
 wire [7:0] inv_data;
 wire [4:0] inv_row,inv_col;
 wire [1:0] inv_code;
 wire [15:0] inv_job;
 wire [31:0] inv_elapsed;
 fec_inverse_cost_probe #(.GENERATOR_FILE(GENERATOR_FILE),.INVERSE_FILE(INVERSE_FILE)) inverse_engine(
  .clk(clk),.rst(rst),.cancel(cancel_core || watchdog || active_deadline),
  .start_valid(state==INV_START && !stop_core),.start_ready(inv_sr),.job_id(active_job[15:0]),
  .index_valid(state==INV_FEED && !stop_core),.index_ready(inv_ir),.index_data({1'b0,selected[feed_index]}),
  .coeff_valid(inv_cv),.coeff_ready(state==INV_COLLECT && !stop_core),.coeff_data(inv_data),
  .coeff_row(inv_row),.coeff_col(inv_col),.coeff_last(inv_last),
  .done_valid(inv_dv),.done_ready(state==INV_COLLECT && !stop_core),.done_code(inv_code),
  .active_job(inv_job),.elapsed_cycles(inv_elapsed));
 always @(posedge clk) if(!stop_core && state==INV_COLLECT && inv_cv)
  coefficients[{inv_row,5'b00000}+inv_col]<=inv_data;

 reg op_valid,op_write,op_first,op_last;
 reg [AW-1:0] op_addr;
 reg [7:0] op_data,op_coefficient;
 wire op_ready,mac_valid;
 wire [7:0] mac_data;
 wire mac_ready=!stop_core && ((state==MAC_RESULT)||(state==PARITY_COMPARE)||
                             (state==OUTPUT_WAIT && source_ready));
 fec_memory_cost_probe #(.BANKS(BANKS),.AW(AW)) storage(
  .clk(clk),.rst(stop_core),.load_we(load_we),.load_addr(load_addr),.load_data(byte_data),
  .op_valid(op_valid && !stop_core),.op_ready(op_ready),.op_write(op_write),.op_addr(op_addr),
  .op_data(op_data),.coefficient(op_coefficient),.first(op_first),.last(op_last),
  .out_valid(mac_valid),.out_ready(mac_ready),.out_data(mac_data));
 always @* begin
  op_valid=0;op_write=0;op_first=1;op_last=1;
  op_addr={active_bank,row[4:0],offset};op_data=result_byte;op_coefficient=1;
  case(state)
   MAC:begin
    op_valid=1;op_first=col==0;op_last=col==19;
    op_addr=verify_phase?{active_bank,col,offset}:{active_bank,selected[col],offset};
    op_coefficient=verify_phase?generator_rom[{row[4:0],5'b00000}+col]:coefficients[{row[4:0],5'b00000}+col];
   end
   WRITEBACK:begin op_valid=1;op_write=1;end
   PARITY_READ,OUTPUT_READ:op_valid=1;
   default:begin end
  endcase
 end
 assign source_valid=!stop_core && state==OUTPUT_WAIT && mac_valid;
 assign source_data=mac_data;
 assign source_bank=active_bank;
 assign source_job=active_job;
 assign source_index=row[4:0];
 assign source_offset=offset;
 assign source_last=offset==511;
 assign source_recovered=!received_mask[row[4:0]];
 assign status_valid=!rst && !cancel_core && state==FINISH;
 assign status_bank=active_bank;
 assign status_job=active_job;

 integer b;
 always @(posedge clk) begin
  if(rst) begin
   for(b=0;b<BANKS;b=b+1) begin bank_state[b]<=FREE;bank_job[b]<=0;bank_mask[b]<=0;bank_count[b]<=0;bank_delivered[b]<=0;end
   deadline_clock<=0;
   state<=IDLE;active_bank<=0;scan_bank<=0;active_job<=0;received_mask<=0;deliver_mask<=0;
   scan_index<=0;selected_count<=0;row<=0;col<=0;feed_index<=0;offset<=0;
   verify_phase<=0;result_byte<=0;status_code<=0;missing_sources<=0;elapsed_cycles<=0;
   packet_active<=0;packet_drop<=0;packet_overlong<=0;rx_bank<=0;rx_job<=0;rx_index<=0;rx_offset<=0;
   packets_committed<=0;packets_bad_crc<=0;packets_bad_length<=0;packets_stale<=0;packets_duplicate<=0;jobs_cancelled<=0;packets_aborted<=0;blocks_expired<=0;early_mode<=0;
  end else begin
   deadline_clock<=deadline_clock+1'b1;
   for(b=0;b<BANKS;b=b+1) if(expiring[b])bank_state[b]<=EXPIRED;
   // Count all banks expiring on this edge; each leaves its aged state once.
   if(|expiring)blocks_expired<=blocks_expired+$countones(expiring);
   if(open_valid && open_ready) begin
    bank_state[open_bank]<=LOADING;bank_job[open_bank]<=open_job;bank_mask[open_bank]<=0;bank_count[open_bank]<=0;bank_delivered[open_bank]<=0;
    bank_deadline[open_bank]<=open_deadline;
   end
   if(seal_valid && seal_ready) bank_state[seal_bank]<=QUEUED;
   if(release_valid && release_ready) begin bank_state[release_bank]<=FREE;bank_mask[release_bank]<=0;end
   if(packet_valid && packet_ready) begin
    packet_active<=1;rx_bank<=packet_bank;rx_job<=packet_job;rx_index<=packet_index;
    rx_offset<=0;packet_overlong<=0;
    packet_drop<=!rx_owner || bank_mask[packet_bank][packet_index];
    if(!rx_owner) packets_stale<=packets_stale+1;
    else if(bank_mask[packet_bank][packet_index]) packets_duplicate<=packets_duplicate+1;
   end
   if(byte_valid && byte_ready) begin
    if(byte_last) begin
     packet_active<=0;
     if(rx_offset!=511 || packet_overlong) packets_bad_length<=packets_bad_length+1;
     else if(!crc_good) packets_bad_crc<=packets_bad_crc+1;
     else if(!packet_drop && rx_owner) begin
      bank_mask[rx_bank][rx_index]<=1;bank_count[rx_bank]<=bank_count[rx_bank]+1'b1;packets_committed<=packets_committed+1;
      if(AUTO_RECOVERY && bank_count[rx_bank]==19)bank_state[rx_bank]<=QUEUED;
     end
    end else if(rx_offset==511) packet_overlong<=1;
    else rx_offset<=rx_offset+1;
   end

   if(packet_abort && packet_active)begin packet_active<=0;packet_drop<=1;rx_offset<=0;packets_aborted<=packets_aborted+1'b1;end

   if(state!=IDLE && state!=FINISH) elapsed_cycles<=elapsed_cycles+1;
   case(state)
    IDLE:begin
     scan_bank<=scan_bank+1;
     if(bank_state[scan_bank]==EXPIRED && !(cancel_match && cancel_bank==scan_bank))begin
      active_bank<=scan_bank;active_job<=bank_job[scan_bank];elapsed_cycles<=0;
      status_code<=5;missing_sources<=~bank_delivered[scan_bank];bank_state[scan_bank]<=REPORTING;state<=FINISH;early_mode<=0;
     end else if(bank_state[scan_bank]==QUEUED && !expiring[scan_bank] && !(cancel_match && cancel_bank==scan_bank)) begin
      active_bank<=scan_bank;active_job<=bank_job[scan_bank];received_mask<=bank_mask[scan_bank];
      bank_state[scan_bank]<=WORKING;elapsed_cycles<=0;status_code<=0;missing_sources<=0;
      scan_index<=0;selected_count<=0;row<=20;deliver_mask<=~bank_delivered[scan_bank];early_mode<=0;
      if(bank_mask[scan_bank][19:0]==20'hfffff) state<=VERIFY_ROW;
      else state<=SELECT;
     end else if(EARLY_SOURCE && !recovery_waiting && bank_state[scan_bank]==LOADING && !expiring[scan_bank] &&
                 |(bank_mask[scan_bank][19:0] & ~bank_delivered[scan_bank]) && !(cancel_match && cancel_bank==scan_bank))begin
      active_bank<=scan_bank;active_job<=bank_job[scan_bank];received_mask<=bank_mask[scan_bank];
      deliver_mask<=bank_mask[scan_bank][19:0] & ~bank_delivered[scan_bank];row<=0;early_mode<=1;elapsed_cycles<=0;state<=OUTPUT_ROW;
     end
    end
    SELECT:begin
     if(received_mask[scan_index]) begin selected[selected_count]<=scan_index[4:0];selected_count<=selected_count+1;end
     if(received_mask[scan_index] && selected_count==19) state<=INV_START;
     else if(scan_index==31) begin
      status_code<=1;missing_sources<=~received_mask[19:0];deliver_mask<=received_mask[19:0] & ~bank_delivered[active_bank];row<=0;state<=OUTPUT_ROW;
     end else scan_index<=scan_index+1;
    end
    INV_START:if(inv_sr) begin feed_index<=0;state<=INV_FEED;end
    INV_FEED:if(inv_ir) begin
     if(feed_index==19) state<=INV_COLLECT;else feed_index<=feed_index+1;
    end
    INV_COLLECT:if(inv_dv) begin
     if(inv_code!=0 || inv_job!=active_job[15:0]) begin status_code<=2;missing_sources<=~bank_delivered[active_bank];bank_state[active_bank]<=REPORTING;state<=FINISH;end
     else begin row<=0;state<=RESTORE_ROW;end
    end
    RESTORE_ROW:if(row==20) begin row<=20;state<=VERIFY_ROW;end
     else if(received_mask[row]) row<=row+1;
     else begin offset<=0;col<=0;verify_phase<=0;state<=MAC;end
    MAC:if(op_ready) begin if(col==19) state<=MAC_RESULT;else col<=col+1;end
    MAC_RESULT:if(mac_valid) begin result_byte<=mac_data;state<=verify_phase?PARITY_READ:WRITEBACK;end
    WRITEBACK:if(op_ready) begin
     if(offset==511) begin row<=row+1;state<=RESTORE_ROW;end
     else begin offset<=offset+1;col<=0;state<=MAC;end
    end
    VERIFY_ROW:if(row==32) begin row<=0;state<=OUTPUT_ROW;end
     else if(!received_mask[row]) row<=row+1;
     else begin offset<=0;col<=0;verify_phase<=1;state<=MAC;end
    PARITY_READ:if(op_ready) state<=PARITY_COMPARE;
    PARITY_COMPARE:if(mac_valid) begin
     if(mac_data!=result_byte) begin status_code<=3;missing_sources<=~bank_delivered[active_bank];bank_state[active_bank]<=REPORTING;state<=FINISH;end
     else if(offset==511) begin row<=row+1;state<=VERIFY_ROW;end
     else begin offset<=offset+1;col<=0;state<=MAC;end
    end
    OUTPUT_ROW:if(row==20)begin state<=FINISH;bank_state[active_bank]<=REPORTING;end
     else if(!deliver_mask[row]) row<=row+1;
     else begin offset<=0;state<=OUTPUT_READ;end
    OUTPUT_READ:if(op_ready) state<=OUTPUT_WAIT;
    OUTPUT_WAIT:if(mac_valid && source_ready) begin
     if(offset==511)begin
      bank_delivered[active_bank][row[4:0]]<=1;
      if(early_mode)begin state<=IDLE;elapsed_cycles<=0;early_mode<=0;end
      else begin row<=row+1;state<=OUTPUT_ROW;end
     end
     else begin offset<=offset+1;state<=OUTPUT_READ;end
    end
    FINISH:if(status_ready) begin bank_state[active_bank]<=TERMINAL;state<=IDLE;end
    default:state<=IDLE;
   endcase
   // A watchdog revokes only the incomplete source record. Completed early
   // records remain in bank_delivered and are not retracted or emitted twice.
   if(watchdog)begin state<=FINISH;status_code<=4;missing_sources<=~bank_delivered[active_bank];bank_state[active_bank]<=REPORTING;early_mode<=0;end
   if(active_deadline)begin state<=FINISH;status_code<=5;missing_sources<=~bank_delivered[active_bank];bank_state[active_bank]<=REPORTING;early_mode<=0;end
   if(cancel_match) begin
    bank_state[cancel_bank]<=FREE;bank_mask[cancel_bank]<=0;bank_count[cancel_bank]<=0;bank_delivered[cancel_bank]<=0;jobs_cancelled<=jobs_cancelled+1;
    if(packet_active && rx_bank==cancel_bank && rx_job==cancel_job) packet_drop<=1;
    if(cancel_core) begin state<=IDLE;elapsed_cycles<=0;early_mode<=0;end
   end
  end
 end
endmodule
