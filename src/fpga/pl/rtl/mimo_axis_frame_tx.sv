`timescale 1ns/1ps

module mimo_axis_frame_tx #(
    parameter logic [31:0] BUILD_ID = 32'h0000_0001
) (
    input  logic                 aclk,
    input  logic                 aresetn,
    input  logic                 flush,

    input  logic                 result_valid,
    input  logic [31:0]          result_frame_id,
    input  logic [31:0]          result_config_id,
    input  logic [15:0]          result_status,
    input  logic [15:0]          warning_bits,
    input  logic [31:0]          actual_iterations,
    input  logic [7:0]           x_hat0,
    input  logic [7:0]           x_hat1,
    input  logic [31:0]          debug_signature,
    input  logic signed [63:0]   best_residual_raw,
    input  logic [63:0]          pl_cycles,
    input  logic [31:0]          overflow_count,
    input  logic [31:0]          saturation_count,
    input  logic [31:0]          error_detail,

    output logic                 busy,
    output logic [63:0]          m_axis_tdata,
    output logic [7:0]           m_axis_tkeep,
    output logic                 m_axis_tvalid,
    input  logic                 m_axis_tready,
    output logic                 m_axis_tlast
);

    localparam logic [31:0] OUTPUT_MAGIC = 32'h3050_4F4D; // "MOP0"

    logic [2:0] beat_index;
    logic [31:0] frame_id_q, config_id_q;
    logic [15:0] status_q, warning_q;
    logic [31:0] iterations_q, debug_q;
    logic [7:0]  x0_q, x1_q;
    logic signed [63:0] residual_q;
    logic [63:0] cycles_q;
    logic [31:0] overflow_q, saturation_q, error_detail_q;

    // flush is a local cancellation: the system must also recover/reset the
    // receiving DMA when an already partially transmitted frame is discarded.
    assign m_axis_tvalid = busy && aresetn && !flush;
    assign m_axis_tkeep  = 8'hFF;
    assign m_axis_tlast  = m_axis_tvalid && (beat_index == 3'd7);

    always_comb begin
        case (beat_index)
            3'd0: m_axis_tdata = {8'd8, 8'd2, 16'd1, OUTPUT_MAGIC};
            3'd1: m_axis_tdata = {config_id_q, frame_id_q};
            3'd2: m_axis_tdata = {iterations_q, warning_q, status_q};
            3'd3: m_axis_tdata = {debug_q, 16'd0, x1_q, x0_q};
            3'd4: m_axis_tdata = $unsigned(residual_q);
            3'd5: m_axis_tdata = cycles_q;
            3'd6: m_axis_tdata = {saturation_q, overflow_q};
            3'd7: m_axis_tdata = {BUILD_ID, error_detail_q};
            default: m_axis_tdata = 64'd0;
        endcase
    end

    always_ff @(posedge aclk) begin
        if (!aresetn) begin
            busy           <= 1'b0;
            beat_index     <= 3'd0;
            frame_id_q     <= 32'd0;
            config_id_q    <= 32'd0;
            status_q       <= 16'd0;
            warning_q      <= 16'd0;
            iterations_q   <= 32'd0;
            x0_q           <= 8'd0;
            x1_q           <= 8'd0;
            debug_q        <= 32'd0;
            residual_q     <= 64'sd0;
            cycles_q       <= 64'd0;
            overflow_q     <= 32'd0;
            saturation_q   <= 32'd0;
            error_detail_q <= 32'd0;
        end else if (flush) begin
            busy       <= 1'b0;
            beat_index <= 3'd0;
        end else begin
            // Allow replacement exactly when the previous final beat transfers.
            // Other busy cycles still require the producer to wait; no FIFO.
            if (result_valid && (!busy || (m_axis_tvalid && m_axis_tready && m_axis_tlast))) begin
                busy           <= 1'b1;
                beat_index     <= 3'd0;
                frame_id_q     <= result_frame_id;
                config_id_q    <= result_config_id;
                status_q       <= result_status;
                warning_q      <= warning_bits;
                iterations_q   <= actual_iterations;
                x0_q           <= x_hat0;
                x1_q           <= x_hat1;
                debug_q        <= debug_signature;
                residual_q     <= best_residual_raw;
                cycles_q       <= pl_cycles;
                overflow_q     <= overflow_count;
                saturation_q   <= saturation_count;
                error_detail_q <= error_detail;
            end else if (m_axis_tvalid && m_axis_tready) begin
                if (beat_index == 3'd7) begin
                    busy       <= 1'b0;
                    beat_index <= 3'd0;
                end else begin
                    beat_index <= beat_index + 3'd1;
                end
            end
        end
    end

endmodule
