`timescale 1ns/1ps

module mimo_qam_stepper (
    input  logic [7:0]        modulation,
    input  logic signed [3:0] level_in,
    input  logic signed [63:0] gradient,
    output logic signed [3:0] level_out
);

    always_comb begin
        level_out = level_in;
        if (gradient > 0) begin
            if (modulation == 8'd1)
                level_out = -4'sd1;
            else begin
                case (level_in)
                    4'sd3:   level_out = 4'sd1;
                    4'sd1:   level_out = -4'sd1;
                    -4'sd1:  level_out = -4'sd3;
                    default: level_out = -4'sd3;
                endcase
            end
        end else if (gradient < 0) begin
            if (modulation == 8'd1)
                level_out = 4'sd1;
            else begin
                case (level_in)
                    -4'sd3:  level_out = -4'sd1;
                    -4'sd1:  level_out = 4'sd1;
                    4'sd1:   level_out = 4'sd3;
                    default: level_out = 4'sd3;
                endcase
            end
        end
    end

endmodule
