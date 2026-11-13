`timescale 1ns/1ps

module mimo_cost_2x2 (
    input  logic               aclk,
    input  logic               aresetn,
    input  logic               start,
    input  logic signed [31:0] g_re [0:3],
    input  logic signed [31:0] g_im [0:3],
    input  logic signed [31:0] b_re [0:1],
    input  logic signed [31:0] b_im [0:1],
    input  logic signed [63:0] c_raw,
    input  logic signed [3:0]  x0_re,
    input  logic signed [3:0]  x0_im,
    input  logic signed [3:0]  x1_re,
    input  logic signed [3:0]  x1_im,
    output logic               valid,
    output logic signed [63:0] cost_raw
);

    // Full input range, not a QPSK-only assumption: signed 32x4 products
    // need 36 bits. Four such terms need at most 38 bits. Multiplying
    // those by signed 4-bit candidates and summing four needs 44 bits.
    // Keep products explicit so expression context cannot widen each
    // multiplier to the final 64-bit cost width. No rounding/saturation.
    logic signed [37:0] gx0_re, gx0_im, gx1_re, gx1_im;
    logic signed [43:0] quadratic;
    logic signed [37:0] linear;
    logic signed [3:0] x0_re_q, x0_im_q, x1_re_q, x1_im_q;
    logic stage_valid;
    // Resource mapping only: keep full signed products and the existing
    // two-edge/II=1 contract. Do not add DSP pipeline registers here.
    (* use_dsp = "yes" *) wire signed [41:0] q0 = x0_re_q * gx0_re;
    (* use_dsp = "yes" *) wire signed [41:0] q1 = x0_im_q * gx0_im;
    (* use_dsp = "yes" *) wire signed [41:0] q2 = x1_re_q * gx1_re;
    (* use_dsp = "yes" *) wire signed [41:0] q3 = x1_im_q * gx1_im;
    (* use_dsp = "yes" *) wire signed [35:0] l0 = x0_re_q * b_re[0];
    (* use_dsp = "yes" *) wire signed [35:0] l1 = x0_im_q * b_im[0];
    (* use_dsp = "yes" *) wire signed [35:0] l2 = x1_re_q * b_re[1];
    (* use_dsp = "yes" *) wire signed [35:0] l3 = x1_im_q * b_im[1];
    (* use_dsp = "yes" *) wire signed [35:0] gr_xr [0:3];
    (* use_dsp = "yes" *) wire signed [35:0] gi_xi [0:3];
    (* use_dsp = "yes" *) wire signed [35:0] gr_xi [0:3];
    (* use_dsp = "yes" *) wire signed [35:0] gi_xr [0:3];
    for (genvar k=0; k<4; k++) begin: gram_products
        wire signed [3:0] xr = (k%2 == 0) ? x0_re : x1_re;
        wire signed [3:0] xi = (k%2 == 0) ? x0_im : x1_im;
        assign gr_xr[k] = g_re[k] * xr;
        assign gi_xi[k] = g_im[k] * xi;
        assign gr_xi[k] = g_re[k] * xi;
        assign gi_xr[k] = g_im[k] * xr;
    end
    wire signed [63:0] quadratic64 = {{20{quadratic[43]}},quadratic};
    wire signed [63:0] linear64 = {{26{linear[37]}},linear};

    always_comb begin
        quadratic = 44'($signed(q0)) + 44'($signed(q1)) +
                    44'($signed(q2)) + 44'($signed(q3));
        linear = 38'($signed(l0)) + 38'($signed(l1)) +
                 38'($signed(l2)) + 38'($signed(l3));
    end

    always_ff @(posedge aclk) begin
        if (!aresetn) begin
            gx0_re     <= 38'sd0;
            gx0_im     <= 38'sd0;
            gx1_re     <= 38'sd0;
            gx1_im     <= 38'sd0;
            x0_re_q    <= 4'sd0;
            x0_im_q    <= 4'sd0;
            x1_re_q    <= 4'sd0;
            x1_im_q    <= 4'sd0;
            stage_valid<= 1'b0;
            valid      <= 1'b0;
            cost_raw   <= 64'sd0;
        end else begin
            valid       <= stage_valid;
            stage_valid <= start;

            if (start) begin
                x0_re_q <= x0_re;
                x0_im_q <= x0_im;
                x1_re_q <= x1_re;
                x1_im_q <= x1_im;
                gx0_re <= 38'($signed(gr_xr[0])) - 38'($signed(gi_xi[0])) +
                          38'($signed(gr_xr[1])) - 38'($signed(gi_xi[1]));
                gx0_im <= 38'($signed(gr_xi[0])) + 38'($signed(gi_xr[0])) +
                          38'($signed(gr_xi[1])) + 38'($signed(gi_xr[1]));
                gx1_re <= 38'($signed(gr_xr[2])) - 38'($signed(gi_xi[2])) +
                          38'($signed(gr_xr[3])) - 38'($signed(gi_xi[3]));
                gx1_im <= 38'($signed(gr_xi[2])) + 38'($signed(gi_xr[2])) +
                          38'($signed(gr_xi[3])) + 38'($signed(gi_xr[3]));
            end

            if (stage_valid)
                // Extend BEFORE shifting, preserving the original 64-bit
                // final wrap semantics even for extreme c_raw values.
                cost_raw <= c_raw + quadratic64 - (linear64 <<< 1);
        end
    end

endmodule
