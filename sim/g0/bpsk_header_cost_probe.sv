`timescale 1ns/1ps
// Finite G0 profile0xf0: TX0 BPSK/repeat2,24-byte LE header, CRC32/ISO-HDLC.
// Full signed26 H and signed21 samples; exact limb products share ONE DSP.
// No length/session is trusted before CRC and bounded public-profile checks.
module bpsk_header_cost_probe #(parameter SINE_FILE="sine.hex",ENABLE_PILOT=0)(
 input wire clk,rst,cancel,cfg_valid,output wire cfg_ready,
 input wire [31:0] cfg_id,input wire [103:0] cfg_h,input wire [31:0] cfg_phase_step,
 input wire in_valid,output wire in_ready,input wire [31:0] in_id,
 input wire [8:0] in_index,input wire in_error,input wire [83:0] in_data,
 output reg out_valid,input wire out_ready,output reg [31:0] out_id,
 output reg [2:0] out_error,output reg [191:0] out_raw_header,
 output reg [31:0] out_session,out_superframe,
 output reg [15:0] out_stream0_bytes,out_stream1_bytes,
 input wire cfg_pilot,input wire [31:0] cfg_pilot_phase,
 output reg out_pilot,output reg signed [67:0] out_pilot_real,out_pilot_imag
);
 localparam IDLE=0,COLLECT=1,DOT=2,ROTATE=3,DECIDE=4,ROTATE_IMAG=5,PILOT_FINISH=6;
 reg [2:0] state;reg [31:0] owner,phase,phase_step;reg [8:0] index;
 reg pilot_mode;wire pilot=ENABLE_PILOT && pilot_mode;
 reg signed [25:0] h[0:3];reg signed [20:0] x[0:3];
 reg signed [48:0] zreal,zimag;reg signed [67:0] soft_value,first_soft,pilot_imag;
 reg [4:0] issued;reg [191:0] raw_header;reg [7:0] byte_shift;
 reg profile_bad;reg [31:0] crc;reg crc_busy;reg [3:0] crc_left;reg [7:0] crc_byte;
 (* rom_style="block" *) reg [16:0] sine_rom[0:1023];
 initial $readmemh(SINE_FILE,sine_rom);
 reg [16:0] sine_q,cosine_q;reg sin_negative,cos_negative;
 wire [31:0] rounded_phase=phase+32'h80000;wire [11:0] bin=rounded_phase[31:20];
 wire [10:0] sin_index=bin[10]?11'd1024-{1'b0,bin[9:0]}:{1'b0,bin[9:0]};
 wire [10:0] cos_index=bin[10]?{1'b0,bin[9:0]}:11'd1024-{1'b0,bin[9:0]};
 wire signed [17:0] sine_value=sin_negative?-$signed({1'b0,sine_q}):$signed({1'b0,sine_q});
 wire signed [17:0] cosine_value=cos_negative?-$signed({1'b0,cosine_q}):$signed({1'b0,cosine_q});
 assign cfg_ready=!rst && !cancel && state==IDLE && !out_valid;
 assign in_ready=!rst && !cancel && state==COLLECT;
 wire cfg_fire=cfg_valid && cfg_ready;wire take=in_valid && in_ready;
 wire input_bad=in_error || in_id!=owner || in_index!=index;
 wire good_take=take && !input_bad;
 reg signed [24:0] multiply_a;reg signed [17:0] multiply_b;
 reg [5:0] issue_shift;reg issue_negative;
 wire imaginary_rotation=ENABLE_PILOT && state==ROTATE_IMAG;
 wire multiply_enable=(state==DOT && issued<16) || ((state==ROTATE || imaginary_rotation) && issued<6);
 reg signed [48:0] selected_z;
 always @* begin
  multiply_a=0;multiply_b=0;issue_shift=0;issue_negative=0;selected_z=0;
  if(state==DOT)begin
   multiply_a={{4{x[issued[2:1]^(issued[3]?2'd1:2'd0)][20]}},x[issued[2:1]^(issued[3]?2'd1:2'd0)]};
   multiply_b=issued[0]?{{9{h[issued[2:1]][25]}},h[issued[2:1]][25:17]}:{1'b0,h[issued[2:1]][16:0]};
   issue_shift=issued[0]?6'd17:6'd0;issue_negative=issued[3] && issued[1];
  end else if(state==ROTATE || imaginary_rotation)begin
   multiply_a=(imaginary_rotation?(issued>=3):(issued<3))?{{7{cosine_value[17]}},cosine_value}:{{7{sine_value[17]}},sine_value};
   selected_z=issued<3?zreal:zimag;issue_negative=!imaginary_rotation && issued>=3;
   case(issued)
    0,3:begin multiply_b={1'b0,selected_z[16:0]};issue_shift=0;end
    1,4:begin multiply_b={1'b0,selected_z[33:17]};issue_shift=17;end
    default:begin multiply_b={{3{selected_z[48]}},selected_z[48:34]};issue_shift=34;end
   endcase
  end
 end
 (* use_dsp="yes" *) reg signed [42:0] product;
 reg product_valid,product_rotation,product_imag,product_negative,product_last,product_second_rotation;
 reg [5:0] product_shift;
 wire signed [48:0] dot_partial=$signed({{6{product[42]}},product})<<<product_shift;
 wire signed [67:0] rotation_partial=$signed({{25{product[42]}},product})<<<product_shift;
 always @(posedge clk)begin
  if(good_take)begin
   sine_q<=sin_index==1024?17'd65536:sine_rom[sin_index[9:0]];
   cosine_q<=cos_index==1024?17'd65536:sine_rom[cos_index[9:0]];
  end
  if(rst || cancel || cfg_fire)product_valid<=0;
  else begin
   product_valid<=multiply_enable;
   if(multiply_enable)begin
    product<=multiply_a*multiply_b;product_rotation<=state!=DOT;product_second_rotation<=imaginary_rotation;
    product_imag<=issued[3];product_negative<=issue_negative;product_shift<=issue_shift;
    product_last<=state==DOT?issued==15:issued==5;
   end
  end
 end
 wire signed [68:0] repeated_soft=$signed({first_soft[67],first_soft})+$signed({soft_value[67],soft_value});
 wire decision=repeated_soft<0;wire [7:0] next_byte={byte_shift[6:0],decision};
 wire [4:0] byte_index=index[8:4];
 reg [191:0] completed_header;
 always @*begin completed_header=raw_header;completed_header[byte_index*8+:8]=next_byte;end
 function automatic bad_profile_byte(input [4:0] i,input [7:0] v);
  begin
   case(i)
    0:bad_profile_byte=v!=8'h56;1:bad_profile_byte=v!=8'h50;
    2:bad_profile_byte=v!=1;3:bad_profile_byte=v!=8'hf0;
    12,14:bad_profile_byte=v!=8'h20;13,15:bad_profile_byte=v!=8'h11;
    16,17:bad_profile_byte=v!=8'd8;18,19:bad_profile_byte=v!=0;
    default:bad_profile_byte=0;
   endcase
  end
 endfunction
 task publish(input [2:0] error,input [191:0] bytes);
  begin
   state<=IDLE;out_valid<=1;out_id<=owner;out_error<=error;out_raw_header<=bytes;crc_busy<=0;
   out_pilot<=pilot;out_pilot_real<=0;out_pilot_imag<=0;
   out_session<=error==0?bytes[63:32]:32'd0;out_superframe<=error==0?bytes[95:64]:32'd0;
   out_stream0_bytes<=error==0?16'd4384:16'd0;out_stream1_bytes<=error==0?16'd4384:16'd0;
  end
 endtask
 integer l;
 always @(posedge clk)begin
  if(rst || cancel)begin
   state<=IDLE;owner<=0;phase<=0;phase_step<=0;index<=0;issued<=0;zreal<=0;zimag<=0;soft_value<=0;first_soft<=0;pilot_imag<=0;pilot_mode<=0;
   raw_header<=0;byte_shift<=0;profile_bad<=0;crc<=32'hffffffff;crc_busy<=0;crc_left<=0;crc_byte<=0;
   out_valid<=0;out_id<=0;out_error<=0;out_raw_header<=0;out_session<=0;out_superframe<=0;out_stream0_bytes<=0;out_stream1_bytes<=0;
   sin_negative<=0;cos_negative<=0;
   out_pilot<=0;out_pilot_real<=0;out_pilot_imag<=0;
   for(l=0;l<4;l=l+1)begin h[l]<=0;x[l]<=0;end
  end else begin
   if(out_valid && out_ready)out_valid<=0;
   if(crc_busy)begin
    crc<=(crc>>1)^((crc[0]^crc_byte[0])?32'hedb88320:32'd0);crc_byte<=crc_byte>>1;crc_left<=crc_left-1'b1;
    if(crc_left==1)crc_busy<=0;
   end
   if(cfg_fire)begin
    owner<=cfg_id;out_id<=cfg_id;phase_step<=cfg_phase_step;pilot_mode<=ENABLE_PILOT && cfg_pilot;
    phase<=ENABLE_PILOT && cfg_pilot?cfg_pilot_phase:32'd0-(cfg_phase_step<<10)-(cfg_phase_step<<7);
    index<=0;state<=COLLECT;raw_header<=0;byte_shift<=0;profile_bad<=0;crc<=32'hffffffff;crc_busy<=0;
    for(l=0;l<4;l=l+1)h[l]<=cfg_h[l*26+:26];
    if(cfg_h==0)begin out_valid<=1;out_error<=5;out_raw_header<=0;out_session<=0;out_superframe<=0;out_stream0_bytes<=0;out_stream1_bytes<=0;out_pilot<=ENABLE_PILOT && cfg_pilot;out_pilot_real<=0;out_pilot_imag<=0;state<=IDLE;end
   end
   if(take)begin
    if(input_bad)publish(3,192'd0);
    else begin
     for(l=0;l<4;l=l+1)x[l]<=in_data[l*21+:21];
     sin_negative<=bin[11];cos_negative<=bin[11]^bin[10];
     state<=DOT;issued<=0;zreal<=0;zimag<=0;soft_value<=0;pilot_imag<=0;
    end
   end
   if(multiply_enable)issued<=issued+1'b1;
   if(product_valid)begin
    if(product_rotation)begin
     if(ENABLE_PILOT && product_second_rotation)pilot_imag<=pilot_imag+rotation_partial;
     else soft_value<=product_negative?soft_value-rotation_partial:soft_value+rotation_partial;
    end
    else if(product_imag)zimag<=product_negative?zimag-dot_partial:zimag+dot_partial;
    else zreal<=zreal+dot_partial;
    if(product_last)begin
     issued<=0;
     if(product_rotation)state<=ENABLE_PILOT && product_second_rotation?PILOT_FINISH:(pilot?ROTATE_IMAG:DECIDE);
     else state<=ROTATE;
    end
   end
   if(state==DECIDE)begin
    phase<=phase-phase_step;
    if(!index[0])first_soft<=soft_value;
    else begin
     byte_shift<=next_byte;
     if(index[3:0]==15)begin
      raw_header<=completed_header;
      if(byte_index<20)begin
       crc_byte<=next_byte;crc_left<=8;crc_busy<=1;
       profile_bad<=profile_bad || bad_profile_byte(byte_index,next_byte);
      end
     end
    end
    if(index==383)begin
     if(completed_header[191:160]!=(crc^32'hffffffff))publish(1,completed_header);
     else if(profile_bad)publish(2,completed_header);
     else publish(0,completed_header);
    end else begin index<=index+1'b1;state<=COLLECT;end
   end
   if(ENABLE_PILOT && state==PILOT_FINISH)begin
    publish(0,192'd0);out_pilot<=1;out_pilot_real<=soft_value;out_pilot_imag<=pilot_imag;
    // Pilot output is a phase statistic, never trusted PHY metadata.
    out_session<=0;out_superframe<=0;out_stream0_bytes<=0;out_stream1_bytes<=0;
   end
  end
 end
endmodule
