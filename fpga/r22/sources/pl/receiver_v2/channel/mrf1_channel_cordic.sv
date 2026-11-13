`timescale 1ns/1ps
// Fixed 16+16-cycle CORDIC.  Input is a signed Q3.29 pilot correlation.
// Outputs are Q1.15 cos(phi), sin(-phi), directly suitable for y*exp(-jphi).
module mrf1_channel_cordic (
    input  logic clk,
    input  logic rst_n,
    input  logic start,
    input  logic signed [31:0] correlation_re,
    input  logic signed [31:0] correlation_im,
    output logic busy,
    output logic valid,
    output logic signed [31:0] phase_q29,
    output logic signed [15:0] cos_q15,
    output logic signed [15:0] sin_neg_q15
);
    localparam logic signed [31:0] PI_Q29 = 32'sd1686629713;
    localparam logic signed [31:0] HALF_PI_Q29 = 32'sd843314857;
    localparam logic signed [47:0] K_Q30 = 48'sd652032874;
    typedef enum logic [1:0] {IDLE, VECTOR, ROTATE} state_t;
    state_t state;
    logic [4:0] iter;
    logic signed [47:0] x, y;
    logic signed [31:0] z;
    logic signed [47:0] nx, ny;
    logic signed [31:0] nz;

    function automatic logic signed [31:0] atan_q29(input logic [4:0] i);
        case (i)
            0: atan_q29=32'sd421657428;  1: atan_q29=32'sd248918915;
            2: atan_q29=32'sd131521918;  3: atan_q29=32'sd66762579;
            4: atan_q29=32'sd33510843;   5: atan_q29=32'sd16771732;
            6: atan_q29=32'sd8387925;    7: atan_q29=32'sd4194219;
            8: atan_q29=32'sd2097141;    9: atan_q29=32'sd1048575;
            10: atan_q29=32'sd524288;    11: atan_q29=32'sd262144;
            12: atan_q29=32'sd131072;    13: atan_q29=32'sd65536;
            14: atan_q29=32'sd32768;     default: atan_q29=32'sd16384;
        endcase
    endfunction
    function automatic logic signed [15:0] q30_to_q15(input logic signed [47:0] value);
        logic signed [47:0] scaled;
        begin
            scaled=value >>> 15;
            if(scaled>32767)q30_to_q15=16'sh7fff;
            else if(scaled< -32768)q30_to_q15=16'sh8000;
            else q30_to_q15=scaled[15:0];
        end
    endfunction

    always_comb begin
        nx = x; ny = y; nz = z;
        if (state == VECTOR) begin
            if (y >= 0) begin nx = x + (y >>> iter); ny = y - (x >>> iter); nz = z + atan_q29(iter); end
            else        begin nx = x - (y >>> iter); ny = y + (x >>> iter); nz = z - atan_q29(iter); end
        end else if (state == ROTATE) begin
            if (z >= 0) begin nx = x - (y >>> iter); ny = y + (x >>> iter); nz = z - atan_q29(iter); end
            else        begin nx = x + (y >>> iter); ny = y - (x >>> iter); nz = z + atan_q29(iter); end
        end
    end

    always_ff @(posedge clk) begin
        if (!rst_n) begin
            state <= IDLE; iter <= '0; x <= '0; y <= '0; z <= '0; phase_q29 <= '0;
            busy <= 1'b0; valid <= 1'b0; cos_q15 <= '0; sin_neg_q15 <= '0;
        end else begin
            valid <= 1'b0;
            case (state)
                IDLE: if (start) begin
                    busy <= 1'b1; iter <= 5'd0;
                    // Bring atan2 into CORDIC's convergence half-plane.
                    if (correlation_re < 0) begin
                        x <= -{{16{correlation_re[31]}},correlation_re};
                        y <= -{{16{correlation_im[31]}},correlation_im};
                        z <= (correlation_im >= 0) ? PI_Q29 : -PI_Q29;
                    end else begin
                        x <= {{16{correlation_re[31]}},correlation_re};
                        y <= {{16{correlation_im[31]}},correlation_im};
                        z <= '0;
                    end
                    state <= VECTOR;
                end
                VECTOR: begin
                    x <= nx; y <= ny; z <= nz;
                    if (iter == 5'd15) begin
                        phase_q29 <= nz;
                        // Rotation mode converges only in the +/-pi/2
                        // half-plane. A negative starting vector supplies
                        // the missing pi for second/third quadrant phases.
                        y <= '0;
                        if(nz>HALF_PI_Q29) begin x<=-K_Q30;z<=PI_Q29-nz;end
                        else if(nz< -HALF_PI_Q29) begin x<=-K_Q30;z<=-PI_Q29-nz;end
                        else begin x<=K_Q30;z<=-nz;end
                        iter <= 5'd0; state <= ROTATE;
                    end else iter <= iter + 1'b1;
                end
                ROTATE: begin
                    x <= nx; y <= ny; z <= nz;
                    if (iter == 5'd15) begin
                        cos_q15 <= q30_to_q15(nx);
                        sin_neg_q15 <= q30_to_q15(ny);
                        valid <= 1'b1; busy <= 1'b0; state <= IDLE;
                    end else iter <= iter + 1'b1;
                end
                default: state <= IDLE;
            endcase
        end
    end
endmodule
