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

    logic signed [63:0] gx0_re, gx0_im, gx1_re, gx1_im;
    logic signed [63:0] quadratic;
    logic signed [63:0] linear;
    logic signed [3:0] x0_re_q, x0_im_q, x1_re_q, x1_im_q;
    logic stage_valid;

    always_comb begin
        quadratic = 64'($signed(x0_re_q)) * gx0_re +
                    64'($signed(x0_im_q)) * gx0_im +
                    64'($signed(x1_re_q)) * gx1_re +
                    64'($signed(x1_im_q)) * gx1_im;
        linear = 36'($signed(x0_re_q)) * 36'($signed(b_re[0])) +
                 36'($signed(x0_im_q)) * 36'($signed(b_im[0])) +
                 36'($signed(x1_re_q)) * 36'($signed(b_re[1])) +
                 36'($signed(x1_im_q)) * 36'($signed(b_im[1]));
    end

    always_ff @(posedge aclk) begin
        if (!aresetn) begin
            gx0_re     <= 64'sd0;
            gx0_im     <= 64'sd0;
            gx1_re     <= 64'sd0;
            gx1_im     <= 64'sd0;
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
                gx0_re <= 36'($signed(g_re[0])) * 36'($signed(x0_re)) -
                          36'($signed(g_im[0])) * 36'($signed(x0_im)) +
                          36'($signed(g_re[1])) * 36'($signed(x1_re)) -
                          36'($signed(g_im[1])) * 36'($signed(x1_im));
                gx0_im <= 36'($signed(g_re[0])) * 36'($signed(x0_im)) +
                          36'($signed(g_im[0])) * 36'($signed(x0_re)) +
                          36'($signed(g_re[1])) * 36'($signed(x1_im)) +
                          36'($signed(g_im[1])) * 36'($signed(x1_re));
                gx1_re <= 36'($signed(g_re[2])) * 36'($signed(x0_re)) -
                          36'($signed(g_im[2])) * 36'($signed(x0_im)) +
                          36'($signed(g_re[3])) * 36'($signed(x1_re)) -
                          36'($signed(g_im[3])) * 36'($signed(x1_im));
                gx1_im <= 36'($signed(g_re[2])) * 36'($signed(x0_im)) +
                          36'($signed(g_im[2])) * 36'($signed(x0_re)) +
                          36'($signed(g_re[3])) * 36'($signed(x1_im)) +
                          36'($signed(g_im[3])) * 36'($signed(x1_re));
            end

            if (stage_valid)
                cost_raw <= c_raw + quadratic - (linear <<< 1);
        end
    end

endmodule
