`timescale 1ns/1ps
// One shared, restoring divider.  It deliberately has no '/' operator in the
// datapath: channel inversion is bounded and does not infer a large
// combinational divider.  Numerator is signed, denominator is positive.
module mrf1_channel_divider #(
    parameter integer NUM_W = 80,
    parameter integer DEN_W = 64,
    parameter integer OUT_W = 32
) (
    input  logic                    clk,
    input  logic                    rst_n,
    input  logic                    start,
    input  logic signed [NUM_W-1:0] numerator,
    input  logic        [DEN_W-1:0] denominator,
    output logic                    busy,
    output logic                    done,
    output logic                    div_zero,
    output logic                    overflow,
    output logic signed [OUT_W-1:0] quotient
);
    logic sign_q;
    logic [NUM_W-1:0] dividend_q, quotient_q;
    logic [DEN_W:0] remainder_q;
    logic [DEN_W-1:0] denominator_q;
    logic [$clog2(NUM_W+1)-1:0] count_q;
    logic [DEN_W:0] shifted_remainder;
    logic [NUM_W-1:0] shifted_quotient;
    logic [NUM_W-1:0] quotient_next;
    wire [NUM_W-1:0] magnitude = numerator[NUM_W-1] ? (~numerator + 1'b1) : numerator;
    localparam logic [NUM_W-1:0] NEGATIVE_LIMIT = ({{(NUM_W-1){1'b0}},1'b1} << (OUT_W-1));
    wire quotient_overflow = sign_q ? (quotient_next > NEGATIVE_LIMIT) : (quotient_next >= NEGATIVE_LIMIT);

    always_comb begin
        shifted_remainder = {remainder_q[DEN_W-1:0], dividend_q[NUM_W-1]};
        shifted_quotient = {quotient_q[NUM_W-2:0], 1'b0};
        if (shifted_remainder >= {1'b0, denominator_q}) begin
            shifted_remainder = shifted_remainder - {1'b0, denominator_q};
            shifted_quotient[0] = 1'b1;
        end
        quotient_next = shifted_quotient;
    end

    always_ff @(posedge clk) begin
        if (!rst_n) begin
            busy <= 1'b0; done <= 1'b0; div_zero <= 1'b0; overflow <= 1'b0;
            quotient <= '0; sign_q <= 1'b0; dividend_q <= '0; quotient_q <= '0;
            remainder_q <= '0; denominator_q <= '0; count_q <= '0;
        end else begin
            done <= 1'b0;
            if (start && !busy) begin
                div_zero <= (denominator == '0);
                overflow <= 1'b0;
                quotient <= '0;
                if (denominator == '0) begin
                    done <= 1'b1;
                end else begin
                    busy <= 1'b1;
                    sign_q <= numerator[NUM_W-1];
                    // Skip only PROVEN zero leading groups. This preserves
                    // exact division for every signed numerator and arbitrary
                    // denominator, while small pilot IIR operands need 24
                    // rather than 80 clocks. No new divider or profile.
                    if(NUM_W>24 && (magnitude>>24)=='0) begin
                        dividend_q<=magnitude<<(NUM_W-24);count_q<=NUM_W-24;
                    end else if(NUM_W>40 && (magnitude>>40)=='0) begin
                        dividend_q<=magnitude<<(NUM_W-40);count_q<=NUM_W-40;
                    end else begin dividend_q<=magnitude;count_q<='0;end
                    quotient_q <= '0;
                    remainder_q <= '0;
                    denominator_q <= denominator;
                end
            end else if (busy) begin
                dividend_q <= {dividend_q[NUM_W-2:0], 1'b0};
                remainder_q <= shifted_remainder;
                quotient_q <= quotient_next;
                if (count_q == NUM_W-1) begin
                    busy <= 1'b0;
                    done <= 1'b1;
                    // Saturation is deterministic and is reported separately.
                    // Compare the complete magnitude: the previous slice
                    // missed bit NUM_W-1 and falsely overflowed exact INT_MIN.
                    overflow <= quotient_overflow;
                    if (quotient_overflow)
                        quotient <= sign_q ? {1'b1, {(OUT_W-1){1'b0}}} : {1'b0, {(OUT_W-1){1'b1}}};
                    else
                        quotient <= sign_q ? -$signed(quotient_next[OUT_W-1:0]) :
                                             $signed(quotient_next[OUT_W-1:0]);
                end else begin
                    count_q <= count_q + 1'b1;
                end
            end
        end
    end
endmodule
