`timescale 1ns/1ps

// Experimental batch-envelope AXIS wrapper for the isolated interleaved MHGD
// candidate.  It is deliberately not connected to the deployed detector IP.
//
// batch_count==1 keeps the legacy 15-beat input / 8-beat output TLAST
// behaviour.  For batch_count>1, a simple-DMA transaction carries N fixed
// input records and N fixed output records: only the transaction's final beat
// has TLAST.  The fixed 15/8 record boundaries remain internal.
module mimo_mhgd_interleaved_axis_candidate #(
    parameter int MAX_CONTEXTS = 10,
    parameter int MAX_BATCH_TASKS = 528,
    parameter logic [31:0] BUILD_ID = 32'h0000_0001,
    parameter int PROPOSAL_POLICY = 0
) (
    input  logic                 aclk,
    input  logic                 aresetn,
    input  logic                 batch_start,
    input  logic [15:0]          batch_count,
    input  logic                 batch_cancel,

    input  logic [63:0]          s_axis_tdata,
    input  logic [7:0]           s_axis_tkeep,
    input  logic                 s_axis_tvalid,
    output logic                 s_axis_tready,
    input  logic                 s_axis_tlast,

    output logic [63:0]          m_axis_tdata,
    output logic [7:0]           m_axis_tkeep,
    output logic                 m_axis_tvalid,
    input  logic                 m_axis_tready,
    output logic                 m_axis_tlast,

    output logic                 batch_busy,
    output logic                 batch_done,
    output logic                 batch_error,
    output logic [15:0]          batch_error_code,
    output logic [15:0]          accepted_tasks,
    output logic [15:0]          emitted_tasks
);

    localparam int CREDIT_W = $clog2(MAX_CONTEXTS + 1);
    localparam logic [31:0] INPUT_MAGIC = 32'h3050_494D; // "MIP0"
    localparam logic [15:0] STATUS_OK = 16'h0000;
    localparam logic [15:0] STATUS_STREAM_ERROR = 16'h0005;
    localparam logic [15:0] ERR_KEEP = 16'h0101;
    localparam logic [15:0] ERR_TLAST = 16'h0102;
    localparam logic [15:0] ERR_MAGIC = 16'h0103;
    localparam logic [15:0] ERR_VERSION = 16'h0104;
    localparam logic [15:0] ERR_CONFIG = 16'h0105;
    localparam logic [15:0] ERR_LENGTH = 16'h0106;
    localparam logic [15:0] ERR_RESERVED = 16'h0107;
    localparam logic [15:0] ERR_BATCH = 16'h0201;

    logic batch_active, input_closed, pending_valid, error_pending;
    logic [15:0] batch_count_q, input_count, serialized_count;
    logic [CREDIT_W-1:0] outstanding;
    logic [3:0] task_beat;
    logic invalid_seen;
    logic [15:0] first_error, beat_error;

    logic [31:0] frame_id_q, config_id_q;
    logic [7:0] modulation_q;
    logic [15:0] flags_q, ns_q;
    logic [31:0] seed_q;
    logic [15:0] fixed_profile_id_q, algorithm_profile_id_q;
    logic [15:0] min_iterations_q, max_iterations_q;
    logic signed [31:0] stop_threshold_raw_q, noise_variance_raw_q, proposal_step_raw_q;
    logic signed [31:0] g_re_q [0:3], g_im_q [0:3];
    logic signed [31:0] b_re_q [0:1], b_im_q [0:1];
    logic signed [63:0] c_raw_q;

    logic [31:0] pending_tag, pending_config;
    logic [7:0] pending_modulation;
    logic [15:0] pending_max_iterations;
    logic [31:0] pending_seed;
    logic signed [31:0] pending_proposal_step_raw;
    logic signed [31:0] pending_g_re [0:3], pending_g_im [0:3];
    logic signed [31:0] pending_b_re [0:1], pending_b_im [0:1];
    logic signed [63:0] pending_c_raw;

    logic core_input_ready, core_result_valid, core_result_ready, core_busy;
    logic [31:0] core_result_tag, core_result_config, core_actual_iterations, core_overflow, core_saturation, core_debug;
    logic [7:0] core_x0, core_x1;
    logic signed [63:0] core_cost;
    logic [63:0] core_cycles;
    logic [15:0] core_warning;
    logic core_submit;

    logic [31:0] error_frame_id, error_config_id;
    logic tx_busy, tx_tvalid, tx_tlast, tx_frame_final, tx_frame_is_error;
    logic [63:0] tx_tdata;
    logic [7:0] tx_tkeep;
    logic tx_can_accept, tx_result_valid, tx_accept, tx_pop;
    logic flush_datapath;

    // The outer TLAST denotes the simple-DMA transaction boundary.  A task
    // slot still has its fixed 15-beat structure, but only the last slot may
    // end the AXIS transaction.
    always_comb begin
        beat_error = 16'd0;
        if (s_axis_tkeep != 8'hFF)
            beat_error = ERR_KEEP;
        else if (s_axis_tlast != ((task_beat == 4'd14) && ((input_count + 16'd1) == batch_count_q)))
            beat_error = ERR_TLAST;
        else case (task_beat)
            4'd0: begin
                if (s_axis_tdata[31:0] != INPUT_MAGIC)
                    beat_error = ERR_MAGIC;
                else if ((s_axis_tdata[47:32] != 16'd1) ||
                         (s_axis_tdata[55:48] != 8'd1) ||
                         (s_axis_tdata[63:56] != 8'd8))
                    beat_error = ERR_VERSION;
            end
            4'd2: begin
                if ((s_axis_tdata[7:0] != 8'd2) || (s_axis_tdata[15:8] != 8'd2) ||
                    ((s_axis_tdata[23:16] != 8'd1) && (s_axis_tdata[23:16] != 8'd2)) ||
                    (s_axis_tdata[31:24] != 8'd3) || (s_axis_tdata[47:32] == 16'd0) ||
                    (s_axis_tdata[63:48] != 16'd0))
                    beat_error = ERR_CONFIG;
            end
            4'd3: begin
                if ((s_axis_tdata[31:0] == 32'd0) || (s_axis_tdata[47:32] != 16'd0) ||
                    (s_axis_tdata[63:48] != 16'd0))
                    beat_error = ERR_CONFIG;
            end
            4'd4: begin
                if ((s_axis_tdata[15:0] != 16'd15) || (s_axis_tdata[31:16] != 16'd8) ||
                    (s_axis_tdata[47:32] == 16'd0) || (s_axis_tdata[63:48] < s_axis_tdata[47:32]) ||
                    (s_axis_tdata[63:48] > ns_q))
                    beat_error = ERR_LENGTH;
            end
            4'd5: if (s_axis_tdata[63:32] != 32'd0) beat_error = ERR_RESERVED;
            4'd7: if (s_axis_tdata != 64'd0) beat_error = ERR_RESERVED;
            default: begin end
        endcase
    end

    // A task cannot begin unless a credit remains until its complete 64-byte
    // result is externally transferred.  This is what makes a stalled S2MM
    // bounded; the candidate's internal result ring is never asked to absorb
    // more than MAX_CONTEXTS outstanding tasks.
    assign s_axis_tready = aresetn && batch_active && !batch_error && !input_closed &&
                           !pending_valid && (outstanding < MAX_CONTEXTS);
    assign core_submit = pending_valid && core_input_ready && batch_active && !batch_error;
    assign flush_datapath = batch_cancel || (!batch_active && !error_pending) || batch_error;

    assign tx_can_accept = !tx_busy || (tx_tvalid && m_axis_tready && tx_tlast);
    assign tx_result_valid = batch_active && (error_pending || core_result_valid);
    assign core_result_ready = batch_active && !batch_error && !error_pending && tx_can_accept;
    assign tx_accept = tx_result_valid && tx_can_accept;
    assign tx_pop = tx_tvalid && m_axis_tready && tx_tlast;

    assign m_axis_tdata = tx_tdata;
    assign m_axis_tkeep = tx_tkeep;
    assign m_axis_tvalid = tx_tvalid && batch_active && !batch_error;
    assign m_axis_tlast = m_axis_tvalid && tx_tlast && tx_frame_final;
    assign batch_busy = batch_active || pending_valid || core_busy || tx_busy || error_pending;

    mimo_mhgd_interleaved_candidate #(
        .MAX_CONTEXTS(MAX_CONTEXTS), .PROPOSAL_POLICY(PROPOSAL_POLICY)
    ) u_core (
        .aclk(aclk), .aresetn(aresetn), .flush(flush_datapath),
        .input_valid(pending_valid && batch_active && !batch_error), .input_ready(core_input_ready),
        .input_tag(pending_tag), .input_config(pending_config), .modulation(pending_modulation), .flags(16'd0),
        .max_iterations(pending_max_iterations), .seed(pending_seed),
        .proposal_step_raw(pending_proposal_step_raw), .g_re(pending_g_re), .g_im(pending_g_im),
        .b_re(pending_b_re), .b_im(pending_b_im), .c_raw(pending_c_raw),
        .result_valid(core_result_valid), .result_ready(core_result_ready), .result_tag(core_result_tag), .result_config(core_result_config),
        .x_hat0(core_x0), .x_hat1(core_x1), .best_residual_raw(core_cost),
        .actual_iterations(core_actual_iterations), .pl_cycles(core_cycles),
        .overflow_count(core_overflow), .saturation_count(core_saturation),
        .warning_bits(core_warning), .debug_signature(core_debug), .busy(core_busy)
    );

    // Its internal TLAST remains the eight-beat result-slot boundary.  The
    // wrapper deliberately exposes TLAST only for the final slot in a batch.
    mimo_axis_frame_tx #(.BUILD_ID(BUILD_ID)) u_tx (
        .aclk(aclk), .aresetn(aresetn), .flush(flush_datapath),
        .result_valid(tx_result_valid),
        .result_frame_id(error_pending ? error_frame_id : core_result_tag),
        .result_config_id(error_pending ? error_config_id : core_result_config),
        .result_status(error_pending ? STATUS_STREAM_ERROR : STATUS_OK),
        .warning_bits(error_pending ? 16'd0 : core_warning),
        .actual_iterations(error_pending ? 32'd0 : core_actual_iterations),
        .x_hat0(error_pending ? 8'd0 : core_x0), .x_hat1(error_pending ? 8'd0 : core_x1),
        .debug_signature(error_pending ? 32'd0 : core_debug),
        .best_residual_raw(error_pending ? 64'sd0 : core_cost),
        .pl_cycles(error_pending ? 64'd0 : core_cycles),
        .overflow_count(error_pending ? 32'd0 : core_overflow),
        .saturation_count(error_pending ? 32'd0 : core_saturation),
        .error_detail(error_pending ? {16'd0, batch_error_code} : 32'd0),
        .busy(tx_busy), .m_axis_tdata(tx_tdata), .m_axis_tkeep(tx_tkeep),
        .m_axis_tvalid(tx_tvalid), .m_axis_tready(m_axis_tready), .m_axis_tlast(tx_tlast)
    );

    always_ff @(posedge aclk) begin
        if (!aresetn) begin
            batch_active <= 1'b0; input_closed <= 1'b0; pending_valid <= 1'b0;
            error_pending <= 1'b0; batch_done <= 1'b0; batch_error <= 1'b0;
            batch_error_code <= 16'd0; batch_count_q <= 16'd0; input_count <= 16'd0;
            serialized_count <= 16'd0; outstanding <= '0; task_beat <= 4'd0;
            invalid_seen <= 1'b0; first_error <= 16'd0; accepted_tasks <= 16'd0; emitted_tasks <= 16'd0;
            frame_id_q <= 32'd0; config_id_q <= 32'd0; modulation_q <= 8'd0; flags_q <= 16'd0; ns_q <= 16'd0;
            seed_q <= 32'd0; fixed_profile_id_q <= 16'd0; algorithm_profile_id_q <= 16'd0;
            min_iterations_q <= 16'd0; max_iterations_q <= 16'd0;
            stop_threshold_raw_q <= 32'sd0; noise_variance_raw_q <= 32'sd0; proposal_step_raw_q <= 32'sd0; c_raw_q <= 64'sd0;
            pending_tag <= 32'd0; pending_config <= 32'd0; pending_modulation <= 8'd0;
            pending_max_iterations <= 16'd0; pending_seed <= 32'd0; pending_proposal_step_raw <= 32'sd0; pending_c_raw <= 64'sd0;
            error_frame_id <= 32'd0; error_config_id <= 32'd0; tx_frame_final <= 1'b0; tx_frame_is_error <= 1'b0;
            for (int i = 0; i < 4; i++) begin
                g_re_q[i] <= 32'sd0; g_im_q[i] <= 32'sd0; pending_g_re[i] <= 32'sd0; pending_g_im[i] <= 32'sd0;
            end
            for (int i = 0; i < 2; i++) begin
                b_re_q[i] <= 32'sd0; b_im_q[i] <= 32'sd0; pending_b_re[i] <= 32'sd0; pending_b_im[i] <= 32'sd0;
            end
        end else begin
            batch_done <= 1'b0;
            if (batch_start) begin
                if (batch_active || tx_busy || core_busy || batch_count == 16'd0 || batch_count > MAX_BATCH_TASKS) begin
                    batch_error <= 1'b1;
                    batch_error_code <= ERR_BATCH;
                end else begin
                    batch_active <= 1'b1; input_closed <= 1'b0; pending_valid <= 1'b0; error_pending <= 1'b0;
                    batch_error <= 1'b0; batch_error_code <= 16'd0; batch_count_q <= batch_count;
                    input_count <= 16'd0; serialized_count <= 16'd0; outstanding <= '0; task_beat <= 4'd0;
                    invalid_seen <= 1'b0; first_error <= 16'd0; accepted_tasks <= 16'd0; emitted_tasks <= 16'd0;
                    tx_frame_final <= 1'b0; tx_frame_is_error <= 1'b0;
                end
            end else if (batch_cancel && batch_active) begin
                batch_active <= 1'b0; input_closed <= 1'b1; pending_valid <= 1'b0; error_pending <= 1'b0;
                batch_error <= 1'b1; batch_error_code <= ERR_BATCH; task_beat <= 4'd0;
            end else begin
                if (core_submit) begin
                    pending_valid <= 1'b0;
                    accepted_tasks <= accepted_tasks + 16'd1;
                end

                if (tx_pop) begin
                    if (!tx_frame_is_error) begin
                        emitted_tasks <= emitted_tasks + 16'd1;
                    end
                    if (tx_frame_final) begin
                        batch_active <= 1'b0;
                        batch_done <= 1'b1;
                    end
                end
                case ({core_submit, tx_pop && !tx_frame_is_error})
                    2'b10: outstanding <= outstanding + CREDIT_W'(1);
                    2'b01: outstanding <= outstanding - CREDIT_W'(1);
                    default: outstanding <= outstanding;
                endcase
                if (tx_accept) begin
                    tx_frame_is_error <= error_pending;
                    if (error_pending) begin
                        error_pending <= 1'b0;
                        tx_frame_final <= 1'b1;
                    end else begin
                        serialized_count <= serialized_count + 16'd1;
                        tx_frame_final <= ((serialized_count + 16'd1) == batch_count_q);
                    end
                end

                if (s_axis_tvalid && s_axis_tready) begin
                    if (beat_error != 16'd0) begin
                        // Count=1 retains the legacy single error response.
                        // A multi-slot simple-DMA payload cannot safely use a
                        // short error stream, so it is cancelled and requires
                        // both DMA channels to be recovered by the driver.
                        batch_error_code <= beat_error;
                        task_beat <= 4'd0;
                        pending_valid <= 1'b0;
                        if (batch_count_q == 16'd1) begin
                            input_closed <= 1'b1;
                            error_pending <= 1'b1;
                            error_frame_id <= (task_beat == 4'd1) ? s_axis_tdata[31:0] :
                                              ((task_beat == 4'd0) ? 32'd0 : frame_id_q);
                            error_config_id <= (task_beat == 4'd1) ? s_axis_tdata[63:32] :
                                                ((task_beat == 4'd0) ? 32'd0 : config_id_q);
                        end else begin
                            batch_active <= 1'b0;
                            batch_error <= 1'b1;
                        end
                    end else begin
                        case (task_beat)
                            4'd0: begin frame_id_q <= 32'd0; config_id_q <= 32'd0; end
                            4'd1: begin frame_id_q <= s_axis_tdata[31:0]; config_id_q <= s_axis_tdata[63:32]; end
                            4'd2: begin modulation_q <= s_axis_tdata[23:16]; ns_q <= s_axis_tdata[47:32]; flags_q <= s_axis_tdata[63:48]; end
                            4'd3: begin seed_q <= s_axis_tdata[31:0]; fixed_profile_id_q <= s_axis_tdata[47:32]; algorithm_profile_id_q <= s_axis_tdata[63:48]; end
                            4'd4: begin min_iterations_q <= s_axis_tdata[47:32]; max_iterations_q <= s_axis_tdata[63:48]; end
                            4'd5: stop_threshold_raw_q <= $signed(s_axis_tdata[31:0]);
                            4'd6: begin noise_variance_raw_q <= $signed(s_axis_tdata[31:0]); proposal_step_raw_q <= $signed(s_axis_tdata[63:32]); end
                            4'd8,4'd9,4'd10,4'd11: begin
                                g_re_q[task_beat-4'd8] <= $signed(s_axis_tdata[31:0]);
                                g_im_q[task_beat-4'd8] <= $signed(s_axis_tdata[63:32]);
                            end
                            4'd12,4'd13: begin
                                b_re_q[task_beat-4'd12] <= $signed(s_axis_tdata[31:0]);
                                b_im_q[task_beat-4'd12] <= $signed(s_axis_tdata[63:32]);
                            end
                            default: begin end
                        endcase
                        if (task_beat == 4'd14) begin
                            pending_valid <= 1'b1; pending_tag <= frame_id_q; pending_config <= config_id_q;
                            pending_modulation <= modulation_q; pending_max_iterations <= max_iterations_q; pending_seed <= seed_q;
                            pending_proposal_step_raw <= proposal_step_raw_q; pending_c_raw <= $signed(s_axis_tdata);
                            for (int i = 0; i < 4; i++) begin pending_g_re[i] <= g_re_q[i]; pending_g_im[i] <= g_im_q[i]; end
                            for (int i = 0; i < 2; i++) begin pending_b_re[i] <= b_re_q[i]; pending_b_im[i] <= b_im_q[i]; end
                            input_count <= input_count + 16'd1;
                            task_beat <= 4'd0;
                            if ((input_count + 16'd1) == batch_count_q) input_closed <= 1'b1;
                        end else begin
                            task_beat <= task_beat + 4'd1;
                        end
                    end
                end
            end
        end
    end
endmodule
