`timescale 1ns/1ps
// Configured finite G0 frontend -> two training columns -> atomic matrix.
// Timing/coarse NCO parameters are explicitly supplied; residual CFO/H comes
// from actual interpolated training. No acquisition/header/pilot consumer.
module training_frontend_cost_probe #(
 parameter SINE_FILE="sine.hex",COEFFICIENT_FILE="rrc.hex",
           SIGN_FILE="training_signs.hex",WAIT_LIMIT=4096,ENABLE_STARTUP=0,ENABLE_FINAL_CONFIG=0,ENABLE_CONJUGATE=0,ENABLE_TIMED_CONFIG=0,ENABLE_AUTOCOMMIT=0,EXTERNAL_HISTORY=0
)(
 input wire clk,rst,cancel,
 input wire cfg_valid,output wire cfg_ready,
 input wire [63:0] cfg_tag,input wire [31:0] cfg_phase,cfg_step,
 input wire in_valid,output wire in_ready,
 input wire [63:0] in_tag,input wire [63:0] in_data,
 input wire job_valid,output wire job_ready,input wire [31:0] job_id,
 input wire [31:0] job_context,job_generation,input wire [15:0] job_epoch,
 input wire [79:0] job_first_position,input wire [34:0] job_step,
 output reg out_valid,input wire out_ready,
 output reg [31:0] out_id,out_context,out_generation,
 output reg [15:0] out_epoch,
 output reg [207:0] out_h,output reg [63:0] out_phase_steps,
 output reg [1:0] out_selected_rx,output reg [199:0] out_energies,
 output reg [2:0] out_error,out_detail,
 output wire armed,window_ready,request_idle,
 output wire [31:0] active_context,history_generation,
 output wire [63:0] latest_tag,output wire [9:0] history_count,
 output wire history_restart,
 input wire job_startup,input wire [8:0] job_count,input wire [9:0] job_first_symbol,
 output reg out_startup,output reg signed [47:0] out_numerator,
 output reg [15:0] out_divisor,output reg [72:0] out_quality_sum,
 output reg [71:0] out_coherent,output reg out_admitted,
 input wire [63:0] job_commit_tag,input wire [47:0] job_phase_origin_index,
 output reg [63:0] out_commit_tag,output reg [31:0] out_final_step,out_final_phase,
 output reg [1:0] out_config_error,
 input wire cfg_conjugate,output wire cfg_applied,output wire [1:0] cfg_error,
 output reg out_committed,output reg [31:0] out_commit_context,
 output wire shared_in_valid,shared_in_start,output wire [63:0] shared_in_tag,output wire [79:0] shared_in_data,
 input wire shared_ready,input wire [31:0] shared_generation,input wire [63:0] shared_latest,
 input wire [12:0] shared_count,input wire shared_restart,
 output wire shared_read_enable,output wire [11:0] shared_read_address,input wire [79:0] shared_read_word
);
 localparam IDLE=0,LAUNCH=1,STREAM=2,WAIT_COLUMN=3,WAIT_CONFIG=4,WAIT_COMMIT=5,WAIT_STATUS=6;
 reg [2:0] state;
 reg column,startup;
 reg [31:0] coarse_step;reg [63:0] commit_tag;reg [47:0] phase_origin;
 wire fine_ready,fine_valid;wire [31:0] fine_id,fine_generation,fine_step,fine_phase;
 wire [63:0] fine_tag;wire [1:0] fine_error;
 reg [8:0] count;
 reg [9:0] first_symbol;
 reg [31:0] owner,owner_context,owner_generation;
 reg [15:0] owner_epoch;
 reg [79:0] first_position;
 reg [34:0] step;
 reg [8:0] received;
 reg request_cancel;
 reg [103:0] first_h;
 reg [31:0] first_step;
 reg first_selected;
 reg [99:0] first_energies;
 reg [63:0] expected_adc_tag;
 wire automatic_config=ENABLE_AUTOCOMMIT && state==WAIT_COMMIT;
 wire frontend_cfg_ready,frontend_cfg_applied;wire [1:0] frontend_cfg_error;
 reg active_sense;
 wire [63:0] effective_tag=automatic_config?out_commit_tag:cfg_tag;
 wire [31:0] effective_phase=automatic_config?out_final_phase:cfg_phase;
 wire [31:0] effective_step=automatic_config?out_final_step:cfg_step;
 wire effective_sense=automatic_config?active_sense:cfg_conjugate;
 assign cfg_ready=!automatic_config && frontend_cfg_ready;
 assign cfg_applied=frontend_cfg_applied;assign cfg_error=frontend_cfg_error;
 wire cfg_fire=frontend_cfg_applied;
 wire own_commit=automatic_config && frontend_cfg_applied;
 always @(posedge clk) begin
  if(rst || cancel) active_sense<=0;
  else if(cfg_fire) active_sense<=effective_sense;
 end
 // Mirror only the documented contiguous ADC interface. This anticipates a
 // rejection on the publication edge, before registered armed drops.
 wire input_gap=in_valid && in_ready && in_tag!=expected_adc_tag;
 always @(posedge clk) begin
  if(rst || cancel) expected_adc_tag<=0;
  else if(cfg_fire) expected_adc_tag<=effective_tag;
  else if(in_valid && in_ready && !input_gap)
    expected_adc_tag<={expected_adc_tag[63:48],expected_adc_tag[47:0]+48'd1};
 end
 wire active=state!=IDLE;
 wire context_lost=cfg_fire || input_gap || !armed || active_context!=owner_context;
 wire generation_lost=history_restart || history_generation!=owner_generation ||
                      latest_tag[63:48]!=owner_epoch;
 wire owner_lost=active && !own_commit && (context_lost || generation_lost);

 wire timing_ready,timing_done;
 wire [1:0] timing_error;
 wire [15:0] timing_emitted;
 wire sample_valid,sample_ready;
 wire [31:0] sample_generation,sample_id;
 wire [63:0] sample_tag;
 wire [15:0] sample_mu;
 wire [2:0] sample_error;
 wire [83:0] sample_data;
 wire [31:0] bound_generation,nco_fences,rrc_fences,history_fences;
 wire train_cfg_ready,train_ready,train_valid;
 wire [31:0] train_id;
 wire train_tx,train_selected;
 wire signed [31:0] train_step;
 wire [103:0] train_h;
 wire [2:0] train_error;
 wire [49:0] train_energy0,train_energy1;
 wire signed [47:0] train_numerator;wire [15:0] train_divisor;
 wire [72:0] train_quality;wire [71:0] train_coherent;wire train_admitted;
 wire [31:0] expected_request_id=(column?32'd512:32'd0)+{23'd0,received};
 wire sample_identity_bad=sample_generation!=owner_generation ||
                          sample_id!=expected_request_id || sample_tag[63:48]!=owner_epoch;
 // No combination from a cancel-gated sample_valid back to request_cancel.
 // A terminal error schedules a synchronous local cancel for the next edge.
 wire launch_enable=state==LAUNCH && !owner_lost && !request_cancel;
 wire timing_valid=launch_enable && train_cfg_ready;
 wire train_cfg_valid=launch_enable && timing_ready;
 wire launch_fire=timing_valid && timing_ready;
 wire [79:0] column_position=first_position+(column?{36'd0,step,9'd0}:80'd0);
 assign sample_ready=state==STREAM && !owner_lost && !request_cancel ?
                     ((sample_identity_bad || sample_error!=0)?1'b1:train_ready):1'b1;
 wire train_input_valid=state==STREAM && !owner_lost && !request_cancel &&
                       sample_valid && !sample_identity_bad && sample_error==0;
 assign job_ready=!rst && !cancel && !request_cancel && state==IDLE && !out_valid &&
                  window_ready && request_idle && train_cfg_ready && !history_restart && !input_gap;
 wire job_fire=job_valid && job_ready;

 wire fine_launch=state==WAIT_COLUMN && startup && train_valid && train_error==0 &&
                  train_admitted && train_id==owner && train_tx==column && request_idle && !owner_lost;
 generate if(ENABLE_FINAL_CONFIG) begin: final_configuration
  fine_nco_config_cost_probe calculator(
   .clk(clk),.rst(rst),.cancel(cancel || request_cancel || owner_lost),
   .in_valid(fine_launch),.in_ready(fine_ready),.in_id(owner),.in_generation(owner_generation),
   .in_numerator(train_numerator),.in_divisor(train_divisor),.in_coarse_step(coarse_step),
   .in_known_tag(commit_tag),.in_phase_origin_index(phase_origin),
   .out_valid(fine_valid),.out_ready(1'b1),.out_id(fine_id),.out_generation(fine_generation),
   .out_known_tag(fine_tag),.out_step(fine_step),.out_phase(fine_phase),.out_error(fine_error));
 end else begin: no_final_configuration
  assign fine_ready=1'b1;assign fine_valid=1'b0;assign fine_id=0;assign fine_generation=0;
  assign fine_tag=0;assign fine_step=0;assign fine_phase=0;assign fine_error=0;
 end endgenerate
 always @(posedge clk) begin
  if(rst || cancel) coarse_step<=0;
  else if(cfg_fire) coarse_step<=effective_step;
 end

 frontend_chain_cost_probe #(.SINE_FILE(SINE_FILE),.COEFFICIENT_FILE(COEFFICIENT_FILE),
                            .WAIT_LIMIT(WAIT_LIMIT),.ENABLE_CONJUGATE(ENABLE_CONJUGATE),.ENABLE_TIMED_CONFIG(ENABLE_TIMED_CONFIG),.EXTERNAL_HISTORY(EXTERNAL_HISTORY)) frontend(
  .clk(clk),.rst(rst),.cancel(cancel),
  .cfg_valid(automatic_config || cfg_valid),.cfg_ready(frontend_cfg_ready),.cfg_tag(effective_tag),.cfg_phase(effective_phase),.cfg_step(effective_step),.cfg_conjugate(effective_sense),.cfg_applied(frontend_cfg_applied),.cfg_error(frontend_cfg_error),
  .in_valid(in_valid),.in_ready(in_ready),.in_tag(in_tag),.in_data(in_data),
  .timing_cfg_valid(timing_valid),.timing_cfg_ready(timing_ready),
  .timing_cfg_epoch(owner_epoch),.timing_cfg_context(owner_context),
  .timing_cfg_first_position(column_position),.timing_cfg_step(step),
  .timing_cfg_count({7'd0,count}),.timing_cfg_first_id(column?32'd512:32'd0),
  .timing_done(timing_done),.timing_cfg_error(timing_error),.timing_emitted_count(timing_emitted),
  .out_valid(sample_valid),.out_ready(sample_ready),.out_generation(sample_generation),
  .out_id(sample_id),.out_tag(sample_tag),.out_mu(sample_mu),.out_error(sample_error),.out_data(sample_data),
  .armed(armed),.window_ready(window_ready),.active_context(active_context),
  .bound_generation(bound_generation),.history_generation(history_generation),.latest_tag(latest_tag),
  .history_count(history_count),.nco_fences(nco_fences),.rrc_fences(rrc_fences),.history_fences(history_fences),
  .request_cancel(request_cancel),.request_idle(request_idle),.history_restart(history_restart),
  .shared_in_valid(shared_in_valid),.shared_in_start(shared_in_start),.shared_in_tag(shared_in_tag),.shared_in_data(shared_in_data),
  .shared_ready(shared_ready),.shared_generation(shared_generation),.shared_latest(shared_latest),.shared_count(shared_count),.shared_restart(shared_restart),
  .shared_read_enable(shared_read_enable),.shared_read_address(shared_read_address),.shared_read_word(shared_read_word));
 training_channel_cost_probe #(.SIGN_FILE(SIGN_FILE),.SINE_FILE(SINE_FILE),.ENABLE_STARTUP(ENABLE_STARTUP)) training(
  .clk(clk),.rst(rst),.cancel(cancel || request_cancel),
  .cfg_valid(train_cfg_valid),.cfg_ready(train_cfg_ready),.cfg_id(owner),.cfg_tx(column),
  .cfg_startup(startup),.cfg_count(count),.cfg_first_symbol(first_symbol),
  .in_valid(train_input_valid),.in_ready(train_ready),.in_id(owner),.in_index(sample_id[8:0]),
  .in_error(1'b0),.in_data(sample_data),.out_valid(train_valid),.out_ready(1'b1),
  .out_id(train_id),.out_tx(train_tx),.out_selected_rx(train_selected),.out_phase_step(train_step),
  .out_h(train_h),.out_error(train_error),.out_energy0(train_energy0),.out_energy1(train_energy1),
  .out_numerator(train_numerator),.out_divisor(train_divisor),.out_quality_sum(train_quality),
  .out_coherent(train_coherent),.out_admitted(train_admitted));

 // Errors invalidate the entire matrix, including a privately staged TX0.
 task terminal_error(input [2:0] reason,input [2:0] detail);
  begin
   state<=IDLE;request_cancel<=1;out_valid<=1;out_error<=reason;out_detail<=detail;
   out_h<=0;out_phase_steps<=0;out_selected_rx<=0;out_energies<=0;
   first_h<=0;first_step<=0;first_selected<=0;first_energies<=0;
   out_numerator<=0;out_divisor<=0;out_quality_sum<=0;out_coherent<=0;out_admitted<=0;
   out_commit_tag<=0;out_final_step<=0;out_final_phase<=0;out_config_error<=0;out_committed<=0;out_commit_context<=0;
  end
 endtask
 always @(posedge clk) begin
  if(rst || cancel) begin
   commit_tag<=0;phase_origin<=0;
   startup<=0;count<=448;first_symbol<=416;out_startup<=0;
   out_numerator<=0;out_divisor<=0;out_quality_sum<=0;out_coherent<=0;out_admitted<=0;
   out_commit_tag<=0;out_final_step<=0;out_final_phase<=0;out_config_error<=0;out_committed<=0;out_commit_context<=0;
   state<=IDLE;column<=0;owner<=0;owner_context<=0;owner_generation<=0;owner_epoch<=0;
   first_position<=0;step<=0;received<=0;request_cancel<=0;
   first_h<=0;first_step<=0;first_selected<=0;first_energies<=0;
   out_valid<=0;out_id<=0;out_context<=0;out_generation<=0;out_epoch<=0;
   out_h<=0;out_phase_steps<=0;out_selected_rx<=0;out_energies<=0;out_error<=0;out_detail<=0;
  end else begin
   request_cancel<=0;
   if(out_valid && out_ready) out_valid<=0;
   if(owner_lost) terminal_error(3'd1,input_gap?3'd4:(context_lost?3'd1:3'd2));
   else if(active && timing_error!=0) terminal_error(3'd2,{1'b0,timing_error});
   else case(state)
    IDLE: if(job_fire) begin
     owner<=job_id;owner_context<=job_context;owner_generation<=job_generation;owner_epoch<=job_epoch;
     out_id<=job_id;out_context<=job_context;out_generation<=job_generation;out_epoch<=job_epoch;
     commit_tag<=job_commit_tag;phase_origin<=job_phase_origin_index;
     startup<=ENABLE_STARTUP && job_startup;out_startup<=ENABLE_STARTUP && job_startup;
     count<=(ENABLE_STARTUP && job_startup)?job_count:9'd448;
     first_symbol<=(ENABLE_STARTUP && job_startup)?job_first_symbol:10'd416;
     out_numerator<=0;out_divisor<=0;out_quality_sum<=0;out_coherent<=0;out_admitted<=0;
     out_commit_tag<=0;out_final_step<=0;out_final_phase<=0;out_config_error<=0;out_committed<=0;out_commit_context<=0;
     first_position<=job_first_position;step<=job_step;column<=0;received<=0;
     out_h<=0;out_phase_steps<=0;out_selected_rx<=0;out_energies<=0;out_error<=0;out_detail<=0;
     first_h<=0;first_step<=0;first_selected<=0;first_energies<=0;
     if(job_context!=active_context || job_generation!=history_generation || job_epoch!=latest_tag[63:48])
       terminal_error(3'd1,3'd3);
     else if(job_step<35'd17176433210 || job_step>35'd17183305158)
       terminal_error(3'd2,3'd2);
     else if(ENABLE_STARTUP && job_startup && (job_count<128 || job_count>448 ||
             job_first_symbol<384 || {1'b0,job_first_symbol}+{2'd0,job_count}>896))
       terminal_error(3'd4,3'd5);
     else state<=LAUNCH;
    end
    LAUNCH: if(launch_fire) begin state<=STREAM;received<=0;end
    STREAM: if(sample_valid && sample_ready) begin
     if(sample_identity_bad) terminal_error(3'd5,3'd1);
     else if(sample_error!=0) terminal_error(3'd3,sample_error);
     else if(received==count-1'b1) state<=WAIT_COLUMN;
     else received<=received+1'b1;
    end
    WAIT_COLUMN: if(train_valid) begin
     if(train_id!=owner || train_tx!=column || !request_idle) terminal_error(3'd5,3'd2);
     else if(startup) begin
      if(train_error!=0) terminal_error(3'd4,train_error);
      else begin
       out_h<=0;out_phase_steps<={32'd0,train_step};out_selected_rx<={1'b0,train_selected};
       out_energies<={100'd0,train_energy1,train_energy0};
       out_error<=0;out_detail<=0;
       if(ENABLE_FINAL_CONFIG) begin
        if(fine_ready) state<=WAIT_CONFIG;
        else terminal_error(3'd6,3'd1);
       end else begin out_valid<=1;state<=IDLE;end
      end
      // Preserve rejected startup arithmetic diagnostics, but never admit it.
      out_numerator<=train_numerator;out_divisor<=train_divisor;
      out_quality_sum<=train_quality;out_coherent<=train_coherent;out_admitted<=train_admitted;
     end
     else if(train_error!=0) terminal_error(3'd4,train_error);
     else if(!column) begin
      first_h<=train_h;first_step<=train_step;first_selected<=train_selected;
      first_energies<={train_energy1,train_energy0};column<=1;state<=LAUNCH;
     end else begin
      out_h<={train_h,first_h};out_phase_steps<={train_step,first_step};
      out_selected_rx<={train_selected,first_selected};
      out_energies<={train_energy1,train_energy0,first_energies};
      out_error<=0;out_detail<=0;out_valid<=1;state<=IDLE;
     end
    end
    WAIT_CONFIG: if(fine_valid) begin
     if(fine_id!=owner || fine_generation!=owner_generation || fine_tag!=commit_tag)
       terminal_error(3'd6,3'd2);
     else if(fine_error!=0) begin
      terminal_error(3'd6,3'd3);out_config_error<=fine_error;
     end else begin
      out_commit_tag<=fine_tag;out_final_step<=fine_step;out_final_phase<=fine_phase;
      out_config_error<=0;
      if(ENABLE_AUTOCOMMIT) state<=WAIT_COMMIT;
      else begin out_valid<=1;state<=IDLE;end
     end
    end
    WAIT_COMMIT: if(frontend_cfg_ready) begin
     if(frontend_cfg_applied) begin
      out_committed<=1;out_commit_context<=active_context+32'd1;out_valid<=1;state<=IDLE;
     end else state<=WAIT_STATUS;
    end
    WAIT_STATUS: begin
     terminal_error(3'd6,3'd4);out_config_error<=frontend_cfg_error;
    end
    default: terminal_error(3'd5,3'd3);
   endcase
  end
 end
endmodule
