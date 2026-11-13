`timescale 1ns/1ps
// RXV2 timing/SFO estimator. Expensive divides are issued to one bounded,
// 80-cycle restoring divider; this source contains no variable '/' datapath.
module rxv2_timing_sfo(
 input wire clk,input wire resetn,input wire start_valid,output wire start_ready,input wire [31:0] start_epoch,input wire [63:0] start_origin,input wire start_locked,input wire signed [31:0] start_tau_q16,input wire signed [31:0] start_sfo_ppm_q16,
 output wire sn_rd_en,output wire [8:0] sn_rd_addr,input wire sn_rd_valid,input wire [63:0] sn_rd_data,
 output wire frac_req_valid,input wire frac_req_ready,output wire [63:0] frac_req_floor,output wire [15:0] frac_req_fraction,input wire frac_m_valid,output wire frac_m_ready,input wire [127:0] frac_m_data,
 output reg done_valid,input wire done_ready,output reg [31:0] done_epoch,output reg [63:0] done_origin,output reg signed [31:0] done_tau_q16,output reg signed [31:0] done_sfo_ppm_q16,output reg [30:0] done_score_q30,output reg done_lock,output reg done_error);
 localparam signed [31:0] TS=8192,TRS=8192,PPM=16384000; localparam [30:0] LOCK=858993459; localparam N=112;
 localparam [5:0] IDLE=0,PREP=1,CS=2,CW=3,CC=4,LOAD=5,SNA=6,SNWA=7,SNB=8,SNWB=9,FREQ=10,FRW=11,SAMP=12,ACC=13,SQI=14,SQR=15,SS=16,SW=17,SC=18,NEXT=19,PS=20,PSTART=21,PW=22,PC=23,STSQ=24,FSET=25,FGATE=26,FSTART=27,FS=28,FC=29,EMIT=30,RINIT=31,RTRANS=32,RPSTART=33,RPWAIT=34,RPSET=35,HANDOFF_CENTER=36;
 reg [5:0] state; reg locked_q; reg [31:0] epoch_q; reg [63:0] origin_q;
 reg signed [31:0] tau_base,sfo_base,cand_tau,cand_sfo,best_tau,best_sfo,innovation,sfo_anchor;
 // Cold acquisition first evaluates the historical 17x9 coarse grid.  A
 // non-zero result is then refined by the exact bounded schedule used by
 // the native receiver: four levels, two alternating tau/SFO passes per
 // level, 13 and 11 candidates respectively.  These are scheduler state,
 // not replicated MAC/divider datapaths.
 reg refine_q,ref_axis; reg [1:0] ref_level,ref_pass; reg [3:0] ref_idx;
 // Cold fitting spans several arriving frames. Its bounded SFO estimation
 // error can extrapolate beyond the steady-state +/-1/16-sample tracker.
 // The first live training window therefore uses a 17-point +/-1 sample
 // handoff sweep, then the ordinary 3-point parabola. Same physical datapath.
 reg handoff_pending,handoff_sweep;
 reg signed [31:0] ref_center_tau,ref_center_sfo,ref_ppm_lo;
 reg [4:0] ti;reg [3:0] si;reg [6:0] pi;reg [8:0] saddr;reg [15:0] frac;reg [63:0] snap_a,snap_b,floor_q;
 reg signed [31:0] c00r,c00i,c10r,c10i,c01r,c01i,c11r,c11i;reg signed [15:0] ry0i,ry0q,ry1i,ry1q;
 reg [47:0] energy,score_num,sqsum;reg [30:0] cand_score,best_score,lscore,cscore,rscore;reg [3:0] sqi;
 reg signed [17:0] isum,sum_new;reg [30:0] isumsq,sumsq_new;reg [4:0] icount;reg [35:0] sum_square;
 reg signed [79:0] dnum;reg [63:0] dden; wire ds=(state==SS)||(state==PSTART)||(state==FSTART)||(state==RPSTART);wire dbusy,ddone,dzero;wire signed [63:0] dq;
 reg signed [39:0] sfo_prod_q; reg signed [63:0] recip_prod_q; reg signed [31:0] sfo_term_q;
 rxv2_timing_serial_divider div0(.clk(clk),.resetn(resetn),.start(ds),.numerator(dnum),.denominator(dden),.busy(dbusy),.done(ddone),.div_zero(dzero),.quotient(dq));
 assign start_ready=resetn&&(state==IDLE);assign sn_rd_en=resetn&&((state==SNA)||(state==SNB));assign sn_rd_addr=saddr;assign frac_req_valid=resetn&&(state==FREQ);assign frac_req_floor=floor_q;assign frac_req_fraction=frac;assign frac_m_ready=resetn&&(state==FRW);
 function automatic [7:0] b(input [5:0] x);begin case(x>>2)0:b=8'h14;1:b=8'h73;2:b=8'hf0;3:b=8'he6;4:b=8'h3f;5:b=8'he3;6:b=8'h64;7:b=8'h37;8:b=8'he0;9:b=8'hb5;10:b=8'h34;11:b=8'h97;12:b=8'hf1;13:b=8'hf1;14:b=8'h6a;default:b=8'h5c;endcase end endfunction
 function automatic signed [1:0] xi(input [5:0] x);reg [1:0] q;begin q=(b(x)>>(6-2*(x&3)))&3;xi=(q==1||q==2)?-1:1;end endfunction
 function automatic signed [1:0] xq(input [5:0] x);reg [1:0] q;begin q=(b(x)>>(6-2*(x&3)))&3;xq=(q==2||q==3)?-1:1;end endfunction
 function automatic signed [15:0] lerp(input signed [15:0] a,input signed [15:0] z,input [15:0] f);reg signed [16:0] delta;reg signed [33:0] p;reg [33:0] mag,rnd;begin delta={z[15],z}-{a[15],a};p=delta*$signed({1'b0,f});mag=p[33]?-p:p;rnd=(mag>>16)+((mag[15:0]>16'h8000)||((mag[15:0]==16'h8000)&&mag[16]));lerp=a+(p[33]?- $signed(rnd[15:0]):$signed(rnd[15:0]));end endfunction
 wire signed [15:0] a0i=snap_a[15:0],a0q=snap_a[31:16],a1i=snap_a[47:32],a1q=snap_a[63:48],z0i=snap_b[15:0],z0q=snap_b[31:16],z1i=snap_b[47:32],z1q=snap_b[63:48];
 // Control helper has S32 Q15 lanes. Explicitly return to this Q12 domain.
 wire signed [15:0] f0i=$signed(frac_m_data[31:0])>>>3,f0q=$signed(frac_m_data[63:32])>>>3,f1i=$signed(frac_m_data[95:64])>>>3,f1q=$signed(frac_m_data[127:96])>>>3;
 wire signed [15:0] y0i=locked_q?f0i:lerp(a0i,z0i,frac),y0q=locked_q?f0q:lerp(a0q,z0q,frac),y1i=locked_q?f1i:lerp(a1i,z1i,frac),y1q=locked_q?f1q:lerp(a1q,z1q,frac);
 wire [5:0] train_sym=(pi<56)?pi[5:0]+4:(pi-56)+4;wire signed [1:0] qpi=xi(train_sym),qpq=xq(train_sym);wire signed [18:0] cr0=ry0i*qpi+ry0q*qpq,ci0=ry0q*qpi-ry0i*qpq,cr1=ry1i*qpi+ry1q*qpq,ci1=ry1q*qpi-ry1i*qpq;wire [33:0] ey=ry0i*ry0i+ry0q*ry0q+ry1i*ry1i+ry1q*ry1q;
 reg signed [31:0] sel;always @* case(sqi)0:sel=c00r;1:sel=c00i;2:sel=c10r;3:sel=c10i;4:sel=c01r;5:sel=c01i;6:sel=c11r;default:sel=c11i;endcase wire [63:0] sel_sq=sel*sel;
 reg signed [12:0] rel;reg [8:0] lb;reg [63:0] sb;always @* if(pi<56)begin lb=16+(pi<<2);rel=-262+$signed({1'b0,pi,2'b00});sb=560;end else begin lb=16+((pi-56)<<2);rel=42+$signed({1'b0,(pi-56),2'b00});sb=864;end
 // The explicit 64-bit signed product is essential: assigning the raw
 // 32x13 expression directly into the 80-bit divider operand sign-extended
 // a zero product to -2^64 for negative rel values in simulation.
 wire signed [63:0] sfo_product=$signed(cand_sfo)*$signed(rel);
 wire signed [63:0] coord=$signed({1'b0,lb,16'd0})+cand_tau+sfo_term_q;wire signed [32:0] curve=$signed({1'b0,lscore})-($signed({1'b0,cscore})<<<1)+$signed({1'b0,rscore}),slope=$signed({1'b0,lscore})-$signed({1'b0,rscore});wire signed [17:0] inclamp=(dq>8191)?8191:(dq<-8191)?-8191:dq[17:0];wire signed [13:0] innov14=innovation[13:0];wire [27:0] insq=innov14*innov14;wire [35:0] sumsqrd=sum_new*sum_new;
 // Complete the signed arithmetic shift before the unsigned absolute-origin
 // addition. Inlining coord>>>16 there made negative coord logically shift
 // under unsigned expression-context propagation (a spurious +2^48 address).
 wire signed [63:0] coord_floor=coord>>>16;
 // 16 innovations of magnitude <=8191 fit signed18 sum and unsigned31
 // sum-of-squares. Widen BEFORE shifts/multiply in the 3SE comparison.
 wire [39:0] gate_left={4'd0,sum_square}<<4;
 wire [39:0] gate_right=((({9'd0,sumsq_new}<<4)-{4'd0,sum_square}))*40'd9;
 function automatic signed [31:0] reciprocal_rne_q16(input signed [63:0] x);reg signed [63:0] mag;reg [21:0] q;reg [41:0] rem;begin mag=x[63]?-x:x;q=mag>>>42;rem=mag[41:0];q=q+((rem>42'h20000000000)||((rem==42'h20000000000)&&q[0]));reciprocal_rne_q16=x[63]?- $signed({1'b0,q}):$signed({1'b0,q});end endfunction
 function automatic signed [31:0] ref_tau_radius(input [1:0] level);begin case(level)2'd0:ref_tau_radius=32'sd78643;2'd1:ref_tau_radius=32'sd13107;2'd2:ref_tau_radius=32'sd2621;default:ref_tau_radius=32'sd524;endcase end endfunction
 function automatic signed [31:0] ref_ppm_radius(input [1:0] level);begin case(level)2'd0:ref_ppm_radius=32'sd65536000;2'd1:ref_ppm_radius=32'sd13107200;2'd2:ref_ppm_radius=32'sd2621440;default:ref_ppm_radius=32'sd524288;endcase end endfunction
 // Explicit Q16 offset tables avoid inferring variable multipliers/dividers
 // for the 13-point constant grids.  Values are nearest-integer samples of
 // [-radius,+radius], with index 6 exactly zero.
 function automatic signed [31:0] ref_tau_offset(input [1:0] level,input [3:0] index);begin
   case(level)
    0:case(index)0:ref_tau_offset=-78643;1:ref_tau_offset=-65536;2:ref_tau_offset=-52429;3:ref_tau_offset=-39322;4:ref_tau_offset=-26214;5:ref_tau_offset=-13107;6:ref_tau_offset=0;7:ref_tau_offset=13107;8:ref_tau_offset=26214;9:ref_tau_offset=39322;10:ref_tau_offset=52429;11:ref_tau_offset=65536;default:ref_tau_offset=78643;endcase
    1:case(index)0:ref_tau_offset=-13107;1:ref_tau_offset=-10923;2:ref_tau_offset=-8738;3:ref_tau_offset=-6554;4:ref_tau_offset=-4369;5:ref_tau_offset=-2185;6:ref_tau_offset=0;7:ref_tau_offset=2185;8:ref_tau_offset=4369;9:ref_tau_offset=6554;10:ref_tau_offset=8738;11:ref_tau_offset=10923;default:ref_tau_offset=13107;endcase
    2:case(index)0:ref_tau_offset=-2621;1:ref_tau_offset=-2184;2:ref_tau_offset=-1747;3:ref_tau_offset=-1311;4:ref_tau_offset=-874;5:ref_tau_offset=-437;6:ref_tau_offset=0;7:ref_tau_offset=437;8:ref_tau_offset=874;9:ref_tau_offset=1311;10:ref_tau_offset=1747;11:ref_tau_offset=2184;default:ref_tau_offset=2621;endcase
    default:case(index)0:ref_tau_offset=-524;1:ref_tau_offset=-437;2:ref_tau_offset=-349;3:ref_tau_offset=-262;4:ref_tau_offset=-175;5:ref_tau_offset=-87;6:ref_tau_offset=0;7:ref_tau_offset=87;8:ref_tau_offset=175;9:ref_tau_offset=262;10:ref_tau_offset=349;11:ref_tau_offset=437;default:ref_tau_offset=524;endcase
   endcase
 end endfunction
 function automatic signed [31:0] ref_tau_trial(input signed [31:0] center,input [1:0] level,input [3:0] index);begin ref_tau_trial=center+ref_tau_offset(level,index);end endfunction
 // Do not synthesize span*index/10 as a combinational divider.  Candidate
 // generation is off the sample-rate path, so it issues the existing bounded
 // 80-cycle divider with a shift/add numerator and registers the result.
 function automatic signed [31:0] ref_ppm_low(input signed [31:0] center,input [1:0] level);reg signed [63:0] v;begin v=$signed(center)-$signed(ref_ppm_radius(level));if(v<-$signed(32'sd65536000))v=-$signed(32'sd65536000);ref_ppm_low=v[31:0];end endfunction
 function automatic signed [31:0] ref_ppm_high(input signed [31:0] center,input [1:0] level);reg signed [63:0] v;begin v=$signed(center)+$signed(ref_ppm_radius(level));if(v>$signed(32'sd65536000))v=$signed(32'sd65536000);ref_ppm_high=v[31:0];end endfunction
 function automatic signed [63:0] ref_ppm_scaled(input signed [31:0] span,input [3:0] index);begin case(index)0:ref_ppm_scaled=0;1:ref_ppm_scaled=span;2:ref_ppm_scaled=$signed(span)<<<1;3:ref_ppm_scaled=span+($signed(span)<<<1);4:ref_ppm_scaled=$signed(span)<<<2;5:ref_ppm_scaled=span+($signed(span)<<<2);6:ref_ppm_scaled=($signed(span)<<<1)+($signed(span)<<<2);7:ref_ppm_scaled=span+($signed(span)<<<1)+($signed(span)<<<2);8:ref_ppm_scaled=$signed(span)<<<3;9:ref_ppm_scaled=span+($signed(span)<<<3);default:ref_ppm_scaled=($signed(span)<<<1)+($signed(span)<<<3);endcase end endfunction
 always @(posedge clk) begin if(!resetn) begin
  state<=IDLE;done_valid<=0;done_epoch<=0;done_origin<=0;done_tau_q16<=0;done_sfo_ppm_q16<=0;done_score_q30<=0;done_lock<=0;done_error<=0;locked_q<=0;epoch_q<=0;origin_q<=0;tau_base<=0;sfo_base<=0;cand_tau<=0;cand_sfo<=0;best_tau<=0;best_sfo<=0;refine_q<=0;ref_axis<=0;ref_level<=0;ref_pass<=0;ref_idx<=0;ref_center_tau<=0;ref_center_sfo<=0;ref_ppm_lo<=0;ti<=0;si<=0;pi<=0;saddr<=0;frac<=0;snap_a<=0;floor_q<=0;c00r<=0;c00i<=0;c10r<=0;c10i<=0;c01r<=0;c01i<=0;c11r<=0;c11i<=0;ry0i<=0;ry0q<=0;ry1i<=0;ry1q<=0;energy<=0;score_num<=0;sqsum<=0;cand_score<=0;best_score<=0;lscore<=0;cscore<=0;rscore<=0;sqi<=0;innovation<=0;sfo_anchor<=0;isum<=0;isumsq<=0;icount<=0;sum_new<=0;sumsq_new<=0;sum_square<=0;dnum<=0;dden<=0;sfo_prod_q<=0;recip_prod_q<=0;sfo_term_q<=0;
  handoff_pending<=0;handoff_sweep<=0;
 end else case(state)
 IDLE:begin done_valid<=0;if(start_valid)begin locked_q<=start_locked;epoch_q<=start_epoch;origin_q<=start_origin;tau_base<=start_tau_q16;sfo_base<=start_sfo_ppm_q16;ti<=0;si<=0;pi<=0;best_score<=0;handoff_sweep<=start_locked&&handoff_pending;if(start_locked)begin cand_tau<=start_tau_q16-(handoff_pending?32'sd65536:TRS);cand_sfo<=start_sfo_ppm_q16;handoff_pending<=0;end else begin cand_tau<=-8*TS;cand_sfo<=-4*PPM;end c00r<=0;c00i<=0;c10r<=0;c10i<=0;c01r<=0;c01i<=0;c11r<=0;c11i<=0;energy<=0;state<=PREP;end end
 // p=cand_ppm_Q16*rel is bounded by 17.2e9.  4398047/2^42
 // approximates 1/1e6; the signed nearest-even result is <.502 Q16 LSB
 // from p/1e6 over the full cold range.  Three registered DSP-friendly taps.
  PREP:begin sfo_prod_q<=sfo_product[39:0];state<=CS;end CS:begin recip_prod_q<=$signed(sfo_prod_q)*$signed(24'sd4398047);state<=CW;end CW:begin sfo_term_q<=reciprocal_rne_q16(recip_prod_q);state<=LOAD;end CC:state<=LOAD;
  LOAD:begin floor_q<=origin_q+40+sb+coord_floor;frac<=coord[15:0];saddr<=(pi<56?0:256)+coord_floor;state<=locked_q?FREQ:SNA;end
 // Capture B on its existing response edge, before SAMP consumes the pair.
 // No extra FSM cycle is added; this cuts BRAM->lerp->energy-DSP timing.
 SNA:state<=SNWA;SNWA:if(sn_rd_valid)begin snap_a<=sn_rd_data;saddr<=saddr+1'b1;state<=SNB;end SNB:state<=SNWB;SNWB:if(sn_rd_valid)begin snap_b<=sn_rd_data;state<=SAMP;end FREQ:if(frac_req_ready)state<=FRW;FRW:if(frac_m_valid)state<=SAMP;SAMP:begin ry0i<=y0i;ry0q<=y0q;ry1i<=y1i;ry1q<=y1q;state<=ACC;end
 ACC:begin if(pi<56)begin c00r<=c00r+cr0;c00i<=c00i+ci0;c10r<=c10r+cr1;c10i<=c10i+ci1;end else begin c01r<=c01r+cr0;c01i<=c01i+ci0;c11r<=c11r+cr1;c11i<=c11i+ci1;end energy<=energy+ey;if(pi==N-1)state<=SQI;else begin pi<=pi+1;state<=PREP;end end
 SQI:begin sqi<=0;sqsum<=0;state<=SQR;end SQR:if(sqi==7)begin score_num<=sqsum+sel_sq[47:0];dnum<=$signed({1'b0,sqsum+sel_sq[47:0],30'd0});dden<=(energy<<6)+(energy<<5)+(energy<<4);state<=SS;end else begin sqsum<=sqsum+sel_sq[47:0];sqi<=sqi+1;end SS:state<=SW;SW:if(ddone)state<=SC;SC:begin cand_score<=dzero?0:dq[30:0];state<=NEXT;end
 // Quantised scores can tie across the 250-ppm cold grid.  Select the
 // smallest-magnitude SFO among exact ties rather than the scan-order edge.
 // The refinement paths use strict comparison only: candidate enumeration
 // is low-to-high and therefore retains the first minimum exactly as the
 // reference implementation does.  Coarse-grid tie handling remains the
 // historical smallest-|SFO| rule for reproducible acquisition.
 NEXT:begin
   if((cand_score>best_score)||(!refine_q&&!locked_q&&cand_score==best_score&&((cand_sfo<0?-cand_sfo:cand_sfo)<(best_sfo<0?-best_sfo:best_sfo))))begin best_score<=cand_score;best_tau<=cand_tau;best_sfo<=cand_sfo;end
   if(refine_q)begin
     if(!ref_axis)begin
       if(ref_idx==12) state<=RTRANS;
       else begin ref_idx<=ref_idx+1'b1;cand_tau<=ref_tau_trial(ref_center_tau,ref_level,ref_idx+1'b1);cand_sfo<=ref_center_sfo;pi<=0;c00r<=0;c00i<=0;c10r<=0;c10i<=0;c01r<=0;c01i<=0;c11r<=0;c11i<=0;energy<=0;state<=PREP;end
     end else begin
       if(ref_idx==10) state<=RTRANS;
       else begin ref_idx<=ref_idx+1'b1;dnum<=ref_ppm_scaled($signed(ref_ppm_high(ref_center_sfo,ref_level))-$signed(ref_ppm_low(ref_center_sfo,ref_level)),ref_idx+1'b1);dden<=10;state<=RPSTART;end
     end
   end else if(locked_q&&handoff_sweep)begin
     if(ti==16)state<=HANDOFF_CENTER;
     else begin ti<=ti+1'b1;cand_tau<=cand_tau+TS;pi<=0;c00r<=0;c00i<=0;c10r<=0;c10i<=0;c01r<=0;c01i<=0;c11r<=0;c11i<=0;energy<=0;state<=PREP;end
   end else if(locked_q)begin if(ti==0)lscore<=cand_score;else if(ti==1)cscore<=cand_score;else rscore<=cand_score;if(ti==2)state<=PS;else begin ti<=ti+1;cand_tau<=tau_base+(ti==0?0:TRS);pi<=0;c00r<=0;c00i<=0;c10r<=0;c10i<=0;c01r<=0;c01i<=0;c11r<=0;c11i<=0;energy<=0;state<=PREP;end end else if(ti==16)begin if(si==8)state<=RINIT;else begin si<=si+1;ti<=0;cand_sfo<=cand_sfo+PPM;cand_tau<=-8*TS;pi<=0;c00r<=0;c00i<=0;c10r<=0;c10i<=0;c01r<=0;c01i<=0;c11r<=0;c11i<=0;energy<=0;state<=PREP;end end else begin ti<=ti+1;cand_tau<=cand_tau+TS;pi<=0;c00r<=0;c00i<=0;c10r<=0;c10i<=0;c01r<=0;c01i<=0;c11r<=0;c11i<=0;energy<=0;state<=PREP;end end
 HANDOFF_CENTER:begin
   handoff_sweep<=0;tau_base<=best_tau;cand_tau<=best_tau-TRS;cand_sfo<=sfo_base;
   best_score<=0;ti<=0;pi<=0;icount<=0;isum<=0;isumsq<=0;
   c00r<=0;c00i<=0;c10r<=0;c10i<=0;c01r<=0;c01i<=0;c11r<=0;c11i<=0;energy<=0;state<=PREP;
 end
 // Coarse selection has settled by the time RINIT is reached.  Preserve its
 // exact zero fixed point, otherwise begin level-0 tau sweep.
 RINIT:begin if(best_tau==0&&best_sfo==0)state<=EMIT;else begin refine_q<=1;ref_axis<=0;ref_level<=0;ref_pass<=0;ref_idx<=0;ref_center_tau<=best_tau;ref_center_sfo<=best_sfo;best_score<=0;cand_tau<=ref_tau_trial(best_tau,0,0);cand_sfo<=best_sfo;pi<=0;c00r<=0;c00i<=0;c10r<=0;c10i<=0;c01r<=0;c01i<=0;c11r<=0;c11i<=0;energy<=0;state<=PREP;end end
 // Separate transition state is intentional: it observes the nonblocking
 // update of best_* made by the final candidate before seeding the next
 // one-dimensional sweep.
 RTRANS:begin
   if(!ref_axis)begin
     ref_axis<=1;ref_idx<=0;ref_center_tau<=best_tau;best_score<=0;
     ref_ppm_lo<=ref_ppm_low(ref_center_sfo,ref_level);dnum<=0;dden<=10;state<=RPSTART;
   end else begin
     ref_center_sfo<=best_sfo;
     if(ref_pass==1)begin
       if(ref_level==3)begin refine_q<=0;state<=EMIT;end
       else begin ref_level<=ref_level+1'b1;ref_pass<=0;ref_axis<=0;ref_idx<=0;ref_center_tau<=best_tau;ref_center_sfo<=best_sfo;best_score<=0;cand_tau<=ref_tau_trial(best_tau,ref_level+1'b1,0);cand_sfo<=best_sfo;pi<=0;c00r<=0;c00i<=0;c10r<=0;c10i<=0;c01r<=0;c01i<=0;c11r<=0;c11i<=0;energy<=0;state<=PREP;end
     end else begin ref_pass<=ref_pass+1'b1;ref_axis<=0;ref_idx<=0;ref_center_tau<=best_tau;best_score<=0;cand_tau<=ref_tau_trial(best_tau,ref_level,0);cand_sfo<=best_sfo;pi<=0;c00r<=0;c00i<=0;c10r<=0;c10i<=0;c01r<=0;c01i<=0;c11r<=0;c11i<=0;energy<=0;state<=PREP;end
   end
 end
 // The same serial divider is idle between candidates.  Its registered
 // quotient creates the next SFO coordinate without a wide comb divide on
 // either the refinement scheduler or the sample-coordinate datapath.
 RPSTART:state<=RPWAIT;
 RPWAIT:if(ddone)state<=RPSET;
 RPSET:begin cand_tau<=ref_center_tau;cand_sfo<=ref_ppm_lo+dq;pi<=0;c00r<=0;c00i<=0;c10r<=0;c10i<=0;c01r<=0;c01i<=0;c11r<=0;c11i<=0;energy<=0;state<=PREP;end
 // Vertex = (left-right)*step/(2*(left-2*center+right)); denominator is made positive.
 PS:if(curve>=0)begin innovation<=0;state<=STSQ;end else begin dnum<=-(slope<<<12);dden<=-curve;state<=PSTART;end PSTART:state<=PW;PW:if(ddone)state<=PC;PC:begin innovation<=inclamp;state<=STSQ;end
 STSQ:if(innovation==0)begin icount<=0;isum<=0;isumsq<=0;state<=EMIT;end else if(icount<15)begin isum<=isum+innovation;isumsq<=isumsq+insq;icount<=icount+1;state<=EMIT;end else begin sum_new<=isum+innovation;sumsq_new<=isumsq+insq;state<=FSET;end
 // 3SE: 16*sum^2 > 9*(16*sumSquares-sum^2), all bounded widths.
 FSET:begin sum_square<=sumsqrd;state<=FGATE;end FGATE:if(gate_left>gate_right)begin dnum<=sum_new*1000000;dden<=61440;state<=FSTART;end else begin icount<=0;isum<=0;isumsq<=0;state<=EMIT;end FSTART:state<=FS;FS:if(ddone)state<=FC;FC:begin if(sfo_base+dq>sfo_anchor+13107200)done_sfo_ppm_q16<=sfo_anchor+13107200;else if(sfo_base+dq<sfo_anchor-13107200)done_sfo_ppm_q16<=sfo_anchor-13107200;else done_sfo_ppm_q16<=sfo_base+dq;icount<=0;isum<=0;isumsq<=0;state<=EMIT;end
 EMIT:begin done_valid<=1;done_epoch<=epoch_q;done_origin<=origin_q;done_tau_q16<=locked_q?(tau_base+innovation):best_tau;if(!locked_q)begin done_sfo_ppm_q16<=best_sfo;sfo_anchor<=best_sfo;icount<=0;isum<=0;isumsq<=0;handoff_pending<=1;end done_score_q30<=best_score;done_lock<=best_score>=LOCK;done_error<=best_score<LOCK;if(done_ready)state<=IDLE;end default:state<=IDLE;endcase end
endmodule
