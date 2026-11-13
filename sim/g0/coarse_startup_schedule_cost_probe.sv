`timescale 1ns/1ps
// Coarse candidate -> causal warmup crop -> configured startup job.
// Public symbols416..863, nominal4ADC/symbol. Software position is endpoint
// peak+20+4*(symbol-256); RTL F1 coordinate is that endpoint minus20.
module coarse_startup_schedule_cost_probe(
 input wire clk,rst,cancel,
 input wire candidate_valid,output wire candidate_ready,
 input wire [31:0] candidate_id,candidate_generation,
 input wire [63:0] candidate_first,candidate_peak,candidate_known,
 input wire [31:0] candidate_step,candidate_phase,input wire candidate_sense,
 input wire [2:0] candidate_error,input wire [47:0] phase_origin,
 output wire cfg_valid,input wire cfg_ready,cfg_applied,input wire [1:0] cfg_error,
 output reg [63:0] cfg_tag,output reg [31:0] cfg_phase,cfg_step,output reg cfg_sense,
 input wire window_ready,history_restart,input wire [31:0] active_context,history_generation,
 output wire job_valid,input wire job_ready,
 output reg [31:0] job_id,job_context,job_generation,output reg [15:0] job_epoch,
 output reg [79:0] job_first_position,output wire [34:0] job_step,
 output reg [8:0] job_count,output reg [9:0] job_first_symbol,
 output reg [63:0] job_commit_tag,output reg [47:0] job_origin,
 input wire result_valid,output wire result_ready,
 input wire [31:0] result_id,result_context,result_generation,
 input wire [2:0] result_error,result_detail,input wire result_committed,
 input wire [31:0] result_final_step,result_final_phase,
 output reg out_valid,input wire out_ready,output reg [31:0] out_id,out_generation,
 output reg [2:0] out_error,out_detail,output reg [31:0] out_final_step,out_final_phase,
 output wire idle
);
 localparam IDLE=0,CONFIGURE=1,STATUS=2,BIND=3,LAUNCH=4,RESULT=5;
 reg [2:0] state;
 wire [47:0] peak_offset=candidate_peak[47:0]-candidate_first[47:0];
 wire [47:0] known_offset=candidate_known[47:0]-candidate_first[47:0];
 wire [47:0] delay_samples=candidate_known[47:0]-candidate_peak[47:0];
 // Strict endpoint>known+82 => 4*(symbol-256)>known-peak+62.
 wire [48:0] crop_symbol=49'd257+(({1'b0,delay_samples}+49'd62)>>2);
 wire [9:0] first_symbol=crop_symbol<416?10'd416:crop_symbol[9:0];
 wire [47:0] first_center=candidate_peak[47:0]+(({38'd0,first_symbol}-48'd256)<<2);
 wire geometry_bad=candidate_first[63:48]!=candidate_peak[63:48] ||
                   candidate_first[63:48]!=candidate_known[63:48] ||
                   peak_offset>511 || known_offset!=1152 || crop_symbol>736;
 assign idle=state==IDLE && !out_valid;
 assign candidate_ready=!rst && !cancel && idle;
 assign cfg_valid=!rst && !cancel && state==CONFIGURE;
 assign job_valid=!rst && !cancel && state==LAUNCH;
 assign job_step=35'd17179869184;
 assign result_ready=!rst && !cancel && state==RESULT && !out_valid;
 task fail(input [2:0] code,input [2:0] detail);
 begin state<=IDLE;out_valid<=1;out_error<=code;out_detail<=detail;out_final_step<=0;out_final_phase<=0;end
 endtask
 always @(posedge clk) begin
  if(rst || cancel) begin
   state<=IDLE;cfg_tag<=0;cfg_phase<=0;cfg_step<=0;cfg_sense<=0;
   job_id<=0;job_context<=0;job_generation<=0;job_epoch<=0;job_first_position<=0;
   job_count<=0;job_first_symbol<=0;job_commit_tag<=0;job_origin<=0;
   out_valid<=0;out_id<=0;out_generation<=0;out_error<=0;out_detail<=0;out_final_step<=0;out_final_phase<=0;
  end else begin
   if(out_valid && out_ready) out_valid<=0;
   case(state)
    IDLE: if(candidate_valid && candidate_ready) begin
     out_id<=candidate_id;out_generation<=candidate_generation;
     out_error<=0;out_detail<=0;out_final_step<=0;out_final_phase<=0;
     cfg_tag<=candidate_known;cfg_phase<=candidate_phase;cfg_step<=candidate_step;cfg_sense<=candidate_sense;
     job_id<=candidate_id;job_epoch<=candidate_peak[63:48];job_origin<=phase_origin;
     job_first_symbol<=first_symbol;job_count<=10'd864-first_symbol;
     job_first_position<={first_center,32'd0};
     job_commit_tag<={candidate_peak[63:48],candidate_peak[47:0]+48'd2960};
     if(candidate_error!=0) fail(3'd1,candidate_error);
     else if(geometry_bad) fail(3'd2,3'd1);
     else state<=CONFIGURE;
    end
    CONFIGURE: if(cfg_ready) begin
     if(cfg_applied) state<=BIND;
     else state<=STATUS;
    end
    STATUS: fail(3'd3,{1'b0,cfg_error});
    BIND: if(window_ready && !history_restart) begin
     job_context<=active_context;job_generation<=history_generation;state<=LAUNCH;
    end
    LAUNCH: if(job_ready) state<=RESULT;
    RESULT: if(result_valid && result_ready) begin
     if(result_id!=job_id || result_context!=job_context || result_generation!=job_generation) fail(3'd5,3'd1);
     else if(result_error!=0) fail(3'd4,result_detail);
     else if(!result_committed) fail(3'd5,3'd2);
     else begin
      state<=IDLE;out_valid<=1;out_error<=0;out_detail<=0;
      out_final_step<=result_final_step;out_final_phase<=result_final_phase;
     end
    end
    default: fail(3'd5,3'd3);
   endcase
  end
 end
endmodule
