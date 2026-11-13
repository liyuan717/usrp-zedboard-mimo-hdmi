`timescale 1ns/1ps
// Real SG-node queues -> group interpolation -> shared F1 requests -> full ML.
// Eight nodes per TX accommodate the four-point SG edge burst plus the left
// endpoint retained until the opposite TX's right endpoint becomes available.
// No sample RAM: every business vector comes from the frame's ONE F1 service.
module payload_schedule_cost_probe(
 input wire clk,rst,cancel,cfg_valid,output wire cfg_ready,
 input wire [31:0] cfg_id,cfg_context,cfg_generation,input wire [15:0] cfg_epoch,
 input wire [79:0] cfg_training_position,input wire [34:0] cfg_symbol_step,
 input wire [207:0] cfg_h,input wire [63:0] cfg_phase_steps,
 input wire trusted_header,
 input wire node_valid,output wire node_ready,input wire [31:0] node_id,node_context,node_generation,
 input wire [15:0] node_epoch,input wire [8:0] node_point,node_known,input wire node_tx,input wire signed [40:0] node_phase,
 output wire req_valid,input wire req_ready,output wire [31:0] req_generation,req_id,
 output wire [63:0] req_tag,output wire [15:0] req_mu,
 input wire sample_valid,output wire sample_ready,input wire [31:0] sample_generation,sample_id,
 input wire [63:0] sample_tag,input wire [15:0] sample_mu,input wire [2:0] sample_error,input wire [83:0] sample_data,
 output wire out_valid,input wire out_ready,output wire [31:0] out_id,out_context,out_generation,
 output wire [15:0] out_epoch,output wire [14:0] out_symbol,output wire [3:0] out_decision,output wire [61:0] out_metric,
 output reg done,output reg abort,output reg [2:0] detail,output wire active
);
 localparam IDLE=0,HEADER=1,NODES=2,GROUP=3,DRAIN=4;
 reg [2:0] state;reg admission_ready,pending;
 reg [31:0] owner,context_id,generation;reg [15:0] epoch;reg [34:0] step;reg [63:0] phase_steps,nominal;
 reg [79:0] position;reg [8:0] group;reg [6:0] sent,joined;reg [14:0] results;
 reg [31:0] pending_id;reg [63:0] pending_tag;reg [15:0] pending_mu;
 (* ram_style="distributed" *) reg signed [40:0] nodes0[0:7],nodes1[0:7];
 reg [2:0] rd0,rd1,wr0,wr1;reg [3:0] count0,count1;reg [8:0] next0,next1;
 wire math_cfg_ready,group_ready,math_in_ready,math_busy,math_valid;wire [2:0] math_error;
 wire [31:0] math_id,math_context,math_generation;wire [15:0] math_epoch;wire [14:0] math_symbol;
 wire [3:0] math_decision;wire [61:0] math_metric;
 wire fence=cancel || abort;
 assign active=state!=IDLE;
 assign cfg_ready=admission_ready && !rst && !fence && !active;
 wire [79:0] wide_step={45'd0,step},initial_step={45'd0,cfg_symbol_step};
 wire [80:0] rounded={1'b0,position}+81'd32768;
 wire have_nodes=count0>=(group==273?1:2) && count1>=(group==273?1:2);
 wire launch=state==NODES && have_nodes && !fence;
 wire pop=launch && group_ready;
 // Full queues wait for registered space. Do not look through a same-edge
 // group pop into the phase consumer's nested configuration/cancel READY.
 wire node_capacity=node_tx?count1<8:count0<8;
 wire [8:0] expected_known=node_point<4?9'd6:(node_point>=270?9'd273:node_point+9'd3);
 wire bad_node=node_id!=owner || node_context!=context_id || node_generation!=generation || node_epoch!=epoch ||
  node_point!=(node_tx?next1:next0) || node_point>273 || node_known!=expected_known;
 assign node_ready=!rst && !fence && (state==NODES || state==GROUP || state==DRAIN) && node_capacity && !bad_node;
 wire node_fire=node_valid && node_ready;
 wire bad_sample=!pending || sample_generation!=generation || sample_id!=pending_id || sample_tag!=pending_tag || sample_mu!=pending_mu || sample_error!=0;
 assign sample_ready=!rst && !fence && state==GROUP && math_in_ready && !bad_sample;
 wire sample_fire=sample_valid && sample_ready;
 wire bad_result=math_error!=0 || math_id!=owner || math_context!=context_id || math_generation!=generation || math_epoch!=epoch || math_symbol!=results;
 assign out_valid=math_valid && !bad_result && !rst && !fence;
 assign out_id=math_id;assign out_context=math_context;assign out_generation=math_generation;assign out_epoch=math_epoch;
 assign out_symbol=math_symbol;assign out_decision=math_decision;assign out_metric=math_metric;
 assign req_valid=!rst && !fence && state==GROUP && !pending && sent<64;
 assign req_generation=generation;assign req_id={17'd0,group,sent[5:0]};assign req_tag={epoch,rounded[79:32]};assign req_mu=rounded[31:16];
 wire [2:0] right0=group==273?rd0:rd0+3'd1,right1=group==273?rd1:rd1+3'd1;
 payload_datapath_cost_probe math(.clk(clk),.rst(rst),.cancel(fence),
  .frame_valid(cfg_valid && cfg_ready),.frame_ready(math_cfg_ready),.frame_id(cfg_id),.frame_context(cfg_context),.frame_generation(cfg_generation),.frame_epoch(cfg_epoch),.frame_h(cfg_h),
  .group_valid(launch),.group_ready(group_ready),.group_id(owner),.group_context(context_id),.group_generation(generation),.group_epoch(epoch),
  .group_index(group),.group_nominal(nominal),.group_steps(phase_steps),.group_left0(nodes0[rd0]),.group_right0(nodes0[right0]),.group_left1(nodes1[rd1]),.group_right1(nodes1[right1]),
  .in_valid(sample_valid && state==GROUP && !bad_sample && !fence),.in_ready(math_in_ready),.in_id(owner),.in_context(context_id),.in_generation(generation),.in_epoch(epoch),.in_symbol(pending_id[14:0]),.in_error(3'd0),.in_y(sample_data),
  .out_valid(math_valid),.out_ready(out_ready && !bad_result),.out_id(math_id),.out_context(math_context),.out_generation(math_generation),.out_epoch(math_epoch),.out_symbol(math_symbol),.out_error(math_error),.out_decision(math_decision),.out_metric(math_metric),.busy(math_busy));
 always @(posedge clk)begin
  if(rst || cancel)begin
   state<=IDLE;admission_ready<=0;pending<=0;owner<=0;context_id<=0;generation<=0;epoch<=0;step<=0;phase_steps<=0;nominal<=0;position<=0;group<=0;sent<=0;joined<=0;results<=0;
   pending_id<=0;pending_tag<=0;pending_mu<=0;rd0<=0;rd1<=0;wr0<=0;wr1<=0;count0<=0;count1<=0;next0<=0;next1<=0;done<=0;abort<=0;detail<=0;
  end else begin
   admission_ready<=!active && math_cfg_ready && !fence;
   if(cfg_valid && cfg_ready)begin
    admission_ready<=0;state<=HEADER;owner<=cfg_id;context_id<=cfg_context;generation<=cfg_generation;epoch<=cfg_epoch;step<=cfg_symbol_step;phase_steps<=cfg_phase_steps;
    position<=cfg_training_position+(initial_step<<10)+(initial_step<<8)+(initial_step<<6)+(initial_step<<5)+(initial_step<<1);
    nominal<={32'((cfg_phase_steps[63:32]<<10)+(cfg_phase_steps[63:32]<<9)+(cfg_phase_steps[63:32]<<1)),32'((cfg_phase_steps[31:0]<<10)+(cfg_phase_steps[31:0]<<9)+(cfg_phase_steps[31:0]<<1))};
    pending<=0;group<=0;sent<=0;joined<=0;results<=0;rd0<=0;rd1<=0;wr0<=0;wr1<=0;count0<=0;count1<=0;next0<=0;next1<=0;done<=0;abort<=0;detail<=0;
   end
   if(state==HEADER && trusted_header)state<=NODES;
   if(active && !fence)begin
    if(node_valid && node_capacity && bad_node)begin abort<=1;detail<=5;end
    else if(sample_valid && bad_sample)begin abort<=1;detail<=sample_error!=0?sample_error:3'd4;end
    else if(math_valid && bad_result)begin abort<=1;detail<=6;end
    else begin
     if(node_fire)begin
      if(node_tx)begin nodes1[wr1]<=node_phase;wr1<=wr1+1'b1;next1<=next1+1'b1;end
      else begin nodes0[wr0]<=node_phase;wr0<=wr0+1'b1;next0<=next0+1'b1;end
     end
     case({node_fire && !node_tx,pop})2'b10:count0<=count0+1'b1;2'b01:count0<=count0-1'b1;default:begin end endcase
     case({node_fire && node_tx,pop})2'b10:count1<=count1+1'b1;2'b01:count1<=count1-1'b1;default:begin end endcase
     if(pop)begin rd0<=rd0+1'b1;rd1<=rd1+1'b1;state<=GROUP;sent<=0;joined<=0;end
     if(req_valid && req_ready)begin
      pending<=1;pending_id<=req_id;pending_tag<=req_tag;pending_mu<=req_mu;sent<=sent+1'b1;
      position<=position+(sent==63?wide_step+(wide_step<<1):wide_step);
     end
     if(sample_fire)begin
      pending<=0;joined<=joined+1'b1;
      if(joined==63)begin
       if(group==273)state<=DRAIN;
       else begin state<=NODES;group<=group+1'b1;nominal<={32'(nominal[63:32]+(phase_steps[63:32]<<6)+(phase_steps[63:32]<<1)),32'(nominal[31:0]+(phase_steps[31:0]<<6)+(phase_steps[31:0]<<1))};end
      end
     end
     if(out_valid && out_ready)results<=results+1'b1;
     if(state==DRAIN && !math_busy && results==17536 && !pending && count0==0 && count1==0)begin done<=1;state<=IDLE;end
    end
   end
  end
 end
endmodule
