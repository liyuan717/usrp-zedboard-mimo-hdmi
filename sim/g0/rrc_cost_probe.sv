`timescale 1ns/1ps
// G0 candidate: 41-tap symmetric RRC, two complex RX / four real lanes.
// 18-bit input; 18-bit Q17 coefficients; 40-bit accumulator; 20-bit output.
// Seven rounds of three products/lane, one registered multiply stage, II=8.
// No NCO, notch, interpolator, acquisition controller or production profile.
module rrc_cost_probe #(parameter COEFFICIENT_FILE="rrc.hex")(
 input wire clk,rst,
 input wire in_valid,output wire in_ready,input wire in_start,
 input wire [63:0] in_tag,input wire [71:0] in_data,
 output reg out_valid,input wire out_ready,
 output reg [63:0] out_tag,output reg [79:0] out_data,
 output reg [31:0] fence_count,output reg out_start,
 input wire [31:0] in_context,output reg [31:0] out_context
);
 // Three recent words bypass first-round reads while every image writes.
 // Full logical history stays41words; three images supply six read ports.
 reg [71:0] recent[0:2];
 reg [5:0] history_head;
 wire [71:0] history_left[0:2],history_right[0:2];
 reg signed [17:0] coefficients[0:20];
 initial $readmemh(COEFFICIENT_FILE,coefficients);
 (* use_dsp="yes" *) reg signed [36:0] products[0:3][0:2];
 reg signed [39:0] accum[0:3];
 reg [3:0] phase;
 reg [5:0] count;
 reg have_previous,work_emit,work_start;
 reg [63:0] previous_tag,work_tag;
 reg [31:0] previous_context,work_context;
 wire gap=!have_previous || in_start || in_tag[63:48]!=previous_tag[63:48] ||
          in_tag[47:0]!=(previous_tag[47:0]+48'd1) || in_context!=previous_context;
 wire finish_ready=!out_valid || out_ready;
 assign in_ready=!rst && (phase==0 || (phase==8 && finish_ready));
 wire accept=in_valid && in_ready;
 wire [5:0] write_head=history_head==40?6'd0:history_head+6'd1;
 wire history_prefetch=phase>=1 && phase<=6;
 function automatic [5:0] history_address(input [5:0] head,input [5:0] offset);
  reg [6:0] sum;
  begin
   sum={1'b0,head}+7'd41-{1'b0,offset};
   history_address=sum>=41?sum-7'd41:sum;
  end
 endfunction
 genvar bank;
 generate for(bank=0;bank<3;bank=bank+1) begin:history_ports
  wire [71:0] a_q,b_q;
  wire [5:0] next_tap=phase*3+bank;
  wire [5:0] address_a=accept?write_head:history_address(history_head,next_tap);
  wire [5:0] address_b=accept?history_address(write_head,6'd40-bank):history_address(history_head,6'd40-next_tap);
  xpm_memory_tdpram #(.MEMORY_SIZE(64*72),.MEMORY_PRIMITIVE("block"),
   .CLOCKING_MODE("common_clock"),.ECC_MODE("no_ecc"),.USE_MEM_INIT(0),
   .WRITE_DATA_WIDTH_A(72),.WRITE_DATA_WIDTH_B(72),.READ_DATA_WIDTH_A(72),.READ_DATA_WIDTH_B(72),
   .BYTE_WRITE_WIDTH_A(72),.BYTE_WRITE_WIDTH_B(72),.ADDR_WIDTH_A(6),.ADDR_WIDTH_B(6),
   .READ_LATENCY_A(1),.READ_LATENCY_B(1),.WRITE_MODE_A("read_first"),.WRITE_MODE_B("read_first")) ram_i(
   .clka(clk),.clkb(clk),.rsta(1'b0),.rstb(1'b0),.regcea(1'b1),.regceb(1'b1),.sleep(1'b0),
   .ena(accept || history_prefetch),.enb(accept || history_prefetch),.wea(accept),.web(1'b0),
   .addra(address_a),.addrb(address_b),.dina(in_data),.dinb(72'd0),.douta(a_q),.doutb(b_q),
   .injectsbiterra(1'b0),.injectdbiterra(1'b0),.injectsbiterrb(1'b0),.injectdbiterrb(1'b0),
   .sbiterra(),.dbiterra(),.sbiterrb(),.dbiterrb());
  assign history_left[bank]=a_q;assign history_right[bank]=b_q;
 end endgenerate
 wire [4:0] tap_base=(phase-1)*3;
 wire signed [39:0] total[0:3];
 genvar lane,slot;
 generate for(lane=0;lane<4;lane=lane+1) begin:lanes
  wire signed [39:0] sum_products=
   {{3{products[lane][0][36]}},products[lane][0]}+
   {{3{products[lane][1][36]}},products[lane][1]}+
   {{3{products[lane][2][36]}},products[lane][2]};
  assign total[lane]=accum[lane]+sum_products;
  for(slot=0;slot<3;slot=slot+1) begin:slots
   wire [4:0] tap=tap_base+slot;
   wire [71:0] left_word=phase==1?recent[slot]:history_left[slot];
   wire [71:0] right_word=history_right[slot];
   wire signed [18:0] left_value={left_word[lane*18+17],left_word[lane*18+:18]};
   wire signed [18:0] right_value={right_word[lane*18+17],right_word[lane*18+:18]};
   wire signed [18:0] pair_value=(tap==20)?left_value:(left_value+right_value);
   always @(posedge clk) if(!rst && phase>=1 && phase<=7)
    products[lane][slot]<=pair_value*coefficients[tap];
  end
 end endgenerate
 integer l,t;
 always @(posedge clk) begin
  if(rst) begin
   phase<=0;count<=0;have_previous<=0;work_emit<=0;work_start<=0;history_head<=40;
   previous_tag<=0;work_tag<=0;out_valid<=0;out_tag<=0;out_data<=0;fence_count<=0;out_start<=0;
   previous_context<=0;work_context<=0;out_context<=0;
  end else begin
   if(out_valid && out_ready) out_valid<=0;
   if(phase>=1 && phase<=7) begin
    for(l=0;l<4;l=l+1) begin
     if(phase==1) accum[l]<=0;
     else accum[l]<=total[l];
    end
    phase<=phase+1;
   end
   if(phase==8 && finish_ready) begin
    if(work_emit) begin
     // Round nearest; an exact half-LSB rounds toward positive infinity.
     // No saturation: the declared input range and tap L1 bound fit 20 bits.
     for(l=0;l<4;l=l+1) out_data[l*20+:20]<=($signed(total[l])+40'sd65536)>>>17;
     out_valid<=1;out_tag<=work_tag;out_start<=work_start;out_context<=work_context;
    end
    phase<=0;
   end
   if(accept) begin
    history_head<=write_head;
    recent[0]<=in_data;recent[1]<=recent[0];recent[2]<=recent[1];
    // RAM/history not reset. Suppress output until all 41 entries belong to
    // this contiguous epoch; unknown/stale warm-up arithmetic is not exposed.
    // Latch the marker with this work item. A new window accepted while an
    // older result finishes must never change the older output's marker.
    work_start<=!gap && count==40;
    work_context<=in_context;previous_context<=in_context;
    if(gap) begin
     count<=1;work_emit<=0;
     if(have_previous) fence_count<=fence_count+1;
    end else begin
     if(count<41) count<=count+1;
     work_emit<=count>=40;
    end
    work_tag<={in_tag[63:48],(in_tag[47:0]-48'd20)};
    previous_tag<=in_tag;have_previous<=1;phase<=1;
   end
  end
 end
endmodule
