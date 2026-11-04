`timescale 1ns/1ps

// Candidate only: an interleaved, profile-0 kernel sharing one unmodified
// mimo_cost_2x2 lane.  It is intentionally not connected to the deployed
// AXI/DMA wrapper.  Each context owns its RNG, Gram inputs and solver state;
// a two-cycle tag pipe associates each cost response with that context.
module mimo_mhgd_interleaved_candidate #(
    // A decision, context-read and request-generation stage precede the cost
    // response.  Ten contexts cover those stages plus continuous re-admission.
    parameter int MAX_CONTEXTS = 10
) (
    input  logic                 aclk,
    input  logic                 aresetn,
    input  logic                 flush,

    input  logic                 input_valid,
    output logic                 input_ready,
    input  logic [31:0]          input_tag,
    input  logic [31:0]          input_config,
    input  logic [7:0]           modulation,
    input  logic [15:0]          flags,
    input  logic [15:0]          max_iterations,
    input  logic [31:0]          seed,
    input  logic signed [31:0]   proposal_step_raw,
    input  logic signed [31:0]   g_re [0:3],
    input  logic signed [31:0]   g_im [0:3],
    input  logic signed [31:0]   b_re [0:1],
    input  logic signed [31:0]   b_im [0:1],
    input  logic signed [63:0]   c_raw,

    output logic                 result_valid,
    input  logic                 result_ready,
    output logic [31:0]          result_tag,
    output logic [31:0]          result_config,
    output logic [7:0]           x_hat0,
    output logic [7:0]           x_hat1,
    output logic signed [63:0]   best_residual_raw,
    output logic [31:0]          actual_iterations,
    output logic [63:0]          pl_cycles,
    output logic [31:0]          overflow_count,
    output logic [31:0]          saturation_count,
    output logic [15:0]          warning_bits,
    output logic [31:0]          debug_signature,
    output logic                 busy
);

    localparam int IDX_W = (MAX_CONTEXTS <= 1) ? 1 : $clog2(MAX_CONTEXTS);
    localparam logic [15:0] PROFILE0_WARNING = 16'h8000;
    typedef enum logic [2:0] {
        C_FREE, C_INIT_READY, C_ISSUE_PENDING, C_INIT_WAIT, C_CAND_READY, C_CAND_WAIT
    } context_state_t;

    context_state_t ctx_state [0:MAX_CONTEXTS-1];
    logic [31:0] ctx_tag [0:MAX_CONTEXTS-1], ctx_config [0:MAX_CONTEXTS-1];
    logic [7:0] ctx_modulation [0:MAX_CONTEXTS-1];
    logic [15:0] ctx_max_iterations [0:MAX_CONTEXTS-1];
    logic [15:0] ctx_uphill_threshold [0:MAX_CONTEXTS-1];
    logic [31:0] ctx_rng [0:MAX_CONTEXTS-1];
    logic [31:0] ctx_iteration [0:MAX_CONTEXTS-1];
    logic signed [31:0] ctx_g_re [0:MAX_CONTEXTS-1][0:3];
    logic signed [31:0] ctx_g_im [0:MAX_CONTEXTS-1][0:3];
    logic signed [31:0] ctx_b_re [0:MAX_CONTEXTS-1][0:1];
    logic signed [31:0] ctx_b_im [0:MAX_CONTEXTS-1][0:1];
    logic signed [63:0] ctx_c [0:MAX_CONTEXTS-1];
    logic signed [3:0] ctx_cur_x0_re [0:MAX_CONTEXTS-1], ctx_cur_x0_im [0:MAX_CONTEXTS-1];
    logic signed [3:0] ctx_cur_x1_re [0:MAX_CONTEXTS-1], ctx_cur_x1_im [0:MAX_CONTEXTS-1];
    logic signed [3:0] ctx_cand_x0_re [0:MAX_CONTEXTS-1], ctx_cand_x0_im [0:MAX_CONTEXTS-1];
    logic signed [3:0] ctx_cand_x1_re [0:MAX_CONTEXTS-1], ctx_cand_x1_im [0:MAX_CONTEXTS-1];
    logic signed [3:0] ctx_best_x0_re [0:MAX_CONTEXTS-1], ctx_best_x0_im [0:MAX_CONTEXTS-1];
    logic signed [3:0] ctx_best_x1_re [0:MAX_CONTEXTS-1], ctx_best_x1_im [0:MAX_CONTEXTS-1];
    logic signed [63:0] ctx_current_cost [0:MAX_CONTEXTS-1], ctx_best_cost [0:MAX_CONTEXTS-1];

    // FIFO decouples completed contexts from result_ready.  Its occupancy plus
    // active contexts is bounded by MAX_CONTEXTS, so a valid cost response
    // never overwrites a result even when the consumer is stalled.
    logic [IDX_W-1:0] result_rd_ptr, result_wr_ptr;
    logic [IDX_W:0] result_count;
    logic [31:0] result_tag_q [0:MAX_CONTEXTS-1], result_config_q [0:MAX_CONTEXTS-1];
    logic [7:0] result_x0_q [0:MAX_CONTEXTS-1], result_x1_q [0:MAX_CONTEXTS-1];
    logic signed [63:0] result_cost_q [0:MAX_CONTEXTS-1];
    logic [31:0] result_iterations_q [0:MAX_CONTEXTS-1], result_debug_q [0:MAX_CONTEXTS-1];
    logic [63:0] result_cycles_q [0:MAX_CONTEXTS-1];

    logic issue_found, issue_initial, decision_valid, decision_initial;
    logic read_pipe_valid, read_pipe_initial, issue_pipe_valid, issue_pipe_initial;
    logic [IDX_W-1:0] issue_index, decision_index, read_pipe_index, issue_pipe_index, free_index, rr_index;
    logic free_found;
    logic cost_start, cost_valid;
    logic [IDX_W-1:0] cost_tag_pipe0, cost_tag_pipe1;
    logic cost_tag_valid0, cost_tag_valid1;
    logic signed [31:0] cost_g_re [0:3], cost_g_im [0:3];
    // This bank is the only point at which the registered scheduler choice
    // reads a context.  Candidate arithmetic below therefore never has a
    // decision-index mux in its input-to-register timing cone.
    logic [7:0] read_pipe_modulation;
    logic [31:0] read_pipe_rng;
    logic signed [31:0] read_pipe_g_re [0:3], read_pipe_g_im [0:3];
    logic signed [31:0] read_pipe_b_re [0:1], read_pipe_b_im [0:1];
    logic signed [63:0] read_pipe_c;
    logic signed [3:0] read_pipe_cur_x0_re, read_pipe_cur_x0_im;
    logic signed [3:0] read_pipe_cur_x1_re, read_pipe_cur_x1_im;
    logic signed [31:0] cost_g_re_q [0:3], cost_g_im_q [0:3];
    logic signed [31:0] cost_b_re_q [0:1], cost_b_im_q [0:1];
    logic signed [63:0] cost_c_q;
    // `mimo_cost_2x2` consumes b/c one cycle after its start.  This second
    // register stage keeps those operands with the already-registered G/x
    // request while the next request enters the issue pipe.
    logic signed [31:0] cost_b_re_stage_q [0:1], cost_b_im_stage_q [0:1];
    logic signed [63:0] cost_c_stage_q;
    logic signed [3:0] cost_x0_re, cost_x0_im, cost_x1_re, cost_x1_im;
    logic signed [3:0] cost_x0_re_q, cost_x0_im_q, cost_x1_re_q, cost_x1_im_q;
    logic signed [63:0] eval_cost;
    logic response_fire, response_is_initial, response_final;
    logic [IDX_W-1:0] response_index;
    logic result_pop;
    logic active_any;

    function automatic logic signed [3:0] initial_level(input logic signed [31:0] matched_component);
        initial_level = (matched_component < 0) ? -4'sd1 : 4'sd1;
    endfunction

    function automatic logic [31:0] next_rng(input logic [31:0] value);
        next_rng = value * 32'd1664525 + 32'd1013904223;
    endfunction

    function automatic logic [IDX_W-1:0] next_context_index(input logic [IDX_W-1:0] value);
        if (value == MAX_CONTEXTS-1)
            next_context_index = '0;
        else
            next_context_index = value + IDX_W'(1);
    endfunction

    function automatic logic [3:0] level_index(input logic [7:0] mod, input logic signed [3:0] level);
        if (mod == 8'd1)
            level_index = (level > 0) ? 4'd1 : 4'd0;
        else begin
            case (level)
                -4'sd3: level_index = 4'd0;
                -4'sd1: level_index = 4'd1;
                4'sd3:  level_index = 4'd2;
                default: level_index = 4'd3;
            endcase
        end
    endfunction

    function automatic logic [7:0] symbol_index(
        input logic [7:0] mod, input logic signed [3:0] re, input logic signed [3:0] im
    );
        logic [3:0] re_index, im_index;
        begin
            re_index = level_index(mod, re);
            im_index = level_index(mod, im);
            if (mod == 8'd1) begin
                case ({re_index[0], im_index[0]})
                    2'b11: symbol_index = 8'd0;
                    2'b01: symbol_index = 8'd1;
                    2'b00: symbol_index = 8'd2;
                    default: symbol_index = 8'd3;
                endcase
            end else
                symbol_index = {4'd0, re_index[1:0], im_index[1:0]};
        end
    endfunction

    // Round-robin issue selection prevents a just-completed low index from
    // monopolising the cost lane while other contexts are already ready.
    always_comb begin
        free_found = 1'b0;
        free_index = '0;
        issue_found = 1'b0;
        issue_index = '0;
        issue_initial = 1'b0;
        active_any = 1'b0;
        for (int i = 0; i < MAX_CONTEXTS; i++) begin
            if (ctx_state[i] != C_FREE)
                active_any = 1'b1;
            if (!free_found && ctx_state[i] == C_FREE) begin
                free_found = 1'b1;
                free_index = IDX_W'(i);
            end
        end
        // Fill admitted contexts with their mandatory initial-cost evaluation
        // before cycling candidates.  This avoids leaving a late-admitted
        // context alone after its peers finish.
        for (int offset = 0; offset < MAX_CONTEXTS; offset++) begin
            int i;
            i = rr_index + offset;
            if (i >= MAX_CONTEXTS) i = i - MAX_CONTEXTS;
            if (!issue_found && ctx_state[i] == C_INIT_READY) begin
                issue_found = 1'b1;
                issue_index = IDX_W'(i);
                issue_initial = 1'b1;
            end
        end
        if (!issue_found) begin
            for (int offset = 0; offset < MAX_CONTEXTS; offset++) begin
                int i;
                i = rr_index + offset;
                if (i >= MAX_CONTEXTS) i = i - MAX_CONTEXTS;
                if (!issue_found && ctx_state[i] == C_CAND_READY) begin
                    issue_found = 1'b1;
                    issue_index = IDX_W'(i);
                    issue_initial = 1'b0;
                end
            end
        end
    end

    always_comb begin
        logic selected_row, selected_component;
        logic [1:0] base;
        logic signed [63:0] p0_re, p0_im, p1_re, p1_im, grad_re, grad_im;
        logic signed [3:0] stepped_level;
        for (int i = 0; i < 4; i++) begin
            cost_g_re[i] = 32'sd0;
            cost_g_im[i] = 32'sd0;
        end
        cost_x0_re = 4'sd1; cost_x0_im = 4'sd1; cost_x1_re = 4'sd1; cost_x1_im = 4'sd1;
        selected_row = 1'b0;
        selected_component = 1'b0;
        base = 2'd0;
        p0_re = 64'sd0; p0_im = 64'sd0; p1_re = 64'sd0; p1_im = 64'sd0;
        grad_re = 64'sd0; grad_im = 64'sd0; stepped_level = 4'sd1;
        if (read_pipe_valid) begin
            for (int i = 0; i < 4; i++) begin
                cost_g_re[i] = read_pipe_g_re[i];
                cost_g_im[i] = read_pipe_g_im[i];
            end
            if (read_pipe_initial) begin
                cost_x0_re = initial_level(read_pipe_b_re[0]);
                cost_x0_im = initial_level(read_pipe_b_im[0]);
                cost_x1_re = initial_level(read_pipe_b_re[1]);
                cost_x1_im = initial_level(read_pipe_b_im[1]);
            end else begin
                selected_row = read_pipe_rng[0];
                selected_component = read_pipe_rng[1];
                base = selected_row ? 2 : 0;
                p0_re = 36'($signed(read_pipe_g_re[base])) * 36'($signed(read_pipe_cur_x0_re)) -
                        36'($signed(read_pipe_g_im[base])) * 36'($signed(read_pipe_cur_x0_im));
                p0_im = 36'($signed(read_pipe_g_re[base])) * 36'($signed(read_pipe_cur_x0_im)) +
                        36'($signed(read_pipe_g_im[base])) * 36'($signed(read_pipe_cur_x0_re));
                p1_re = 36'($signed(read_pipe_g_re[base+1])) * 36'($signed(read_pipe_cur_x1_re)) -
                        36'($signed(read_pipe_g_im[base+1])) * 36'($signed(read_pipe_cur_x1_im));
                p1_im = 36'($signed(read_pipe_g_re[base+1])) * 36'($signed(read_pipe_cur_x1_im)) +
                        36'($signed(read_pipe_g_im[base+1])) * 36'($signed(read_pipe_cur_x1_re));
                grad_re = p0_re + p1_re - 64'($signed(read_pipe_b_re[selected_row]));
                grad_im = p0_im + p1_im - 64'($signed(read_pipe_b_im[selected_row]));
                stepped_level = selected_component ? grad_im[63] ? 4'sd1 : 4'sd1 : 4'sd1;
                // This case tree is exactly mimo_qam_stepper, inlined only to
                // form an issue-side candidate; no arithmetic policy changes.
                if (selected_component ? (grad_im > 0) : (grad_re > 0)) begin
                    if (read_pipe_modulation == 8'd1)
                        stepped_level = -4'sd1;
                    else case (selected_row ? (selected_component ? read_pipe_cur_x1_im : read_pipe_cur_x1_re) :
                                             (selected_component ? read_pipe_cur_x0_im : read_pipe_cur_x0_re))
                        4'sd3: stepped_level = 4'sd1;
                        4'sd1: stepped_level = -4'sd1;
                        -4'sd1: stepped_level = -4'sd3;
                        default: stepped_level = -4'sd3;
                    endcase
                end else if (selected_component ? (grad_im < 0) : (grad_re < 0)) begin
                    if (read_pipe_modulation == 8'd1)
                        stepped_level = 4'sd1;
                    else case (selected_row ? (selected_component ? read_pipe_cur_x1_im : read_pipe_cur_x1_re) :
                                             (selected_component ? read_pipe_cur_x0_im : read_pipe_cur_x0_re))
                        -4'sd3: stepped_level = -4'sd1;
                        -4'sd1: stepped_level = 4'sd1;
                        4'sd1: stepped_level = 4'sd3;
                        default: stepped_level = 4'sd3;
                    endcase
                end else begin
                    stepped_level = selected_row ? (selected_component ? read_pipe_cur_x1_im : read_pipe_cur_x1_re) :
                                                  (selected_component ? read_pipe_cur_x0_im : read_pipe_cur_x0_re);
                end
                cost_x0_re = read_pipe_cur_x0_re; cost_x0_im = read_pipe_cur_x0_im;
                cost_x1_re = read_pipe_cur_x1_re; cost_x1_im = read_pipe_cur_x1_im;
                if (!selected_row && !selected_component) cost_x0_re = stepped_level;
                else if (!selected_row && selected_component) cost_x0_im = stepped_level;
                else if (selected_row && !selected_component) cost_x1_re = stepped_level;
                else cost_x1_im = stepped_level;
            end
        end
    end

    // Register the full selected cost request.  The scheduler/gradient mux is
    // consequently no longer in the cost lane's multiplier setup path.  State
    // becomes WAIT when the request enters this pipe, not when it reaches the
    // cost lane, so one context cannot be selected twice.
    assign cost_start = issue_pipe_valid && aresetn && !flush;
    assign response_fire = cost_valid && cost_tag_valid1 && !flush;
    assign response_index = cost_tag_pipe1;
    assign response_is_initial = response_fire && (ctx_state[response_index] == C_INIT_WAIT);
    assign response_final = response_fire &&
        ((response_is_initial && ctx_max_iterations[response_index] == 16'd0) ||
         (!response_is_initial && ((ctx_iteration[response_index] + 32'd1) >= ctx_max_iterations[response_index])));
    assign result_valid = (result_count != 0);
    assign result_pop = result_valid && result_ready;
    assign result_tag = result_tag_q[result_rd_ptr];
    assign result_config = result_config_q[result_rd_ptr];
    assign x_hat0 = result_x0_q[result_rd_ptr];
    assign x_hat1 = result_x1_q[result_rd_ptr];
    assign best_residual_raw = result_cost_q[result_rd_ptr];
    assign actual_iterations = result_iterations_q[result_rd_ptr];
    assign pl_cycles = result_cycles_q[result_rd_ptr];
    assign overflow_count = 32'd0;
    assign saturation_count = 32'd0;
    assign warning_bits = result_valid ? PROFILE0_WARNING : 16'd0;
    assign debug_signature = result_debug_q[result_rd_ptr];
    assign input_ready = aresetn && !flush && free_found;
    assign busy = active_any || (result_count != 0) || issue_pipe_valid || cost_tag_valid0 || cost_tag_valid1;

    mimo_cost_2x2 u_cost (
        .aclk(aclk), .aresetn(aresetn && !flush), .start(cost_start),
        .g_re(cost_g_re_q), .g_im(cost_g_im_q), .b_re(cost_b_re_stage_q), .b_im(cost_b_im_stage_q), .c_raw(cost_c_stage_q),
        .x0_re(cost_x0_re_q), .x0_im(cost_x0_im_q), .x1_re(cost_x1_re_q), .x1_im(cost_x1_im_q),
        .valid(cost_valid), .cost_raw(eval_cost)
    );

    always_ff @(posedge aclk) begin
        if (!aresetn || flush) begin
            result_rd_ptr <= '0; result_wr_ptr <= '0; result_count <= '0;
            cost_tag_pipe0 <= '0; cost_tag_pipe1 <= '0; rr_index <= '0;
            cost_tag_valid0 <= 1'b0; cost_tag_valid1 <= 1'b0;
            decision_valid <= 1'b0; decision_initial <= 1'b0; decision_index <= '0;
            read_pipe_valid <= 1'b0; read_pipe_initial <= 1'b0; read_pipe_index <= '0;
            issue_pipe_valid <= 1'b0; issue_pipe_initial <= 1'b0; issue_pipe_index <= '0;
            read_pipe_modulation <= 8'd0; read_pipe_rng <= 32'd1; read_pipe_c <= 64'sd0;
            read_pipe_cur_x0_re <= 4'sd1; read_pipe_cur_x0_im <= 4'sd1;
            read_pipe_cur_x1_re <= 4'sd1; read_pipe_cur_x1_im <= 4'sd1;
            cost_c_q <= 64'sd0; cost_c_stage_q <= 64'sd0;
            cost_x0_re_q <= 4'sd1; cost_x0_im_q <= 4'sd1; cost_x1_re_q <= 4'sd1; cost_x1_im_q <= 4'sd1;
            for (int i = 0; i < 2; i++) begin
                cost_b_re_q[i] <= 32'sd0; cost_b_im_q[i] <= 32'sd0;
                cost_b_re_stage_q[i] <= 32'sd0; cost_b_im_stage_q[i] <= 32'sd0;
            end
            for (int i = 0; i < 4; i++) begin
                cost_g_re_q[i] <= 32'sd0; cost_g_im_q[i] <= 32'sd0;
                read_pipe_g_re[i] <= 32'sd0; read_pipe_g_im[i] <= 32'sd0;
            end
            for (int i = 0; i < 2; i++) begin
                read_pipe_b_re[i] <= 32'sd0; read_pipe_b_im[i] <= 32'sd0;
            end
            for (int i = 0; i < MAX_CONTEXTS; i++) begin
                ctx_state[i] <= C_FREE;
                ctx_tag[i] <= 32'd0; ctx_config[i] <= 32'd0; ctx_rng[i] <= 32'd1; ctx_iteration[i] <= 32'd0;
                ctx_current_cost[i] <= 64'sd0; ctx_best_cost[i] <= 64'sd0;
                ctx_cur_x0_re[i] <= 4'sd1; ctx_cur_x0_im[i] <= 4'sd1; ctx_cur_x1_re[i] <= 4'sd1; ctx_cur_x1_im[i] <= 4'sd1;
                ctx_cand_x0_re[i] <= 4'sd1; ctx_cand_x0_im[i] <= 4'sd1; ctx_cand_x1_re[i] <= 4'sd1; ctx_cand_x1_im[i] <= 4'sd1;
                ctx_best_x0_re[i] <= 4'sd1; ctx_best_x0_im[i] <= 4'sd1; ctx_best_x1_re[i] <= 4'sd1; ctx_best_x1_im[i] <= 4'sd1;
            end
        end else begin
            cost_tag_valid1 <= cost_tag_valid0;
            cost_tag_pipe1 <= cost_tag_pipe0;
            cost_tag_valid0 <= cost_start;
            if (cost_start) begin
                cost_tag_pipe0 <= issue_pipe_index;
                for (int i = 0; i < 2; i++) begin
                    cost_b_re_stage_q[i] <= cost_b_re_q[i];
                    cost_b_im_stage_q[i] <= cost_b_im_q[i];
                end
                cost_c_stage_q <= cost_c_q;
            end

            if (result_pop) result_rd_ptr <= next_context_index(result_rd_ptr);
            case ({response_final, result_pop})
                2'b10: result_count <= result_count + 1'b1;
                2'b01: result_count <= result_count - 1'b1;
                default: result_count <= result_count;
            endcase

            if (input_valid && input_ready) begin
                ctx_state[free_index] <= C_INIT_READY;
                ctx_tag[free_index] <= input_tag;
                ctx_config[free_index] <= input_config;
                ctx_modulation[free_index] <= modulation;
                ctx_max_iterations[free_index] <= max_iterations;
                ctx_uphill_threshold[free_index] <= proposal_step_raw[15:0];
                ctx_rng[free_index] <= (seed == 32'd0) ? 32'h1 : seed;
                ctx_iteration[free_index] <= 32'd0;
                ctx_c[free_index] <= c_raw;
                for (int i = 0; i < 4; i++) begin
                    ctx_g_re[free_index][i] <= g_re[i];
                    ctx_g_im[free_index][i] <= g_im[i];
                end
                for (int i = 0; i < 2; i++) begin
                    ctx_b_re[free_index][i] <= b_re[i];
                    ctx_b_im[free_index][i] <= b_im[i];
                end
            end

            // Decision registration breaks rr_index/scheduler selection away
            // from context-array reads.  The pending state prevents a second
            // selection before its read bank has been captured.
            decision_valid <= issue_found;
            if (issue_found) begin
                decision_index <= issue_index;
                decision_initial <= issue_initial;
                ctx_state[issue_index] <= C_ISSUE_PENDING;
                rr_index <= next_context_index(issue_index);
            end

            // The context-read bank isolates the decision-index mux from the
            // candidate gradient/step logic.  It carries an immutable request
            // image, so a following response cannot alter an in-flight
            // candidate's symbols or RNG selection.
            read_pipe_valid <= decision_valid;
            if (decision_valid) begin
                read_pipe_index <= decision_index;
                read_pipe_initial <= decision_initial;
                read_pipe_modulation <= ctx_modulation[decision_index];
                read_pipe_rng <= ctx_rng[decision_index];
                read_pipe_c <= ctx_c[decision_index];
                read_pipe_cur_x0_re <= ctx_cur_x0_re[decision_index];
                read_pipe_cur_x0_im <= ctx_cur_x0_im[decision_index];
                read_pipe_cur_x1_re <= ctx_cur_x1_re[decision_index];
                read_pipe_cur_x1_im <= ctx_cur_x1_im[decision_index];
                for (int i = 0; i < 4; i++) begin
                    read_pipe_g_re[i] <= ctx_g_re[decision_index][i];
                    read_pipe_g_im[i] <= ctx_g_im[decision_index][i];
                end
                for (int i = 0; i < 2; i++) begin
                    read_pipe_b_re[i] <= ctx_b_re[decision_index][i];
                    read_pipe_b_im[i] <= ctx_b_im[decision_index][i];
                end
            end

            issue_pipe_valid <= read_pipe_valid;
            if (read_pipe_valid) begin
                for (int i = 0; i < 4; i++) begin
                    cost_g_re_q[i] <= cost_g_re[i];
                    cost_g_im_q[i] <= cost_g_im[i];
                end
                for (int i = 0; i < 2; i++) begin
                    cost_b_re_q[i] <= read_pipe_b_re[i];
                    cost_b_im_q[i] <= read_pipe_b_im[i];
                end
                cost_c_q <= read_pipe_c;
                cost_x0_re_q <= cost_x0_re; cost_x0_im_q <= cost_x0_im;
                cost_x1_re_q <= cost_x1_re; cost_x1_im_q <= cost_x1_im;
                issue_pipe_index <= read_pipe_index;
                issue_pipe_initial <= read_pipe_initial;
                ctx_state[read_pipe_index] <= read_pipe_initial ? C_INIT_WAIT : C_CAND_WAIT;
            end

            // Submit symbols/RNG only as the registered request reaches the
            // cost lane.  The response arrives one cycle later and therefore
            // observes precisely these values, as in the original kernel.
            if (cost_start) begin
                if (issue_pipe_initial) begin
                    ctx_cur_x0_re[issue_pipe_index] <= cost_x0_re_q; ctx_cur_x0_im[issue_pipe_index] <= cost_x0_im_q;
                    ctx_cur_x1_re[issue_pipe_index] <= cost_x1_re_q; ctx_cur_x1_im[issue_pipe_index] <= cost_x1_im_q;
                end else begin
                    ctx_cand_x0_re[issue_pipe_index] <= cost_x0_re_q; ctx_cand_x0_im[issue_pipe_index] <= cost_x0_im_q;
                    ctx_cand_x1_re[issue_pipe_index] <= cost_x1_re_q; ctx_cand_x1_im[issue_pipe_index] <= cost_x1_im_q;
                    ctx_rng[issue_pipe_index] <= next_rng(ctx_rng[issue_pipe_index]);
                end
            end

            if (response_fire) begin
                if (response_is_initial) begin
                    ctx_current_cost[response_index] <= eval_cost;
                    ctx_best_cost[response_index] <= eval_cost;
                    ctx_best_x0_re[response_index] <= ctx_cur_x0_re[response_index];
                    ctx_best_x0_im[response_index] <= ctx_cur_x0_im[response_index];
                    ctx_best_x1_re[response_index] <= ctx_cur_x1_re[response_index];
                    ctx_best_x1_im[response_index] <= ctx_cur_x1_im[response_index];
                    if (ctx_max_iterations[response_index] == 16'd0) begin
                        result_tag_q[result_wr_ptr] <= ctx_tag[response_index];
                        result_config_q[result_wr_ptr] <= ctx_config[response_index];
                        result_x0_q[result_wr_ptr] <= symbol_index(ctx_modulation[response_index], ctx_cur_x0_re[response_index], ctx_cur_x0_im[response_index]);
                        result_x1_q[result_wr_ptr] <= symbol_index(ctx_modulation[response_index], ctx_cur_x1_re[response_index], ctx_cur_x1_im[response_index]);
                        result_cost_q[result_wr_ptr] <= eval_cost;
                        result_iterations_q[result_wr_ptr] <= 32'd0;
                        result_cycles_q[result_wr_ptr] <= 64'd4;
                        result_debug_q[result_wr_ptr] <= ctx_rng[response_index];
                        result_wr_ptr <= next_context_index(result_wr_ptr);
                        ctx_state[response_index] <= C_FREE;
                    end else
                        ctx_state[response_index] <= C_CAND_READY;
                end else begin
                    logic accept_move, better_move;
                    logic [31:0] next_iteration;
                    accept_move = (eval_cost <= ctx_current_cost[response_index]) ||
                                  (ctx_rng[response_index][31:16] < ctx_uphill_threshold[response_index]);
                    better_move = eval_cost < ctx_best_cost[response_index];
                    next_iteration = ctx_iteration[response_index] + 32'd1;
                    ctx_iteration[response_index] <= next_iteration;
                    if (accept_move) begin
                        ctx_cur_x0_re[response_index] <= ctx_cand_x0_re[response_index];
                        ctx_cur_x0_im[response_index] <= ctx_cand_x0_im[response_index];
                        ctx_cur_x1_re[response_index] <= ctx_cand_x1_re[response_index];
                        ctx_cur_x1_im[response_index] <= ctx_cand_x1_im[response_index];
                        ctx_current_cost[response_index] <= eval_cost;
                    end
                    if (better_move) begin
                        ctx_best_cost[response_index] <= eval_cost;
                        ctx_best_x0_re[response_index] <= ctx_cand_x0_re[response_index];
                        ctx_best_x0_im[response_index] <= ctx_cand_x0_im[response_index];
                        ctx_best_x1_re[response_index] <= ctx_cand_x1_re[response_index];
                        ctx_best_x1_im[response_index] <= ctx_cand_x1_im[response_index];
                    end
                    if (next_iteration >= ctx_max_iterations[response_index]) begin
                        result_tag_q[result_wr_ptr] <= ctx_tag[response_index];
                        result_config_q[result_wr_ptr] <= ctx_config[response_index];
                        result_x0_q[result_wr_ptr] <= better_move ? symbol_index(ctx_modulation[response_index], ctx_cand_x0_re[response_index], ctx_cand_x0_im[response_index]) : symbol_index(ctx_modulation[response_index], ctx_best_x0_re[response_index], ctx_best_x0_im[response_index]);
                        result_x1_q[result_wr_ptr] <= better_move ? symbol_index(ctx_modulation[response_index], ctx_cand_x1_re[response_index], ctx_cand_x1_im[response_index]) : symbol_index(ctx_modulation[response_index], ctx_best_x1_re[response_index], ctx_best_x1_im[response_index]);
                        result_cost_q[result_wr_ptr] <= better_move ? eval_cost : ctx_best_cost[response_index];
                        result_iterations_q[result_wr_ptr] <= next_iteration;
                        result_cycles_q[result_wr_ptr] <= 64'd4 + (64'(next_iteration) * 64'd5);
                        result_debug_q[result_wr_ptr] <= ctx_rng[response_index] ^ next_iteration;
                        result_wr_ptr <= next_context_index(result_wr_ptr);
                        ctx_state[response_index] <= C_FREE;
                    end else
                        ctx_state[response_index] <= C_CAND_READY;
                end
            end
        end
    end
endmodule
