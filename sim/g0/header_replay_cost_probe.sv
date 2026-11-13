`timescale 1ns/1ps
// Actual shared4096 RRC -> existing Q32/F1 sampler -> TX0 coherent header.
// One job; no private RRC/history copy. A source fence explicitly cancels
// active/held ownership. Decoder errors erase this frame, never request RF ACK.
module header_replay_cost_probe #(parameter WORK_LIMIT=50000)(
 input wire clk,rst,cancel,cfg_valid,output wire cfg_ready,
 input wire [31:0] cfg_id,cfg_context,cfg_generation,input wire [15:0] cfg_epoch,
 input wire [79:0] cfg_training_position,input wire [34:0] cfg_symbol_step,
 input wire [103:0] cfg_h,input wire [31:0] cfg_phase_step,
 input wire window_ready,input wire [31:0] current_context,
 input wire rrc_valid,rrc_start,shared_ready,input wire [63:0] rrc_tag,input wire [79:0] rrc_data,
 input wire [31:0] history_generation,input wire [63:0] latest_tag,input wire [12:0] history_count,input wire history_restart,
 output wire reader_active,read_enable,output wire [11:0] read_address,input wire [79:0] read_word,
 output wire out_valid,input wire out_ready,output reg [31:0] out_id,out_context,out_generation,
 output reg [15:0] out_epoch,output reg [2:0] out_error,out_detail,output reg [191:0] out_raw_header,
 output reg [31:0] out_session,out_superframe,output reg [15:0] out_stream0_bytes,out_stream1_bytes
);
 localparam IDLE=0,LAUNCH=1,ACTIVE=2;localparam AGE_BITS=$clog2(WORK_LIMIT+1)>0?$clog2(WORK_LIMIT+1):1;
 reg [1:0] state;reg result_valid,local_cancel;reg [AGE_BITS-1:0] age;
 reg [31:0] owner,owner_context,generation;reg [15:0] epoch;reg [79:0] header_position;reg [34:0] step;
 reg [103:0] h;reg [31:0] phase_step;reg [8:0] received;
 wire timing_ready,timing_done,sample_valid,sample_ready,decoder_ready,decoder_valid,decoder_cfg_ready;
 wire [1:0] timing_error;wire [31:0] req_generation,req_id,sample_generation,sample_id,decoded_id;
 wire [63:0] req_tag,sample_tag;wire [15:0] req_mu,sample_mu;
 wire req_valid,req_ready,request_idle;wire [2:0] sample_error,decoded_error;wire [83:0] sample_data;
 wire [191:0] decoded_header;wire [31:0] decoded_session,decoded_frame;wire [15:0] decoded_l0,decoded_l1;
 wire owner_lost=history_restart || generation!=history_generation || owner_context!=current_context || epoch!=latest_tag[63:48] || !window_ready;
 assign reader_active=state!=IDLE;
 assign out_valid=result_valid && !rst && !cancel;
 assign cfg_ready=!rst && !cancel && !local_cancel && state==IDLE && !result_valid && request_idle && timing_ready && decoder_cfg_ready;
 wire cfg_fire=cfg_valid && cfg_ready;
 wire launch=state==LAUNCH && !owner_lost && !local_cancel;
 wire timing_valid=launch && decoder_cfg_ready;
 wire decoder_cfg_valid=launch && timing_ready;
 wire local_abort=cancel || local_cancel;
 wire sample_bad=sample_generation!=generation || sample_id!={23'd0,received} || sample_tag[63:48]!=epoch;
 assign sample_ready=state==ACTIVE && !owner_lost && !local_cancel?decoder_ready:1'b1;
 wire decoder_input=state==ACTIVE && !owner_lost && !local_cancel && sample_valid;
 timing_request_cost_probe timing(
  .clk(clk),.rst(rst),.cancel(local_abort),.cfg_valid(timing_valid),.cfg_ready(timing_ready),
  .cfg_generation(generation),.cfg_epoch(epoch),.cfg_first_position(header_position),.cfg_step(step),.cfg_count(16'd384),.cfg_first_id(32'd0),
  .req_valid(req_valid),.req_ready(req_ready),.req_generation(req_generation),.req_id(req_id),.req_tag(req_tag),.req_mu(req_mu),
  .done(timing_done),.cfg_error(timing_error),.emitted_count());
 interpolation_cost_probe #(.EXTERNAL_HISTORY(1)) sampler(
  .clk(clk),.rst(rst),.cancel(cancel),.in_valid(rrc_valid),.in_ready(),.in_start(rrc_start),.in_tag(rrc_tag),.in_data(rrc_data),
  .req_valid(req_valid),.req_ready(req_ready),.req_generation(req_generation),.req_id(req_id),.req_tag(req_tag),.req_mu(req_mu),
  .out_valid(sample_valid),.out_ready(sample_ready),.out_generation(sample_generation),.out_id(sample_id),.out_tag(sample_tag),.out_mu(sample_mu),.out_error(sample_error),.out_data(sample_data),
  .history_generation(),.latest_tag(),.history_count(),.fence_count(),.request_cancel(local_cancel),.request_idle(request_idle),.history_restart(),
  .shared_ready(shared_ready),.shared_generation(history_generation),.shared_latest(latest_tag),.shared_count(history_count),.shared_restart(history_restart),
  .shared_read_enable(read_enable),.shared_read_address(read_address),.shared_read_word(read_word));
 bpsk_header_cost_probe decoder(
  .clk(clk),.rst(rst),.cancel(local_abort),.cfg_valid(decoder_cfg_valid),.cfg_ready(decoder_cfg_ready),.cfg_id(owner),.cfg_h(h),.cfg_phase_step(phase_step),
  .in_valid(decoder_input),.in_ready(decoder_ready),.in_id(owner),.in_index(received),.in_error(sample_bad || sample_error!=0),.in_data(sample_data),
  .out_valid(decoder_valid),.out_ready(1'b1),.out_id(decoded_id),.out_error(decoded_error),.out_raw_header(decoded_header),
  .out_session(decoded_session),.out_superframe(decoded_frame),.out_stream0_bytes(decoded_l0),.out_stream1_bytes(decoded_l1));
 task publish(input [2:0] error,input [2:0] detail);
  begin
   state<=IDLE;result_valid<=1;local_cancel<=1;out_error<=error;out_detail<=detail;
   out_id<=owner;out_context<=owner_context;out_generation<=generation;out_epoch<=epoch;
   out_raw_header<=0;out_session<=0;out_superframe<=0;out_stream0_bytes<=0;out_stream1_bytes<=0;
  end
 endtask
 always @(posedge clk)begin
  if(rst || cancel)begin
   state<=IDLE;result_valid<=0;local_cancel<=0;age<=0;owner<=0;owner_context<=0;generation<=0;epoch<=0;header_position<=0;step<=0;h<=0;phase_step<=0;received<=0;
   out_id<=0;out_context<=0;out_generation<=0;out_epoch<=0;out_error<=0;out_detail<=0;out_raw_header<=0;out_session<=0;out_superframe<=0;out_stream0_bytes<=0;out_stream1_bytes<=0;
  end else begin
   local_cancel<=0;
   if(out_valid && out_ready)result_valid<=0;
   if(cfg_fire)begin
    owner<=cfg_id;owner_context<=cfg_context;generation<=cfg_generation;epoch<=cfg_epoch;step<=cfg_symbol_step;h<=cfg_h;phase_step<=cfg_phase_step;
    header_position<=cfg_training_position+({35'd0,cfg_symbol_step,10'd0}-{40'd0,cfg_symbol_step,5'd0});
    age<=0;received<=0;out_detail<=0;state<=LAUNCH;
   end
   if(state!=IDLE)begin
    age<=age+1'b1;
    if(owner_lost)publish(4,0);
    else if(age==WORK_LIMIT-1)publish(6,0);
    else if(state==LAUNCH && timing_valid && timing_ready)state<=ACTIVE;
    else if(state==ACTIVE)begin
     if(sample_valid && sample_ready)begin received<=received+1'b1;if(sample_bad || sample_error!=0)out_detail<=sample_bad?3'd4:sample_error;end
     if(timing_error!=0)publish(5,{1'b0,timing_error});
     else if(decoder_valid)begin
      publish(decoded_id!=owner?3'd4:decoded_error,decoded_error==3?out_detail:3'd0);
      if(decoded_id==owner)begin
       out_raw_header<=decoded_header;out_session<=decoded_session;out_superframe<=decoded_frame;out_stream0_bytes<=decoded_l0;out_stream1_bytes<=decoded_l1;
      end
     end
    end
   end
  end
 end
endmodule
