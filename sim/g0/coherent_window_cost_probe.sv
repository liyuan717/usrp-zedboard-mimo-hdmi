`timescale 1ns/1ps
// Bounded 192-public-symbol coherent metrics on actual signed20 RRC centers.
// N=sum_RX |sum_m x[start+4*m]*conj(sign[m])|^2, E=sum_768_samples |x|^2.
// Public signs are +/-1+/-j (sqrt2 omitted): normalized score is N/(96*E).
// One common-clock history supports nonblocking writes and a priority read
// port for the existing interpolator. No payload, CFO or peak oracle input.
module coherent_window_cost_probe #(
 parameter ADDRESS_BITS=12,WAIT_LIMIT=1250000,SIGN_FILE="coherent_signs.hex",EXTERNAL_RESERVATION=0
)(
 input wire clk,rst,cancel,
 input wire in_valid,output wire in_ready,input wire in_start,
 input wire [63:0] in_tag,input wire [79:0] in_data,
 input wire external_read_enable,input wire [ADDRESS_BITS-1:0] external_read_address,
 output wire [79:0] read_word,
 input wire cfg_valid,output wire cfg_ready,
 input wire [31:0] cfg_id,cfg_generation,input wire [63:0] cfg_first_tag,
 input wire [6:0] cfg_points,
 output wire out_valid,input wire out_ready,
 output reg [31:0] out_id,out_generation,output reg [63:0] out_tag,
 output reg [6:0] out_point,output reg out_last,output reg [2:0] out_error,
 output reg [115:0] out_c,output reg [59:0] out_n,output reg [51:0] out_e,
 output reg [31:0] history_generation,output reg [63:0] latest_tag,
 output reg [ADDRESS_BITS:0] history_count,
 output wire history_restart,output wire busy,input wire external_read_reservation
);
 localparam IDLE=0,WAIT_WINDOW=1,ENERGY=2,ENERGY_DRAIN=3,CORRELATE=4,
  CORRELATE_DRAIN=5,SQUARE=6,SQUARE_DRAIN=7,EMIT=8,SHIFT=9,SHIFT_DRAIN=10;
 localparam DEPTH=1<<ADDRESS_BITS;
 localparam WAIT_BITS=$clog2(WAIT_LIMIT+1)>0?$clog2(WAIT_LIMIT+1):1;
 reg [3:0] state;
 reg response_valid;
 reg [31:0] owner,owner_generation;
 reg [63:0] base_tag;
 reg [6:0] point,points;
 reg [9:0] read_index;
 reg [WAIT_BITS-1:0] waited;
 reg [1:0] signs[0:191];initial $readmemh(SIGN_FILE,signs);
 reg [51:0] energy;
 reg signed [28:0] correlation[0:3];
 reg [59:0] norm;
 reg [2:0] square_index;
 reg square_valid;reg [1:0] square_lane;
 (* use_dsp="yes" *) reg [57:0] square;
 wire signed [28:0] square_operand=correlation[square_index[1:0]];
 reg read_valid;reg [1:0] read_kind;reg [9:0] read_number;
 // kind0: initial energy, kind1: correlation, kind2: remove, kind3: add.
 reg power_valid;reg [1:0] power_kind;reg power_last;
 (* use_dsp="yes" *) reg [39:0] powers[0:3];
 wire [41:0] power_sum={2'd0,powers[0]}+{2'd0,powers[1]}+{2'd0,powers[2]}+{2'd0,powers[3]};
 wire [63:0] next_tag={latest_tag[63:48],latest_tag[47:0]+48'd1};
 assign in_ready=!rst && !cancel;
 wire input_fire=in_valid && in_ready;
 wire new_window=input_fire && (history_count==0 || in_start || in_tag!=next_tag);
 assign history_restart=new_window;
 assign out_valid=response_valid && !rst && !cancel;
 assign busy=state!=IDLE;
 assign cfg_ready=!rst && !cancel && !new_window && history_count!=0 && state==IDLE && !response_valid;
 wire owner_lost=new_window || owner_generation!=history_generation;
 wire [47:0] last_offset=48'd767+{41'd0,points}-48'd1;
 wire [63:0] last_required={base_tag[63:48],base_tag[47:0]+last_offset};
 wire [47:0] end_distance=latest_tag[47:0]-last_required[47:0];
 wire all_present=latest_tag[63:48]==base_tag[63:48] && !end_distance[47];
 reg [63:0] own_read_tag;
 reg [1:0] own_kind;
 reg own_request;
 always @* begin
  own_request=state==ENERGY || state==CORRELATE || state==SHIFT;
  own_kind=0;own_read_tag=base_tag;
  if(state==ENERGY) own_read_tag={base_tag[63:48],base_tag[47:0]+{38'd0,read_index}};
  if(state==CORRELATE) begin
   own_read_tag={base_tag[63:48],base_tag[47:0]+{36'd0,read_index,2'd0}};own_kind=1;
  end
  if(state==SHIFT) begin
   own_kind=read_index==0?2:3;
   own_read_tag={base_tag[63:48],read_index==0?base_tag[47:0]-48'd1:base_tag[47:0]+48'd767};
  end
 end
 wire [47:0] read_distance=latest_tag[47:0]-own_read_tag[47:0];
 wire read_present=history_count!=0 && own_read_tag[63:48]==latest_tag[63:48] &&
  !read_distance[47] && read_distance<{{(47-ADDRESS_BITS){1'b0}},history_count};
 wire read_collision=input_fire && in_tag[ADDRESS_BITS-1:0]==own_read_tag[ADDRESS_BITS-1:0];
 wire external_slot=EXTERNAL_RESERVATION?external_read_reservation:external_read_enable;
 wire own_read=own_request && !external_slot && !owner_lost && read_present && !read_collision && !rst && !cancel;
 wire memory_read=external_read_enable || own_read;
 wire [ADDRESS_BITS-1:0] memory_address=external_read_enable?external_read_address:own_read_tag[ADDRESS_BITS-1:0];
 xpm_memory_sdpram #(
  .ADDR_WIDTH_A(ADDRESS_BITS),.ADDR_WIDTH_B(ADDRESS_BITS),.AUTO_SLEEP_TIME(0),
  .BYTE_WRITE_WIDTH_A(80),.CLOCKING_MODE("common_clock"),.ECC_MODE("no_ecc"),
  .MEMORY_INIT_FILE("none"),.MEMORY_INIT_PARAM("0"),.MEMORY_OPTIMIZATION("true"),
  .MEMORY_PRIMITIVE("block"),.MEMORY_SIZE(80*DEPTH),.MESSAGE_CONTROL(0),
  .READ_DATA_WIDTH_B(80),.READ_LATENCY_B(1),.READ_RESET_VALUE_B("0"),
  .RST_MODE_A("SYNC"),.RST_MODE_B("SYNC"),.SIM_ASSERT_CHK(1),
  .USE_EMBEDDED_CONSTRAINT(0),.USE_MEM_INIT(1),.WAKEUP_TIME("disable_sleep"),
  .WRITE_DATA_WIDTH_A(80),.WRITE_MODE_B("read_first")
 ) history(
  .clka(clk),.ena(input_fire),.wea(input_fire),.addra(in_tag[ADDRESS_BITS-1:0]),.dina(in_data),
  .injectsbiterra(1'b0),.injectdbiterra(1'b0),.clkb(clk),.rstb(1'b0),
  .enb(memory_read && !rst && !cancel),.regceb(1'b1),.addrb(memory_address),.doutb(read_word),
  .sbiterrb(),.dbiterrb(),.sleep(1'b0));

 task publish(input [2:0] error_code,input last);
  begin
   response_valid<=1;out_id<=owner;out_generation<=owner_generation;
   out_tag<=base_tag;out_point<=point;out_last<=last;out_error<=error_code;
   out_n<=0;out_e<=0;out_c<=0;
   read_valid<=0;power_valid<=0;square_valid<=0;state<=EMIT;
  end
 endtask
 always @(posedge clk) begin
  if(rst) begin
   state<=IDLE;response_valid<=0;owner<=0;owner_generation<=0;base_tag<=0;
   point<=0;points<=0;read_index<=0;waited<=0;energy<=0;norm<=0;square_index<=0;
   read_valid<=0;read_kind<=0;read_number<=0;power_valid<=0;power_kind<=0;power_last<=0;
   square_valid<=0;square_lane<=0;square<=0;
   out_id<=0;out_generation<=0;out_tag<=0;out_point<=0;out_last<=0;out_error<=0;out_c<=0;out_n<=0;out_e<=0;
   history_generation<=0;history_count<=0;latest_tag<=0;
   for(integer l=0;l<4;l=l+1) begin correlation[l]<=0;powers[l]<=0;end
  end else if(cancel) begin
   state<=IDLE;response_valid<=0;read_valid<=0;power_valid<=0;square_valid<=0;
   history_count<=0;history_generation<=history_generation+32'd1;
  end else begin
   // Bound unpublished work even when the priority sampler occupies the port.
   if(state!=IDLE && state!=EMIT) waited<=waited+1'b1;
   if(input_fire) begin
    latest_tag<=in_tag;
    if(new_window) begin history_generation<=history_generation+32'd1;history_count<=1;end
    else if(history_count<DEPTH) history_count<=history_count+1'b1;
   end
   read_valid<=own_read;read_kind<=own_kind;read_number<=read_index;
   power_valid<=read_valid && read_kind!=1;power_kind<=read_kind;
   power_last<=read_kind==0 && read_number==767;
   if(read_valid && read_kind!=1)
    for(integer l=0;l<4;l=l+1) powers[l]<=$signed(read_word[l*20+:20])*$signed(read_word[l*20+:20]);
   if(power_valid) begin
    if(power_kind==2) energy<=energy-{10'd0,power_sum};
    else energy<=energy+{10'd0,power_sum};
    if(power_last) begin state<=CORRELATE;read_index<=0;end
    if(power_kind==3) begin state<=CORRELATE;read_index<=0;end
   end
   if(read_valid && read_kind==1) begin
    for(integer rx=0;rx<2;rx=rx+1) begin
     correlation[rx*2]<=correlation[rx*2]+
      (signs[read_number][0]?-$signed({{9{read_word[rx*40+19]}},read_word[rx*40+:20]}):$signed({{9{read_word[rx*40+19]}},read_word[rx*40+:20]}))+
      (signs[read_number][1]?-$signed({{9{read_word[rx*40+39]}},read_word[rx*40+20+:20]}):$signed({{9{read_word[rx*40+39]}},read_word[rx*40+20+:20]}));
     correlation[rx*2+1]<=correlation[rx*2+1]+
      (signs[read_number][0]?-$signed({{9{read_word[rx*40+39]}},read_word[rx*40+20+:20]}):$signed({{9{read_word[rx*40+39]}},read_word[rx*40+20+:20]}))-
      (signs[read_number][1]?-$signed({{9{read_word[rx*40+19]}},read_word[rx*40+:20]}):$signed({{9{read_word[rx*40+19]}},read_word[rx*40+:20]}));
    end
    if(read_number==191) begin state<=SQUARE;square_index<=0;norm<=0;end
   end
   square_valid<=state==SQUARE;square_lane<=square_index[1:0];
   if(state==SQUARE) begin square<=square_operand*square_operand;square_index<=square_index+1'b1;end
   if(square_valid) begin
    norm<=norm+{2'd0,square};
    if(square_lane==3) begin
     publish(0,point+7'd1==points);out_n<=norm+{2'd0,square};out_e<=energy;
     for(integer l=0;l<4;l=l+1) out_c[l*29+:29]<=correlation[l];
    end
   end
   if(state!=IDLE && state!=EMIT && owner_lost) publish(1,1);
   else if(own_request && !external_slot && (!read_present || read_collision)) publish(2,1);
   else if(state!=IDLE && state!=EMIT && waited==WAIT_LIMIT-1) publish(3,1);
   else case(state)
    IDLE:if(cfg_valid && cfg_ready) begin
     owner<=cfg_id;owner_generation<=cfg_generation;base_tag<=cfg_first_tag;
     points<=cfg_points;point<=0;read_index<=0;waited<=0;energy<=0;
     for(integer l=0;l<4;l=l+1) correlation[l]<=0;
     if(cfg_points==0 || cfg_points>65 || cfg_generation!=history_generation || cfg_first_tag[63:48]!=latest_tag[63:48]) begin
      response_valid<=1;out_id<=cfg_id;out_generation<=cfg_generation;out_tag<=cfg_first_tag;
      out_point<=0;out_last<=1;out_error<=4;out_n<=0;out_e<=0;out_c<=0;state<=EMIT;
     end else state<=WAIT_WINDOW;
    end
    WAIT_WINDOW:begin
     if(all_present) begin state<=ENERGY;read_index<=0;end
    end
    ENERGY:if(own_read) begin
     if(read_index==767) state<=ENERGY_DRAIN;else read_index<=read_index+1'b1;
    end
    CORRELATE:if(own_read) begin
     if(read_index==191) state<=CORRELATE_DRAIN;else read_index<=read_index+1'b1;
    end
    SQUARE:if(square_index==3) state<=SQUARE_DRAIN;
    EMIT:if(out_valid && out_ready) begin
     response_valid<=0;
     if(out_last) state<=IDLE;
     else if(owner_lost) publish(1,1);
     else begin
      base_tag<={base_tag[63:48],base_tag[47:0]+48'd1};point<=point+1'b1;read_index<=0;
      for(integer l=0;l<4;l=l+1) correlation[l]<=0;
      state<=SHIFT;
     end
    end
    SHIFT:if(own_read) begin if(read_index==1) state<=SHIFT_DRAIN;else read_index<=1;end
   endcase
  end
 end
endmodule
