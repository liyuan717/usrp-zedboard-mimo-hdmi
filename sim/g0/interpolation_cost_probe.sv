`timescale 1ns/1ps
// G0 bounded, nonblocking-write RRC history and configured linear interpolation.
// Input tags are RRC window CENTERS. This block does not estimate timing.
// One active request + one response slot. No endpoint hold for missing history.
// WAIT_LIMIT must be >=1; OUT_FRACTION_BITS is supported in0..2.
// Selected candidate retains one extra fraction bit: signed21 at scale4.
module interpolation_cost_probe #(parameter WAIT_LIMIT=4096,OUT_FRACTION_BITS=1,EXTERNAL_HISTORY=0,EXCLUSIVE_REQUEST=0)(
 input wire clk,rst,cancel,
 input wire in_valid,output wire in_ready,input wire in_start,
 input wire [63:0] in_tag,input wire [79:0] in_data,
 input wire req_valid,output wire req_ready,
 input wire [31:0] req_generation,req_id,input wire [63:0] req_tag,
 input wire [15:0] req_mu,
 output wire out_valid,input wire out_ready,
 output reg [31:0] out_generation,out_id,output reg [63:0] out_tag,
 output reg [15:0] out_mu,output reg [2:0] out_error,
 output reg [4*(20+OUT_FRACTION_BITS)-1:0] out_data,
 output wire [31:0] history_generation,output wire [63:0] latest_tag,
 output wire [9:0] history_count,output reg [31:0] fence_count,
 input wire request_cancel,output wire request_idle,output wire history_restart,
 // Optional coherent4096 pool: priority, synchronous one-cycle read response.
 // Legacy history_count is capped at512; range checking uses all13 bits.
 input wire shared_ready,input wire [31:0] shared_generation,
 input wire [63:0] shared_latest,input wire [12:0] shared_count,input wire shared_restart,
  output wire shared_read_enable,output wire [11:0] shared_read_address,input wire [79:0] shared_read_word,
  output wire shared_read_reservation
);
 localparam IDLE=0,CHECK=1,READ0=2,READ1=3,DIFF=4,MUL=5,ADD=6,FINISH=7;
 localparam WAIT_BITS=$clog2(WAIT_LIMIT+1)>0?$clog2(WAIT_LIMIT+1):1;
 localparam OUT_BITS=20+OUT_FRACTION_BITS;
 localparam signed [37:0] ROUND_BIAS=38'sd1<<(15-OUT_FRACTION_BITS);
 reg [2:0] state;
 reg response_valid;
 // Request-only cancellation never fences sample history. Suppress a held
 // response immediately so the cancellation edge cannot also transfer it.
 assign out_valid=response_valid && !request_cancel;
 assign request_idle=!rst && !cancel && !request_cancel && state==IDLE && !response_valid;
 reg [31:0] own_generation;reg [63:0] own_latest;reg [9:0] own_count;
 assign history_generation=EXTERNAL_HISTORY?shared_generation:own_generation;
 assign latest_tag=EXTERNAL_HISTORY?shared_latest:own_latest;
 assign history_count=EXTERNAL_HISTORY?(shared_count>512?10'd512:shared_count[9:0]):own_count;
 wire [12:0] effective_count=EXTERNAL_HISTORY?shared_count:{3'd0,own_count};
 reg [79:0] private_read_word,x0,x1;
 wire [79:0] read_word=EXTERNAL_HISTORY?shared_read_word:private_read_word;
 reg [4*OUT_BITS-1:0] result;
 reg [31:0] work_generation,work_id;
 reg [63:0] work_tag;
 reg [15:0] work_mu;
 reg [WAIT_BITS-1:0] waited;
 reg [1:0] lane;
 reg signed [20:0] delta;
 (* use_dsp="yes" *) reg signed [37:0] product;
 wire signed [19:0] lane0=x0[lane*20+:20],lane1=x1[lane*20+:20];
 wire signed [37:0] rounded_delta=(product+ROUND_BIAS)>>>(16-OUT_FRACTION_BITS);
 wire signed [37:0] interpolated=($signed({{18{lane0[19]}},lane0})<<<OUT_FRACTION_BITS)+rounded_delta;

 assign in_ready=!rst && !cancel && (!EXTERNAL_HISTORY || shared_ready);
 wire input_fire=in_valid && in_ready;
 wire [63:0] next_tag={latest_tag[63:48],latest_tag[47:0]+48'd1};
 wire new_window=EXTERNAL_HISTORY?shared_restart:(input_fire && (history_count==0 || in_start || in_tag!=next_tag));
 // Same-edge ownership event; request cancellation never suppresses writes.
 assign history_restart=new_window;
 assign req_ready=!rst && !cancel && !request_cancel && state==IDLE &&
                  (EXCLUSIVE_REQUEST?!response_valid:(!response_valid || out_ready));
 wire [47:0] distance=latest_tag[47:0]-work_tag[47:0];
 wire same_window=work_generation==history_generation && work_tag[63:48]==latest_tag[63:48];
 wire endpoints_present=effective_count!=0 && same_window && !distance[47] &&
                         distance>=48'd1 && distance<{35'd0,effective_count};
 wire [11:0] read_address=state==READ0?work_tag[11:0]+12'd1:work_tag[11:0];
 wire collision=input_fire && (EXTERNAL_HISTORY?in_tag[11:0]==read_address:in_tag[8:0]==read_address[8:0]);
 wire read_enable=!rst && !cancel && !request_cancel && !new_window && !collision &&
                  ((state==CHECK && endpoints_present) || state==READ0);

 // Separate write and synchronous read ports; collisions are rejected above.
 // RAM itself is never reset. Window metadata determines every valid access.
 assign shared_read_enable=EXTERNAL_HISTORY && read_enable;
 // Request state/endpoints reserve the port independently of current cancel.
 // Cancel may empty that slot; it cannot feed the other reader's counters.
 assign shared_read_reservation=EXTERNAL_HISTORY && ((state==CHECK && endpoints_present) || state==READ0);
 assign shared_read_address=read_address;
 generate if(!EXTERNAL_HISTORY) begin: private_history
  (* ram_style="block" *) reg [79:0] history[0:511];
  always @(posedge clk) begin
   if(input_fire) history[in_tag[8:0]]<=in_data;
   if(read_enable) private_read_word<=history[read_address[8:0]];
  end
 end endgenerate

 task respond(input [2:0] error_code,input [4*OUT_BITS-1:0] value);
 begin
  response_valid<=1;out_generation<=work_generation;out_id<=work_id;
  out_tag<=work_tag;out_mu<=work_mu;out_error<=error_code;out_data<=value;
  state<=IDLE;
 end
 endtask

 always @(posedge clk) begin
  if(rst) begin
   state<=IDLE;own_generation<=0;own_latest<=0;own_count<=0;fence_count<=0;
   response_valid<=0;out_generation<=0;out_id<=0;out_tag<=0;out_mu<=0;out_error<=0;out_data<=0;
   work_generation<=0;work_id<=0;work_tag<=0;work_mu<=0;waited<=0;
   x0<=0;x1<=0;result<=0;lane<=0;delta<=0;product<=0;
  end else if(cancel) begin
   state<=IDLE;own_generation<=own_generation+32'd1;
   own_count<=0;response_valid<=0;waited<=0;
  end else begin
   if(out_valid && out_ready) response_valid<=0;
   if(input_fire) begin
    if(!EXTERNAL_HISTORY) begin
     own_latest<=in_tag;
     if(new_window) begin own_generation<=own_generation+32'd1;own_count<=1;end
     else if(own_count<512) own_count<=own_count+1'b1;
    end
    if(new_window) fence_count<=fence_count+32'd1;
   end
   // In-flight work belongs to one generation. Already emitted responses are
   // immutable under ordinary window restarts; only reset/cancel retract them.
   if(request_cancel) begin
    state<=IDLE;response_valid<=0;waited<=0;
   end else if(state!=IDLE && (new_window || work_generation!=history_generation)) begin
    respond(3'd1,{4*OUT_BITS{1'b0}});
   end else case(state)
    IDLE:if(req_valid && req_ready) begin
     work_generation<=req_generation;work_id<=req_id;work_tag<=req_tag;work_mu<=req_mu;
     waited<=0;lane<=0;state<=CHECK;
    end
    CHECK:begin
     if(effective_count!=0 && work_tag[63:48]!=latest_tag[63:48]) respond(3'd1,{4*OUT_BITS{1'b0}});
     else if(effective_count!=0 && !distance[47] && distance>={35'd0,effective_count})
      respond(3'd2,{4*OUT_BITS{1'b0}});
     else if(endpoints_present) begin
      if(collision) respond(3'd2,{4*OUT_BITS{1'b0}});
      else state<=READ0;
     end else if(waited==WAIT_LIMIT-1) respond(3'd3,{4*OUT_BITS{1'b0}});
     else waited<=waited+1'b1;
    end
    READ0:begin
     if(collision) respond(3'd2,{4*OUT_BITS{1'b0}});
     else begin x0<=read_word;state<=READ1;end
    end
    READ1:begin x1<=read_word;state<=DIFF;end
    DIFF:begin
     delta<=$signed({lane1[19],lane1})-$signed({lane0[19],lane0});state<=MUL;
    end
    MUL:begin product<=delta*$signed({1'b0,work_mu});state<=ADD;end
    ADD:begin
     result[lane*OUT_BITS+:OUT_BITS]<=interpolated[OUT_BITS-1:0];
     if(lane==3) state<=FINISH;
     else begin lane<=lane+1'b1;state<=DIFF;end
    end
    FINISH:respond(3'd0,result);
    default:state<=IDLE;
   endcase
  end
 end
endmodule
