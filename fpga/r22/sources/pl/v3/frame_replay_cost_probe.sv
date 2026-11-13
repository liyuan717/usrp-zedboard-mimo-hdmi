`timescale 1ns/1ps
// One actual shared4096 F1 sampler and header/pilot DSP for an entire frame.
// Verified header precedes all pilot work. Header may be held independently;
// frame abort explicitly revokes active/held header, phase and sample ownership.
module frame_replay_cost_probe #(parameter WORK_LIMIT=1100000,BUSINESS_ENABLED=0)(
 input wire clk,rst,cancel,cfg_valid,output wire cfg_ready,
 input wire [31:0] cfg_id,cfg_context,cfg_generation,input wire [15:0] cfg_epoch,
 input wire [79:0] cfg_training_position,input wire [34:0] cfg_symbol_step,
 input wire [207:0] cfg_h,input wire [63:0] cfg_phase_steps,
 input wire window_ready,input wire [31:0] current_context,
 input wire rrc_valid,rrc_start,shared_ready,input wire [63:0] rrc_tag,input wire [79:0] rrc_data,
 input wire [31:0] history_generation,input wire [63:0] latest_tag,input wire [12:0] history_count,input wire history_restart,
 output wire reader_active,read_enable,output wire [11:0] read_address,input wire [79:0] read_word,
 output wire header_valid,input wire header_ready,
 output wire [31:0] header_id,header_context,header_generation,output wire [15:0] header_epoch,
 output reg [2:0] header_error,header_detail,output reg [191:0] header_raw,
 output reg [31:0] header_session,header_superframe,output reg [15:0] header_stream0_bytes,header_stream1_bytes,
 output wire phase_valid,input wire phase_ready,
 output wire [31:0] phase_id,phase_context,phase_generation,output wire [15:0] phase_epoch,
 output wire [8:0] phase_point,phase_known_point,output wire phase_tx,output wire signed [40:0] phase_value,
 output wire frame_abort,output wire status_valid,input wire status_ready,
 output wire [31:0] status_id,status_context,status_generation,output wire [15:0] status_epoch,
 output reg [2:0] status_error,status_detail,output reg [8:0] status_point,
 // Optional business consumer shares this exact F1 instance. Requests own the
 // response through its handshake; header/pilot timing cannot relabel them.
 input wire business_req_valid,output wire business_req_ready,
 input wire [31:0] business_req_generation,business_req_id,input wire [63:0] business_req_tag,input wire [15:0] business_req_mu,
 output wire business_sample_valid,input wire business_sample_ready,
 output wire [31:0] business_sample_generation,business_sample_id,output wire [63:0] business_sample_tag,
 output wire [15:0] business_sample_mu,output wire [2:0] business_sample_error,output wire [83:0] business_sample_data,
 input wire business_done,business_abort,input wire [2:0] business_detail,output wire read_reservation
);
 localparam IDLE=0,HEADER_LAUNCH=1,HEADER_ACTIVE=2,FRAME_LAUNCH=3,PILOT_LAUNCH=4,PILOT_ACTIVE=5,DRAIN=6;
 localparam AGE_BITS=$clog2(WORK_LIMIT+1)>0?$clog2(WORK_LIMIT+1):1;
 reg [2:0] state;reg [AGE_BITS-1:0] age;reg local_cancel,header_held,status_held,admission_ready,job_launch_ready;
 reg [31:0] owner,owner_context,generation;reg [15:0] epoch;
 reg [79:0] header_position,pilot_position;reg [34:0] step;
 reg [207:0] h;reg [63:0] phase_steps;reg [31:0] nominal0,nominal1;
 reg [8:0] received,point;reg tx,sample_sent;
 wire [79:0] wide_step={45'd0,step},cfg_wide_step={45'd0,cfg_symbol_step};
 wire busy=state!=IDLE;
 wire owner_lost=busy && (history_restart || generation!=history_generation || owner_context!=current_context || epoch!=latest_tag[63:48] || !window_ready);
 wire business_fault=BUSINESS_ENABLED && business_abort;
 wire abort_work=cancel || local_cancel || owner_lost || business_fault;
 wire timing_ready,timing_done,request_idle,req_valid,req_ready;
 wire [1:0] timing_error;wire [31:0] req_generation,req_id,sample_generation,sample_id;
 wire [63:0] req_tag,sample_tag;wire [15:0] req_mu,sample_mu;
 wire sample_valid,sample_ready;wire [2:0] sample_error;wire [83:0] sample_data;
 wire math_ready,math_in_ready,decoded_valid;wire [31:0] decoded_id,decoded_session,decoded_frame;
 wire [2:0] decoded_error;wire [191:0] decoded_header;wire [15:0] decoded_l0,decoded_l1;
 wire smooth_ready,smooth_active,smooth_abort,observation_accepted;wire [1:0] smooth_reason;wire [8:0] smooth_point;
 wire phase_valid_q;wire piloting=state==PILOT_LAUNCH || state==PILOT_ACTIVE;
 wire f1_req_ready,f1_valid,f1_ready;reg business_owner;
 wire sampler_reservation;
 wire choose_business=BUSINESS_ENABLED && business_req_valid && (piloting || state==DRAIN) && !abort_work;
 // Request admission uses the empty response slot, never downstream READY.
 // The arithmetic consumer may hold a response; no new owner may overlap it.
 wire f1_req_valid=(choose_business || req_valid) && request_idle;
 wire [31:0] f1_generation,f1_id;wire [63:0] f1_tag;wire [15:0] f1_mu;wire [2:0] f1_error;wire [83:0] f1_data;
 assign req_ready=request_idle && !choose_business;
 assign business_req_ready=BUSINESS_ENABLED && request_idle && choose_business;
 assign sample_valid=f1_valid && !business_owner;
 assign business_sample_valid=BUSINESS_ENABLED && f1_valid && business_owner;
 assign f1_ready=business_owner?business_sample_ready:sample_ready;
 assign sample_generation=f1_generation;assign sample_id=f1_id;assign sample_tag=f1_tag;assign sample_mu=f1_mu;assign sample_error=f1_error;assign sample_data=f1_data;
 assign business_sample_generation=f1_generation;assign business_sample_id=f1_id;assign business_sample_tag=f1_tag;
 assign business_sample_mu=f1_mu;assign business_sample_error=f1_error;assign business_sample_data=f1_data;
 always @(posedge clk)begin
  if(rst || abort_work || smooth_abort)business_owner<=0;
  else if(f1_req_valid && f1_req_ready)business_owner<=choose_business;
 end
 // A pilot descriptor may wait behind an owned business F1 request. Predict
 // the two idle configuration slots; current fences still gate both directly.
 // This cuts raw-source cancellation -> nested math READY -> Q32 adder paths.
 wire launching=(state==HEADER_LAUNCH || state==PILOT_LAUNCH) && !abort_work && !smooth_abort;
 wire timing_cfg=launching && job_launch_ready;
 wire math_cfg=launching && job_launch_ready;
 wire sample_bad=sample_generation!=generation || sample_id!={23'd0,received} || sample_tag[63:48]!=epoch;
 wire sampling=state==HEADER_ACTIVE || state==PILOT_ACTIVE;
 assign sample_ready=sampling && !abort_work?math_in_ready:1'b1;
 assign reader_active=busy;
 assign read_reservation=BUSINESS_ENABLED?sampler_reservation:read_enable;
 assign header_valid=header_held && !rst && !abort_work && !smooth_abort;
 assign phase_valid=phase_valid_q && !rst && !abort_work;
 assign status_valid=status_held && !rst && !cancel;
 assign frame_abort=!rst && (cancel || owner_lost || smooth_abort || business_fault || (status_held && status_error!=0));
 assign header_id=owner;assign header_context=owner_context;assign header_generation=generation;assign header_epoch=epoch;
 assign status_id=owner;assign status_context=owner_context;assign status_generation=generation;assign status_epoch=epoch;
 // All subunits stay idle without an accepted descriptor. Register their
 // availability so the raw-source fence does not traverse every nested READY
 // before reaching normal_valid. Current cancel/ownership gates remain direct.
 assign cfg_ready=admission_ready && !rst && !cancel && !local_cancel && !busy && !header_held && !status_held;
 timing_request_cost_probe timing(.clk(clk),.rst(rst),.cancel(abort_work),.cfg_valid(timing_cfg),.cfg_ready(timing_ready),
  .cfg_generation(generation),.cfg_epoch(epoch),.cfg_first_position(piloting?pilot_position:header_position),.cfg_step(step),
  .cfg_count(piloting?16'd1:16'd384),.cfg_first_id(32'd0),.req_valid(req_valid),.req_ready(req_ready),
  .req_generation(req_generation),.req_id(req_id),.req_tag(req_tag),.req_mu(req_mu),.done(timing_done),.cfg_error(timing_error),.emitted_count());
 interpolation_cost_probe #(.EXTERNAL_HISTORY(1),.EXCLUSIVE_REQUEST(BUSINESS_ENABLED)) sampler(
  .clk(clk),.rst(rst),.cancel(cancel),.in_valid(rrc_valid),.in_ready(),.in_start(rrc_start),.in_tag(rrc_tag),.in_data(rrc_data),
  .req_valid(f1_req_valid),.req_ready(f1_req_ready),.req_generation(choose_business?business_req_generation:req_generation),.req_id(choose_business?business_req_id:req_id),.req_tag(choose_business?business_req_tag:req_tag),.req_mu(choose_business?business_req_mu:req_mu),
  .out_valid(f1_valid),.out_ready(f1_ready),.out_generation(f1_generation),.out_id(f1_id),.out_tag(f1_tag),.out_mu(f1_mu),.out_error(f1_error),.out_data(f1_data),
  .history_generation(),.latest_tag(),.history_count(),.fence_count(),.request_cancel(local_cancel || owner_lost || smooth_abort || business_fault),.request_idle(request_idle),.history_restart(),
  .shared_ready(shared_ready),.shared_generation(history_generation),.shared_latest(latest_tag),.shared_count(history_count),.shared_restart(history_restart),
  .shared_read_enable(read_enable),.shared_read_address(read_address),.shared_read_word(read_word),.shared_read_reservation(sampler_reservation));
 pilot_tracking_cost_probe tracking(.clk(clk),.rst(rst),.cancel(abort_work),
  .frame_valid(state==FRAME_LAUNCH && !abort_work),.frame_ready(smooth_ready),
  .frame_id(owner),.frame_context(owner_context),.frame_generation(generation),.frame_epoch(epoch),
  .cfg_valid(math_cfg),.cfg_ready(math_ready),.cfg_pilot(piloting),
  .cfg_id(owner),.cfg_context(owner_context),.cfg_generation(generation),.cfg_epoch(epoch),.cfg_point(point),.cfg_tx(tx),
  .cfg_h(piloting && tx?h[207:104]:h[103:0]),.cfg_phase_step(phase_steps[31:0]),.cfg_pilot_phase(tx?nominal1:nominal0),
  .in_valid(sampling && !abort_work && sample_valid),.in_ready(math_in_ready),.in_id(owner),.in_index(received),
  .in_error(sample_bad || sample_error!=0),.in_data(sample_data),
  .header_valid(decoded_valid),.header_ready(1'b1),.header_id(decoded_id),.header_error(decoded_error),.header_raw(decoded_header),
  .header_session(decoded_session),.header_superframe(decoded_frame),.header_stream0_bytes(decoded_l0),.header_stream1_bytes(decoded_l1),
  .out_valid(phase_valid_q),.out_ready(phase_ready && !abort_work),.out_id(phase_id),.out_context(phase_context),
  .out_generation(phase_generation),.out_epoch(phase_epoch),.out_point(phase_point),.out_known_point(phase_known_point),.out_tx(phase_tx),.out_phase(phase_value),
  .frame_abort(smooth_abort),.abort_reason(smooth_reason),.abort_point(smooth_point),.active(smooth_active),.observation_accepted(observation_accepted));
 task finish(input [2:0] error,input [2:0] detail);
  begin state<=IDLE;status_held<=1;status_error<=error;status_detail<=detail;status_point<=point;local_cancel<=1;end
 endtask
 always @(posedge clk)begin
  if(rst || cancel)begin
   state<=IDLE;age<=0;local_cancel<=0;header_held<=0;status_held<=0;admission_ready<=0;job_launch_ready<=0;
   owner<=0;owner_context<=0;generation<=0;epoch<=0;header_position<=0;pilot_position<=0;step<=0;h<=0;phase_steps<=0;nominal0<=0;nominal1<=0;
   received<=0;point<=0;tx<=0;sample_sent<=0;header_error<=0;header_detail<=0;header_raw<=0;header_session<=0;header_superframe<=0;header_stream0_bytes<=0;header_stream1_bytes<=0;
   status_error<=0;status_detail<=0;status_point<=0;
  end else begin
   local_cancel<=0;
   job_launch_ready<=timing_ready && math_ready;
   if(timing_cfg && math_cfg)job_launch_ready<=0;
   admission_ready<=!busy && !header_held && !status_held && !local_cancel && request_idle && timing_ready && math_ready && smooth_ready;
   if(header_valid && header_ready)header_held<=0;
   if(status_valid && status_ready)status_held<=0;
   if(cfg_valid && cfg_ready)begin
    admission_ready<=0;owner<=cfg_id;owner_context<=cfg_context;generation<=cfg_generation;epoch<=cfg_epoch;step<=cfg_symbol_step;h<=cfg_h;phase_steps<=cfg_phase_steps;
    header_position<=cfg_training_position+(cfg_wide_step<<10)-(cfg_wide_step<<5);
    pilot_position<=cfg_training_position+(cfg_wide_step<<10)+(cfg_wide_step<<8)+(cfg_wide_step<<6)+(cfg_wide_step<<5);
    nominal0<=32'd0-(cfg_phase_steps[31:0]<<10)-(cfg_phase_steps[31:0]<<9);
    nominal1<=32'd0-(cfg_phase_steps[63:32]<<10)-(cfg_phase_steps[63:32]<<9)-cfg_phase_steps[63:32];
    age<=0;received<=0;point<=0;tx<=0;sample_sent<=0;header_detail<=0;state<=HEADER_LAUNCH;
   end
   if(busy)begin
    age<=age+1'b1;
    if(owner_lost)begin finish(4,0);header_held<=0;end
    else if(business_fault)begin finish(7,business_detail);header_held<=0;end
    else if(age==WORK_LIMIT-1)begin finish(6,0);header_held<=0;end
    else if(smooth_abort)begin finish(3,{1'b0,smooth_reason});status_point<=smooth_point;header_held<=0;end
    else if(timing_error!=0)begin finish(5,{1'b0,timing_error});header_held<=0;end
    else begin
     if(launching && job_launch_ready)begin
      received<=0;sample_sent<=0;state<=piloting?PILOT_ACTIVE:HEADER_ACTIVE;
     end
     if(sample_valid && sample_ready && sampling)begin
      received<=received+1'b1;sample_sent<=1;
      if(sample_bad || sample_error!=0)begin
       if(piloting)begin finish(2,sample_bad?3'd4:sample_error);header_held<=0;end
       else header_detail<=sample_bad?3'd4:sample_error;
      end
     end
     if(state==HEADER_ACTIVE && decoded_valid)begin
      header_held<=1;header_error<=decoded_id==owner?decoded_error:3'd4;
      header_raw<=decoded_header;header_session<=decoded_id==owner?decoded_session:32'd0;header_superframe<=decoded_id==owner?decoded_frame:32'd0;
      header_stream0_bytes<=decoded_id==owner?decoded_l0:16'd0;header_stream1_bytes<=decoded_id==owner?decoded_l1:16'd0;
      if(decoded_error!=0 || decoded_id!=owner)finish(1,decoded_id==owner?decoded_error:3'd4);
      else state<=FRAME_LAUNCH;
     end
     if(state==FRAME_LAUNCH && smooth_ready)state<=PILOT_LAUNCH;
     if(state==PILOT_ACTIVE && observation_accepted && sample_sent)begin
      if(tx && point==273)state<=DRAIN;
      else begin
       pilot_position<=pilot_position+(tx?((wide_step<<6)+wide_step):wide_step);
       tx<=!tx;if(tx)begin point<=point+1'b1;nominal0<=nominal0-(phase_steps[31:0]<<6)-(phase_steps[31:0]<<1);nominal1<=nominal1-(phase_steps[63:32]<<6)-(phase_steps[63:32]<<1);end
       state<=PILOT_LAUNCH;
      end
     end
     if(state==DRAIN && !smooth_active && !phase_valid_q && !header_held && (!BUSINESS_ENABLED || business_done))finish(0,0);
    end
   end
  end
 end
endmodule
