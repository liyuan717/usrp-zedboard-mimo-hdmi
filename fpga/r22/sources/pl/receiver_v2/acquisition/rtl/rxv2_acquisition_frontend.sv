`timescale 1ns/1ps
// Production acquisition boundary.  This is deliberately separate from the
// earlier rxv2_acq_engine prototype: ingress is decoupled by 512 scans, the
// P/P metric is the shared six-clock MAC, and timing consumes a copied warm-up
// snapshot rather than retaining a stale capture-ring address.
module rxv2_acquisition_frontend #(
  parameter integer RING_DEPTH=8192,
  parameter NCO_MEM_FILE="pl/receiver_v2/dsp/rtl/rxv2_nco_q15.mem"
) (
  input wire clk,input wire resetn,input wire soft_flush,
  input wire [127:0] s_data,input wire [31:0] s_epoch,input wire [63:0] s_sample,
  input wire s_discontinuity,input wire s_valid,output wire s_ready,
  input wire fb_valid,input wire [31:0] fb_epoch,input wire [63:0] fb_origin,
  input wire signed [31:0] fb_cfo_delta_hz_q16,
  output wire [127:0] m_symbol_data,output wire [9:0] m_symbol_index,
  output wire [31:0] m_epoch,output wire [63:0] m_origin,
  output wire m_frame_begin,output reg m_frame_abort,output wire m_lock_ok,
  output wire m_valid,input wire m_ready,
  output wire nco_cfg_commit,output wire [31:0] nco_rx_phase_inc_q0_32,
  output wire [31:0] nco_phase_q0_32,output wire [31:0] nco_apply_epoch,
  output wire [63:0] nco_apply_sample
);
  localparam integer AW=$clog2(RING_DEPTH);
  localparam integer FRAME_SCANS=3840;
  localparam integer TOTAL_DELAY=168;

  // The elastic FIFO prevents a bounded SFD service from propagating into the
  // DSP's 129-scan FIFO.  Capture writes still occur at full scan rate.
  wire [127:0] q_data; wire [31:0] q_epoch; wire [63:0] q_sample; wire q_disc,q_valid,q_ready,q_fire;
  rxv2_scan_elastic_fifo #(.DEPTH(512)) ingress_q(
    .clk,.resetn,.flush(soft_flush),.s_data(s_data),.s_epoch(s_epoch),.s_sample(s_sample),
    .s_disc(s_discontinuity),.s_valid(s_valid),.s_ready(s_ready),.m_data(q_data),
    .m_epoch(q_epoch),.m_sample(q_sample),.m_disc(q_disc),.m_valid(q_valid),.m_ready(q_ready),.overflow_sticky());

  wire signed [31:0] qi0=q_data[31:0], qq0=q_data[63:32], qi1=q_data[95:64], qq1=q_data[127:96];
  wire signed [15:0] ci0=qi0>>>3,cq0=qq0>>>3,ci1=qi1>>>3,cq1=qq1>>>3;
  wire [63:0] capture_word={cq1,ci1,cq0,ci0}; // low-to-high I0,Q0,I1,Q1
  reg [63:0] write_sample; reg [31:0] write_epoch; reg have_coord;
  wire capture_discontinuity=q_fire&&(q_disc||!have_coord||
    q_epoch!=write_epoch||q_sample!=write_sample+64'd1);
  // Latest coordinate accepted from DSP, before the intentional 512-scan
  // elasticity.  It is the only safe "now" for a future NCO commit.
  reg now_accept_valid; reg [31:0] now_accept_epoch; reg [63:0] now_accept_sample;

  // Capture is one write/one registered read XPM.  No asynchronous-array read
  // is used, so this is an 8192x64 two-frame BRAM ring on 7-series devices.
  wire ram_req; wire [AW-1:0] ram_addr; wire [63:0] ram_data;
  xpm_memory_sdpram #(
    .ADDR_WIDTH_A(AW),.ADDR_WIDTH_B(AW),.AUTO_SLEEP_TIME(0),.BYTE_WRITE_WIDTH_A(64),
    .CLOCKING_MODE("common_clock"),.ECC_MODE("no_ecc"),.MEMORY_INIT_FILE("none"),
    .MEMORY_INIT_PARAM("0"),.MEMORY_OPTIMIZATION("true"),.MEMORY_PRIMITIVE("block"),
    .MEMORY_SIZE(RING_DEPTH*64),.MESSAGE_CONTROL(0),.READ_DATA_WIDTH_B(64),
    .READ_LATENCY_B(1),.READ_RESET_VALUE_B("0"),.RST_MODE_A("SYNC"),.RST_MODE_B("SYNC"),
    .SIM_ASSERT_CHK(0),.USE_EMBEDDED_CONSTRAINT(0),.USE_MEM_INIT(0),.WAKEUP_TIME("disable_sleep"),
    .WRITE_DATA_WIDTH_A(64)
  ) capture_ram(
    .clka(clk),.ena(q_fire),.wea(q_fire),.addra(q_sample[AW-1:0]),.dina(capture_word),
    .injectsbiterra(1'b0),.injectdbiterra(1'b0),.clkb(clk),.enb(ram_req),.addrb(ram_addr),
    .doutb(ram_data),.regceb(1'b1),.rstb(~resetn),.sleep(1'b0),.sbiterrb(),.dbiterrb());

  // Shared P/P MAC has exact six-clock acceptance; pipe the accepted absolute
  // tag beside it so a correlation hit never loses sample provenance.
  wire mac_ready,mac_hit; wire signed [47:0] mac_re,mac_im; wire [47:0] mac_ea,mac_eb;
  reg [63:0] mac_tag[0:5]; reg [31:0] mac_ep[0:5]; reg [5:0] mac_tag_v;
  assign q_ready=mac_ready;
  assign q_fire=q_valid && q_ready;
  wire mac_fire=q_fire && !capture_discontinuity;
  rxv2_repetition_mac pp_mac(.clk,.resetn,.s_data(capture_word),.s_valid(mac_fire),.s_ready(mac_ready),
    .flush(soft_flush|q_disc|capture_discontinuity),.candidate_valid(mac_hit),.corr_re(mac_re),.corr_im(mac_im),.energy_a(mac_ea),.energy_b(mac_eb));

  // CORDIC and serial 41x32 SFD confirmation.  These reads are taken only
  // from already-written coordinates; they do not pause capture or ingress.
  function automatic signed [32:0] atan_turn(input integer n);
    begin case(n)
      0:atan_turn=536870912;1:atan_turn=316933406;2:atan_turn=167458907;3:atan_turn=85004756;
      4:atan_turn=42667331;5:atan_turn=21354465;6:atan_turn=10679838;7:atan_turn=5340245;
      8:atan_turn=2670163;9:atan_turn=1335087;10:atan_turn=667544;11:atan_turn=333772;
      12:atan_turn=166886;13:atan_turn=83443;14:atan_turn=41722;default:atan_turn=20861; endcase end
  endfunction
  function automatic signed [15:0] sfd_i(input [5:0] k); reg [1:0] v; begin
    case(k) 0:v=3;1:v=3;2:v=1;3:v=2;4:v=0;5:v=3;6:v=0;7:v=1;8:v=2;9:v=3;10:v=3;11:v=3;12:v=3;13:v=0;14:v=1;15:v=0;16:v=1;17:v=0;18:v=3;19:v=1;20:v=2;21:v=1;22:v=1;23:v=0;24:v=3;25:v=2;26:v=3;27:v=3;28:v=0;29:v=1;30:v=1;default:v=2; endcase
    sfd_i=(v==1||v==2)?-16'sd23170:16'sd23170; end endfunction
  function automatic signed [15:0] sfd_q(input [5:0] k); reg [1:0] v; begin
    case(k) 0:v=3;1:v=3;2:v=1;3:v=2;4:v=0;5:v=3;6:v=0;7:v=1;8:v=2;9:v=3;10:v=3;11:v=3;12:v=3;13:v=0;14:v=1;15:v=0;16:v=1;17:v=0;18:v=3;19:v=1;20:v=2;21:v=1;22:v=1;23:v=0;24:v=3;25:v=2;26:v=3;27:v=3;28:v=0;29:v=1;30:v=1;default:v=2; endcase
    sfd_q=(v==2||v==3)?-16'sd23170:16'sd23170; end endfunction
  reg signed [15:0] sine[0:1023]; initial $readmemh(NCO_MEM_FILE,sine);
  reg [31:0] sfd_phase,cfo_inc; reg [63:0] sfd_word; reg [5:0] sfd_k;
  wire signed [15:0] ssin=sine[sfd_phase[31:22]], scos=sine[(sfd_phase+32'h40000000)>>22];
  wire signed [15:0] si0=sfd_word[15:0],sq0=sfd_word[31:16],si1=sfd_word[47:32],sq1=sfd_word[63:48];
  // Widen before the post-product shift: a 16-bit assignment context would
  // truncate each Q12xQ15 sum before >>>15, collapsing valid rotations to
  // -1/0/1.  Capture's <=17406 component bound keeps final Q12 values S16.
  function automatic signed [15:0] rotate_rne(input signed [32:0] value);
    reg signed [32:0] magnitude;
    begin
      if(value>=0) rotate_rne=(value+33'sd16384)>>>15;
      else begin magnitude=-value; rotate_rne=-((magnitude+33'sd16384)>>>15); end
    end
  endfunction
  reg signed [31:0] ri0_pi,ri0_pq,rq0_pi,rq0_pq,ri1_pi,ri1_pq,rq1_pi,rq1_pq;
  wire signed [32:0] ri0_sum={{1{ri0_pi[31]}},ri0_pi}+{{1{ri0_pq[31]}},ri0_pq};
  wire signed [32:0] rq0_sum={{1{rq0_pi[31]}},rq0_pi}-{{1{rq0_pq[31]}},rq0_pq};
  wire signed [32:0] ri1_sum={{1{ri1_pi[31]}},ri1_pi}+{{1{ri1_pq[31]}},ri1_pq};
  wire signed [32:0] rq1_sum={{1{rq1_pi[31]}},rq1_pi}-{{1{rq1_pq[31]}},rq1_pq};
  reg signed [15:0] ri0,rq0,ri1,rq1;
  wire signed [31:0] sr0=(sfd_i(sfd_k)*ri0+sfd_q(sfd_k)*rq0)>>>15;
  wire signed [31:0] si0c=(sfd_i(sfd_k)*rq0-sfd_q(sfd_k)*ri0)>>>15;
  wire signed [31:0] sr1=(sfd_i(sfd_k)*ri1+sfd_q(sfd_k)*rq1)>>>15;
  wire signed [31:0] si1c=(sfd_i(sfd_k)*rq1-sfd_q(sfd_k)*ri1)>>>15;
  wire [31:0] smag0=ri0*ri0+rq0*rq0,smag1=ri1*ri1+rq1*rq1;
  reg signed [31:0] sr0_q,si0_q,sr1_q,si1_q;reg [31:0] smag0_q,smag1_q;
  // Each rotated Q12 correlation term is bounded by 17 bits and 32 terms
  // need 22 bits.  Keep explicit 32-bit accumulators (rather than the old
  // prototype's accidental 64-bit squares) so SFD normalization is four
  // bounded 32x32 products, not a 128-bit DSP explosion.
  reg signed [31:0] acc_r0,acc_i0,acc_r1,acc_i1; reg [39:0] acc_energy;
  reg [65:0] score_num;reg [1:0] score_index;
  reg signed [31:0] square_operand;
  always @* case(score_index)0:square_operand=acc_r0;1:square_operand=acc_i0;2:square_operand=acc_r1;default:square_operand=acc_i1;endcase
  wire [63:0] score_square=$signed(square_operand)*$signed(square_operand);
  wire [44:0] score_den={acc_energy,5'd0};
  reg [65:0] best_num; reg [44:0] best_den; reg [63:0] best_p;
  reg [63:0] hit_sample, p_center; reg [31:0] hit_epoch;
  reg signed [49:0] cx,cy; reg signed [32:0] cz; reg [4:0] cordic_n;
  reg [5:0] p_try; reg sfd_rd_en; reg [63:0] sfd_rd_sample;
  localparam A_SEARCH=0,A_CORDIC=1,A_WAIT=2,A_INIT=3,A_READ=4,A_WAITRD=5,A_ACC=6,A_REPORT=7,
             A_ROT_PROD=8,A_ROTATE=9,A_CORRELATE=10,A_SCORE=11;
  reg [3:0] acq_state; reg sfd_rsp;

  // Warm-up snapshot: two contiguous 256-scan regions, not 112 pre-picked
  // symbols.  Fractional timing candidates therefore use genuine neighbours.
  reg [63:0] snapshot[0:511]; reg [8:0] copy_addr; reg copy_active,copy_wait,copy_rd_en,copy_rsp;
  reg [63:0] warm_origin; reg [31:0] warm_epoch,cold_epoch; reg snapshot_pending,cold_commit_pending;
  reg timing_start,timing_active,timing_locked;
  reg timing_pretrack,pretrack_started;
  wire timing_start_ready;
  reg [31:0] timing_epoch; reg [63:0] timing_origin; reg signed [31:0] timing_tau,timing_sfo;
  wire sn_rd_en; wire [8:0] sn_rd_addr; reg sn_rd_valid; reg [63:0] sn_rd_data;
  always @(posedge clk) if(sn_rd_en) begin sn_rd_data<=snapshot[sn_rd_addr];sn_rd_valid<=1;end else sn_rd_valid<=0;

  // The timing owner chooses actual tau/SFO; its only external sample source
  // is the retained warm-up snapshot above.
  wire tr_req_valid,tr_req_ready,tr_m_ready;
  wire [63:0] tr_floor; wire [15:0] tr_fraction;
  wire timing_done_valid; reg timing_done_ready; wire [31:0] done_epoch; wire [63:0] done_origin;
  wire signed [31:0] done_tau,done_sfo; wire [30:0] done_score; wire done_lock,done_error;
  reg replay_req,fr_busy,fr_owner_replay; reg [63:0] replay_floor; reg [15:0] replay_frac;
  wire fr_req_ready,fr_rd_en,fr_m_valid; wire [63:0] fr_rd_sample; reg fr_rd_valid; reg [63:0] fr_rd_data; wire [127:0] fr_m_data;
  wire reader_resetn=resetn&&!soft_flush&&!m_frame_abort;
  rxv2_timing_sfo timing(.clk,.resetn(reader_resetn),.start_valid(timing_start),.start_ready(timing_start_ready),
    .start_epoch(timing_epoch),.start_origin(timing_origin),.start_locked(timing_locked),.start_tau_q16(timing_tau),.start_sfo_ppm_q16(timing_sfo),
    .sn_rd_en(sn_rd_en),.sn_rd_addr(sn_rd_addr),.sn_rd_valid(sn_rd_valid),.sn_rd_data(sn_rd_data),
    .frac_req_valid(tr_req_valid),.frac_req_ready(tr_req_ready),.frac_req_floor(tr_floor),.frac_req_fraction(tr_fraction),
    .frac_m_valid(fr_busy && !fr_owner_replay && fr_m_valid),.frac_m_ready(tr_m_ready),.frac_m_data(fr_m_data),.done_valid(timing_done_valid),.done_ready(timing_done_ready),
    .done_epoch(done_epoch),.done_origin(done_origin),.done_tau_q16(done_tau),.done_sfo_ppm_q16(done_sfo),.done_score_q30(done_score),.done_lock(done_lock),.done_error(done_error));

  // One fractional-reader instance is transaction-shared. Replay has priority;
  // successor tracking may use the gaps without changing an active owner.
  // It performs exactly one outstanding capture
  // request and preserves Q12->Q15 scaling/endian lane order.
  wire fr_req_valid=!fr_busy && (replay_req?1'b1:tr_req_valid);
  wire [63:0] fr_floor=replay_req?replay_floor:tr_floor; wire [15:0] fr_fraction=replay_req?replay_frac:tr_fraction;
  wire fr_m_ready=fr_busy ? (fr_owner_replay ? m_ready : tr_m_ready) : 1'b0;
  assign tr_req_ready=!fr_busy && !replay_req && fr_req_ready;
  rfv2_fractional_reader frac(.clk,.resetn(reader_resetn),.req_valid(fr_req_valid),.req_ready(fr_req_ready),.req_floor(fr_floor),.req_fraction(fr_fraction),
    .rd_en(fr_rd_en),.rd_sample(fr_rd_sample),.rd_valid(fr_rd_valid),.rd_data(fr_rd_data),.m_valid(fr_m_valid),.m_ready(fr_m_ready),.m_data(fr_m_data));

  // RAM read arbitration.  A request is accepted only for a coordinate still
  // inside the live ring.  Violation invalidates the whole current frame.
  reg [63:0] copy_sample;
  wire fr_safe=have_coord && (fr_rd_sample+RING_DEPTH>write_sample) && (fr_rd_sample<=write_sample);
  wire cp_safe=have_coord && (copy_sample+RING_DEPTH>write_sample) && (copy_sample<=write_sample);
  wire sfd_safe=have_coord && (sfd_rd_sample+RING_DEPTH>write_sample) && (sfd_rd_sample<=write_sample);
  reg [1:0] ram_tag,ram_tag_d; reg ram_pending;
  assign ram_req=sfd_rd_en?sfd_safe:(copy_rd_en?cp_safe:(fr_rd_en?fr_safe:1'b0));
  wire [63:0] ram_sample=sfd_rd_en?sfd_rd_sample:(copy_rd_en?copy_sample:fr_rd_sample);
  assign ram_addr=ram_sample[AW-1:0];
  always @(posedge clk) begin
    if(!resetn||soft_flush) begin ram_pending<=0;fr_rd_valid<=0;sfd_rsp<=0;copy_rsp<=0;end else begin
      fr_rd_valid<=0;sfd_rsp<=0;copy_rsp<=0;
      if(ram_pending) case(ram_tag_d)
        2'd0: begin sfd_word<=ram_data;sfd_rsp<=1;end
        2'd1: begin snapshot[copy_addr]<=ram_data;copy_rsp<=1;end
        default: begin fr_rd_data<=ram_data;fr_rd_valid<=1;end
      endcase
      ram_pending<=ram_req;
      if(ram_req) begin ram_tag_d<=ram_tag; end
    end
  end
  always @(*) begin
    if(sfd_rd_en) ram_tag=0; else if(copy_rd_en) ram_tag=1; else ram_tag=2;
  end

  // CFO commit is coordinate targeted.  The signed feedback coefficient and
  // stale-epoch rejection are owned by this controller, never wall-clocked.
  reg acq_desc_valid; reg [31:0] acq_desc_epoch,acq_desc_inc; reg [63:0] acq_desc_origin;
  wire nco_lock;
  rxv2_nco_commit_controller nco(.clk,.resetn,.soft_flush,.acq_valid(acq_desc_valid),.acq_epoch(acq_desc_epoch),.acq_origin(acq_desc_origin),.acq_rx_inc(acq_desc_inc),.now_valid(now_accept_valid),.now_epoch(now_accept_epoch),.now_sample(now_accept_sample),
    .fb_valid,.fb_epoch,.fb_origin,.fb_delta_hz_q16(fb_cfo_delta_hz_q16),.cfg_commit(nco_cfg_commit),.cfg_rx_phase_inc_q0_32(nco_rx_phase_inc_q0_32),
    .cfg_phase_q0_32(nco_phase_q0_32),.cfg_apply_epoch(nco_apply_epoch),.cfg_apply_sample(nco_apply_sample),.lock_ok(nco_lock),.stale_feedback_sticky());

  // Replay one frame at a time.  A 950-symbol downstream stall is monitored
  // against live capture coordinates; it aborts instead of emitting a mixed
  // epoch/overwritten frame.  Under normal channel readiness it consumes
  // ~6650 reader clocks (and remains bounded even with AXIS backpressure).
  reg locked; reg [31:0] lock_epoch; reg [63:0] replay_origin; reg signed [31:0] replay_tau,replay_sfo;
  reg [9:0] replay_index; reg replay_active,replay_wait_data;
  // Do not calculate the next frame using a 64-bit /3840 in the completion
  // cycle.  A cold grid can finish many frames after its snapshot and that
  // division was a 68-level, -22.9ns OOC path.  This small scheduler advances
  // one 3840-scan frame each clock until its candidate is strictly in front of
  // the live writer.  At 4 MSPS the writer advances once per 12.5 clk50 clocks,
  // so it catches up rapidly without holding capture or timing out a frame.
  reg future_calc_active;
  // A locked estimate is calculated from the current frame's two training
  // windows while that frame is still arriving.  Keep the current replay
  // coordinates immutable and stage the resulting following frame here; this
  // prevents the old frame-949 launch from missing one complete 3840-scan
  // frame while the serial timing grid runs.
  reg future_calc_to_next,track_prepared,next_replay_valid;
  reg [63:0] next_replay_origin;
  reg signed [31:0] next_replay_tau,next_replay_sfo;
  reg [63:0] future_calc_origin;
  reg signed [63:0] future_calc_tau,future_calc_step;
  // Exact-near reciprocal 1/1e6 as a shift/add Q20 coefficient 1099512.
  // This removes a variable-width /1,000,000 divider from the per-symbol
  // coordinate path; residual coefficient error is <4e-10.
  function automatic signed [63:0] ppm_term(input signed [95:0] x);
    begin ppm_term=(x>>>20)+(x>>>25)+(x>>>26)+(x>>>30)+(x>>>31)+
                   (x>>>33)+(x>>>34)+(x>>>35)+(x>>>36)+(x>>>37); end
  endfunction
  // One frame's SFO advance: 3840 = 4096 - 256.  Keeping it as shifts avoids
  // the 32x65 multiplier that formerly accompanied the frame divide.  All
  // timing estimates are bounded well within the signed Q16 replay_tau range;
  // the 64-bit scheduler accumulator protects the intermediate sum.
  function automatic signed [63:0] sfo_frame_step(input signed [31:0] sfo);
    reg signed [47:0] frame_product;
    begin
      frame_product=($signed(sfo)<<<12)-($signed(sfo)<<<8);
      sfo_frame_step=ppm_term(frame_product);
    end
  endfunction
  // One-entry coordinate lookahead overlaps the fractional reader/current
  // symbol. No index->multiplier->ten-adder->80-bit add->overwrite-reset cone.
  // Delta is exactly -838..2958: signed13 suffices, not the former signed64.
  reg [2:0] coord_stage;
  reg [9:0] coord_index;
  reg coord_valid;
  reg signed [44:0] replay_product_q;
  reg signed [31:0] ppm_lo_q,ppm_hi_q,ppm_result_q;
  reg signed [79:0] coord_base_q,coord_tau_q,replay_coord_q16;
  wire signed [12:0] replay_delta=$signed({1'b0,coord_index,2'b00})-13'sd838;
  wire replay_fire=m_valid&&m_ready;
  assign m_symbol_data=fr_m_data; assign m_symbol_index=replay_index; assign m_epoch=lock_epoch; assign m_origin=replay_origin;
  assign m_valid=replay_active&&fr_busy&&fr_owner_replay&&fr_m_valid&&!capture_discontinuity;
  assign m_frame_begin=m_valid&&(replay_index==0); assign m_lock_ok=locked;

  integer t;
  always @(posedge clk) begin
    if(!resetn||soft_flush) begin
      write_sample<=0;write_epoch<=0;have_coord<=0;now_accept_valid<=0;now_accept_epoch<=0;now_accept_sample<=0;mac_tag_v<=0;for(t=0;t<6;t=t+1)begin mac_tag[t]<=0;mac_ep[t]<=0;end
      acq_state<=A_SEARCH;cordic_n<=0;sfd_rd_en<=0;acq_desc_valid<=0;acq_desc_epoch<=0;acq_desc_origin<=0;acq_desc_inc<=0;
      warm_origin<=0;warm_epoch<=0;cold_epoch<=0;snapshot_pending<=0;cold_commit_pending<=0;copy_active<=0;copy_wait<=0;copy_rd_en<=0;copy_addr<=0;copy_sample<=0;timing_start<=0;timing_active<=0;timing_locked<=0;timing_epoch<=0;timing_origin<=0;timing_tau<=0;timing_sfo<=0;timing_done_ready<=0;timing_pretrack<=0;pretrack_started<=0;
      locked<=0;lock_epoch<=0;replay_origin<=0;replay_tau<=0;replay_sfo<=0;replay_active<=0;replay_index<=0;replay_req<=0;fr_busy<=0;fr_owner_replay<=0;replay_wait_data<=0;future_calc_active<=0;future_calc_to_next<=0;track_prepared<=0;next_replay_valid<=0;next_replay_origin<=0;next_replay_tau<=0;next_replay_sfo<=0;future_calc_origin<=0;future_calc_tau<=0;future_calc_step<=0;m_frame_abort<=0;
      coord_stage<=0;coord_index<=0;coord_valid<=0;replay_product_q<=0;ppm_lo_q<=0;ppm_hi_q<=0;ppm_result_q<=0;coord_base_q<=0;coord_tau_q<=0;replay_coord_q16<=0;
    end else begin
      sfd_rd_en<=0;copy_rd_en<=0;acq_desc_valid<=0;timing_done_ready<=0;m_frame_abort<=0;
      if(s_valid&&s_ready) begin now_accept_valid<=1;now_accept_epoch<=s_epoch;now_accept_sample<=s_sample;end
      if(q_fire) begin
        if(capture_discontinuity) begin
          have_coord<=1;write_sample<=q_sample;write_epoch<=q_epoch;mac_tag_v<=0;acq_state<=A_SEARCH;locked<=0;future_calc_active<=0;future_calc_to_next<=0;track_prepared<=0;next_replay_valid<=0;replay_active<=0;replay_req<=0;fr_busy<=0;timing_pretrack<=0;pretrack_started<=0;
          if(replay_active) m_frame_abort<=1;
        end else write_sample<=q_sample;
      end
      // MAC latency is six *clocks*, not six accepted scans.  Shift the
      // provenance pipe every clock so a hit is paired with its true sample.
      begin
        mac_tag_v[0]<=mac_fire;
        if(mac_fire) begin mac_tag[0]<=q_sample;mac_ep[0]<=q_epoch;end
        for(t=1;t<6;t=t+1) begin mac_tag[t]<=mac_tag[t-1];mac_ep[t]<=mac_ep[t-1];mac_tag_v[t]<=mac_tag_v[t-1];end
      end
      // Cold P/P -> CORDIC -> 41 candidate SFD.  No offset is supplied by SW.
      case(acq_state)
        // One cold descriptor owns the NCO/snapshot/timing transaction.  Do
        // not let subsequent repeated-P hits overwrite its target while the
        // 153-point grid is still running; reacquisition is enabled again if
        // that grid returns an explicit non-lock result.
        A_SEARCH: if(mac_hit&&mac_tag_v[5]&&!cold_commit_pending&&!snapshot_pending&&!copy_active&&!timing_start&&!timing_active&&!future_calc_active&&!locked) begin
          // The recurrence tag names the final P/P interior sample before
          // the four-scan symbol-centre alignment used by the SFD reader.
          // Without this correction the 41-candidate range ends one symbol
          // before the true P start (and therefore reads SFD at k-1).
          hit_sample<=mac_tag[5]+44;hit_epoch<=mac_ep[5];
          if(mac_re[47])begin cx<=-mac_re;cy<=-mac_im;cz<=mac_im[47]?-33'sd2147483648:33'sd2147483648;end else begin cx<=mac_re;cy<=mac_im;cz<=0;end
          cordic_n<=0;acq_state<=A_CORDIC; end
        A_CORDIC: begin
          if(cy>=0)begin cx<=cx+(cy>>>cordic_n);cy<=cy-(cx>>>cordic_n);cz<=cz+atan_turn(cordic_n);end
          else begin cx<=cx-(cy>>>cordic_n);cy<=cy+(cx>>>cordic_n);cz<=cz-atan_turn(cordic_n);end
          if(cordic_n==15)begin cfo_inc<=(cz+(cy>=0?atan_turn(15):-atan_turn(15)))>>>7;acq_state<=A_WAIT;end else cordic_n<=cordic_n+1; end
        A_WAIT: if(write_sample>=hit_sample+165) acq_state<=A_INIT;
        A_INIT: begin p_try<=0;best_num<=0;best_den<=1;acq_state<=A_READ;end
        A_READ: begin p_center<=hit_sample-255+p_try;sfd_k<=0;acc_r0<=0;acc_i0<=0;acc_r1<=0;acc_i1<=0;acc_energy<=0;sfd_phase<=cfo_inc*256;sfd_rd_sample<=hit_sample-255+p_try+256;sfd_rd_en<=1;acq_state<=A_WAITRD;end
        A_WAITRD: if(sfd_rsp) acq_state<=A_ROT_PROD;
        A_ROT_PROD:begin
          ri0_pi<=si0*scos;ri0_pq<=sq0*ssin;rq0_pi<=sq0*scos;rq0_pq<=si0*ssin;
          ri1_pi<=si1*scos;ri1_pq<=sq1*ssin;rq1_pi<=sq1*scos;rq1_pq<=si1*ssin;acq_state<=A_ROTATE;
        end
        A_ROTATE:begin ri0<=rotate_rne(ri0_sum);rq0<=rotate_rne(rq0_sum);ri1<=rotate_rne(ri1_sum);rq1<=rotate_rne(rq1_sum);acq_state<=A_CORRELATE;end
        A_CORRELATE:begin sr0_q<=sr0;si0_q<=si0c;sr1_q<=sr1;si1_q<=si1c;smag0_q<=smag0;smag1_q<=smag1;acq_state<=A_ACC;end
        A_ACC: begin
          acc_r0<=acc_r0+sr0_q;acc_i0<=acc_i0+si0_q;acc_r1<=acc_r1+sr1_q;acc_i1<=acc_i1+si1_q;acc_energy<=acc_energy+smag0_q+smag1_q;
          if(sfd_k==31)begin score_num<=0;score_index<=0;acq_state<=A_SCORE;end else begin sfd_k<=sfd_k+1;sfd_phase<=sfd_phase+(cfo_inc<<<2);sfd_rd_sample<=p_center+256+(sfd_k+1)*4;sfd_rd_en<=1;acq_state<=A_WAITRD;end
        end
        A_SCORE:begin
          score_num<=score_num+score_square;
          if(score_index==3)acq_state<=A_REPORT;else score_index<=score_index+1'b1;
        end
        A_REPORT: begin
          if(score_num*10>=score_den*6 && score_num>best_num) begin best_num<=score_num;best_den<=score_den;best_p<=p_center;end
          if(p_try==40) begin
            if((score_num>best_num?score_num:best_num)!=0 && ((score_num>best_num?score_num:best_num)*10 >= (score_num>best_num?score_den:best_den)*6)) begin
              acq_desc_valid<=1;acq_desc_epoch<=hit_epoch;acq_desc_origin<=(score_num>best_num?p_center:best_p)-TOTAL_DELAY;acq_desc_inc<=cfo_inc;
              // Do not assume O+3840: wait for the controller's actual
              // coordinate-targeted cold commit receipt before snapshotting.
              cold_epoch<=hit_epoch;cold_commit_pending<=1;
            end
            acq_state<=A_SEARCH;
          end else begin p_try<=p_try+1;acq_state<=A_READ;end
        end
        default:acq_state<=A_SEARCH;
      endcase
      if(cold_commit_pending && nco_cfg_commit && nco_apply_epoch==cold_epoch) begin
        warm_epoch<=nco_apply_epoch;warm_origin<=nco_apply_sample;
        snapshot_pending<=1;cold_commit_pending<=0;
      end
      // Copy both full-rate training windows only after their last coordinate exists.
      if(snapshot_pending&&!copy_active&&write_epoch==warm_epoch&&write_sample>=warm_origin+40+4*216+255) begin copy_active<=1;copy_addr<=0;copy_wait<=0;snapshot_pending<=0;end
      if(copy_active) begin
        if(!copy_wait) begin copy_sample<=copy_addr<256 ? warm_origin+40+4*140+copy_addr : warm_origin+40+4*216+(copy_addr-256);copy_rd_en<=1;copy_wait<=1;end
        else if(copy_rsp) begin copy_wait<=0;if(copy_addr==511)begin
          copy_active<=0; timing_epoch<=warm_epoch;timing_origin<=warm_origin;timing_locked<=0;timing_tau<=0;timing_sfo<=0;timing_start<=1;timing_pretrack<=0;
        end else copy_addr<=copy_addr+1;end
      end
      if(timing_start&&timing_start_ready) begin timing_start<=0;timing_active<=1;end
      // A successor result must not overwrite the next_replay record that
      // still belongs to the current replay's final promotion. The timing
      // engine holds DONE stable until ready, so consume it only afterwards.
      if(timing_done_valid&&timing_active&&!(timing_pretrack&&replay_active)) begin
        timing_done_ready<=1;timing_active<=0;timing_pretrack<=0;
        if(done_lock&&done_epoch==timing_epoch&&done_origin==timing_origin) begin
          locked<=timing_locked?locked:1'b0;lock_epoch<=done_epoch;
          // Cold grid may take longer than one ring revolution.  Its snapshot
          // is valid, but its warm-up payload is intentionally dropped.  The
          // multi-cycle scheduler below selects a future complete frame and
          // advances tau by one SFO frame step for every origin increment.
          future_calc_active<=1;future_calc_to_next<=timing_locked;future_calc_origin<=done_origin;
          future_calc_tau<=done_tau;future_calc_step<=sfo_frame_step(done_sfo);
          if(timing_locked) begin
            // The early measurement describes THIS retained frame. Native
            // reception samples its header/data using the fitted current
            // offset, not the old prediction. Stage its successor separately.
            // Pretracked DONE is consumed after promotion, when this is now
            // the current retained frame and no replay has started for it.
            replay_tau<=done_tau;replay_sfo<=done_sfo;
            next_replay_sfo<=done_sfo;
            track_prepared<=1;pretrack_started<=0;
          end
          else begin
            replay_sfo<=done_sfo;
            // A cold result owns a new lock generation. Never reuse a
            // successor prepared for the overwritten/rejected old frame.
            track_prepared<=0;next_replay_valid<=0;pretrack_started<=0;
          end
        end
        else begin locked<=0;track_prepared<=0;next_replay_valid<=0;pretrack_started<=0;end
      end
      if(future_calc_active) begin
        if(future_calc_origin>write_sample) begin
          future_calc_active<=0;
          if(future_calc_to_next) begin
            next_replay_valid<=1;
            next_replay_origin<=future_calc_origin;
            next_replay_tau<=future_calc_tau[31:0];
          end else begin
            locked<=1;replay_origin<=future_calc_origin;
            replay_tau<=future_calc_tau[31:0];
          end
        end else begin
          future_calc_origin<=future_calc_origin+FRAME_SCANS;
          future_calc_tau<=future_calc_tau+future_calc_step;
        end
      end
      // Start locked tracking as soon as both training windows are retained
      // (second 256-scan window ends at O+40+4*216+255).  It owns the shared
      // reader before replay begins, so its ~2971 clocks elapse while live
      // capture still has >2k scans before the current frame is complete.
      if(locked&&!track_prepared&&!future_calc_active&&!timing_active&&!timing_start&&!replay_active&&!replay_req&&!capture_discontinuity&&write_epoch==lock_epoch&&write_sample>=replay_origin+64'd1159) begin
        timing_epoch<=lock_epoch;timing_origin<=replay_origin;timing_locked<=1;timing_tau<=replay_tau;timing_sfo<=replay_sfo;timing_start<=1;track_prepared<=1;timing_pretrack<=0;
      end
      // Track R+1 while R is replaying, as soon as R+1's two training
      // windows exist. Keep R and its saved successor immutable. The shared
      // reader arbitrates each request with replay priority; this is not a
      // second outstanding read or a second timing engine.
      if(locked&&replay_active&&track_prepared&&next_replay_valid&&!pretrack_started&&
         !future_calc_active&&!timing_active&&!timing_start&&!capture_discontinuity&&
         write_epoch==lock_epoch&&write_sample>=next_replay_origin+64'd1159) begin
        timing_epoch<=lock_epoch;timing_origin<=next_replay_origin;timing_locked<=1;
        timing_tau<=next_replay_tau;timing_sfo<=next_replay_sfo;
        timing_start<=1;timing_pretrack<=1;pretrack_started<=1;
      end
      // The first frame may not start until the early locked measurement has
      // staged its successor.  This makes each completed replay promote an
      // already-known adjacent origin instead of waiting until symbol 949.
      if(locked&&track_prepared&&next_replay_valid&&!future_calc_active&&!timing_active&&!timing_start&&!replay_active&&!replay_req&&write_epoch==lock_epoch&&write_sample>=replay_origin+FRAME_SCANS-1) begin replay_active<=1;replay_index<=0;replay_wait_data<=0;end
      if(replay_active&&!replay_req&&!replay_wait_data&&coord_valid) begin
        replay_floor<=replay_coord_q16>>>16;replay_frac<=replay_coord_q16[15:0];replay_req<=1;coord_valid<=0;
      end
      if(!replay_active) begin coord_stage<=0;coord_index<=0;coord_valid<=0;end
      else case(coord_stage)
        0:if(!coord_valid&&coord_index<950)begin
          replay_product_q<=$signed(replay_sfo)*$signed(replay_delta);
          coord_base_q<=(($signed({1'b0,replay_origin})+64'sd40+($signed({1'b0,coord_index})<<<2))<<<16);
          coord_stage<=1;
        end
        1:begin
          // Preserve the original separately-truncated shift/add reciprocal
          // exactly, including negative operands. Each partial fits signed32.
          ppm_lo_q<=(replay_product_q>>>20)+(replay_product_q>>>25)+(replay_product_q>>>26)+(replay_product_q>>>30)+(replay_product_q>>>31);
          ppm_hi_q<=(replay_product_q>>>33)+(replay_product_q>>>34)+(replay_product_q>>>35)+(replay_product_q>>>36)+(replay_product_q>>>37);
          coord_stage<=2;
        end
        2:begin ppm_result_q<=ppm_lo_q+ppm_hi_q;coord_tau_q<=coord_base_q+$signed(replay_tau);coord_stage<=3;end
        3:begin replay_coord_q16<=coord_tau_q+$signed(ppm_result_q);coord_stage<=4;end
        4:begin coord_valid<=1;coord_index<=coord_index+1'b1;coord_stage<=0;end
        default:coord_stage<=0;
      endcase
      if(fr_req_valid&&fr_req_ready) begin
        fr_busy<=1;fr_owner_replay<=replay_req;
        if(replay_req) begin replay_req<=0;replay_wait_data<=1;end
      end
      if(fr_busy&&fr_m_valid&&fr_m_ready) fr_busy<=0;
      if(replay_fire) begin replay_wait_data<=0;
        if(replay_index==949) begin
          // Promote the estimate prepared from this frame's training windows.
          // No post-frame timing run is launched here: doing so was proven to
          // advance origins by 7680 scans under the real physical fixture.
          replay_active<=0;replay_origin<=next_replay_origin;replay_tau<=next_replay_tau;replay_sfo<=next_replay_sfo;
          next_replay_valid<=0;track_prepared<=0;pretrack_started<=0;
        end else replay_index<=replay_index+1;
      end
      // Overwrite validity follows the CURRENT issued sample, not a future
      // coordinate being prefetched. The capture arbiter checks each read too.
      if(replay_active && (replay_req||replay_wait_data) && (!have_coord || (replay_floor+RING_DEPTH<=write_sample))) begin
        replay_active<=0;replay_req<=0;replay_wait_data<=0;locked<=0;
        track_prepared<=0;next_replay_valid<=0;future_calc_active<=0;
        // A rejected channel can hold the outstanding fractional response
        // with ready=0 until the ring overwrites the frame. Abandon BOTH the
        // owner state and helper transaction; otherwise reacquisition queues
        // behind a response that no downstream frame will ever consume.
        fr_busy<=0;fr_owner_replay<=0;
        timing_active<=0;timing_start<=0;timing_done_ready<=0;timing_pretrack<=0;pretrack_started<=0;
        copy_active<=0;copy_wait<=0;m_frame_abort<=1;
        coord_stage<=0;coord_index<=0;coord_valid<=0;
      end
      // Highest-priority cancellation includes a coincident timing DONE or
      // replay promotion. Reset the helper AND its frontend owner; clearing
      // only pretrack leaves timing_active waiting for a DONE erased by reset.
      if(capture_discontinuity) begin
        mac_tag_v<=0;
        acq_state<=A_SEARCH;sfd_rd_en<=0;acq_desc_valid<=0;
        cold_commit_pending<=0;snapshot_pending<=0;copy_active<=0;copy_wait<=0;copy_rd_en<=0;
        timing_active<=0;timing_start<=0;timing_done_ready<=0;
        timing_pretrack<=0;pretrack_started<=0;
        locked<=0;future_calc_active<=0;future_calc_to_next<=0;
        track_prepared<=0;next_replay_valid<=0;
        replay_active<=0;replay_req<=0;replay_wait_data<=0;
        fr_busy<=0;fr_owner_replay<=0;
        coord_stage<=0;coord_index<=0;coord_valid<=0;
        if(replay_active||timing_active||timing_start||fr_busy||copy_active||snapshot_pending)
          m_frame_abort<=1;
      end
    end
  end
endmodule
