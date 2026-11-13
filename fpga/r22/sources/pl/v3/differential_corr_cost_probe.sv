`timescale 1ns/1ps
// G0 exact-integer correlator COST prototype, not complete acquisition.
// Input z[n]=conj(y[n-4])*y[n], endpoint tag n, four signed components.
// Output =127*correlation(z,D-mean(D)) for both IQ senses and two RXs.
// D is the PUBLIC repeated16 QPSK differential sequence, length127.
// Factor128 taps into8-tap stride64 comb and16-tap stride4 FIR, then
// remove the unwanted128th tap. No golden payload/frame/CFO input exists.
module differential_corr_cost_probe #(
 parameter integer W=29,
 parameter integer UW=W+4,
 parameter integer AW=W+9,
 parameter integer CW=W+16
)(
 input wire clk,rst,
 input wire in_valid,
 output wire in_ready,
 input wire [4*W-1:0] in_z,
 input wire [63:0] in_tag,
 input wire in_start,
 output reg out_valid,
 input wire out_ready,
 output reg [8*CW-1:0] out_c,
 output reg [63:0] out_tag,
 output reg [31:0] fences
);
 reg [3:0] phase;
 reg [8:0] cursor,active_cursor;
 reg [9:0] count,active_count;
 reg have_tag;
 reg [63:0] last_tag,active_tag;
 wire pipe_enable=!out_valid || out_ready;
 assign in_ready=!rst && (phase==0 || (phase==7 && pipe_enable));
 wire fire=in_valid && in_ready;
 wire gap=in_start || !have_tag || in_tag[63:48]!=last_tag[63:48] ||
          in_tag[47:0]!=(last_tag[47:0]+48'd1);
 wire [8:0] write_cursor=gap?9'd0:cursor;
 wire [9:0] next_count=gap?10'd1:((count<513)?count+10'd1:count);

 (* ram_style="block" *) reg [4*W-1:0] z_memory [0:511];
 // Two identical images provide four synchronous reads without LUTRAM.
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
 end endgenerate
 reg [4*W-1:0] z512_q,z508_q,current_z;

 reg [3:0] tap_number_q[0:3];
 reg tap_valid_q[0:3];
 reg signed [AW-1:0] accumulator[0:11];
 reg signed [AW-1:0] partial[0:11];
 reg signed [AW-1:0] n1[0:11],n2[0:11];
 reg [4*W-1:0] n1_old;
 reg [63:0] n1_tag,n2_tag;
 reg n1_valid,n2_valid;
 integer i,j,rx,tap,offset,base;
 reg signed [AW-1:0] re,im;

 function automatic integer coeff_re(input integer t);
  begin
   // D[(126-t) mod16] real part, exact +/-1 or0.
   case(t)
    0,2,3,5,14:coeff_re=1;
    6,9,13:coeff_re=-1;
    default:coeff_re=0;
   endcase
  end
 endfunction
 function automatic integer coeff_im(input integer t);
  begin
   case(t)
    1,7,10,11,12:coeff_im=1;
    4,8,15:coeff_im=-1;
    default:coeff_im=0;
   endcase
  end
 endfunction
 function automatic signed [AW-1:0] extend_z(input [W-1:0] x);
  begin extend_z={{(AW-W){x[W-1]}},x};end
 endfunction
 function automatic signed [AW-1:0] extend_u(input [UW-1:0] x);
  begin extend_u={{(AW-UW){x[UW-1]}},x};end
 endfunction
 function automatic signed [CW-1:0] wide(input signed [AW-1:0] x);
  begin wide={{(CW-AW){x[AW-1]}},x};end
 endfunction
 function automatic signed [CW-1:0] times127(input signed [CW-1:0] x);
  begin times127=(x<<<7)-x;end
 endfunction

 // Same-clock READ_FIRST on portA (512-old input before replacement),
 // independent portB reads508-old. All invalid history is logically masked.
 always @(posedge clk) if(fire) begin
  z512_q<=z_memory[write_cursor];
  z_memory[write_cursor]<=in_z;
  z508_q<=z_memory[(write_cursor+9'd4)&9'h1ff];
 end
 always @(posedge clk) begin
  if(phase>=3 && phase<=6) begin
   for(integer k=0;k<4;k=k+1) begin
    tap_number_q[k]<=(phase-3)*4+k;
    tap_valid_q[k]<=active_count>((phase-3)*16+k*4);
   end
  end
 end

 // Only four lanes. Real/imag coeffs are sign/select, not multipliers.
 always @* begin
  for(i=0;i<12;i=i+1) partial[i]=0;
  for(j=0;j<4;j=j+1) begin
   for(rx=0;rx<2;rx=rx+1) begin
    re=tap_valid_q[j]?extend_u(tap_q[j][(rx*2)*UW+:UW]):0;
    im=tap_valid_q[j]?extend_u(tap_q[j][(rx*2+1)*UW+:UW]):0;
    tap=tap_number_q[j];base=rx*6;
    if(coeff_re(tap)==1) begin partial[base]+=re;partial[base+1]+=im;end
    if(coeff_re(tap)==-1) begin partial[base]-=re;partial[base+1]-=im;end
    if(coeff_im(tap)==1) begin partial[base+2]+=re;partial[base+3]+=im;end
    if(coeff_im(tap)==-1) begin partial[base+2]-=re;partial[base+3]-=im;end
    partial[base+4]+=re;partial[base+5]+=im;
   end
  end
 end

 always @(posedge clk) begin
  if(rst) begin
   phase<=0;cursor<=0;active_cursor<=0;count<=0;active_count<=0;
   have_tag<=0;last_tag<=0;active_tag<=0;fences<=0;
   n1_valid<=0;n2_valid<=0;out_valid<=0;out_tag<=0;out_c<=0;
   n1_tag<=0;n2_tag<=0;n1_old<=0;
   for(integer k=0;k<12;k=k+1) begin accumulator[k]<=0;n1[k]<=0;n2[k]<=0;end
  end else begin
   if(pipe_enable) begin
    out_valid<=n2_valid;out_tag<=n2_tag;
    n2_valid<=n1_valid;n2_tag<=n1_tag;
    n1_valid<=(phase==7 && active_count>=505);
    if(n1_valid) begin
     // The omitted j127 coefficient is D15=-j.
     for(integer r=0;r<2;r=r+1) begin
      n2[r*6]<=n1[r*6];n2[r*6+1]<=n1[r*6+1];
      n2[r*6+2]<=n1[r*6+2]+extend_z(n1_old[(r*2)*W+:W]);
      n2[r*6+3]<=n1[r*6+3]+extend_z(n1_old[(r*2+1)*W+:W]);
      n2[r*6+4]<=n1[r*6+4]-extend_z(n1_old[(r*2)*W+:W]);
      n2[r*6+5]<=n1[r*6+5]-extend_z(n1_old[(r*2+1)*W+:W]);
     end
    end
    if(n2_valid) begin
     for(integer r=0;r<2;r=r+1) begin
      // mean(D)=(16+17j)/127. Wide intermediates prevent truncation.
      out_c[(r*2)*CW+:CW]<=times127(wide(n2[r*6])+wide(n2[r*6+3]))-
       (wide(n2[r*6+4])<<<4)-((wide(n2[r*6+5])<<<4)+wide(n2[r*6+5]));
      out_c[(r*2+1)*CW+:CW]<=times127(wide(n2[r*6+1])-wide(n2[r*6+2]))-
       (wide(n2[r*6+5])<<<4)+((wide(n2[r*6+4])<<<4)+wide(n2[r*6+4]));
      out_c[(4+r*2)*CW+:CW]<=times127(wide(n2[r*6])-wide(n2[r*6+3]))-
       (wide(n2[r*6+4])<<<4)+((wide(n2[r*6+5])<<<4)+wide(n2[r*6+5]));
      out_c[(4+r*2+1)*CW+:CW]<=times127(wide(n2[r*6+1])+wide(n2[r*6+2]))-
       (wide(n2[r*6+5])<<<4)-((wide(n2[r*6+4])<<<4)+wide(n2[r*6+4]));
     end
    end
    if(phase==7) begin
     for(integer k=0;k<12;k=k+1) n1[k]<=accumulator[k]+partial[k];
     n1_old<=active_count>508?z508_q:0;
     n1_tag<={active_tag[63:48],active_tag[47:0]-48'd508};
    end
   end
   if(phase==1) begin
    for(integer k=0;k<4;k=k+1)
     current_u[k*UW+:UW]<=
      (active_count>64?extend_u(old_u_q[k*UW+:UW]):0)+extend_z(current_z[k*W+:W])-
      (active_count>512?extend_z(z512_q[k*W+:W]):0);
   end
   if(phase==2) for(integer k=0;k<12;k=k+1) accumulator[k]<=0;
   if(phase>=4 && phase<=6)
    for(integer k=0;k<12;k=k+1) accumulator[k]<=accumulator[k]+partial[k];
   if(phase>=1 && phase<7) phase<=phase+1;
   if(phase==7 && pipe_enable) phase<=0;
   if(fire) begin
    phase<=1;current_z<=in_z;active_cursor<=write_cursor;active_tag<=in_tag;
    active_count<=next_count;count<=next_count;cursor<=write_cursor+9'd1;
    have_tag<=1;last_tag<=in_tag;
    if(have_tag && gap) fences<=fences+1;
   end
  end
 end
endmodule
