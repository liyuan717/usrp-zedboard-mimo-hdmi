`timescale 1ns/1ps

module mimo_mhgd_kernel_2x2 (
    input  logic                 aclk,
    input  logic                 aresetn,
    input  logic                 flush,
    input  logic                 frame_valid,

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

    output logic                 busy,
    output logic                 result_valid,
    output logic [7:0]           x_hat0,
    output logic [7:0]           x_hat1,
    output logic signed [63:0]   best_residual_raw,
    output logic [31:0]          actual_iterations,
    output logic [63:0]          pl_cycles,
    output logic [31:0]          overflow_count,
    output logic [31:0]          saturation_count,
    output logic [15:0]          warning_bits,
    output logic [31:0]          debug_signature
);

    typedef enum logic [3:0] {
        K_IDLE,
        K_INIT_COST,
        K_STORE_INIT,
        K_PROPOSE,
        K_EVAL_CAND,
        K_WAIT_CAND,
        K_DECIDE,
        K_FINISH
    } kernel_state_t;

    kernel_state_t state;

    // One task at a time. RX owns validation (profile 0, flags=0,
    // modulation=1/2, nonzero iteration budget). Snapshot its payload so
    // a later RX update cannot change an in-flight calculation.
    logic accept_frame, datapath_aresetn;
    logic [7:0] modulation_q;
    logic [15:0] max_iterations_q, uphill_threshold_q;
    logic signed [31:0] g_re_q [0:3], g_im_q [0:3];
    logic signed [31:0] b_re_q [0:1], b_im_q [0:1];
    logic signed [63:0] c_q;

    assign accept_frame = aresetn && !flush && (state == K_IDLE) && frame_valid;
    // Both children use synchronous active-low reset. Cancel pending cost
    // valid stages as well as the controller when aborting a task.
    assign datapath_aresetn = aresetn && !flush;

    logic signed [3:0] cur_x0_re, cur_x0_im, cur_x1_re, cur_x1_im;
    logic signed [3:0] cand_x0_re, cand_x0_im, cand_x1_re, cand_x1_im;
    logic signed [3:0] best_x0_re, best_x0_im, best_x1_re, best_x1_im;
    logic signed [63:0] current_cost, candidate_cost, best_cost;
    logic [31:0] iteration;

    logic [31:0] rng_value;
    logic rng_load, rng_advance;
    logic selected_row, selected_component;
    logic signed [63:0] grad_re, grad_im;
    logic signed [3:0] stepped_level;
    logic signed [63:0] eval_cost;
    logic cost_start, cost_valid;

    assign selected_row       = rng_value[0];
    assign selected_component = rng_value[1];

    mimo_lcg_rng u_rng (
        .aclk(aclk),
        .aresetn(datapath_aresetn),
        .load_seed(rng_load),
        .seed(seed),
        .advance(rng_advance),
        .value(rng_value)
    );

    mimo_gradient_2x2 u_gradient (
        .row_select(selected_row),
        .g_re(g_re_q), .g_im(g_im_q),
        .b_re(b_re_q), .b_im(b_im_q),
        .x0_re(cur_x0_re), .x0_im(cur_x0_im),
        .x1_re(cur_x1_re), .x1_im(cur_x1_im),
        .grad_re(grad_re), .grad_im(grad_im)
    );

    mimo_qam_stepper u_stepper (
        .modulation(modulation_q),
        .level_in(selected_row ?
                  (selected_component ? cur_x1_im : cur_x1_re) :
                  (selected_component ? cur_x0_im : cur_x0_re)),
        .gradient(selected_component ? grad_im : grad_re),
        .level_out(stepped_level)
    );

    mimo_cost_2x2 u_cost (
        .aclk(aclk), .aresetn(datapath_aresetn), .start(cost_start),
        .g_re(g_re_q), .g_im(g_im_q),
        .b_re(b_re_q), .b_im(b_im_q), .c_raw(c_q),
        .x0_re((state == K_INIT_COST || state == K_STORE_INIT) ? cur_x0_re : cand_x0_re),
        .x0_im((state == K_INIT_COST || state == K_STORE_INIT) ? cur_x0_im : cand_x0_im),
        .x1_re((state == K_INIT_COST || state == K_STORE_INIT) ? cur_x1_re : cand_x1_re),
        .x1_im((state == K_INIT_COST || state == K_STORE_INIT) ? cur_x1_im : cand_x1_im),
        .valid(cost_valid), .cost_raw(eval_cost)
    );

    function automatic logic signed [3:0] initial_level(
        input logic signed [31:0] matched_component
    );
        initial_level = (matched_component < 0) ? -4'sd1 : 4'sd1;
    endfunction

    function automatic logic [3:0] level_index(
        input logic [7:0] mod,
        input logic signed [3:0] level
    );
        if (mod == 8'd1)
            level_index = (level > 0) ? 4'd1 : 4'd0;
        else begin
            case (level)
                -4'sd3: level_index = 4'd0;
                -4'sd1: level_index = 4'd1;
                4'sd3:  level_index = 4'd2;
                default:level_index = 4'd3; // +1
            endcase
        end
    endfunction

    function automatic logic [7:0] symbol_index(
        input logic [7:0] mod,
        input logic signed [3:0] re,
        input logic signed [3:0] im
    );
        logic [3:0] re_index;
        logic [3:0] im_index;
        begin
            re_index = level_index(mod, re);
            im_index = level_index(mod, im);
            if (mod == 8'd1) begin
                // Match host Gray order: (+,+), (-,+), (-,-), (+,-).
                case ({re_index[0], im_index[0]})
                    2'b11: symbol_index = 8'd0;
                    2'b01: symbol_index = 8'd1;
                    2'b00: symbol_index = 8'd2;
                    default: symbol_index = 8'd3;
                endcase
            end else
                // Host index is {real Gray code, imaginary Gray code}.
                symbol_index = {4'd0, re_index[1:0], im_index[1:0]};
        end
    endfunction

    always_comb begin
        rng_load    = accept_frame;
        rng_advance = 1'b0;
        cost_start  = 1'b0;
        if (aresetn && !flush && state == K_PROPOSE)
            rng_advance = 1'b1;
        if (aresetn && !flush && ((state == K_INIT_COST) || (state == K_EVAL_CAND)))
            cost_start = 1'b1;
    end

    always_ff @(posedge aclk) begin
        if (!aresetn) begin
            state                <= K_IDLE;
            modulation_q         <= 8'd1;
            max_iterations_q     <= 16'd0;
            uphill_threshold_q   <= 16'd0;
            c_q                  <= 64'sd0;
            for (int i = 0; i < 4; i++) begin
                g_re_q[i] <= 32'sd0;
                g_im_q[i] <= 32'sd0;
            end
            for (int i = 0; i < 2; i++) begin
                b_re_q[i] <= 32'sd0;
                b_im_q[i] <= 32'sd0;
            end
            busy                 <= 1'b0;
            result_valid         <= 1'b0;
            x_hat0               <= 8'd0;
            x_hat1               <= 8'd0;
            best_residual_raw    <= 64'sd0;
            actual_iterations    <= 32'd0;
            pl_cycles            <= 64'd0;
            overflow_count       <= 32'd0;
            saturation_count     <= 32'd0;
            warning_bits         <= 16'd0;
            debug_signature      <= 32'd0;
            iteration            <= 32'd0;
            cur_x0_re            <= 4'sd1;
            cur_x0_im            <= 4'sd1;
            cur_x1_re            <= 4'sd1;
            cur_x1_im            <= 4'sd1;
            cand_x0_re           <= 4'sd1;
            cand_x0_im           <= 4'sd1;
            cand_x1_re           <= 4'sd1;
            cand_x1_im           <= 4'sd1;
            best_x0_re           <= 4'sd1;
            best_x0_im           <= 4'sd1;
            best_x1_re           <= 4'sd1;
            best_x1_im           <= 4'sd1;
            current_cost         <= 64'sd0;
            candidate_cost       <= 64'sd0;
            best_cost            <= 64'sd0;
        end else begin
            result_valid <= 1'b0;

            if (flush) begin
                state <= K_IDLE;
                busy  <= 1'b0;
            end else begin
                if (busy)
                    pl_cycles <= pl_cycles + 64'd1;

                case (state)
                    K_IDLE: begin
                        busy <= 1'b0;
                        if (accept_frame) begin
                            modulation_q       <= modulation;
                            max_iterations_q   <= max_iterations;
                            // Preserve the profile-0 acceptance policy and RNG
                            // consumption order; this is not a QAM step size.
                            uphill_threshold_q <= proposal_step_raw[15:0];
                            c_q                <= c_raw;
                            for (int i = 0; i < 4; i++) begin
                                g_re_q[i] <= g_re[i];
                                g_im_q[i] <= g_im[i];
                            end
                            for (int i = 0; i < 2; i++) begin
                                b_re_q[i] <= b_re[i];
                                b_im_q[i] <= b_im[i];
                            end
                            busy              <= 1'b1;
                            pl_cycles         <= 64'd0;
                            iteration         <= 32'd0;
                            overflow_count    <= 32'd0;
                            saturation_count  <= 32'd0;
                            // Bit 15 marks algorithm_profile 0 as a bring-up
                            // kernel, not the frozen paper-accurate MHGD profile.
                            warning_bits      <= 16'h8000;
                            cur_x0_re         <= initial_level(b_re[0]);
                            cur_x0_im         <= initial_level(b_im[0]);
                            cur_x1_re         <= initial_level(b_re[1]);
                            cur_x1_im         <= initial_level(b_im[1]);
                            state             <= K_INIT_COST;
                        end
                    end

                    K_INIT_COST: begin
                        state <= K_STORE_INIT;
                    end

                    K_STORE_INIT: begin
                        if (cost_valid) begin
                            current_cost <= eval_cost;
                            best_cost    <= eval_cost;
                            best_x0_re   <= cur_x0_re;
                            best_x0_im   <= cur_x0_im;
                            best_x1_re   <= cur_x1_re;
                            best_x1_im   <= cur_x1_im;
                            // Defensive direct-kernel behavior: a zero budget
                            // returns the initial estimate without a proposal.
                            // Normal stream traffic rejects zero in frame_rx.
                            if (max_iterations_q == 16'd0) begin
                                x_hat0 <= symbol_index(modulation_q, cur_x0_re, cur_x0_im);
                                x_hat1 <= symbol_index(modulation_q, cur_x1_re, cur_x1_im);
                                best_residual_raw <= eval_cost;
                                actual_iterations <= 32'd0;
                                debug_signature <= rng_value;
                                state <= K_FINISH;
                            end else begin
                                state <= K_PROPOSE;
                            end
                        end
                    end

                    K_PROPOSE: begin
                        cand_x0_re <= cur_x0_re;
                        cand_x0_im <= cur_x0_im;
                        cand_x1_re <= cur_x1_re;
                        cand_x1_im <= cur_x1_im;
                        if (!selected_row && !selected_component)
                            cand_x0_re <= stepped_level;
                        else if (!selected_row && selected_component)
                            cand_x0_im <= stepped_level;
                        else if (selected_row && !selected_component)
                            cand_x1_re <= stepped_level;
                        else
                            cand_x1_im <= stepped_level;
                        state <= K_EVAL_CAND;
                    end

                    K_EVAL_CAND: begin
                        state <= K_WAIT_CAND;
                    end

                    K_WAIT_CAND: begin
                        if (cost_valid) begin
                            candidate_cost <= eval_cost;
                            state <= K_DECIDE;
                        end
                    end

                    K_DECIDE: begin
                        iteration <= iteration + 32'd1;

                        // Provisional profile 0: always accept an improving
                        // move; optionally accept an uphill move according to
                        // a fixed 16-bit threshold. This acceptance policy is
                        // deliberately versioned and must be replaced/locked
                        // by the paper-derived bit-accurate model.
                        if ((candidate_cost <= current_cost) ||
                            (rng_value[31:16] < uphill_threshold_q)) begin
                            cur_x0_re    <= cand_x0_re;
                            cur_x0_im    <= cand_x0_im;
                            cur_x1_re    <= cand_x1_re;
                            cur_x1_im    <= cand_x1_im;
                            current_cost <= candidate_cost;
                        end

                        if (candidate_cost < best_cost) begin
                            best_cost  <= candidate_cost;
                            best_x0_re <= cand_x0_re;
                            best_x0_im <= cand_x0_im;
                            best_x1_re <= cand_x1_re;
                            best_x1_im <= cand_x1_im;
                        end

                        if ((iteration + 32'd1) >= max_iterations_q) begin
                            if (candidate_cost < best_cost) begin
                                x_hat0            <= symbol_index(modulation_q, cand_x0_re, cand_x0_im);
                                x_hat1            <= symbol_index(modulation_q, cand_x1_re, cand_x1_im);
                                best_residual_raw <= candidate_cost;
                            end else begin
                                x_hat0            <= symbol_index(modulation_q, best_x0_re, best_x0_im);
                                x_hat1            <= symbol_index(modulation_q, best_x1_re, best_x1_im);
                                best_residual_raw <= best_cost;
                            end
                            actual_iterations <= iteration + 32'd1;
                            debug_signature   <= rng_value ^ (iteration + 32'd1);
                            state             <= K_FINISH;
                        end else begin
                            state <= K_PROPOSE;
                        end
                    end

                    K_FINISH: begin
                        busy         <= 1'b0;
                        result_valid <= 1'b1;
                        state        <= K_IDLE;
                    end

                    default: begin
                        state <= K_IDLE;
                        busy  <= 1'b0;
                    end
                endcase
            end
        end
    end

endmodule
