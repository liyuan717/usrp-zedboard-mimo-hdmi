`timescale 1ns/1ps
// Finite G0 public differential coarse candidate. Calibration/notch,
// variable-length startup fine CFO/quality and coherent timing are separate.
module differential_acquisition_cost_probe(
 input wire clk,rst,cancel,
 input wire in_valid,output wire in_ready,input wire in_start,
 input wire [63:0] in_tag,in_data,
 input wire cfg_valid,output wire cfg_ready,input wire [31:0] cfg_id,cfg_generation,
 input wire [63:0] cfg_search_tag,
 input wire [47:0] cfg_phase_origin_index,
 output reg out_valid,input wire out_ready,
 output reg [31:0] out_id,out_generation,
 output reg [63:0] out_first_tag,out_peak_tag,out_last_sample_tag,out_known_tag,
 output reg out_sense,out_rx,
 output reg [31:0] out_raw_phase,out_nco_step,out_nco_phase,
 output reg signed [48:0] out_re,out_im,
 output reg [99:0] out_n,output reg [69:0] out_energy,
 output reg [5:0] out_shift,output reg [2:0] out_error,
 output wire [31:0] active_generation,output wire history_restart,
 output wire [31:0] input_fences,correlation_fences
);
 localparam IDLE=0,SEARCH=1,WINDOW=2,DRAIN=3,NORMALIZE=4,ANGLE_START=5,ANGLE_WAIT=6,
            NORMALIZE_PREP=7,PHASE_FINISH=8;
 reg [3:0] state;
 reg [31:0] owner,owner_generation;
 reg [63:0] search_tag,first_tag,best_tag;
 reg [47:0] phase_origin_index;
 reg [33:0] phase_product;
 reg [9:0] window_count;
 reg [99:0] best_n;reg [69:0] best_energy;
 reg best_sense,best_rx;reg signed [48:0] best_re,best_im;
 // Scores arrive no faster than every8 clocks. Register operands and reuse
 // one exact100x70 multiplier twice; commit the comparison three clocks
 // after accepting a score. Candidate identity stays private until commit.
 localparam COMPARE_IDLE=0,MULTIPLY_FIRST=1,MULTIPLY_SECOND=2,COMPARE_COMMIT=3;
 reg [1:0] compare_phase;
 reg [99:0] multiply_n;reg [69:0] multiply_energy;
 (* use_dsp="yes" *) reg [169:0] cross_product;
 reg [169:0] cross_new;
 reg pending_last,pending_sense,pending_rx;
 reg [63:0] pending_tag;reg [99:0] pending_n;reg [69:0] pending_energy;
 reg signed [48:0] pending_re,pending_im;
 reg signed [26:0] angle_x,angle_y;
 reg [5:0] angle_shift;reg angle_cancel;
 wire active=state!=IDLE;
 // Current or explicitly named next generation may be armed; old tails are
 // ignored. A genuinely later ADC restart takes priority over publication.
 wire owner_lost=active && (history_restart ? active_generation+32'd1!=owner_generation :
              (active_generation!=owner_generation && active_generation+32'd1!=owner_generation));
 wire metric_valid,metric_ready;wire [391:0] metric_c;wire [69:0] metric_energy;
 wire [63:0] metric_tag;wire [31:0] metric_generation;
 wire score_valid,score_sense,score_rx,score_hit;
 wire [99:0] score_n0,score_n1,score_n;wire [69:0] score_energy;
 wire [63:0] score_tag;wire [31:0] score_generation;
 wire signed [48:0] score_re,score_im;
 wire [47:0] search_distance=score_tag[47:0]-search_tag[47:0];
 wire at_search=!search_distance[47];
 wire angle_ready,angle_valid,angle_error;wire [31:0] angle_id,angle_phase;
 wire [47:0] known_index=first_tag[47:0]+48'd1152;
 wire [47:0] phase_offset=known_index-phase_origin_index;
 wire signed [33:0] raw_phase_wide={{2{angle_phase[31]}},angle_phase};
 wire signed [33:0] directed_phase=best_sense?raw_phase_wide:-raw_phase_wide;
 // Exact modulo reduction of round_ties_up(directed_phase * offset / 4).
 // Only product bits 33:0 can affect the phase output. Epoch is not time.
 wire [33:0] rounded_phase_product=phase_product+34'd2;
 assign cfg_ready=!rst && !cancel && !angle_cancel && state==IDLE && !out_valid && angle_ready;
 wire cfg_fire=cfg_valid && cfg_ready;
 wire replace_best=pending_energy!=0 &&
  (cross_new>cross_product || (cross_new==cross_product && best_sense && !pending_sense));

 differential_frontend_cost_probe frontend(
  .clk(clk),.rst(rst),.cancel(cancel),.in_valid(in_valid),.in_ready(in_ready),
  .in_start(in_start),.in_tag(in_tag),.in_data(in_data),
  .out_valid(metric_valid),.out_ready(metric_ready),.out_c(metric_c),.out_energy(metric_energy),
  .out_tag(metric_tag),.out_generation(metric_generation),.active_generation(active_generation),
  .history_restart(history_restart),.input_fences(input_fences),.correlation_fences(correlation_fences));
 differential_score_cost_probe scores(
  .clk(clk),.rst(rst),.cancel(cancel),.in_valid(metric_valid),.in_ready(metric_ready),
  .in_c(metric_c),.in_energy(metric_energy),.in_tag(metric_tag),.in_generation(metric_generation),
  .out_valid(score_valid),.out_ready(1'b1),.out_n0(score_n0),.out_n1(score_n1),.out_n(score_n),
  .out_energy(score_energy),.out_tag(score_tag),.out_generation(score_generation),
  .out_sense(score_sense),.out_rx(score_rx),.out_hit(score_hit),.out_re(score_re),.out_im(score_im));
 phase_angle_cost_probe angle(
  .clk(clk),.rst(rst),.cancel(cancel || angle_cancel),
  .in_valid(state==ANGLE_START && !owner_lost && !angle_cancel),.in_ready(angle_ready),
  .in_id(owner),.in_x(angle_x),.in_y(angle_y),.out_valid(angle_valid),.out_ready(1'b1),
  .out_id(angle_id),.out_phase(angle_phase),.out_error(angle_error));

 // Keep25 magnitude bits plus sign guard; uniform nearest-ties-up rounding.
 wire signed [49:0] wide_re={best_re[48],best_re},wide_im={best_im[48],best_im};
 wire [49:0] abs_re=wide_re<0?-wide_re:wide_re,abs_im=wide_im<0?-wide_im:wide_im;
 wire [49:0] magnitude=abs_re>abs_im?abs_re:abs_im;
 integer bit_length,shift_amount,j;
 reg signed [50:0] rounding,rounded_re,rounded_im;
 always @* begin
  bit_length=0;
  for(j=0;j<50;j=j+1) if(magnitude[j]) bit_length=j+1;
  shift_amount=bit_length>25?bit_length-25:0;
  rounding=angle_shift==0?51'sd0:(51'sd1<<<(angle_shift-1));
  rounded_re=($signed({{2{best_re[48]}},best_re})+rounding)>>>angle_shift;
  rounded_im=($signed({{2{best_im[48]}},best_im})+rounding)>>>angle_shift;
 end
 function automatic [31:0] corrective_step(input [31:0] phase,input sense);
  reg signed [33:0] signed_phase,directed;
  begin
   signed_phase={{2{phase[31]}},phase};directed=sense?signed_phase:-signed_phase;
   corrective_step=(directed+34'sd2)>>>2;
  end
 endfunction
 task fail(input [2:0] reason);
  begin
   state<=IDLE;compare_phase<=COMPARE_IDLE;angle_cancel<=1;out_valid<=1;out_error<=reason;
   out_first_tag<=0;out_peak_tag<=0;out_last_sample_tag<=0;out_known_tag<=0;
   out_sense<=0;out_rx<=0;out_raw_phase<=0;out_nco_step<=0;out_nco_phase<=0;
   out_re<=0;out_im<=0;out_n<=0;out_energy<=0;out_shift<=0;
  end
 endtask
 always @(posedge clk) begin
  if(rst || cancel) begin
   state<=IDLE;owner<=0;owner_generation<=0;search_tag<=0;first_tag<=0;best_tag<=0;
   phase_origin_index<=0;phase_product<=0;
   window_count<=0;best_n<=0;best_energy<=0;best_sense<=0;best_rx<=0;best_re<=0;best_im<=0;
   cross_new<=0;cross_product<=0;multiply_n<=0;multiply_energy<=0;compare_phase<=COMPARE_IDLE;
   pending_last<=0;pending_sense<=0;pending_rx<=0;
   pending_tag<=0;pending_n<=0;pending_energy<=0;pending_re<=0;pending_im<=0;
   angle_x<=0;angle_y<=0;angle_shift<=0;angle_cancel<=0;
   out_valid<=0;out_id<=0;out_generation<=0;out_first_tag<=0;out_peak_tag<=0;
   out_last_sample_tag<=0;out_known_tag<=0;out_sense<=0;out_rx<=0;
   out_raw_phase<=0;out_nco_step<=0;out_nco_phase<=0;out_re<=0;out_im<=0;out_n<=0;out_energy<=0;
   out_shift<=0;out_error<=0;
  end else begin
   angle_cancel<=0;
   if(out_valid && out_ready) out_valid<=0;
   if(owner_lost) fail(3'd1);
   else begin
    // A single multiplication expression guarantees structural sharing.
    // All operands are unsigned; both products retain their complete170bits.
    if(compare_phase==MULTIPLY_FIRST || compare_phase==MULTIPLY_SECOND)
     cross_product<=multiply_n*multiply_energy;
    if(compare_phase==MULTIPLY_FIRST) begin
     multiply_n<=best_n;multiply_energy<=pending_energy;
     compare_phase<=MULTIPLY_SECOND;
    end
    if(compare_phase==MULTIPLY_SECOND) begin
     cross_new<=cross_product;compare_phase<=COMPARE_COMMIT;
    end
    if(compare_phase==COMPARE_COMMIT) begin
     compare_phase<=COMPARE_IDLE;
     if(replace_best) begin
      best_n<=pending_n;best_energy<=pending_energy;best_sense<=pending_sense;best_rx<=pending_rx;
      best_re<=pending_re;best_im<=pending_im;best_tag<=pending_tag;
     end
     if(pending_last) state<=NORMALIZE_PREP;
    end
    if((state==SEARCH || state==WINDOW) && score_valid && score_generation==owner_generation) begin
     if(score_tag[63:48]!=search_tag[63:48]) fail(3'd3);
     else if(state==SEARCH && at_search && score_hit) begin
      state<=WINDOW;window_count<=1;first_tag<=score_tag;best_tag<=score_tag;
      best_n<=score_n;best_energy<=score_energy;best_sense<=score_sense;best_rx<=score_rx;
      best_re<=score_re;best_im<=score_im;
     end else if(state==WINDOW) begin
      multiply_n<=score_n;multiply_energy<=best_energy;compare_phase<=MULTIPLY_FIRST;
      pending_last<=window_count==511;pending_n<=score_n;pending_energy<=score_energy;
      pending_sense<=score_sense;pending_rx<=score_rx;pending_re<=score_re;pending_im<=score_im;
      pending_tag<=score_tag;window_count<=window_count+1'b1;
      if(window_count==511) state<=DRAIN;
     end
    end
    if(state==NORMALIZE_PREP) begin
     // Split abs/priority encoding from rounded variable shift for 50 MHz.
     angle_shift<=shift_amount;state<=NORMALIZE;
    end
    if(state==NORMALIZE) begin
     angle_x<=rounded_re[26:0];angle_y<=rounded_im[26:0];state<=ANGLE_START;
    end
    if(state==ANGLE_START && angle_ready) state<=ANGLE_WAIT;
    if(state==ANGLE_WAIT && angle_valid) begin
     if(angle_id!=owner) fail(3'd5);
     else if(angle_error) fail(3'd4);
     else begin
      out_error<=0;state<=PHASE_FINISH;out_first_tag<=first_tag;out_peak_tag<=best_tag;
      phase_product<=directed_phase*phase_offset[33:0];
      out_last_sample_tag<={first_tag[63:48],first_tag[47:0]+48'd1019};
      // Preserve reference's conservative end+512+128 availability boundary.
      out_known_tag<={first_tag[63:48],known_index};
      out_sense<=best_sense;out_rx<=best_rx;out_raw_phase<=angle_phase;
      out_nco_step<=corrective_step(angle_phase,best_sense);out_shift<=angle_shift;
      out_re<=best_re;out_im<=best_im;out_n<=best_n;out_energy<=best_energy;
     end
    end
    if(state==PHASE_FINISH) begin
     out_nco_phase<=rounded_phase_product[33:2];out_valid<=1;state<=IDLE;
    end
    if(cfg_fire) begin
     owner<=cfg_id;owner_generation<=cfg_generation;search_tag<=cfg_search_tag;
     phase_origin_index<=cfg_phase_origin_index;
     out_id<=cfg_id;out_generation<=cfg_generation;out_error<=0;window_count<=0;compare_phase<=COMPARE_IDLE;
     if(cfg_generation!=active_generation && cfg_generation!=active_generation+32'd1) fail(3'd2);
     else if(history_restart && cfg_generation!=active_generation+32'd1) fail(3'd2);
     else state<=SEARCH;
    end
   end
  end
 end
endmodule
