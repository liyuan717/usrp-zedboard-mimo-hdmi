`timescale 1ns/1ps

module mimo_gradient_2x2 (
    input  logic               row_select,
    input  logic signed [31:0] g_re [0:3],
    input  logic signed [31:0] g_im [0:3],
    input  logic signed [31:0] b_re [0:1],
    input  logic signed [31:0] b_im [0:1],
    input  logic signed [3:0]  x0_re,
    input  logic signed [3:0]  x0_im,
    input  logic signed [3:0]  x1_re,
    input  logic signed [3:0]  x1_im,
    output logic signed [63:0] grad_re,
    output logic signed [63:0] grad_im
);

    logic [1:0] base;
    logic signed [63:0] p0_re, p0_im, p1_re, p1_im;

    always_comb begin
        base = row_select ? 2 : 0;
        p0_re = 36'($signed(g_re[base]))   * 36'($signed(x0_re)) -
                36'($signed(g_im[base]))   * 36'($signed(x0_im));
        p0_im = 36'($signed(g_re[base]))   * 36'($signed(x0_im)) +
                36'($signed(g_im[base]))   * 36'($signed(x0_re));
        p1_re = 36'($signed(g_re[base+1])) * 36'($signed(x1_re)) -
                36'($signed(g_im[base+1])) * 36'($signed(x1_im));
        p1_im = 36'($signed(g_re[base+1])) * 36'($signed(x1_im)) +
                36'($signed(g_im[base+1])) * 36'($signed(x1_re));
        grad_re = p0_re + p1_re - 64'($signed(b_re[row_select]));
        grad_im = p0_im + p1_im - 64'($signed(b_im[row_select]));
    end

endmodule
