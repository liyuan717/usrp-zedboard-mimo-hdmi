`timescale 1ns/1ps
// Finite G0 training estimator. Default: one448-symbol TX column.
// ENABLE_STARTUP adds a variable-length TX0 fine-CFO/quality mode, sharing
// history, CORDIC, divider and rotation hardware with the regular column.
// Raw input is four signed21 components at scale4 after interpolation.
// Public QPSK signs are ROM data; no CFO/channel truth is supplied.
module training_channel_cost_probe #(
 parameter SIGN_FILE="training_signs.hex", SINE_FILE="sine.hex",
 parameter ENABLE_STARTUP=0
)(
 input wire clk,rst,cancel,
 input wire cfg_valid,output wire cfg_ready,
 input wire [31:0] cfg_id,input wire cfg_tx,
 input wire cfg_startup,input wire [8:0] cfg_count,
 input wire [9:0] cfg_first_symbol,
 input wire in_valid,output wire in_ready,
 input wire [31:0] in_id,input wire [8:0] in_index,
 input wire in_error,input wire [83:0] in_data,
 output reg out_valid,input wire out_ready,
 output reg [31:0] out_id,output reg out_tx,out_selected_rx,
 output reg signed [31:0] out_phase_step,
 output reg [103:0] out_h,output reg [2:0] out_error,
 output reg [49:0] out_energy0,out_energy1,
 output reg signed [47:0] out_numerator,output reg [15:0] out_divisor,
 output reg [72:0] out_quality_sum,output reg [71:0] out_coherent,
 output reg out_admitted
);
 localparam IDLE=0,COLLECT=1,WAIT_ANGLES=2,DIVIDE=3,REPLAY_START=4,
            REPLAY0=5,REPLAY1=6,REPLAY2=7,NORM_MUL=8,NORM_STORE=9,
            QUALITY_REAL=10,QUALITY_IMAG=11,QUALITY_SUM=12,QUALITY_STORE=13;
 reg [3:0] state;
 reg [31:0] owner;
 reg tx;
 reg startup_mode;
 wire startup=ENABLE_STARTUP && startup_mode;
 reg [8:0] sample_count,sign_offset;
 reg [4:0] block_count;
 reg [15:0] divisor;
 reg frequency_valid;
 wire cfg_bad=ENABLE_STARTUP && cfg_startup &&
   (cfg_tx || cfg_count<128 || cfg_count>448 || cfg_first_symbol<384 ||
    ({1'b0,cfg_first_symbol}+{2'b0,cfg_count})>11'd896);
 function automatic [15:0] block_divisor(input [4:0] blocks);
  begin
   case(blocks)
    8:block_divisor=1344;9:block_divisor=1920;10:block_divisor=2640;
    11:block_divisor=3520;12:block_divisor=4576;13:block_divisor=5824;
    14:block_divisor=7280;15:block_divisor=8960;16:block_divisor=10880;
    17:block_divisor=13056;18:block_divisor=15504;19:block_divisor=18240;
    20:block_divisor=21280;21:block_divisor=24640;22:block_divisor=28336;
    23:block_divisor=32384;24:block_divisor=36800;25:block_divisor=41600;
    26:block_divisor=46800;27:block_divisor=52416;28:block_divisor=58464;
    default:block_divisor=0;
   endcase
  end
 endfunction
 reg [8:0] accepted_count,replay_index;
 (* ram_style="block" *) reg [83:0] history[0:447];
 // Bit0: negative real sign; bit1: negative imaginary sign. Each TX has512.
 (* rom_style="distributed" *) reg [1:0] signs_rom[0:1023];
 (* rom_style="block" *) reg [16:0] sine_rom[0:1023];
 initial begin $readmemh(SIGN_FILE,signs_rom);$readmemh(SINE_FILE,sine_rom);end
 reg [49:0] energy[0:1];
 reg signed [26:0] block_sum[0:3],pair_sum[0:3];
 reg pair_pending,pair_rx;
 reg [4:0] pair_block;
 reg [5:0] angles_finished;
 reg signed [31:0] previous_phase[0:1];
 reg signed [37:0] unwrapped[0:1];
 reg signed [47:0] weighted_sum[0:1];
 reg [1:0] bad_branch;
 wire cfg_fire=cfg_valid && cfg_ready;
 assign cfg_ready=!rst && !cancel && state==IDLE && !out_valid;
 // A single pending pair bounds storage even with an arbitrarily bursty input.
 // At32/50 cycle symbol cadence the pair is drained long before the next block.
 assign in_ready=!rst && !cancel && state==COLLECT &&
                 !(accepted_count[3:0]==15 && pair_pending);
 wire take=in_valid && in_ready;
 wire input_bad=take && (in_error || in_id!=owner || in_index!=accepted_count);
 wire good_take=take && !input_bad;
 wire [8:0] sign_address=sign_offset+accepted_count;
 wire [1:0] input_signs=signs_rom[{tx,sign_address}];

 function automatic signed [22:0] despread(
   input [83:0] raw,input [1:0] signs,input integer lane);
  reg signed [22:0] i,q,si,sq;
  begin
   i={{2{raw[(lane/2)*42+20]}},raw[(lane/2)*42+:21]};
   q={{2{raw[(lane/2)*42+41]}},raw[(lane/2)*42+21+:21]};
   if(lane%2==0) begin si=signs[0]?-i:i;sq=signs[1]?-q:q;despread=si+sq;end
   else begin si=signs[0]?-q:q;sq=signs[1]?-i:i;despread=si-sq;end
  end
 endfunction
 wire signed [22:0] de[0:3];
 wire signed [20:0] raw_component[0:3];
 (* use_dsp="yes" *) wire signed [41:0] square[0:3];
 genvar g;
 generate for(g=0;g<4;g=g+1) begin: input_math
  assign de[g]=despread(in_data,input_signs,g);
  assign raw_component[g]=in_data[g*21+:21];
  assign square[g]=raw_component[g]*raw_component[g];
 end endgenerate

 wire angle_in_ready,angle_valid,angle_error;
 wire [31:0] angle_id,angle_phase;
 wire angle_take=pair_pending && angle_in_ready && !rst && !cancel && !input_bad;
 phase_angle_cost_probe angle(
  .clk(clk),.rst(rst),.cancel(cancel || cfg_fire || input_bad),
  .in_valid(pair_pending && !input_bad),.in_ready(angle_in_ready),
  .in_id({26'd0,pair_block,pair_rx}),
  .in_x(pair_rx?pair_sum[2]:pair_sum[0]),.in_y(pair_rx?pair_sum[3]:pair_sum[1]),
  .out_valid(angle_valid),.out_ready(1'b1),.out_id(angle_id),
  .out_phase(angle_phase),.out_error(angle_error));
 wire angle_rx=angle_id[0];
 reg signed [33:0] delta;
 reg signed [37:0] next_unwrapped;
 reg signed [6:0] weight;
 (* use_dsp="no" *) reg signed [44:0] weighted_phase;
 always @* begin
  delta=$signed({{2{angle_phase[31]}},angle_phase})-
        $signed({{2{previous_phase[angle_rx][31]}},previous_phase[angle_rx]});
  if(delta>34'sd2147483648) delta=delta-34'sd4294967296;
  else if(delta< -34'sd2147483648) delta=delta+34'sd4294967296;
  if(angle_id[5:1]==0) next_unwrapped={{6{angle_phase[31]}},angle_phase};
  else next_unwrapped=unwrapped[angle_rx]+delta;
  weight=$signed({1'b0,angle_id[5:1],1'b0})-$signed({2'b00,block_count})+7'sd1;
  weighted_phase=next_unwrapped*weight;
 end

 reg division_negative;
 reg [47:0] dividend,quotient;
 reg [15:0] remainder;
 reg [5:0] divide_iteration;
 wire [16:0] shifted_remainder={remainder,dividend[47]};
 wire quotient_bit=shifted_remainder>={1'b0,divisor};
 wire [16:0] reduced_remainder=quotient_bit?shifted_remainder-{1'b0,divisor}:shifted_remainder;
 wire [47:0] next_quotient={quotient[46:0],quotient_bit};
 wire choose_rx=energy[1]>energy[0];
 wire signed [47:0] chosen_numerator=weighted_sum[choose_rx];
 wire [47:0] numerator_magnitude=chosen_numerator[47]?-chosen_numerator:chosen_numerator;
 // abs(CFO)<=20000Hz, using the unrounded Q32-per-symbol rational num/D.
 wire [53:0] fifty_magnitude=({6'd0,numerator_magnitude}<<5)+
    ({6'd0,numerator_magnitude}<<4)+({6'd0,numerator_magnitude}<<1);
 wire frequency_in_range=fifty_magnitude<={6'd0,divisor,32'd0};

 reg signed [31:0] phase_step;
 reg [31:0] phase_counter;
 wire signed [42:0] initial_phase_product=phase_step*(tx?11'sd672:11'sd160);
 wire [31:0] lookup_phase=state==REPLAY_START?(startup?32'd0:-initial_phase_product[31:0]):phase_counter;
 wire [31:0] rounded_phase=lookup_phase+32'h00080000;
 wire [11:0] bin=rounded_phase[31:20];
 wire [10:0] sin_index=bin[10]?11'd1024-{1'b0,bin[9:0]}:{1'b0,bin[9:0]};
 wire [10:0] cos_index=bin[10]?{1'b0,bin[9:0]}:11'd1024-{1'b0,bin[9:0]};
 wire replay_read=!rst && !cancel &&
    (state==REPLAY_START || (state==REPLAY2 && replay_index!=sample_count-9'd1));
 wire [8:0] read_address=state==REPLAY_START?9'd0:replay_index+9'd1;
 wire [8:0] replay_sign_address=read_address+sign_offset;
 reg [83:0] replay_data;
 reg [1:0] replay_signs;
 reg [16:0] sine_q,cosine_q;
 reg sin_negative,cos_negative;
 wire signed [17:0] sine_value=sin_negative?-$signed({1'b0,sine_q}):$signed({1'b0,sine_q});
 wire signed [17:0] cosine_value=cos_negative?-$signed({1'b0,cosine_q}):$signed({1'b0,cosine_q});
 wire signed [22:0] a=despread(replay_data,replay_signs,state==REPLAY0?0:2);
 wire signed [22:0] b=despread(replay_data,replay_signs,state==REPLAY0?1:3);
 (* use_dsp="yes" *) reg signed [40:0] product[0:3];
 wire signed [41:0] re_sum=$signed({product[0][40],product[0]})-$signed({product[1][40],product[1]});
 wire signed [41:0] im_sum=$signed({product[2][40],product[2]})+$signed({product[3][40],product[3]});
 wire signed [26:0] re_rounded=(re_sum+42'sd2048)>>>12;
 wire signed [26:0] im_rounded=(im_sum+42'sd2048)>>>12;
 reg signed [35:0] hsum[0:3];
 reg [1:0] norm_lane;
 wire quality_multiply=ENABLE_STARTUP && (state==QUALITY_REAL || state==QUALITY_IMAG);
 wire signed [35:0] multiply_a=quality_multiply?
    hsum[{out_selected_rx,state==QUALITY_IMAG}]:hsum[norm_lane];
 wire signed [35:0] multiply_b=quality_multiply?multiply_a:36'sd1735428857;
 // One actual multiplier serves H normalization and both quality squares.
 (* use_dsp="yes" *) reg signed [71:0] norm_product;
 wire signed [71:0] norm_rounded=(norm_product+72'sd549755813888)>>>40;
 reg [71:0] quality_real_square;
 reg [72:0] quality_sum;
 reg [58:0] quality_ME;
 wire [75:0] quality_lhs=({3'd0,quality_sum}<<2)+{3'd0,quality_sum};
 wire [75:0] quality_rhs={7'd0,quality_ME,10'd0};
 wire quality_pass=(out_selected_rx?out_energy1:out_energy0)!=0 && quality_lhs>=quality_rhs;

 // Separate synchronous RAM/ROM and arithmetic registers permit actual BRAM
 // inference. Memory has no reset; ownership/counts fence all stale contents.
 always @(posedge clk) begin
  if(good_take) history[accepted_count]<=in_data;
  if(replay_read) begin
   replay_data<=history[read_address];replay_signs<=signs_rom[{tx,replay_sign_address}];
   sine_q<=sin_index==1024?17'd65536:sine_rom[sin_index[9:0]];
   cosine_q<=cos_index==1024?17'd65536:sine_rom[cos_index[9:0]];
  end
  if(!rst && !cancel && (state==REPLAY0 || state==REPLAY1)) begin
   product[0]<=a*cosine_value;product[1]<=b*sine_value;
   product[2]<=a*sine_value;product[3]<=b*cosine_value;
  end
  if(!rst && !cancel && (state==NORM_MUL || quality_multiply)) norm_product<=multiply_a*multiply_b;
 end

 integer l;
 always @(posedge clk) begin
  if(rst || cancel) begin
   state<=IDLE;owner<=0;tx<=0;accepted_count<=0;replay_index<=0;
   startup_mode<=0;sample_count<=448;sign_offset<=32;block_count<=28;divisor<=58464;frequency_valid<=0;
   quality_real_square<=0;quality_sum<=0;quality_ME<=0;
   pair_pending<=0;pair_rx<=0;pair_block<=0;angles_finished<=0;bad_branch<=0;
   division_negative<=0;dividend<=0;quotient<=0;remainder<=0;divide_iteration<=0;
   phase_step<=0;phase_counter<=0;sin_negative<=0;cos_negative<=0;norm_lane<=0;
   out_valid<=0;out_id<=0;out_tx<=0;out_selected_rx<=0;out_phase_step<=0;
   out_h<=0;out_error<=0;out_energy0<=0;out_energy1<=0;
   out_numerator<=0;out_divisor<=0;out_quality_sum<=0;out_coherent<=0;out_admitted<=0;
   for(l=0;l<2;l=l+1) begin energy[l]<=0;previous_phase[l]<=0;unwrapped[l]<=0;weighted_sum[l]<=0;end
   for(l=0;l<4;l=l+1) begin block_sum[l]<=0;pair_sum[l]<=0;hsum[l]<=0;end
  end else begin
   if(out_valid && out_ready) out_valid<=0;
   if(angle_take) begin
    if(pair_rx) begin pair_pending<=0;pair_rx<=0;end
    else pair_rx<=1;
   end
   if(angle_valid && !input_bad && (state==COLLECT || state==WAIT_ANGLES)) begin
    previous_phase[angle_rx]<=angle_phase;unwrapped[angle_rx]<=next_unwrapped;
    weighted_sum[angle_rx]<=weighted_sum[angle_rx]+weighted_phase;
    angles_finished<=angles_finished+1'b1;
    // After wrapping, a half-turn difference is direction-ambiguous. The64LSB
    // guard covers two32LSB angle errors; a regular +/-pi crossing stays valid.
    if(angle_error || (angle_id[5:1]!=0 &&
       (delta>=34'sd2147483584 || delta<= -34'sd2147483584))) bad_branch[angle_rx]<=1;
   end
   if(cfg_fire) begin
    state<=COLLECT;owner<=cfg_id;tx<=cfg_tx;accepted_count<=0;
    startup_mode<=ENABLE_STARTUP && cfg_startup;
    sample_count<=ENABLE_STARTUP && cfg_startup?cfg_count:9'd448;
    sign_offset<=ENABLE_STARTUP && cfg_startup?cfg_first_symbol-10'd384:9'd32;
    block_count<=ENABLE_STARTUP && cfg_startup?cfg_count[8:4]:5'd28;
    divisor<=ENABLE_STARTUP && cfg_startup?block_divisor(cfg_count[8:4]):16'd58464;
    frequency_valid<=0;quality_real_square<=0;quality_sum<=0;quality_ME<=0;
    pair_pending<=0;pair_rx<=0;pair_block<=0;angles_finished<=0;bad_branch<=0;
    out_h<=0;out_error<=0;out_id<=cfg_id;out_tx<=cfg_tx;
    out_numerator<=0;out_divisor<=0;out_quality_sum<=0;out_coherent<=0;out_admitted<=0;
    out_phase_step<=0;out_selected_rx<=0;out_energy0<=0;out_energy1<=0;
    for(l=0;l<2;l=l+1) begin energy[l]<=0;previous_phase[l]<=0;unwrapped[l]<=0;weighted_sum[l]<=0;end
    for(l=0;l<4;l=l+1) begin block_sum[l]<=0;hsum[l]<=0;end
    if(cfg_bad) begin state<=IDLE;out_valid<=1;out_error<=5;end
   end
   if(good_take) begin
    energy[0]<=energy[0]+{8'd0,square[0]}+{8'd0,square[1]};
    energy[1]<=energy[1]+{8'd0,square[2]}+{8'd0,square[3]};
    for(l=0;l<4;l=l+1) begin
     if(accepted_count[3:0]==15) begin pair_sum[l]<=block_sum[l]+de[l];block_sum[l]<=0;end
     else block_sum[l]<=block_sum[l]+de[l];
    end
    if(accepted_count[3:0]==15) begin pair_pending<=1;pair_rx<=0;pair_block<=accepted_count[8:4];end
    if(accepted_count==sample_count-9'd1) state<=WAIT_ANGLES;
    else accepted_count<=accepted_count+1'b1;
   end
   if(state==WAIT_ANGLES && angles_finished=={block_count,1'b0}) begin
    out_selected_rx<=choose_rx;out_energy0<=energy[0];out_energy1<=energy[1];
    if(bad_branch[choose_rx]) begin out_error<=2;out_h<=0;out_phase_step<=0;out_valid<=1;state<=IDLE;end
    else begin
     out_numerator<=chosen_numerator;out_divisor<=divisor;
     frequency_valid<=frequency_in_range;
     division_negative<=chosen_numerator[47];
     // Positive ties round up; negative ties round toward zero.
     dividend<=numerator_magnitude+{32'd0,(divisor>>1)}-(chosen_numerator[47]?48'd1:48'd0);
     quotient<=0;remainder<=0;divide_iteration<=0;state<=DIVIDE;
    end
   end
   if(state==DIVIDE) begin
    remainder<=reduced_remainder[15:0];dividend<={dividend[46:0],1'b0};quotient<=next_quotient;
    if(divide_iteration==47) begin
     phase_step<=division_negative?-$signed(next_quotient[31:0]):$signed(next_quotient[31:0]);
     out_phase_step<=division_negative?-$signed(next_quotient[31:0]):$signed(next_quotient[31:0]);
     state<=REPLAY_START;
    end else divide_iteration<=divide_iteration+1'b1;
   end
   if(replay_read) begin
    sin_negative<=bin[11];cos_negative<=bin[11]^bin[10];
    phase_counter<=lookup_phase-phase_step;
   end
   if(state==REPLAY_START) begin replay_index<=0;state<=REPLAY0;end
   if(state==REPLAY0) state<=REPLAY1;
   if(state==REPLAY1) begin
    hsum[0]<=hsum[0]+re_rounded;hsum[1]<=hsum[1]+im_rounded;state<=REPLAY2;
   end
   if(state==REPLAY2) begin
    hsum[2]<=hsum[2]+re_rounded;hsum[3]<=hsum[3]+im_rounded;
    if(replay_index==sample_count-9'd1) begin norm_lane<=0;state<=startup?QUALITY_REAL:NORM_MUL;end
    else begin replay_index<=replay_index+1'b1;state<=REPLAY0;end
   end
   if(state==NORM_MUL) state<=NORM_STORE;
   if(state==NORM_STORE) begin
    out_h[norm_lane*26+:26]<=norm_rounded[25:0];
    if(norm_lane==3) begin out_valid<=1;out_error<=0;out_admitted<=1;state<=IDLE;end
    else begin norm_lane<=norm_lane+1'b1;state<=NORM_MUL;end
   end
   // Keep the optional multiplier/admission cone out of the default build.
   // Merely making these FSM states unreachable did not remove it in OOC.
   if(ENABLE_STARTUP && state==QUALITY_REAL) begin
    quality_ME<=(out_selected_rx?out_energy1:out_energy0)*sample_count;
    state<=QUALITY_IMAG;
   end
   if(ENABLE_STARTUP && state==QUALITY_IMAG) begin quality_real_square<=norm_product;state<=QUALITY_SUM;end
   if(ENABLE_STARTUP && state==QUALITY_SUM) begin
    quality_sum<={1'b0,quality_real_square}+{1'b0,norm_product};state<=QUALITY_STORE;
   end
   if(ENABLE_STARTUP && state==QUALITY_STORE) begin
    out_quality_sum<=quality_sum;out_coherent<={hsum[{out_selected_rx,1'b1}],hsum[{out_selected_rx,1'b0}]};
    out_admitted<=frequency_valid && quality_pass;out_error<=!frequency_valid?3'd3:quality_pass?3'd0:3'd4;
    out_h<=0;out_valid<=1;state<=IDLE;
   end
   if(input_bad) begin
    state<=IDLE;pair_pending<=0;out_error<=1;out_h<=0;out_phase_step<=0;
    out_energy0<=energy[0];out_energy1<=energy[1];out_selected_rx<=0;out_valid<=1;
    out_numerator<=0;out_divisor<=0;out_quality_sum<=0;out_coherent<=0;out_admitted<=0;
   end
  end
 end
endmodule
