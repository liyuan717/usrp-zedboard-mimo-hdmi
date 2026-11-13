#!/usr/bin/env python3
"""Pure RRC history mapping; numeric widths, coefficient/math and II8 preserved."""
def transform(s):
    old=' reg signed [17:0] history[0:3][0:40];';assert s.count(old)==1
    s=s.replace(old,''' // Three recent words bypass first-round reads while every image writes.
 // Full logical history stays41words; three images supply six read ports.
 reg [71:0] recent[0:2];
 reg [5:0] history_head;
 wire [71:0] history_left[0:2],history_right[0:2];''')
    marker=' wire accept=in_valid && in_ready;';assert s.count(marker)==1
    s=s.replace(marker,marker+'''
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
 end endgenerate''')
    old='''   wire signed [18:0] left_value={history[lane][tap][17],history[lane][tap]};
   wire signed [18:0] right_value={history[lane][40-tap][17],history[lane][40-tap]};'''
    new='''   wire [71:0] left_word=phase==1?recent[slot]:history_left[slot];
   wire [71:0] right_word=history_right[slot];
   wire signed [18:0] left_value={left_word[lane*18+17],left_word[lane*18+:18]};
   wire signed [18:0] right_value={right_word[lane*18+17],right_word[lane*18+:18]};'''
    assert s.count(old)==1;s=s.replace(old,new)
    old='   phase<=0;count<=0;have_previous<=0;work_emit<=0;work_start<=0;';assert s.count(old)==1
    s=s.replace(old,old+'history_head<=40;')
    old='''    for(l=0;l<4;l=l+1) begin
     history[l][0]<=in_data[l*18+:18];
     for(t=1;t<41;t=t+1) history[l][t]<=history[l][t-1];
    end'''
    assert s.count(old)==1
    s=s.replace(old,'''    history_head<=write_head;
    recent[0]<=in_data;recent[1]<=recent[0];recent[2]<=recent[1];''')
    return s
