#!/usr/bin/env python3
"""Pure, reviewable replacement of only the coarse correlator history mapping."""
def transform(s):
    old=' (* ram_style="distributed" *) reg [4*UW-1:0] u_memory [0:63];'
    assert s.count(old)==1
    s=s.replace(old,''' // Two identical images provide four synchronous reads without LUTRAM.
 // Each image uses true dual-port READ_FIRST block RAM. PortA is shared
 // with writes/old-u reads in disjoint phases; no extra cycle or history.
 reg [4*UW-1:0] current_u;
 wire [4*UW-1:0] u_a_q[0:1],u_b_q[0:1];
 wire [4*UW-1:0] old_u_q=u_a_q[0];
 wire [4*UW-1:0] tap_q[0:3];
 assign tap_q[0]=u_a_q[0];assign tap_q[1]=u_b_q[0];
 assign tap_q[2]=u_a_q[1];assign tap_q[3]=u_b_q[1];
 wire u_tap_read=phase>=3 && phase<=6;
 wire u_write=phase==2;
 genvar bank;
 generate for(bank=0;bank<2;bank=bank+1) begin:history_ports
  (* ram_style="block" *) reg [4*UW-1:0] memory[0:63];
  reg [4*UW-1:0] a_q,b_q;
  wire [5:0] tap_a=active_cursor[5:0]-((phase-3)*16+bank*8);
  wire [5:0] tap_b=active_cursor[5:0]-((phase-3)*16+bank*8+4);
  wire [5:0] address_a=u_write?active_cursor[5:0]:(fire?write_cursor[5:0]:tap_a);
  always @(posedge clk) if(u_write || fire || u_tap_read) begin
   if(u_write) memory[address_a]<=current_u;
   a_q<=memory[address_a];
  end
  always @(posedge clk) if(u_tap_read) b_q<=memory[tap_b];
  assign u_a_q[bank]=a_q;assign u_b_q[bank]=b_q;
 end endgenerate''')
    s=s.replace(' reg [4*UW-1:0] old_u_q,current_u;','')
    s=s.replace(' reg [4*UW-1:0] tap_q[0:3];\n','')
    for old in ['  old_u_q<=u_memory[write_cursor[5:0]];\n','  if(phase==2) u_memory[active_cursor[5:0]]<=current_u;\n','    tap_q[k]<=u_memory[(active_cursor[5:0]-((phase-3)*16+k*4))&6\'h3f];\n']:
        assert s.count(old)==1,old;s=s.replace(old,'')
    return s
