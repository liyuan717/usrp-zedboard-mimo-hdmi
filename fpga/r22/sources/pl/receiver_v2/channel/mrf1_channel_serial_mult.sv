`timescale 1ns/1ps
// Fixed-latency signed shift/add multiplier.  This is intentionally a
// serial lane: no '*' appears in the datapath. The partial product shifts
// right through high/low halves, so only a W+1-bit adder is needed instead
// of adding two 2W-bit words every cycle. Exact signed result and W-cycle
// service latency are unchanged, including two's-complement minimum inputs.
module mrf1_channel_serial_mult #(
    parameter integer W = 64
) (
    input  logic                   clk,
    input  logic                   rst_n,
    input  logic                   start,
    input  logic signed [W-1:0]    a,
    input  logic signed [W-1:0]    b,
    output logic                   busy,
    output logic                   done,
    output logic signed [2*W-1:0]  product
);
    logic [W-1:0] multiplier;
    logic [W-1:0] multiplicand, accumulator;
    logic sign_q;
    logic [$clog2(W+1)-1:0] count;
    logic [W:0] high_sum;
    logic [2*W-1:0] shifted_product;

    always_comb begin
        high_sum={1'b0,accumulator}+(multiplier[0] ? {1'b0,multiplicand} : {(W+1){1'b0}});
        shifted_product={high_sum,multiplier}>>1;
    end

    always_ff @(posedge clk) begin
        if(!rst_n) begin
            busy<=0; done<=0; product<='0; multiplier<='0;
            multiplicand<='0; accumulator<='0; sign_q<=0; count<='0;
        end else begin
            done<=0;
            if(start && !busy) begin
                busy<=1; count<='0; accumulator<='0;
                // Two's-complement magnitude is kept unsigned for the
                // shift/add loop; sign is restored only on retirement.
                multiplier <= b[W-1] ? (~b+1'b1) : b;
                multiplicand <= a[W-1] ? (~a+1'b1) : a;
                sign_q<=a[W-1]^b[W-1];
            end else if(busy) begin
                accumulator<=shifted_product[2*W-1:W];
                multiplier<=shifted_product[W-1:0];
                if(count==W-1) begin
                    product<=sign_q ? -$signed(shifted_product) : $signed(shifted_product);
                    busy<=0; done<=1;
                end else count<=count+1'b1;
            end
        end
    end
endmodule
