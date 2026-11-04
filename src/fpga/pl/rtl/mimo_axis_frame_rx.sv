`timescale 1ns/1ps

module mimo_axis_frame_rx (
    input  logic                 aclk,
    input  logic                 aresetn,
    input  logic                 arm,
    input  logic                 flush,

    input  logic [63:0]          s_axis_tdata,
    input  logic [7:0]           s_axis_tkeep,
    input  logic                 s_axis_tvalid,
    output logic                 s_axis_tready,
    input  logic                 s_axis_tlast,

    output logic                 receiving,
    output logic                 frame_valid,
    output logic                 frame_error,
    output logic [15:0]          error_code,

    output logic [31:0]          frame_id,
    output logic [31:0]          config_id,
    output logic [7:0]           modulation,
    output logic [15:0]          flags,
    output logic [15:0]          ns,
    output logic [31:0]          seed,
    output logic [15:0]          fixed_profile_id,
    output logic [15:0]          algorithm_profile_id,
    output logic [15:0]          min_iterations,
    output logic [15:0]          max_iterations,
    output logic signed [31:0]   stop_threshold_raw,
    output logic signed [31:0]   noise_variance_raw,
    output logic signed [31:0]   proposal_step_raw,
    output logic signed [31:0]   g_re [0:3],
    output logic signed [31:0]   g_im [0:3],
    output logic signed [31:0]   b_re [0:1],
    output logic signed [31:0]   b_im [0:1],
    output logic signed [63:0]   c_raw
);

    localparam logic [31:0] INPUT_MAGIC = 32'h3050_494D; // "MIP0"
    localparam logic [15:0] ERR_KEEP     = 16'h0101;
    localparam logic [15:0] ERR_TLAST    = 16'h0102;
    localparam logic [15:0] ERR_MAGIC    = 16'h0103;
    localparam logic [15:0] ERR_VERSION  = 16'h0104;
    localparam logic [15:0] ERR_CONFIG   = 16'h0105;
    localparam logic [15:0] ERR_LENGTH   = 16'h0106;
    localparam logic [15:0] ERR_RESERVED = 16'h0107;

    logic [3:0] beat_index;
    logic       invalid_seen;
    logic [15:0] first_error;
    logic [15:0] beat_error;

    assign s_axis_tready = receiving && aresetn && !flush;

    // Pick exactly one error per beat, then retain the earliest bad beat.
    // Within a beat: KEEP > TLAST > payload (MAGIC before VERSION).
    always_comb begin
        beat_error = 16'd0;
        if (s_axis_tkeep != 8'hFF)
            beat_error = ERR_KEEP;
        else if (s_axis_tlast != (beat_index == 4'd14))
            beat_error = ERR_TLAST;
        else case (beat_index)
            4'd0: begin
                if (s_axis_tdata[31:0] != INPUT_MAGIC)
                    beat_error = ERR_MAGIC;
                else if ((s_axis_tdata[47:32] != 16'd1) ||
                         (s_axis_tdata[55:48] != 8'd1) ||
                         (s_axis_tdata[63:56] != 8'd8))
                    beat_error = ERR_VERSION;
            end
            4'd2: begin
                if ((s_axis_tdata[7:0] != 8'd2) ||
                    (s_axis_tdata[15:8] != 8'd2) ||
                    ((s_axis_tdata[23:16] != 8'd1) && (s_axis_tdata[23:16] != 8'd2)) ||
                    (s_axis_tdata[31:24] != 8'd3) ||
                    (s_axis_tdata[47:32] == 16'd0) || (s_axis_tdata[63:48] != 16'd0))
                    beat_error = ERR_CONFIG;
            end
            4'd3: begin
                if ((s_axis_tdata[31:0] == 32'd0) ||
                    (s_axis_tdata[47:32] != 16'd0) || (s_axis_tdata[63:48] != 16'd0))
                    beat_error = ERR_CONFIG;
            end
            4'd4: begin
                if ((s_axis_tdata[15:0] != 16'd15) ||
                    (s_axis_tdata[31:16] != 16'd8) ||
                    (s_axis_tdata[47:32] == 16'd0) ||
                    (s_axis_tdata[63:48] < s_axis_tdata[47:32]) ||
                    (s_axis_tdata[63:48] > ns))
                    beat_error = ERR_LENGTH;
            end
            4'd5: if (s_axis_tdata[63:32] != 32'd0) beat_error = ERR_RESERVED;
            4'd7: if (s_axis_tdata != 64'd0) beat_error = ERR_RESERVED;
            default: begin end
        endcase
    end

    always_ff @(posedge aclk) begin
        if (!aresetn) begin
            receiving           <= 1'b0;
            frame_valid         <= 1'b0;
            frame_error         <= 1'b0;
            error_code          <= 16'h0000;
            beat_index          <= 4'd0;
            invalid_seen        <= 1'b0;
            first_error         <= 16'h0000;
            frame_id            <= 32'd0;
            config_id           <= 32'd0;
            modulation          <= 8'd0;
            flags               <= 16'd0;
            ns                  <= 16'd0;
            seed                <= 32'd0;
            fixed_profile_id    <= 16'd0;
            algorithm_profile_id<= 16'd0;
            min_iterations      <= 16'd0;
            max_iterations      <= 16'd0;
            stop_threshold_raw  <= 32'sd0;
            noise_variance_raw  <= 32'sd0;
            proposal_step_raw   <= 32'sd0;
            c_raw               <= 64'sd0;
            for (int i = 0; i < 4; i++) begin
                g_re[i] <= 32'sd0;
                g_im[i] <= 32'sd0;
            end
            for (int i = 0; i < 2; i++) begin
                b_re[i] <= 32'sd0;
                b_im[i] <= 32'sd0;
            end
        end else begin
            frame_valid <= 1'b0;
            frame_error <= 1'b0;

            if (flush) begin
                receiving    <= 1'b0;
                beat_index   <= 4'd0;
                invalid_seen <= 1'b0;
                first_error  <= 16'h0000;
                error_code   <= 16'h0000;
            end else if (arm && !receiving) begin
                receiving    <= 1'b1;
                beat_index   <= 4'd0;
                invalid_seen <= 1'b0;
                first_error  <= 16'h0000;
                error_code   <= 16'h0000;
                // A short new frame must never inherit the previous task IDs.
                frame_id     <= 32'd0;
                config_id    <= 32'd0;
            end else if (s_axis_tvalid && s_axis_tready) begin
                if (!invalid_seen && beat_error != 16'd0) begin
                    invalid_seen <= 1'b1;
                    first_error <= beat_error;
                end

                case (beat_index)
                    4'd0: begin end
                    4'd1: begin
                        frame_id  <= s_axis_tdata[31:0];
                        config_id <= s_axis_tdata[63:32];
                    end
                    4'd2: begin
                        modulation <= s_axis_tdata[23:16];
                        ns         <= s_axis_tdata[47:32];
                        flags      <= s_axis_tdata[63:48];
                    end
                    4'd3: begin
                        seed                 <= s_axis_tdata[31:0];
                        fixed_profile_id     <= s_axis_tdata[47:32];
                        algorithm_profile_id <= s_axis_tdata[63:48];
                    end
                    4'd4: begin
                        min_iterations <= s_axis_tdata[47:32];
                        max_iterations <= s_axis_tdata[63:48];
                    end
                    4'd5: begin
                        stop_threshold_raw <= $signed(s_axis_tdata[31:0]);
                    end
                    4'd6: begin
                        noise_variance_raw <= $signed(s_axis_tdata[31:0]);
                        proposal_step_raw  <= $signed(s_axis_tdata[63:32]);
                    end
                    4'd7: begin end
                    4'd8, 4'd9, 4'd10, 4'd11: begin
                        g_re[beat_index-4'd8] <= $signed(s_axis_tdata[31:0]);
                        g_im[beat_index-4'd8] <= $signed(s_axis_tdata[63:32]);
                    end
                    4'd12, 4'd13: begin
                        b_re[beat_index-4'd12] <= $signed(s_axis_tdata[31:0]);
                        b_im[beat_index-4'd12] <= $signed(s_axis_tdata[63:32]);
                    end
                    4'd14: c_raw <= $signed(s_axis_tdata);
                    default: begin end
                endcase

                if (s_axis_tlast && beat_index != 4'd14) begin
                    receiving  <= 1'b0;
                    frame_error<= 1'b1;
                    error_code <= invalid_seen ? first_error : beat_error;
                    beat_index <= 4'd0;
                end else if (beat_index == 4'd14) begin
                    receiving <= 1'b0;
                    if (invalid_seen || beat_error != 16'd0) begin
                        frame_error <= 1'b1;
                        error_code  <= invalid_seen ? first_error : beat_error;
                    end else begin
                        frame_valid <= 1'b1;
                    end
                    beat_index <= 4'd0;
                end else begin
                    beat_index <= beat_index + 4'd1;
                end
            end
        end
    end

endmodule
