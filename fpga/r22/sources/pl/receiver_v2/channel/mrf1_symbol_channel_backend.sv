`timescale 1ns/1ps
// MRF1 symbol-domain receive backend, owned by receiver_v2/channel.
//
// Input is *only* the recovered 1 Msym/s stream from acquisition.  This block
// intentionally has no ADC, RRC, timing-search or CFO-NCO logic.  It performs
// training H estimation, header decode/CRC, pilot phase/H update and MIP0
// task formation.  All arithmetic is fixed point; there is no real/float or
// combinational general divider in the synthesizable path.
module mrf1_symbol_channel_backend (
    input  logic                         clk50,
    input  logic                         rst_n,
    // Wire ABI is low-to-high I0,Q0,I1,Q1.  In packed-vector notation this is
    // {Q1,I1,Q0,I0}; keep the unpacking below paired with the acquisition DSP.
    input  logic [127:0]                 s_symbol_data, // {rx1_q,rx1_i,rx0_q,rx0_i}, signed Q15 in s32
    input  logic [9:0]                   s_symbol_index,
    input  logic [31:0]                  s_epoch,
    input  logic [63:0]                  s_frame_origin,
    input  logic                         s_frame_begin,
    input  logic                         s_frame_abort,
    input  logic                         s_lock_ok,
    input  logic                         s_symbol_valid,
    output logic                         s_symbol_ready,

    // The context is atomically bound before any MIP task can be emitted.
    // headers are byte-little-endian: headers[7:0] is stream0 byte0.
    output logic [319:0]                 m_ctx_headers,
    output logic [31:0]                  m_ctx_frame_id,
    output logic [31:0]                  m_ctx_epoch,
    output logic [63:0]                  m_ctx_origin,
    output logic signed [255:0]          m_ctx_h_q15, // row-major {im,re} packed by 32-bit lanes
    output logic                         m_ctx_valid,
    input  logic                         m_ctx_ready,

    output logic [63:0]                  m_mip_tdata,
    output logic [7:0]                   m_mip_tkeep,
    output logic                         m_mip_tvalid,
    input  logic                         m_mip_tready,
    output logic                         m_mip_tlast,

    output logic                         m_frame_abort,
    output logic [7:0]                   m_error_code,
    output logic                         m_frame_complete,
    // Feedback is applied by acquisition only at its next frame boundary.
    output logic                         m_cfo_fb_valid,
    output logic [31:0]                  m_cfo_fb_epoch,
    output logic [63:0]                  m_cfo_fb_origin,
    output logic signed [31:0]           m_cfo_fb_delta_hz_q16
);
    localparam logic signed [31:0] QPSK_A_Q15 = 32'sd23170; // round(2^15/sqrt(2))
    // round(2^32 * 2^15 / (56 * 2 * A^2)); A=round(2^15/sqrt(2)).
    localparam logic signed [31:0] H_RECIP_56A_Q32 = 32'sd2341;
    localparam logic [31:0] CONFIG_ID = 32'd2;
    localparam logic [31:0] PROPOSAL_Q16 = 32'd32768;
    localparam logic [7:0] ERR_NONE=8'd0, ERR_INPUT=8'd1, ERR_TRAIN=8'd2,
                           ERR_CONDITION=8'd3, ERR_INVERSE=8'd4, ERR_HEADER=8'd5,
                           ERR_PILOT=8'd6, ERR_OVERFLOW=8'd7;

    typedef enum logic [1:0] {TASK_IDLE, TASK_MAC, TASK_OUTPUT} task_state_t;
    task_state_t task_state;

    logic frame_active, frame_bad, header_hold, pilot_hold, ctx_accepted;
    logic [9:0] expected_index;
    logic [31:0] epoch_q, rf_frame_q;
    logic [63:0] origin_q;
    logic [7:0] header0 [0:19];
    logic [7:0] header1 [0:19];
    logic [31:0] crc0, crc1, header_crc0, header_crc1;
    logic header_eval_pending, ctx_valid_q;

    // H is Q15.  corr is Q30 summed over 56 known symbols.
    logic signed [63:0] train_corr_re [0:3], train_corr_im [0:3];
    logic signed [63:0] train_energy;
    logic train_finish_pending, h_est_pending, inv_prepare_pending;
    // H = corr * fixed reciprocal is time-shared after training.  Eight
    // 64-cycle products fit in the pre-header control interval (with normal
    // ready/valid backpressure if a source presents symbols without spacing),
    // replacing the former eight inferred 64x32 cones.
    logic h_est_serial_active,h_est_serial_start,h_est_serial_busy,h_est_serial_done;
    logic [2:0] h_est_serial_word;
    logic signed [63:0] h_est_serial_operand;
    logic signed [127:0] h_est_serial_product;
    logic signed [31:0] h_re [0:3], h_im [0:3];
    logic signed [31:0] h_train_re [0:3], h_train_im [0:3], h_prev_re [0:3], h_prev_im [0:3];
    logic have_previous_h, frame_started_locked;
    logic signed [63:0] det_re, det_im;
    logic [63:0] det_mag;
    logic inverse_valid, inv_busy_re, inv_busy_im, inv_start, inv_div_zero_re, inv_div_zero_im, inv_overflow_re, inv_overflow_im, inv_done_re, inv_done_im;
    logic [1:0] inv_word;
    logic signed [79:0] inv_numerator_re, inv_numerator_im;
    logic signed [31:0] inv_quotient_re, inv_quotient_im;
    logic signed [31:0] inv00_re,inv00_im,inv01_re,inv01_im,inv10_re,inv10_im,inv11_re,inv11_im;
    // The adjugate numerators used to be four 32x64 expression trees.  Two
    // serial lanes retain re/im parallelism: two products per component per
    // inverse word, followed by the existing two restoring dividers.
    logic inv_num_active,inv_num_start,inv_num_re_busy,inv_num_im_busy,inv_num_re_done,inv_num_im_done,inv_num_phase;
    logic signed [63:0] inv_num_re_a,inv_num_re_b,inv_num_im_a,inv_num_im_b;
    logic signed [127:0] inv_num_re_product,inv_num_im_product,inv_num_re_acc,inv_num_im_acc;
    // Condition is a serial 68-bit compare, not a pair of 128-bit inferred
    // multipliers.  Q15 H bounds norm(H)^2 and |det|^2 to this width.
    logic condition_check_start, condition_check_pending, condition_check_launch_pending;
    logic signed [67:0] condition_norm_operand, condition_det_operand, condition_det_b;
    logic condition_norm_busy, condition_det_busy, condition_norm_done, condition_det_done;
    logic signed [135:0] condition_norm_square, condition_det_scaled;
    logic condition_result_valid, condition_result_pass;
    logic condition_is_pilot;
    logic condition_square_mode,matrix_active;
    logic [4:0] matrix_issue;
    logic signed [67:0] matrix_norm,matrix_re,matrix_im;
    wire [136:0] determinant_square_sum={1'b0,condition_norm_square}+{1'b0,condition_det_scaled};
    logic nmse_launch_pending, nmse_start, nmse_pending, nmse_total_done;
    logic nmse_result_valid, nmse_result_pass;
    logic [1:0] nmse_index;
    logic signed [127:0] nmse_serial_total, nmse_serial_residual;
    logic nmse_pair_ready;
    logic signed [127:0] nmse_pair_re, nmse_pair_im;
    logic nmse_re_busy,nmse_im_busy,nmse_total_busy,nmse_re_done,nmse_im_done,nmse_total_done_pulse;
    logic signed [127:0] nmse_re_square,nmse_im_square,nmse_total_product;

    // First/second pilot are stored in Q15; CORDIC produces current y rotation.
    logic signed [31:0] pilot0_re [0:1], pilot0_im [0:1];
    logic signed [31:0] pilot1_re [0:1], pilot1_im [0:1];
    logic cordic_start, cordic_busy, cordic_valid;
    logic signed [31:0] cordic_phase_q29;
    logic signed [15:0] rotate_cos, rotate_sin_neg;
    logic signed [63:0] pilot_corr_re, pilot_corr_im;
    logic pilot_update_pending, pilot_condition_pending;

    // Gram matrix Q16 cached per pilot interval.  One real multiplier is
    // scheduled over 32 cycles; b/c uses the same fixed Q16 ABI per task.
    logic gram_active, gram_start_pending, gram_valid;
    logic [1:0] gram_i, gram_j;
    logic [2:0] gram_term;
    logic [5:0] gram_issue;
    logic signed [63:0] gram_acc_re, gram_acc_im;
    logic signed [31:0] g_re_q16 [0:3], g_im_q16 [0:3];

    logic signed [31:0] fifo_r0_re [0:7], fifo_r0_im [0:7], fifo_r1_re [0:7], fifo_r1_im [0:7];
    logic [2:0] fifo_wp, fifo_rp;
    logic [3:0] fifo_count;
    logic signed [31:0] task_y_re [0:1], task_y_im [0:1];
    logic signed [63:0] task_b_re [0:1], task_b_im [0:1], task_energy;
    logic [4:0] task_phase;
    logic [9:0] task_id;
    logic [3:0] out_beat;
    logic signed [31:0] b_re_q16 [0:1], b_im_q16 [0:1];
    logic signed [63:0] c_q16;

    // Exactly one scheduled finite-width multiply pipe is shared by Gram and
    // payload construction.  Gram owns it only while pilot_hold is asserted;
    // payload owns it after the cached Gram is valid, so no arbitration can
    // silently change a numeric result.  The 64-bit A input is needed only
    // for the four final b*1/sqrt(2) operations; all Gram/b/c MAC products
    // sign-extend their Q15 operands into this pipe.
    localparam logic [2:0] MAC_NONE=3'd0, MAC_GRAM=3'd1, MAC_TASK=3'd2, MAC_HEADER=3'd3, MAC_PILOT=3'd4, MAC_ROTATE=3'd5, MAC_MATRIX=3'd6;
    logic                         mac_issue;
    logic [2:0]                   mac_owner_d, mac_owner_q;
    logic [5:0]                   mac_tag_d, mac_tag_q;
    logic signed [63:0]           mac_a_d;
    logic signed [31:0]           mac_b_d;
    logic signed [95:0]           mac_product_q;
    logic                         mac_valid_q;
    logic [4:0]                   task_issue;
    // Header equalization is folded into the same tagged pipe.  A complete
    // 2x2 complex inverse needs sixteen real products per symbol.
    logic                         header_mac_active;
    logic [4:0]                   header_mac_issue;
    logic [6:0]                   header_mac_hs;
    logic signed [31:0]           header_y_re [0:1], header_y_im [0:1];
    logic signed [63:0]           header_z_re [0:1], header_z_im [0:1];
    logic pilot_mac_active,pilot_mac_pending;
    logic [1:0] pilot_mac_entry;
    logic [2:0] pilot_mac_step;
    logic signed [63:0] pilot_hp_re,pilot_hp_im;
    logic pilot_mac_update;
    logic signed [63:0] pilot_update_re_acc,pilot_update_im_acc;
    logic signed [31:0] pilot_update_rr;
    logic pilot_iir_active,pilot_iir_start;
    logic [1:0] pilot_iir_entry;
    logic signed [39:0] pilot_iir_num_re,pilot_iir_num_im;
    logic payload_rot_active;
    logic [3:0] payload_rot_issue;
    logic signed [31:0] payload_rot_re[0:1],payload_rot_im[0:1];
    logic signed [15:0] payload_rot_cos,payload_rot_sin;
    logic signed [63:0] payload_rot_acc;
    logic signed [31:0] payload_rot_out[0:2];

    logic [3:0] pilot_pair_count;
    logic signed [31:0] previous_phase_q29, residual_phase [0:7], sort_phase [0:7];
    logic cfo_sort_load, cfo_sort_active;
    logic [2:0] cfo_sort_pass, cfo_sort_index;
    logic signed [31:0] last_residual_phase;
    logic signed [31:0] cfo_integral_hz_q16;
    logic [2:0] cfo_feedback_stage;
    logic signed [31:0] cfo_feedback_phase,cfo_feedback_hz;
    logic signed [63:0] cfo_feedback_product;

    logic signed [31:0] in_re [0:1], in_im [0:1];
    logic input_fire, payload_accept, payload_push, task_pop;

    // Frozen MRF1 pilot positions. Decode the finite set directly instead
    // of synthesizing modulo-66 arithmetic in the ready/accept feedback path.
    function automatic logic is_pilot0(input logic [9:0] index);
        case(index)
            10'd372,10'd438,10'd504,10'd570,10'd636,
            10'd702,10'd768,10'd834,10'd900:is_pilot0=1'b1;
            default:is_pilot0=1'b0;
        endcase
    endfunction
    function automatic logic is_pilot1(input logic [9:0] index);
        case(index)
            10'd373,10'd439,10'd505,10'd571,10'd637,
            10'd703,10'd769,10'd835,10'd901:is_pilot1=1'b1;
            default:is_pilot1=1'b0;
        endcase
    endfunction
    function automatic logic is_payload_index(input logic [9:0] index);
        is_payload_index = (index >= 10'd374) && (index <= 10'd917) &&
                           !is_pilot0(index) && !is_pilot1(index);
    endfunction
    function automatic logic [7:0] train_byte(input logic [3:0] byte_index);
        case (byte_index)
            0:train_byte=8'h14; 1:train_byte=8'h73; 2:train_byte=8'hf0; 3:train_byte=8'he6;
            4:train_byte=8'h3f; 5:train_byte=8'he3; 6:train_byte=8'h64; 7:train_byte=8'h37;
            8:train_byte=8'he0; 9:train_byte=8'hb5; 10:train_byte=8'h34; 11:train_byte=8'h97;
            12:train_byte=8'hf1;13:train_byte=8'hf1;14:train_byte=8'h6a;default:train_byte=8'h5c;
        endcase
    endfunction
    function automatic logic [1:0] train_symbol(input logic [5:0] k);
        logic [7:0] b;
        begin b=train_byte(k>>2); train_symbol=b >> (6-({4'd0,k[1:0]}<<1)); end
    endfunction
    function automatic logic signed [31:0] qpsk_re(input logic [1:0] symbol);
        qpsk_re = (symbol[1]^symbol[0]) ? -QPSK_A_Q15 : QPSK_A_Q15;
    endfunction
    function automatic logic signed [31:0] qpsk_im(input logic [1:0] symbol);
        qpsk_im = symbol[1] ? -QPSK_A_Q15 : QPSK_A_Q15;
    endfunction
    function automatic logic [1:0] slicer(input logic signed [63:0] re, input logic signed [63:0] im);
        if (!re[63] && !im[63]) slicer=2'd0;
        else if (re[63] && !im[63]) slicer=2'd1;
        else if (re[63] && im[63]) slicer=2'd2;
        else slicer=2'd3;
    endfunction
    function automatic logic [31:0] crc_byte(input logic [31:0] crc_in, input logic [7:0] d);
        logic [31:0] c; integer n;
        begin
            c=crc_in ^ d;
            for(n=0;n<8;n=n+1) c=(c>>1) ^ (c[0] ? 32'hedb88320 : 32'd0);
            crc_byte=c;
        end
    endfunction
    // Fixed-width signed round-to-nearest-even.  An arithmetic shift yields
    // floor(v/2^shift), including for negative v; the dropped two's-complement
    // bits are consequently an unsigned remainder in [0,2^shift).  Guard plus
    // sticky, with the floor quotient LSB as tie parity, gives exact RNE
    // without an absolute-value cone or a variable barrel shifter.
    function automatic logic signed [31:0] rne128(input logic signed [127:0] v, input integer shift);
        logic signed [127:0] q; logic guard,sticky;
        begin
            case (shift)
                2: begin q=v >>> 2;guard=v[1];sticky=v[0];end
                14: begin q=v >>> 14;guard=v[13];sticky=|v[12:0];end
                15: begin q=v >>> 15;guard=v[14];sticky=|v[13:0];end
                16: begin q=v >>> 16;guard=v[15];sticky=|v[14:0];end
                30: begin q=v >>> 30;guard=v[29];sticky=|v[28:0];end
                32: begin q=v >>> 32;guard=v[31];sticky=|v[30:0];end
                default: begin q=v;guard=1'b0;sticky=1'b0;end
            endcase
            if(guard && (sticky || q[0])) q=q+1'b1;
            rne128=q[31:0];
        end
    endfunction
    function automatic logic signed [31:0] rne_div10(input logic signed [39:0] v);
        logic sign; logic [39:0] mag,q,r;
        begin
            sign=v[39]; mag=sign ? (~v+1'b1) : v; q=mag/10; r=mag%10;
            if ((r>5)||((r==5)&&q[0])) q=q+1'b1;
            rne_div10=sign ? -$signed(q[31:0]) : $signed(q[31:0]);
        end
    endfunction
    function automatic logic signed [31:0] finish_iir_div10(input logic signed [39:0] n,input logic signed [31:0] q);
        logic signed [39:0] wide_q,remainder;
        logic [3:0] magnitude;
        begin
            wide_q=q;remainder=n-((wide_q<<<3)+(wide_q<<<1));
            magnitude=remainder<0 ? -remainder:remainder;
            finish_iir_div10=q;
            if(magnitude>5||(magnitude==5&&q[0]))finish_iir_div10=n<0?q-1:q+1;
        end
    endfunction
    function automatic logic signed [31:0] sat_q29(input logic signed [63:0] v);
        begin
            if (v > 64'sh000000007fffffff) sat_q29 = 32'sh7fffffff;
            else if (v < -64'sh0000000080000000) sat_q29 = 32'sh80000000;
            else sat_q29 = v[31:0];
        end
    endfunction

    // input order is explicitly pinned at this boundary.
    always_comb begin
        in_re[0]=$signed(s_symbol_data[31:0]);  in_im[0]=$signed(s_symbol_data[63:32]);
        in_re[1]=$signed(s_symbol_data[95:64]); in_im[1]=$signed(s_symbol_data[127:96]);
    end

    // Do not consume a header until inverse is complete.  Pilot transitions
    // hold upstream for <=~40 cycles, well below a symbol interval at 50 MHz.
    always_comb begin
        s_symbol_ready = rst_n && !frame_bad && !header_hold && !pilot_hold && !header_mac_active && !payload_rot_active;
        // Reject the remainder of a damaged frame, but do not permanently
        // wedge the receiver. Only a validated new frame boundary may rearm.
        if(frame_bad && s_frame_begin && s_lock_ok && s_symbol_index==0)
            s_symbol_ready=rst_n;
        if (frame_active && (expected_index >= 10'd292) && (expected_index <= 10'd371) && !inverse_valid)
            s_symbol_ready = 1'b0;
        if (frame_active && is_payload_index(expected_index) && (fifo_count == 4'd8))
            s_symbol_ready = 1'b0;
        // Every queued y belongs to the current pilot-region H/G/rotation.
        // Drain it before accepting a new pilot pair; changing H while a
        // shared task is paused would mix two channel estimates in one b.
        if(frame_active && is_pilot0(expected_index) &&
           ((fifo_count!=0)||(task_state!=TASK_IDLE)))s_symbol_ready=1'b0;
    end
    assign input_fire = s_symbol_valid && s_symbol_ready;
    assign payload_accept = input_fire && frame_active && is_payload_index(s_symbol_index) &&
                          (s_symbol_index == expected_index);
    assign payload_push = mac_valid_q && mac_owner_q==MAC_ROTATE && payload_rot_active && mac_tag_q==7;
    assign task_pop = (task_state == TASK_IDLE) && gram_valid && ctx_accepted && (fifo_count != 0);

    // Re/im run in parallel. Four bounded 80-cycle reciprocal slots fit in
    // the 600 clocks between training and the first header symbol.
    mrf1_channel_divider #(.NUM_W(80),.DEN_W(64),.OUT_W(32)) u_inverse_div_re (
        .clk(clk50),.rst_n(rst_n),.start(inv_start||pilot_iir_start),.numerator(pilot_iir_active?{{40{pilot_iir_num_re[39]}},pilot_iir_num_re}:inv_numerator_re),.denominator(pilot_iir_active?64'd10:det_mag),
        .busy(inv_busy_re),.done(inv_done_re),.div_zero(inv_div_zero_re),.overflow(inv_overflow_re),.quotient(inv_quotient_re));
    mrf1_channel_divider #(.NUM_W(80),.DEN_W(64),.OUT_W(32)) u_inverse_div_im (
        .clk(clk50),.rst_n(rst_n),.start(inv_start||pilot_iir_start),.numerator(pilot_iir_active?{{40{pilot_iir_num_im[39]}},pilot_iir_num_im}:inv_numerator_im),.denominator(pilot_iir_active?64'd10:det_mag),
        .busy(inv_busy_im),.done(inv_done_im),.div_zero(inv_div_zero_im),.overflow(inv_overflow_im),.quotient(inv_quotient_im));
    mrf1_channel_serial_mult #(.W(68)) u_condition_norm_square (
        .clk(clk50),.rst_n(rst_n),.start(condition_check_start),
        .a(condition_norm_operand),.b(condition_norm_operand),.busy(condition_norm_busy),
        .done(condition_norm_done),.product(condition_norm_square));
    mrf1_channel_serial_mult #(.W(68)) u_condition_det_scale (
        .clk(clk50),.rst_n(rst_n),.start(condition_check_start),
        .a(condition_det_operand),.b(condition_det_b),.busy(condition_det_busy),
        .done(condition_det_done),.product(condition_det_scaled));
    // Two correlation-square lanes retire one receive correlation each 64
    // clocks; a third lane obtains E*56*2*A^2.  Four rounds preserve the
    // original residual/energy NMSE criterion without wide inferred '*'.
    mrf1_channel_serial_mult #(.W(64)) u_nmse_re_square (
        .clk(clk50),.rst_n(rst_n),.start(nmse_start),.a(train_corr_re[nmse_index]),.b(train_corr_re[nmse_index]),
        .busy(nmse_re_busy),.done(nmse_re_done),.product(nmse_re_square));
    mrf1_channel_serial_mult #(.W(64)) u_nmse_im_square (
        .clk(clk50),.rst_n(rst_n),.start(nmse_start),.a(train_corr_im[nmse_index]),.b(train_corr_im[nmse_index]),
        .busy(nmse_im_busy),.done(nmse_im_done),.product(nmse_im_square));
    mrf1_channel_serial_mult #(.W(64)) u_nmse_total_mult (
        .clk(clk50),.rst_n(rst_n),.start(nmse_start && !nmse_total_done),.a(train_energy),.b(64'sd60127076800),
        .busy(nmse_total_busy),.done(nmse_total_done_pulse),.product(nmse_total_product));
    mrf1_channel_serial_mult #(.W(64)) u_h_est_serial (
        .clk(clk50),.rst_n(rst_n),.start(h_est_serial_start),
        .a(h_est_serial_operand),.b({{32{H_RECIP_56A_Q32[31]}},H_RECIP_56A_Q32}),
        .busy(h_est_serial_busy),.done(h_est_serial_done),.product(h_est_serial_product));
    mrf1_channel_serial_mult #(.W(64)) u_inv_num_re_serial (
        .clk(clk50),.rst_n(rst_n),.start(inv_num_start),.a(inv_num_re_a),.b(inv_num_re_b),
        .busy(inv_num_re_busy),.done(inv_num_re_done),.product(inv_num_re_product));
    mrf1_channel_serial_mult #(.W(64)) u_inv_num_im_serial (
        .clk(clk50),.rst_n(rst_n),.start(inv_num_start),.a(inv_num_im_a),.b(inv_num_im_b),
        .busy(inv_num_im_busy),.done(inv_num_im_done),.product(inv_num_im_product));
    mrf1_channel_cordic u_pilot_cordic (
        .clk(clk50),.rst_n(rst_n),.start(cordic_start),.correlation_re(sat_q29(pilot_corr_re >>> 16)),
        .correlation_im(sat_q29(pilot_corr_im >>> 16)),.busy(cordic_busy),.valid(cordic_valid),.phase_q29(cordic_phase_q29),
        .cos_q15(rotate_cos),.sin_neg_q15(rotate_sin_neg));

    logic signed [127:0] condition_left, condition_right, nmse_total, nmse_residual;

    always_comb begin
        h_est_serial_operand = h_est_serial_word[0] ?
            train_corr_im[h_est_serial_word[2:1]] : train_corr_re[h_est_serial_word[2:1]];
    end

    // phase 0 is the det.re term for both components; phase 1 is det.im.
    // Signs reproduce the old adjugate equations exactly before the final
    // Q30 left shift into the existing 80-bit divider interface.
    always_comb begin
        inv_num_re_b = inv_num_phase ? $signed(det_im) : $signed(det_re);
        inv_num_im_b = inv_num_re_b;
        inv_num_re_a='0;inv_num_im_a='0;
        case(inv_word)
            2'd0: begin
                if(!inv_num_phase) begin inv_num_re_a=$signed(h_re[3]);inv_num_im_a=$signed(h_im[3]);end
                else begin inv_num_re_a=$signed(h_im[3]);inv_num_im_a=-$signed(h_re[3]);end
            end
            2'd1: begin
                if(!inv_num_phase) begin inv_num_re_a=-$signed(h_re[1]);inv_num_im_a=-$signed(h_im[1]);end
                else begin inv_num_re_a=-$signed(h_im[1]);inv_num_im_a=$signed(h_re[1]);end
            end
            2'd2: begin
                if(!inv_num_phase) begin inv_num_re_a=-$signed(h_re[2]);inv_num_im_a=-$signed(h_im[2]);end
                else begin inv_num_re_a=-$signed(h_im[2]);inv_num_im_a=$signed(h_re[2]);end
            end
            default: begin
                if(!inv_num_phase) begin inv_num_re_a=$signed(h_re[0]);inv_num_im_a=$signed(h_im[0]);end
                else begin inv_num_re_a=$signed(h_im[0]);inv_num_im_a=-$signed(h_re[0]);end
            end
        endcase
    end

    // Fixed extraction from the explicitly bounded multiply pipe.  These are
    // deliberately not generic variable shifts: each binary point is part of
    // the MRF1 ABI and avoids a barrel shifter being inferred per call site.
    function automatic logic signed [31:0] rne96(input logic signed [95:0] v, input integer shift);
        logic signed [95:0] q; logic guard,sticky;
        begin
            case (shift)
                2: begin q=v >>> 2;guard=v[1];sticky=v[0];end
                14: begin q=v >>> 14;guard=v[13];sticky=|v[12:0];end
                15: begin q=v >>> 15;guard=v[14];sticky=|v[13:0];end
                16: begin q=v >>> 16;guard=v[15];sticky=|v[14:0];end
                30: begin q=v >>> 30;guard=v[29];sticky=|v[28:0];end
                32: begin q=v >>> 32;guard=v[31];sticky=|v[30:0];end
                default: begin q=v;guard=1'b0;sticky=1'b0;end
            endcase
            if(guard && (sticky || q[0])) q=q+1'b1;
            rne96=q[31:0];
        end
    endfunction

    // `n` is entry*8+term.  Term order is {Re*Re,Im*Im,Re*Im,Im*Re}
    // for receive rows 0 then 1, yielding H^H H without array over-indexing.
    function automatic logic signed [31:0] gram_operand_a(input logic [5:0] n);
        logic [1:0] entry, term, row, ci;
        begin
            entry=n[5:3]; term=n[1:0]; row=n[2]; ci=entry[1];
            gram_operand_a=((term==0)||(term==2)) ? h_re[row*2+ci] : h_im[row*2+ci];
        end
    endfunction
    function automatic logic signed [31:0] gram_operand_b(input logic [5:0] n);
        logic [1:0] entry, term, row, cj;
        begin
            entry=n[5:3]; term=n[1:0]; row=n[2]; cj=entry[0];
            gram_operand_b=((term==0)||(term==3)) ? h_re[row*2+cj] : h_im[row*2+cj];
        end
    endfunction
    function automatic logic signed [31:0] task_operand_a(input logic [4:0] n);
        logic [1:0] p, term, row;
        begin
            if(n<16) begin
                p=n[3]; row=n[2]; term=n[1:0];
                task_operand_a=((term==0)||(term==2)) ? h_re[row*2+p] : h_im[row*2+p];
            end else if(n<20) begin
                case(n[1:0])
                    0: task_operand_a=task_y_re[0]; 1: task_operand_a=task_y_im[0];
                    2: task_operand_a=task_y_re[1]; default: task_operand_a=task_y_im[1];
                endcase
            end else task_operand_a=32'sd0;
        end
    endfunction
    function automatic logic signed [31:0] task_operand_b(input logic [4:0] n);
        logic [1:0] term, row;
        begin
            if(n<16) begin
                row=n[2]; term=n[1:0];
                case(term)
                    0: task_operand_b=task_y_re[row]; 1: task_operand_b=task_y_im[row];
                    2: task_operand_b=task_y_im[row]; default: task_operand_b=task_y_re[row];
                endcase
            end else if(n<20) task_operand_b=task_operand_a(n);
            else task_operand_b=32'sd46341;
        end
    endfunction

    // `n` is {output-row, output-component, receive-row, coefficient-
    // component}.  The sixteen products form two 2x2 complex inverse-vector
    // multiplies.  Keeping this mapping next to the shared MAC makes the sign
    // convention explicit instead of inferring wide complex expressions.
    function automatic logic signed [31:0] header_operand_a(input logic [4:0] n);
        logic sample_imag;
        begin
            sample_imag = n[2] ^ n[0];
            header_operand_a = sample_imag ? header_y_im[n[1]] : header_y_re[n[1]];
        end
    endfunction
    function automatic logic signed [31:0] header_operand_b(input logic [4:0] n);
        begin
            case ({n[3],n[1]})
                2'b00: header_operand_b = n[0] ? inv00_im : inv00_re;
                2'b01: header_operand_b = n[0] ? inv01_im : inv01_re;
                2'b10: header_operand_b = n[0] ? inv10_im : inv10_re;
                default: header_operand_b = n[0] ? inv11_im : inv11_re;
            endcase
        end
    endfunction

    // The sole broad multiplier is physically registered.  Its tag carries
    // the operation to the accumulator one cycle later; therefore synthesis
    // cannot clone products from expression branches.
    always_comb begin
        mac_issue=1'b0; mac_owner_d=MAC_NONE; mac_tag_d='0; mac_a_d='0; mac_b_d='0;
        if(matrix_active && matrix_issue<16) begin
            mac_issue=1;mac_owner_d=MAC_MATRIX;mac_tag_d={1'b0,matrix_issue};
            if(matrix_issue<8)begin
                mac_a_d=matrix_issue[0] ? h_im[matrix_issue[2:1]] : h_re[matrix_issue[2:1]];
                mac_b_d=mac_a_d[31:0];
            end else case(matrix_issue)
                8:begin mac_a_d=h_re[0];mac_b_d=h_re[3];end
                9:begin mac_a_d=h_im[0];mac_b_d=h_im[3];end
                10:begin mac_a_d=h_re[1];mac_b_d=h_re[2];end
                11:begin mac_a_d=h_im[1];mac_b_d=h_im[2];end
                12:begin mac_a_d=h_re[0];mac_b_d=h_im[3];end
                13:begin mac_a_d=h_im[0];mac_b_d=h_re[3];end
                14:begin mac_a_d=h_re[1];mac_b_d=h_im[2];end
                15:begin mac_a_d=h_im[1];mac_b_d=h_re[2];end
                default:begin end
            endcase
        end else if(pilot_mac_active && !pilot_mac_pending) begin
            mac_issue=1'b1;mac_owner_d=MAC_PILOT;
            mac_tag_d={1'b0,pilot_mac_entry,pilot_mac_step};
            if(pilot_mac_update) begin
                case(pilot_mac_step)
                    0,1:begin
                        if(pilot_mac_entry[0]) begin
                            mac_a_d=pilot_mac_step==0 ?
                                $signed(pilot1_re[pilot_mac_entry[1]])+$signed(pilot1_im[pilot_mac_entry[1]]) :
                                $signed(pilot1_im[pilot_mac_entry[1]])-$signed(pilot1_re[pilot_mac_entry[1]]);
                        end else begin
                            mac_a_d=pilot_mac_step==0 ?
                                $signed(pilot0_re[pilot_mac_entry[1]])+$signed(pilot0_im[pilot_mac_entry[1]]) :
                                $signed(pilot0_im[pilot_mac_entry[1]])-$signed(pilot0_re[pilot_mac_entry[1]]);
                        end
                        mac_b_d=QPSK_A_Q15;
                    end
                    2:begin mac_a_d=pilot_hp_re;mac_b_d=rotate_cos;end
                    3:begin mac_a_d=pilot_hp_im;mac_b_d=rotate_sin_neg;end
                    4:begin mac_a_d=pilot_hp_re;mac_b_d=rotate_sin_neg;end
                    5:begin mac_a_d=pilot_hp_im;mac_b_d=rotate_cos;end
                    default:begin end
                endcase
            end else case(pilot_mac_step)
                0: begin mac_a_d=$signed(h_re[pilot_mac_entry])-$signed(h_im[pilot_mac_entry]);mac_b_d=QPSK_A_Q15;end
                1: begin mac_a_d=$signed(h_re[pilot_mac_entry])+$signed(h_im[pilot_mac_entry]);mac_b_d=QPSK_A_Q15;end
                2,4: begin
                    mac_a_d=pilot_hp_re;
                    mac_b_d=pilot_mac_entry[0] ?
                        (pilot_mac_step==2 ? pilot1_re[pilot_mac_entry[1]] : pilot1_im[pilot_mac_entry[1]]) :
                        (pilot_mac_step==2 ? pilot0_re[pilot_mac_entry[1]] : pilot0_im[pilot_mac_entry[1]]);
                end
                default: begin
                    mac_a_d=pilot_hp_im;
                    mac_b_d=pilot_mac_entry[0] ?
                        (pilot_mac_step==3 ? pilot1_im[pilot_mac_entry[1]] : pilot1_re[pilot_mac_entry[1]]) :
                        (pilot_mac_step==3 ? pilot0_im[pilot_mac_entry[1]] : pilot0_re[pilot_mac_entry[1]]);
                end
            endcase
        end else if(payload_rot_active && payload_rot_issue<8) begin
            mac_issue=1;mac_owner_d=MAC_ROTATE;mac_tag_d={2'd0,payload_rot_issue};
            case(payload_rot_issue[1:0])
                0:begin mac_a_d=payload_rot_re[payload_rot_issue[2]];mac_b_d=payload_rot_cos;end
                1:begin mac_a_d=payload_rot_im[payload_rot_issue[2]];mac_b_d=payload_rot_sin;end
                2:begin mac_a_d=payload_rot_re[payload_rot_issue[2]];mac_b_d=payload_rot_sin;end
                3:begin mac_a_d=payload_rot_im[payload_rot_issue[2]];mac_b_d=payload_rot_cos;end
            endcase
        end else if(header_mac_active && (header_mac_issue<5'd16)) begin
            mac_issue=1'b1; mac_owner_d=MAC_HEADER; mac_tag_d={1'b0,header_mac_issue};
            mac_a_d=header_operand_a(header_mac_issue);
            mac_b_d=header_operand_b(header_mac_issue);
        end else if(gram_active && (gram_issue<6'd32)) begin
            mac_issue=1'b1; mac_owner_d=MAC_GRAM; mac_tag_d=gram_issue;
            mac_a_d=gram_operand_a(gram_issue);
            mac_b_d=gram_operand_b(gram_issue);
        end else if((task_state==TASK_MAC) && (task_issue<5'd24)) begin
            mac_issue=1'b1; mac_owner_d=MAC_TASK; mac_tag_d={1'b0,task_issue};
            if(task_issue<5'd20)
                mac_a_d=task_operand_a(task_issue);
            else begin
                case(task_issue[1:0])
                    0: mac_a_d=task_b_re[0]; 1: mac_a_d=task_b_im[0];
                    2: mac_a_d=task_b_re[1]; default: mac_a_d=task_b_im[1];
                endcase
            end
            mac_b_d=task_operand_b(task_issue);
        end
    end
    always_ff @(posedge clk50) begin
        if(!rst_n) begin
            mac_valid_q<=1'b0; mac_owner_q<=MAC_NONE; mac_tag_q<='0; mac_product_q<='0;
        end else begin
            mac_valid_q<=mac_issue;
            if(mac_issue) begin
                mac_owner_q<=mac_owner_d; mac_tag_q<=mac_tag_d;
                mac_product_q<=mac_a_d*mac_b_d;
            end
        end
    end

    // MIP0 is byte-for-byte the frozen native ABI, emitted as fifteen 64-bit
    // beats.  TLAST is only the 528th task's final beat (one MHGD batch).
    always_comb begin
        m_mip_tvalid=(task_state==TASK_OUTPUT);
        m_mip_tkeep=8'hff;
        m_mip_tlast=(task_state==TASK_OUTPUT)&&(out_beat==4'd14)&&(task_id==10'd528);
        case(out_beat)
            0: m_mip_tdata=64'h0801_0001_3050_494d;
            1: m_mip_tdata={CONFIG_ID,22'd0,task_id};
            2: m_mip_tdata=64'h0000_0040_0301_0202;
            3: m_mip_tdata={32'd0,{22'd0,(task_id|10'd1)}};
            4: m_mip_tdata=64'h0040_0040_0008_000f;
            5: m_mip_tdata=64'd0;
            6: m_mip_tdata={PROPOSAL_Q16,32'd0};
            7: m_mip_tdata=64'd0;
            8: m_mip_tdata={g_im_q16[0],g_re_q16[0]};
            9: m_mip_tdata={g_im_q16[1],g_re_q16[1]};
            10:m_mip_tdata={g_im_q16[2],g_re_q16[2]};
            11:m_mip_tdata={g_im_q16[3],g_re_q16[3]};
            12:m_mip_tdata={b_im_q16[0],b_re_q16[0]};
            13:m_mip_tdata={b_im_q16[1],b_re_q16[1]};
            default:m_mip_tdata=c_q16;
        endcase
    end
    assign m_ctx_valid=ctx_valid_q;
    always_comb begin
        m_ctx_headers='0; m_ctx_h_q15='0;
        for(integer ctx_i=0;ctx_i<20;ctx_i=ctx_i+1) begin
            m_ctx_headers[ctx_i*8 +: 8]=header0[ctx_i];
            m_ctx_headers[(20+ctx_i)*8 +: 8]=header1[ctx_i];
        end
        for(integer ctx_i=0;ctx_i<4;ctx_i=ctx_i+1) begin
            m_ctx_h_q15[(ctx_i*2)*32 +:32]=h_re[ctx_i];
            m_ctx_h_q15[(ctx_i*2+1)*32 +:32]=h_im[ctx_i];
        end
    end
    assign m_ctx_frame_id=rf_frame_q; assign m_ctx_epoch=epoch_q; assign m_ctx_origin=origin_q;

    always_ff @(posedge clk50) begin : sequencer
        logic [5:0] k; logic [1:0] xs; logic signed [31:0] xr,xi;
        logic signed [63:0] cr,ci;
        logic signed [127:0] normh,detm,cmag;
        logic [6:0] hs; logic [4:0] hb; logic [1:0] z0,z1; logic [7:0] nb0,nb1;
        logic signed [63:0] zre0,zim0,zre1,zim1;
        logic signed [63:0] pr,pi;
        logic signed [31:0] hp_re,hp_im,nhr,nhi;
        logic signed [127:0] invnum;
        logic signed [63:0] rr,ri;
        integer ii;
        if(!rst_n) begin
            frame_active<=0; frame_bad<=0; header_hold<=0; pilot_hold<=0; ctx_accepted<=0; expected_index<=0;
            epoch_q<=0;origin_q<=0;rf_frame_q<=0;crc0<=32'hffff_ffff;crc1<=32'hffff_ffff;header_crc0<=0;header_crc1<=0;
            header_eval_pending<=0;ctx_valid_q<=0;train_finish_pending<=0;h_est_pending<=0;inv_prepare_pending<=0;
            h_est_serial_active<=0;h_est_serial_start<=0;h_est_serial_word<=0;
            inverse_valid<=0;inv_start<=0;inv_word<=0;det_re<=0;det_im<=0;det_mag<=0;
            inv_num_active<=0;inv_num_start<=0;inv_num_phase<=0;inv_num_re_acc<=0;inv_num_im_acc<=0;
            condition_check_start<=0;condition_check_pending<=0;condition_check_launch_pending<=0;condition_norm_operand<=0;condition_det_operand<=0;condition_is_pilot<=0;condition_det_b<=1000000;
            condition_square_mode<=0;matrix_active<=0;matrix_issue<=0;matrix_norm<=0;matrix_re<=0;matrix_im<=0;
            condition_result_valid<=0;condition_result_pass<=0;nmse_launch_pending<=0;nmse_start<=0;nmse_pending<=0;nmse_total_done<=0;nmse_result_valid<=0;nmse_result_pass<=0;nmse_index<=0;nmse_serial_total<=0;nmse_serial_residual<=0;nmse_pair_ready<=0;nmse_pair_re<=0;nmse_pair_im<=0;
            cordic_start<=0;pilot_update_pending<=0;pilot_condition_pending<=0;gram_active<=0;gram_start_pending<=0;gram_valid<=0;
            gram_i<=0;gram_j<=0;gram_term<=0;gram_issue<=0;gram_acc_re<=0;gram_acc_im<=0;
            header_mac_active<=0;header_mac_issue<=0;header_mac_hs<=0;
            pilot_mac_active<=0;pilot_mac_pending<=0;pilot_mac_entry<=0;pilot_mac_step<=0;
            pilot_hp_re<=0;pilot_hp_im<=0;pilot_corr_re<=0;pilot_corr_im<=0;
            pilot_mac_update<=0;pilot_update_re_acc<=0;pilot_update_im_acc<=0;pilot_update_rr<=0;
            pilot_iir_active<=0;pilot_iir_start<=0;pilot_iir_entry<=0;pilot_iir_num_re<=0;pilot_iir_num_im<=0;
            payload_rot_active<=0;payload_rot_issue<=0;payload_rot_cos<=0;payload_rot_sin<=0;payload_rot_acc<=0;
            for(ii=0;ii<2;ii=ii+1)begin payload_rot_re[ii]<=0;payload_rot_im[ii]<=0;end
            for(ii=0;ii<3;ii=ii+1)payload_rot_out[ii]<=0;
            for(ii=0;ii<2;ii=ii+1) begin
                header_y_re[ii]<=0;header_y_im[ii]<=0;header_z_re[ii]<=0;header_z_im[ii]<=0;
            end
            fifo_wp<=0;fifo_rp<=0;fifo_count<=0;task_state<=TASK_IDLE;task_phase<=0;task_issue<=0;task_id<=10'd1;out_beat<=0;
            m_frame_abort<=0;m_error_code<=ERR_NONE;m_frame_complete<=0;train_energy<=0;task_energy<=0;
            m_cfo_fb_valid<=0;m_cfo_fb_epoch<=0;m_cfo_fb_origin<=0;m_cfo_fb_delta_hz_q16<=0;
            have_previous_h<=0;frame_started_locked<=0;pilot_pair_count<=0;previous_phase_q29<=0;
            cfo_sort_load<=0;cfo_sort_active<=0;cfo_sort_pass<=0;cfo_sort_index<=0;last_residual_phase<=0;cfo_integral_hz_q16<=0;
            cfo_feedback_stage<=0;cfo_feedback_phase<=0;cfo_feedback_hz<=0;cfo_feedback_product<=0;
            for(ii=0;ii<20;ii=ii+1) begin header0[ii]<=0;header1[ii]<=0;end
            for(ii=0;ii<4;ii=ii+1) begin train_corr_re[ii]<=0;train_corr_im[ii]<=0;h_re[ii]<=0;h_im[ii]<=0;h_train_re[ii]<=0;h_train_im[ii]<=0;h_prev_re[ii]<=0;h_prev_im[ii]<=0;g_re_q16[ii]<=0;g_im_q16[ii]<=0;end
            for(ii=0;ii<2;ii=ii+1) begin pilot0_re[ii]<=0;pilot0_im[ii]<=0;pilot1_re[ii]<=0;pilot1_im[ii]<=0;task_y_re[ii]<=0;task_y_im[ii]<=0;task_b_re[ii]<=0;task_b_im[ii]<=0;b_re_q16[ii]<=0;b_im_q16[ii]<=0;end
            c_q16<=0;
        end else begin
            inv_start<=0;inv_num_start<=0; cordic_start<=0; condition_check_start<=0; nmse_start<=0;h_est_serial_start<=0; m_frame_abort<=0; m_frame_complete<=0; m_cfo_fb_valid<=0;
            pilot_iir_start<=0;
            // Operands are registers.  Launch one clock after loading them so
            // both serial lanes sample this frame's norm/determinant rather
            // than the preceding frame's values.
            if(condition_check_launch_pending) begin
                condition_check_launch_pending<=0;
                condition_check_start<=1;
                condition_check_pending<=1;
            end
            if(nmse_launch_pending) begin
                nmse_launch_pending<=0; nmse_start<=1; nmse_pending<=1;
            end
            if(s_frame_abort && frame_active) begin
                frame_bad<=1; frame_active<=0; ctx_valid_q<=0; m_frame_abort<=1; m_error_code<=ERR_INPUT;
                condition_check_pending<=0; condition_check_launch_pending<=0; nmse_pending<=0; nmse_launch_pending<=0;
                header_mac_active<=0;h_est_serial_active<=0;inv_num_active<=0;
                pilot_mac_active<=0;pilot_mac_pending<=0;
                payload_rot_active<=0;
                matrix_active<=0;
                pilot_iir_active<=0;
            end
            if(mac_issue && mac_owner_d==MAC_PILOT)pilot_mac_pending<=1;
            if(mac_valid_q && mac_owner_q==MAC_PILOT && pilot_mac_active) begin
                pilot_mac_pending<=0;
                if(pilot_mac_update) begin
                    case(pilot_mac_step)
                        0:pilot_hp_re<=$signed(rne96(mac_product_q,15));
                        1:pilot_hp_im<=$signed(rne96(mac_product_q,15));
                        2:pilot_update_re_acc<=mac_product_q[63:0];
                        3:pilot_update_rr<=rne128($signed(pilot_update_re_acc)-$signed(mac_product_q[63:0]),15);
                        4:pilot_update_im_acc<=mac_product_q[63:0];
                        5:begin
                            ri=rne128($signed(pilot_update_im_acc)+$signed(mac_product_q[63:0]),15);
                            pilot_iir_num_re<=9*$signed(h_re[pilot_mac_entry])+$signed(pilot_update_rr);
                            pilot_iir_num_im<=9*$signed(h_im[pilot_mac_entry])+$signed(ri);
                            pilot_iir_entry<=pilot_mac_entry;pilot_iir_active<=1;pilot_iir_start<=1;
                        end
                        default:begin end
                    endcase
                end else case(pilot_mac_step)
                    0:pilot_hp_re<=mac_product_q[63:0];
                    1:pilot_hp_im<=mac_product_q[63:0];
                    2,3:pilot_corr_re<=pilot_corr_re+$signed(mac_product_q[63:0]);
                    4:pilot_corr_im<=pilot_corr_im+$signed(mac_product_q[63:0]);
                    5:pilot_corr_im<=pilot_corr_im-$signed(mac_product_q[63:0]);
                    default:begin end
                endcase
                if(pilot_mac_step==5)begin
                    pilot_mac_step<=0;
                    if(pilot_mac_update)pilot_mac_active<=0;
                    else if(pilot_mac_entry==3)begin
                        pilot_mac_active<=0;
                        cordic_start<=1;
                    end
                    else pilot_mac_entry<=pilot_mac_entry+1'b1;
                end else pilot_mac_step<=pilot_mac_step+1'b1;
            end
            if(ctx_valid_q && m_ctx_ready) begin ctx_valid_q<=0; ctx_accepted<=1; header_hold<=0; end
            // Keep phase->Hz, gain and integral clamp on separate clock
            // boundaries. This feedback is once per frame, not a symbol-rate
            // critical path through two constant multiplies and saturation.
            case(cfo_feedback_stage)
                1:begin cfo_feedback_product<=$signed(cfo_feedback_phase)*64'sd1264288285;cfo_feedback_stage<=2;end
                2:begin cfo_feedback_hz<=rne128(cfo_feedback_product,32);cfo_feedback_stage<=3;end
                3:begin cfo_feedback_product<=$signed(cfo_feedback_hz)*64'sd2621;cfo_feedback_stage<=4;end
                4:begin
                    ri=rne128(cfo_feedback_product,16);
                    if($signed(cfo_integral_hz_q16)+$signed(ri)>32'sd32768000)ri=32'sd32768000-$signed(cfo_integral_hz_q16);
                    else if($signed(cfo_integral_hz_q16)+$signed(ri)< -32'sd32768000)ri=-32'sd32768000-$signed(cfo_integral_hz_q16);
                    if(frame_started_locked&&!frame_bad)begin
                        cfo_integral_hz_q16<=cfo_integral_hz_q16+ri;
                        m_cfo_fb_delta_hz_q16<=ri;m_cfo_fb_epoch<=epoch_q;m_cfo_fb_origin<=origin_q;m_cfo_fb_valid<=1;
                    end
                    cfo_feedback_stage<=0;
                end
                default:begin end
            endcase

            // Serialize the eight fixed-reciprocal H estimates.  `done`
            // belongs to h_est_serial_word, whose operands stayed registered
            // for the full 64-cycle multiply.  The smoothing-by-three is
            // shift/add only and preserves the existing numeric expression.
            if(train_finish_pending) begin
                train_finish_pending<=0;h_est_serial_active<=1;h_est_serial_word<=0;h_est_serial_start<=1;
            end else if(h_est_serial_active && h_est_serial_done) begin
                nhr=rne128(h_est_serial_product,32);
                if(h_est_serial_word[0]) begin
                    h_train_im[h_est_serial_word[2:1]]<=nhr;
                    if(frame_started_locked) h_im[h_est_serial_word[2:1]]<=rne128($signed(h_prev_im[h_est_serial_word[2:1]])*3+$signed(nhr),2);
                    else h_im[h_est_serial_word[2:1]]<=nhr;
                end else begin
                    h_train_re[h_est_serial_word[2:1]]<=nhr;
                    if(frame_started_locked) h_re[h_est_serial_word[2:1]]<=rne128($signed(h_prev_re[h_est_serial_word[2:1]])*3+$signed(nhr),2);
                    else h_re[h_est_serial_word[2:1]]<=nhr;
                end
                if(h_est_serial_word==3'd7) begin h_est_serial_active<=0;h_est_pending<=1;end
                else begin h_est_serial_word<=h_est_serial_word+1'b1;h_est_serial_start<=1;end
            end else if(h_est_pending) begin
                h_est_pending<=0; inv_prepare_pending<=1;
            end else if(inv_prepare_pending) begin
                inv_prepare_pending<=0;
                matrix_active<=1;matrix_issue<=0;matrix_norm<=0;matrix_re<=0;matrix_im<=0;condition_is_pilot<=0;
                nmse_index<=0; nmse_total_done<=0; nmse_result_valid<=0; condition_result_valid<=0; nmse_pair_ready<=0;
                nmse_launch_pending<=1;
            end
            if(mac_issue&&mac_owner_d==MAC_MATRIX)matrix_issue<=matrix_issue+1'b1;
            if(mac_valid_q&&mac_owner_q==MAC_MATRIX&&matrix_active)begin
                if(mac_tag_q<8)matrix_norm<=matrix_norm+$signed(mac_product_q);
                else if(mac_tag_q<12)begin
                    if(mac_tag_q==9||mac_tag_q==10)matrix_re<=matrix_re-$signed(mac_product_q);
                    else matrix_re<=matrix_re+$signed(mac_product_q);
                end else begin
                    if(mac_tag_q>=14)matrix_im<=matrix_im-$signed(mac_product_q);
                    else matrix_im<=matrix_im+$signed(mac_product_q);
                end
                if(mac_tag_q==15)begin
                    matrix_active<=0;
                    condition_norm_operand<=matrix_re;
                    condition_det_operand<=matrix_im-$signed(mac_product_q);
                    condition_det_b<=matrix_im-$signed(mac_product_q);
                    condition_square_mode<=1;condition_check_launch_pending<=1;
                    if(!condition_is_pilot)begin det_re<=matrix_re[63:0];det_im<=matrix_im-$signed(mac_product_q);end
                end
            end
            if(condition_check_pending && condition_norm_done && condition_det_done) begin
                condition_check_pending<=0;
                if(condition_square_mode)begin
                    condition_square_mode<=0;
                    if(determinant_square_sum==0 || |determinant_square_sum[136:67] || matrix_norm[67])begin
                        frame_bad<=1;m_frame_abort<=1;m_error_code<=condition_is_pilot?ERR_PILOT:ERR_TRAIN;pilot_hold<=0;
                    end else begin
                        condition_norm_operand<=matrix_norm;
                        condition_det_operand<=determinant_square_sum[67:0];condition_det_b<=1000000;
                        condition_check_launch_pending<=1;
                    end
                end else if(condition_is_pilot) begin
                    condition_is_pilot<=0;
                    if(condition_norm_square > condition_det_scaled) begin
                        frame_bad<=1;m_frame_abort<=1;m_error_code<=ERR_PILOT;pilot_hold<=0;
                    end else gram_start_pending<=1;
                end else begin
                    condition_result_valid<=1;
                    condition_result_pass<=!(condition_norm_square > condition_det_scaled);
                end
            end
            if(nmse_total_done_pulse) begin
                nmse_total_done<=1; nmse_serial_total<=nmse_total_product; nmse_serial_residual<=nmse_total_product;
            end
            if(nmse_pending && nmse_re_done && nmse_im_done) begin
                nmse_pair_re<=nmse_re_square; nmse_pair_im<=nmse_im_square; nmse_pair_ready<=1;
            end
            if(nmse_pending && nmse_pair_ready && nmse_total_done) begin
                nmse_pair_ready<=0;
                if(nmse_index==3) begin
                    nmse_pending<=0; nmse_result_valid<=1;
                    nmse_result_pass<=((nmse_serial_residual-nmse_pair_re-nmse_pair_im)>=0) &&
                                      (((nmse_serial_residual-nmse_pair_re-nmse_pair_im)<<<2)+(nmse_serial_residual-nmse_pair_re-nmse_pair_im) <= nmse_serial_total);
                end else begin
                    nmse_serial_residual<=nmse_serial_residual-nmse_pair_re-nmse_pair_im;
                    nmse_index<=nmse_index+1'b1; nmse_launch_pending<=1;
                end
            end
            if(condition_result_valid && nmse_result_valid) begin
                condition_result_valid<=0;nmse_result_valid<=0;
                if(!condition_result_pass) begin frame_bad<=1;m_frame_abort<=1;m_error_code<=ERR_CONDITION;end
                else if(!nmse_result_pass) begin frame_bad<=1;m_frame_abort<=1;m_error_code<=ERR_TRAIN;end
                else begin
                    det_mag<=condition_det_operand[63:0];inv_word<=0;
                    inv_num_phase<=0;inv_num_re_acc<=0;inv_num_im_acc<=0;
                    inv_num_active<=1;inv_num_start<=1;
                end
            end
            if(inv_num_active && inv_num_re_done && inv_num_im_done) begin
                if(!inv_num_phase) begin
                    inv_num_re_acc<=inv_num_re_product;
                    inv_num_im_acc<=inv_num_im_product;
                    inv_num_phase<=1;inv_num_start<=1;
                end else begin
                    inv_numerator_re<=($signed(inv_num_re_acc)+$signed(inv_num_re_product)) <<< 30;
                    inv_numerator_im<=($signed(inv_num_im_acc)+$signed(inv_num_im_product)) <<< 30;
                    inv_num_active<=0;inv_start<=1;
                end
            end
            if(pilot_iir_active && inv_done_re && inv_done_im)begin
                pilot_iir_active<=0;
                if(inv_div_zero_re||inv_div_zero_im||inv_overflow_re||inv_overflow_im)begin
                    frame_bad<=1;m_frame_abort<=1;m_error_code<=ERR_PILOT;pilot_hold<=0;
                end else begin
                    h_re[pilot_iir_entry]<=finish_iir_div10(pilot_iir_num_re,inv_quotient_re);
                    h_im[pilot_iir_entry]<=finish_iir_div10(pilot_iir_num_im,inv_quotient_im);
                    if(pilot_iir_entry==3)pilot_condition_pending<=1;
                    else begin pilot_mac_entry<=pilot_iir_entry+1'b1;pilot_mac_step<=0;pilot_mac_active<=1;end
                end
            end else if(inv_done_re && inv_done_im) begin
                if(inv_div_zero_re||inv_div_zero_im||inv_overflow_re||inv_overflow_im) begin frame_bad<=1;m_frame_abort<=1;m_error_code<=ERR_INVERSE;end
                else begin
                    case(inv_word)
                        0: begin inv00_re<=inv_quotient_re;inv00_im<=inv_quotient_im;end
                        1: begin inv01_re<=inv_quotient_re;inv01_im<=inv_quotient_im;end
                        2: begin inv10_re<=inv_quotient_re;inv10_im<=inv_quotient_im;end
                        default: begin inv11_re<=inv_quotient_re;inv11_im<=inv_quotient_im;end
                    endcase
                    if(inv_word==3) begin inverse_valid<=1; end
                    else begin
                        inv_word<=inv_word+1'b1;inv_num_phase<=0;
                        inv_num_re_acc<=0;inv_num_im_acc<=0;inv_num_active<=1;inv_num_start<=1;
                    end
                end
            end
            // Header CRC and context acceptance are deliberately a hard gate.
            if(header_eval_pending) begin
                header_eval_pending<=0;
                if((header0[0]!=8'h4d)||(header0[1]!=8'h52)||(header0[2]!=8'h46)||(header0[3]!=8'h31)||
                   (header1[0]!=8'h4d)||(header1[1]!=8'h52)||(header1[2]!=8'h46)||(header1[3]!=8'h31)||
                   (header0[4]!=1)||(header1[4]!=1)||(header0[5]!=0)||(header1[5]!=1)||
                   (header0[6]!=1)||(header1[6]!=1)||(header0[7]!=0)||(header1[7]!=0)||
                   (header0[12]!=8'h80)||(header0[13]!=0)||(header0[14]!=0)||(header0[15]!=0)||
                   (header1[12]!=8'h80)||(header1[13]!=0)||(header1[14]!=0)||(header1[15]!=0)||
                   (header_crc0!={header0[19],header0[18],header0[17],header0[16]})||
                   (header_crc1!={header1[19],header1[18],header1[17],header1[16]})||
                   ({header0[11],header0[10],header0[9],header0[8]}!={header1[11],header1[10],header1[9],header1[8]})) begin
                    frame_bad<=1;m_frame_abort<=1;m_error_code<=ERR_HEADER;header_hold<=0;
                end else begin
                    rf_frame_q<={header0[11],header0[10],header0[9],header0[8]};ctx_valid_q<=1;
                end
            end
            // CORDIC result is applied before any payload symbol is released.
            if(cordic_valid) begin
                // Phase belongs to the pilot *pair* just observed.  The
                // acquisition NCO never sees it until this whole frame has
                // completed, so there is no second rotation of current y.
                if(pilot_pair_count==0) begin
                    previous_phase_q29<=cordic_phase_q29;
                end else begin
                    cr=$signed(cordic_phase_q29)-$signed(previous_phase_q29);
                    if(cr > 64'sd1686629713) cr=cr-64'sd3373259426;
                    else if(cr < -64'sd1686629713) cr=cr+64'sd3373259426;
                    residual_phase[pilot_pair_count-1]<=cr[31:0];
                    last_residual_phase<=cr[31:0];
                    previous_phase_q29<=cordic_phase_q29;
                    if(pilot_pair_count==8) cfo_sort_load<=1;
                end
                pilot_pair_count<=pilot_pair_count+1'b1;
                // The same physical MAC now evaluates pilot division,
                // rotation and IIR update one H coefficient at a time.
                pilot_mac_active<=1;pilot_mac_pending<=0;pilot_mac_update<=1;
                pilot_mac_entry<=0;pilot_mac_step<=0;
            end
            // Eight adjacent pilot phase deltas are sorted in-place.  The
            // upper median is exactly the native receiver convention for an
            // even sample count.  This runs in the tail/slack, never on a
            // symbol-rate critical combinational path.
            if(cfo_sort_load) begin
                cfo_sort_load<=0;cfo_sort_active<=1;cfo_sort_pass<=0;cfo_sort_index<=0;
                for(ii=0;ii<8;ii=ii+1) sort_phase[ii]<=residual_phase[ii];
            end else if(cfo_sort_active) begin
                if(sort_phase[cfo_sort_index] > sort_phase[cfo_sort_index+1]) begin
                    sort_phase[cfo_sort_index]<=sort_phase[cfo_sort_index+1];
                    sort_phase[cfo_sort_index+1]<=sort_phase[cfo_sort_index];
                end
                if(cfo_sort_index == (6-cfo_sort_pass)) begin
                    cfo_sort_index<=0;
                    if(cfo_sort_pass==6) begin
                        cfo_sort_active<=0;
                        if(frame_started_locked && !frame_bad) begin
                            cfo_feedback_phase<=sort_phase[4];cfo_feedback_stage<=1;
                        end
                    end else cfo_sort_pass<=cfo_sort_pass+1'b1;
                end else cfo_sort_index<=cfo_sort_index+1'b1;
            end
            if(pilot_condition_pending) begin
                pilot_condition_pending<=0;
                matrix_active<=1;matrix_issue<=0;matrix_norm<=0;matrix_re<=0;matrix_im<=0;condition_is_pilot<=1;
            end
            if(gram_start_pending) begin
                gram_start_pending<=0;gram_active<=1;gram_i<=0;gram_j<=0;gram_term<=0;
                gram_issue<=0;gram_acc_re<=0;gram_acc_im<=0;gram_valid<=0;
            end
            if(mac_issue && (mac_owner_d==MAC_GRAM)) gram_issue<=gram_issue+1'b1;
            // Header equalization is deliberately retired through the same
            // physical multiplier as Gram and task generation.  `mac_tag_q`
            // is stable across the registered product, and bit ordering is
            // fixed by header_operand_{a,b} above.  Product 15 uses the
            // completed last accumulator value before producing its QPSK
            // decisions, so no old-value nonblocking-assignment hazard can
            // drop the final coefficient.
            if(mac_issue && (mac_owner_d==MAC_HEADER))
                header_mac_issue<=header_mac_issue+1'b1;
            if(mac_valid_q && (mac_owner_q==MAC_HEADER) && header_mac_active && frame_active) begin
                rr=$signed(mac_product_q[63:0]);
                case(mac_tag_q[3:2])
                    2'd0: begin
                        zre0 = (mac_tag_q[1:0]==0) ? rr :
                               (mac_tag_q[0] ? $signed(header_z_re[0])-$signed(rr) : $signed(header_z_re[0])+$signed(rr));
                        header_z_re[0]<=zre0;
                    end
                    2'd1: begin
                        zim0 = (mac_tag_q[1:0]==0) ? rr : $signed(header_z_im[0])+$signed(rr);
                        header_z_im[0]<=zim0;
                    end
                    2'd2: begin
                        zre1 = (mac_tag_q[1:0]==0) ? rr :
                               (mac_tag_q[0] ? $signed(header_z_re[1])-$signed(rr) : $signed(header_z_re[1])+$signed(rr));
                        header_z_re[1]<=zre1;
                    end
                    default: begin
                        zim1 = (mac_tag_q[1:0]==0) ? rr : $signed(header_z_im[1])+$signed(rr);
                        header_z_im[1]<=zim1;
                    end
                endcase
                if(mac_tag_q==6'd15) begin
                    // The last operation is z1.im (+ inv11.im*y1.re).
                    // All other completed terms are already in the banks.
                    hs=header_mac_hs;hb=hs>>2;
                    z0=slicer(header_z_re[0],header_z_im[0]);
                    z1=slicer(header_z_re[1],zim1);
                    nb0=header0[hb]|(z0<<(6-2*hs[1:0]));
                    nb1=header1[hb]|(z1<<(6-2*hs[1:0]));
                    header0[hb]<=nb0;header1[hb]<=nb1;
                    if(hs[1:0]==3) begin
                        if(hb<16) begin
                            crc0<=crc_byte(crc0,nb0);crc1<=crc_byte(crc1,nb1);
                            if(hb==15) begin
                                header_crc0<=crc_byte(crc0,nb0)^32'hffff_ffff;
                                header_crc1<=crc_byte(crc1,nb1)^32'hffff_ffff;
                            end
                        end
                    end
                    header_mac_active<=0;
                    if(hs==79) begin header_eval_pending<=1;header_hold<=1;end
                end
            end
            // Retire the sole multiplier result.  Each matrix element has
            // eight tagged products: four real/imag terms for each receive
            // row.  This is one product per clock rather than eight cloned
            // 32x32 expressions.
            if(mac_valid_q && (mac_owner_q==MAC_GRAM)) begin
                gram_i<=mac_tag_q[4]; gram_j<=mac_tag_q[3]; gram_term<=mac_tag_q[2:0];
                case(mac_tag_q[1:0])
                    2'd0: gram_acc_re<=gram_acc_re+mac_product_q[63:0];
                    2'd1: gram_acc_re<=gram_acc_re+mac_product_q[63:0];
                    2'd2: gram_acc_im<=gram_acc_im+mac_product_q[63:0];
                    default: gram_acc_im<=gram_acc_im-mac_product_q[63:0];
                endcase
                if(mac_tag_q[2:0]==3'd7) begin
                    g_re_q16[mac_tag_q[5:3]]<=rne96(gram_acc_re,15);
                    g_im_q16[mac_tag_q[5:3]]<=rne96(gram_acc_im-mac_product_q[63:0],15);
                    gram_acc_re<=0;gram_acc_im<=0;
                    if(mac_tag_q==6'd31) begin gram_active<=0;gram_valid<=1;pilot_hold<=0;end
                end
            end
            // FIFO accounting and task engine; a task costs 20 MAC + 15 AXIS
            // cycles, so at 50 MHz it sustains a 1 Msym/s payload source.
            case({payload_push,task_pop})
                2'b10: fifo_count<=fifo_count+1'b1;
                2'b01: fifo_count<=fifo_count-1'b1;
                default: fifo_count<=fifo_count;
            endcase
            if(payload_accept)begin
                payload_rot_re[0]<=in_re[0];payload_rot_im[0]<=in_im[0];
                payload_rot_re[1]<=in_re[1];payload_rot_im[1]<=in_im[1];
                payload_rot_cos<=rotate_cos;payload_rot_sin<=rotate_sin_neg;
                payload_rot_active<=1;payload_rot_issue<=0;
            end
            if(mac_issue && mac_owner_d==MAC_ROTATE)payload_rot_issue<=payload_rot_issue+1'b1;
            if(mac_valid_q && mac_owner_q==MAC_ROTATE && payload_rot_active)begin
                if(!mac_tag_q[0])payload_rot_acc<=mac_product_q[63:0];
                else if(mac_tag_q==1)payload_rot_out[0]<=rne128($signed(payload_rot_acc)-$signed(mac_product_q[63:0]),15);
                else if(mac_tag_q==3)payload_rot_out[1]<=rne128($signed(payload_rot_acc)+$signed(mac_product_q[63:0]),15);
                else if(mac_tag_q==5)payload_rot_out[2]<=rne128($signed(payload_rot_acc)-$signed(mac_product_q[63:0]),15);
            end
            if(payload_push) begin
                fifo_r0_re[fifo_wp]<=payload_rot_out[0];
                fifo_r0_im[fifo_wp]<=payload_rot_out[1];
                fifo_r1_re[fifo_wp]<=payload_rot_out[2];
                fifo_r1_im[fifo_wp]<=rne128($signed(payload_rot_acc)+$signed(mac_product_q[63:0]),15);
                payload_rot_active<=0;
                fifo_wp<=fifo_wp+1'b1;
            end
            if(task_pop) begin
                task_y_re[0]<=fifo_r0_re[fifo_rp];task_y_im[0]<=fifo_r0_im[fifo_rp];task_y_re[1]<=fifo_r1_re[fifo_rp];task_y_im[1]<=fifo_r1_im[fifo_rp];
                fifo_rp<=fifo_rp+1'b1;task_phase<=0;task_issue<=0;task_state<=TASK_MAC;
                task_b_re[0]<=0;task_b_re[1]<=0;task_b_im[0]<=0;task_b_im[1]<=0;task_energy<=0;
            end
            // A pilot Gram refresh can preempt a queued payload task.  Only
            // advance the issue pointer when the shared pipe actually accepts
            // this task's operation; otherwise the unfinished tagged MACs
            // would be skipped and the task would deadlock at issue=24.
            if(mac_issue && (mac_owner_d==MAC_TASK)) task_issue<=task_issue+1'b1;
            if(mac_valid_q && (mac_owner_q==MAC_TASK)) begin
                if(mac_tag_q<16) begin
                    case(mac_tag_q[1:0])
                        2'd0: task_b_re[mac_tag_q[3]]<=task_b_re[mac_tag_q[3]]+mac_product_q[63:0];
                        2'd1: task_b_re[mac_tag_q[3]]<=task_b_re[mac_tag_q[3]]+mac_product_q[63:0];
                        2'd2: task_b_im[mac_tag_q[3]]<=task_b_im[mac_tag_q[3]]+mac_product_q[63:0];
                        default: task_b_im[mac_tag_q[3]]<=task_b_im[mac_tag_q[3]]-mac_product_q[63:0];
                    endcase
                end else if(mac_tag_q<20) begin
                    if(mac_tag_q==16) task_energy<=mac_product_q[63:0];
                    else task_energy<=task_energy+mac_product_q[63:0];
                    if(mac_tag_q==19) c_q16<=rne96(task_energy+mac_product_q[63:0],14);
                end else begin
                    case(mac_tag_q[1:0])
                        0: b_re_q16[0]<=rne96(mac_product_q,30);
                        1: b_im_q16[0]<=rne96(mac_product_q,30);
                        2: b_re_q16[1]<=rne96(mac_product_q,30);
                        default: begin
                            b_im_q16[1]<=rne96(mac_product_q,30);
                            out_beat<=0;task_state<=TASK_OUTPUT;
                        end
                    endcase
                end
            end else if((task_state==TASK_OUTPUT)&&m_mip_tready) begin
                if(out_beat==14) begin
                    task_state<=TASK_IDLE;
                    if(task_id==528) begin
                        task_id<=task_id;
                        if(expected_index==950) begin
                            frame_active<=0;m_frame_complete<=1;have_previous_h<=1;
                            for(ii=0;ii<4;ii=ii+1) begin h_prev_re[ii]<=h_re[ii];h_prev_im[ii]<=h_im[ii];end
                        end
                    end else task_id<=task_id+1'b1;
                end else out_beat<=out_beat+1'b1;
            end
            // Input capture and strict index/control checks are last so a new
            // frame atomically clears every prior partial result.
            if(input_fire) begin
                if(!frame_active || frame_bad) begin
                    if(!s_frame_begin||!s_lock_ok||(s_symbol_index!=0)) begin m_frame_abort<=1;m_error_code<=ERR_INPUT;end
                    else begin
                        frame_active<=1;frame_bad<=0;expected_index<=1;epoch_q<=s_epoch;origin_q<=s_frame_origin;ctx_accepted<=0;ctx_valid_q<=0;inverse_valid<=0;header_hold<=0;pilot_hold<=0;header_mac_active<=0;header_mac_issue<=0;h_est_serial_active<=0;h_est_serial_start<=0;inv_num_active<=0;inv_num_start<=0;crc0<=32'hffff_ffff;crc1<=32'hffff_ffff;train_energy<=0;task_id<=1;fifo_count<=0;fifo_wp<=0;fifo_rp<=0;gram_valid<=0;frame_started_locked<=have_previous_h;pilot_pair_count<=0;cfo_sort_load<=0;cfo_sort_active<=0;train_finish_pending<=0;h_est_pending<=0;inv_prepare_pending<=0;header_eval_pending<=0;pilot_condition_pending<=0;condition_check_pending<=0;condition_check_launch_pending<=0;
                        task_state<=TASK_IDLE;task_issue<=0;out_beat<=0;
                        gram_active<=0;gram_start_pending<=0;gram_issue<=0;
                        nmse_pending<=0;nmse_launch_pending<=0;
                        nmse_result_valid<=0;condition_result_valid<=0;condition_is_pilot<=0;
                        matrix_active<=0;matrix_issue<=0;condition_square_mode<=0;
                        pilot_update_pending<=0;cordic_start<=0;inv_start<=0;
                        pilot_mac_active<=0;pilot_mac_pending<=0;
                        pilot_iir_active<=0;pilot_iir_start<=0;
                        payload_rot_active<=0;payload_rot_issue<=0;
                        cfo_feedback_stage<=0;
                        for(ii=0;ii<20;ii=ii+1) begin header0[ii]<=0;header1[ii]<=0;end
                        for(ii=0;ii<4;ii=ii+1) begin train_corr_re[ii]<=0;train_corr_im[ii]<=0;end
                    end
                end else if(s_frame_begin||(s_symbol_index!=expected_index)||(s_epoch!=epoch_q)) begin
                frame_bad<=1;frame_active<=0;m_frame_abort<=1;m_error_code<=ERR_INPUT;ctx_valid_q<=0;header_mac_active<=0;h_est_serial_active<=0;inv_num_active<=0;
                end else begin
                    expected_index<=expected_index+1'b1;
                    // Training: 140..203 -> col0, 216..279 -> col1, keep k4..59.
                    if(((s_symbol_index>=140)&&(s_symbol_index<=203))||((s_symbol_index>=216)&&(s_symbol_index<=279))) begin
                        k=(s_symbol_index>=216)?(s_symbol_index-216):(s_symbol_index-140);
                        if((k>=4)&&(k<=59)) begin
                            xs=train_symbol(k);xr=qpsk_re(xs);xi=qpsk_im(xs);
                            for(ii=0;ii<2;ii=ii+1) begin
                                cr=$signed(in_re[ii])*$signed(xr)+$signed(in_im[ii])*$signed(xi);
                                ci=$signed(in_im[ii])*$signed(xr)-$signed(in_re[ii])*$signed(xi);
                                if(s_symbol_index<204) begin train_corr_re[ii*2]<=train_corr_re[ii*2]+cr;train_corr_im[ii*2]<=train_corr_im[ii*2]+ci;end
                                else begin train_corr_re[ii*2+1]<=train_corr_re[ii*2+1]+cr;train_corr_im[ii*2+1]<=train_corr_im[ii*2+1]+ci;end
                            end
                            train_energy<=train_energy+$signed(in_re[0])*$signed(in_re[0])+$signed(in_im[0])*$signed(in_im[0])+$signed(in_re[1])*$signed(in_re[1])+$signed(in_im[1])*$signed(in_im[1]);
                        end
                        if(s_symbol_index==279) train_finish_pending<=1;
                    end
                    if((s_symbol_index>=292)&&(s_symbol_index<=371)) begin
                        // Snapshot y then serialize all sixteen real products
                        // through MAC_HEADER.  The input is held until the
                        // tagged pipe retires product 15, so this cannot race
                        // the next symbol even under arbitrary upstream valid.
                        header_y_re[0]<=in_re[0];header_y_im[0]<=in_im[0];
                        header_y_re[1]<=in_re[1];header_y_im[1]<=in_im[1];
                        header_z_re[0]<=0;header_z_im[0]<=0;header_z_re[1]<=0;header_z_im[1]<=0;
                        header_mac_hs<=s_symbol_index-10'd292;
                        header_mac_issue<=0;header_mac_active<=1;
                    end
                    if(is_pilot0(s_symbol_index)) begin pilot0_re[0]<=in_re[0];pilot0_im[0]<=in_im[0];pilot0_re[1]<=in_re[1];pilot0_im[1]<=in_im[1];end
                    if(is_pilot1(s_symbol_index)) begin
                        pilot1_re[0]<=in_re[0];pilot1_im[0]<=in_im[0];pilot1_re[1]<=in_re[1];pilot1_im[1]<=in_im[1];
                        pilot_mac_active<=1;pilot_mac_pending<=0;pilot_mac_entry<=0;pilot_mac_step<=0;
                        pilot_mac_update<=0;
                        pilot_corr_re<=0;pilot_corr_im<=0;pilot_hold<=1;
                    end
                    if(s_symbol_index==949) begin
                        if((task_id==528)&&(task_state==TASK_IDLE)&&(fifo_count==0)) begin
                            frame_active<=0;m_frame_complete<=1;have_previous_h<=1;
                            for(ii=0;ii<4;ii=ii+1) begin h_prev_re[ii]<=h_re[ii];h_prev_im[ii]<=h_im[ii];end
                        end
                    end
                end
            end
        end
    end

endmodule
