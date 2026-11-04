`timescale 1ns/1ps

module mimo_lcg_rng (
    input  logic        aclk,
    input  logic        aresetn,
    input  logic        load_seed,
    input  logic [31:0] seed,
    input  logic        advance,
    output logic [31:0] value
);

    // Provisional algorithm profile 0. Constants and consumption order are
    // intentionally explicit so a later paper-accurate profile is versioned.
    always_ff @(posedge aclk) begin
        if (!aresetn)
            value <= 32'h0000_0001;
        else if (load_seed)
            value <= (seed == 32'd0) ? 32'h0000_0001 : seed;
        else if (advance)
            value <= value * 32'd1664525 + 32'd1013904223;
    end

endmodule
