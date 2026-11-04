`timescale 1ns/1ps

module mimo_mhgd_axis_top #(
    parameter int AXIL_ADDR_WIDTH = 6,
    parameter logic [31:0] BUILD_ID = 32'h0000_0001
) (
    (* X_INTERFACE_INFO = "xilinx.com:signal:clock:1.0 aclk CLK",
       X_INTERFACE_PARAMETER = "XIL_INTERFACENAME aclk, ASSOCIATED_BUSIF s_axi:s_axis:m_axis, ASSOCIATED_RESET aresetn, FREQ_HZ 50000000" *)
    input  logic                       aclk,
    (* X_INTERFACE_INFO = "xilinx.com:signal:reset:1.0 aresetn RST",
       X_INTERFACE_PARAMETER = "XIL_INTERFACENAME aresetn, POLARITY ACTIVE_LOW" *)
    input  logic                       aresetn,

    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi AWADDR",
       X_INTERFACE_PARAMETER = "XIL_INTERFACENAME s_axi, PROTOCOL AXI4LITE, DATA_WIDTH 32, ADDR_WIDTH 6, FREQ_HZ 50000000, HAS_BURST 0, HAS_LOCK 0, HAS_CACHE 0, HAS_REGION 0, HAS_QOS 0" *)
    input  logic [AXIL_ADDR_WIDTH-1:0] s_axi_awaddr,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi AWPROT" *)
    input  logic [2:0]                 s_axi_awprot,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi AWVALID" *)
    input  logic                       s_axi_awvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi AWREADY" *)
    output logic                       s_axi_awready,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi WDATA" *)
    input  logic [31:0]                s_axi_wdata,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi WSTRB" *)
    input  logic [3:0]                 s_axi_wstrb,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi WVALID" *)
    input  logic                       s_axi_wvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi WREADY" *)
    output logic                       s_axi_wready,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi BRESP" *)
    output logic [1:0]                 s_axi_bresp,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi BVALID" *)
    output logic                       s_axi_bvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi BREADY" *)
    input  logic                       s_axi_bready,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi ARADDR" *)
    input  logic [AXIL_ADDR_WIDTH-1:0] s_axi_araddr,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi ARPROT" *)
    input  logic [2:0]                 s_axi_arprot,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi ARVALID" *)
    input  logic                       s_axi_arvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi ARREADY" *)
    output logic                       s_axi_arready,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi RDATA" *)
    output logic [31:0]                s_axi_rdata,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi RRESP" *)
    output logic [1:0]                 s_axi_rresp,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi RVALID" *)
    output logic                       s_axi_rvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi RREADY" *)
    input  logic                       s_axi_rready,

    (* X_INTERFACE_INFO = "xilinx.com:interface:axis:1.0 s_axis TDATA",
       X_INTERFACE_PARAMETER = "XIL_INTERFACENAME s_axis, TDATA_NUM_BYTES 8, HAS_TKEEP 1, HAS_TLAST 1, HAS_TREADY 1, FREQ_HZ 50000000" *)
    input  logic [63:0]                s_axis_tdata,
    (* X_INTERFACE_INFO = "xilinx.com:interface:axis:1.0 s_axis TKEEP" *)
    input  logic [7:0]                 s_axis_tkeep,
    (* X_INTERFACE_INFO = "xilinx.com:interface:axis:1.0 s_axis TVALID" *)
    input  logic                       s_axis_tvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:axis:1.0 s_axis TREADY" *)
    output logic                       s_axis_tready,
    (* X_INTERFACE_INFO = "xilinx.com:interface:axis:1.0 s_axis TLAST" *)
    input  logic                       s_axis_tlast,

    (* X_INTERFACE_INFO = "xilinx.com:interface:axis:1.0 m_axis TDATA",
       X_INTERFACE_PARAMETER = "XIL_INTERFACENAME m_axis, TDATA_NUM_BYTES 8, HAS_TKEEP 1, HAS_TLAST 1, HAS_TREADY 1, FREQ_HZ 50000000" *)
    output logic [63:0]                m_axis_tdata,
    (* X_INTERFACE_INFO = "xilinx.com:interface:axis:1.0 m_axis TKEEP" *)
    output logic [7:0]                 m_axis_tkeep,
    (* X_INTERFACE_INFO = "xilinx.com:interface:axis:1.0 m_axis TVALID" *)
    output logic                       m_axis_tvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:axis:1.0 m_axis TREADY" *)
    input  logic                       m_axis_tready,
    (* X_INTERFACE_INFO = "xilinx.com:interface:axis:1.0 m_axis TLAST" *)
    output logic                       m_axis_tlast,
    (* X_INTERFACE_INFO = "xilinx.com:signal:interrupt:1.0 irq INTERRUPT",
       X_INTERFACE_PARAMETER = "XIL_INTERFACENAME irq, SENSITIVITY LEVEL_HIGH" *)
    output logic                       irq
);

    logic unused_axi_prot;
    assign unused_axi_prot = ^{s_axi_awprot, s_axi_arprot};

    localparam logic [15:0] STATUS_OK          = 16'h0000;
    localparam logic [15:0] STATUS_STREAM_ERROR= 16'h0005;

    logic start_pulse, abort_pulse, soft_reset_pulse;
    logic clear_done_pulse, clear_error_pulse, clear_aborted_pulse;
    logic irq_enable;
    logic armed, done_sticky, error_sticky, aborted_sticky;
    logic rx_receiving, rx_frame_valid, rx_frame_error;
    logic [15:0] rx_error_code;
    logic [31:0] frame_id, config_id;
    logic [7:0] modulation;
    logic [15:0] flags, ns;
    logic [31:0] seed;
    logic [15:0] fixed_profile_id, algorithm_profile_id;
    logic [15:0] min_iterations, max_iterations;
    logic signed [31:0] stop_threshold_raw, noise_variance_raw, proposal_step_raw;
    logic signed [31:0] g_re [0:3];
    logic signed [31:0] g_im [0:3];
    logic signed [31:0] b_re [0:1];
    logic signed [31:0] b_im [0:1];
    logic signed [63:0] c_raw;

    logic kernel_busy, kernel_result_valid;
    logic [7:0] x_hat0, x_hat1;
    logic signed [63:0] best_residual_raw;
    logic [31:0] actual_iterations;
    logic [63:0] pl_cycles;
    logic [31:0] overflow_count, saturation_count;
    logic [15:0] kernel_warning_bits;
    logic [31:0] debug_signature;

    logic tx_busy, tx_result_valid;
    logic [15:0] tx_status, tx_warning;
    logic [31:0] tx_iterations, tx_error_detail;
    logic [7:0] tx_x0, tx_x1;
    logic signed [63:0] tx_residual;
    logic [63:0] tx_cycles;
    logic [31:0] tx_overflow, tx_saturation, tx_debug;
    logic [31:0] input_beats_q, output_beats_q;
    logic [15:0] status_error_code;
    logic [15:0] status_warning_bits;
    logic [31:0] debug_state;
    logic flush_datapath;
    logic accept_start;

    assign flush_datapath = soft_reset_pulse || abort_pulse;
    assign accept_start = start_pulse && !armed && !rx_receiving &&
                          !kernel_busy && !tx_busy;
    assign tx_result_valid = kernel_result_valid || rx_frame_error;
    assign tx_status       = rx_frame_error ? STATUS_STREAM_ERROR : STATUS_OK;
    assign tx_warning      = rx_frame_error ? 16'd0 : kernel_warning_bits;
    assign tx_iterations   = rx_frame_error ? 32'd0 : actual_iterations;
    assign tx_x0           = rx_frame_error ? 8'd0 : x_hat0;
    assign tx_x1           = rx_frame_error ? 8'd0 : x_hat1;
    assign tx_debug        = rx_frame_error ? 32'd0 : debug_signature;
    assign tx_residual     = rx_frame_error ? 64'sd0 : best_residual_raw;
    assign tx_cycles       = rx_frame_error ? 64'd0 : pl_cycles;
    assign tx_overflow     = rx_frame_error ? 32'd0 : overflow_count;
    assign tx_saturation   = rx_frame_error ? 32'd0 : saturation_count;
    assign tx_error_detail = rx_frame_error ? {16'd0, rx_error_code} : 32'd0;

    assign debug_state = {24'd0, tx_busy, kernel_busy, rx_receiving,
                          armed, aborted_sticky, error_sticky,
                          done_sticky, irq_enable};

    mimo_axis_frame_rx u_rx (
        .aclk(aclk), .aresetn(aresetn), .arm(accept_start), .flush(flush_datapath),
        .s_axis_tdata(s_axis_tdata), .s_axis_tkeep(s_axis_tkeep),
        .s_axis_tvalid(s_axis_tvalid), .s_axis_tready(s_axis_tready),
        .s_axis_tlast(s_axis_tlast),
        .receiving(rx_receiving), .frame_valid(rx_frame_valid),
        .frame_error(rx_frame_error), .error_code(rx_error_code),
        .frame_id(frame_id), .config_id(config_id), .modulation(modulation),
        .flags(flags), .ns(ns), .seed(seed),
        .fixed_profile_id(fixed_profile_id),
        .algorithm_profile_id(algorithm_profile_id),
        .min_iterations(min_iterations), .max_iterations(max_iterations),
        .stop_threshold_raw(stop_threshold_raw),
        .noise_variance_raw(noise_variance_raw),
        .proposal_step_raw(proposal_step_raw),
        .g_re(g_re), .g_im(g_im), .b_re(b_re), .b_im(b_im), .c_raw(c_raw)
    );

    mimo_mhgd_kernel_2x2 u_kernel (
        .aclk(aclk), .aresetn(aresetn), .flush(flush_datapath),
        .frame_valid(rx_frame_valid), .modulation(modulation), .flags(flags),
        .max_iterations(max_iterations), .seed(seed),
        .proposal_step_raw(proposal_step_raw),
        .g_re(g_re), .g_im(g_im), .b_re(b_re), .b_im(b_im), .c_raw(c_raw),
        .busy(kernel_busy), .result_valid(kernel_result_valid),
        .x_hat0(x_hat0), .x_hat1(x_hat1),
        .best_residual_raw(best_residual_raw),
        .actual_iterations(actual_iterations), .pl_cycles(pl_cycles),
        .overflow_count(overflow_count), .saturation_count(saturation_count),
        .warning_bits(kernel_warning_bits), .debug_signature(debug_signature)
    );

    mimo_axis_frame_tx #(.BUILD_ID(BUILD_ID)) u_tx (
        .aclk(aclk), .aresetn(aresetn), .flush(flush_datapath),
        .result_valid(tx_result_valid), .result_frame_id(frame_id),
        .result_config_id(config_id), .result_status(tx_status),
        .warning_bits(tx_warning), .actual_iterations(tx_iterations),
        .x_hat0(tx_x0), .x_hat1(tx_x1), .debug_signature(tx_debug),
        .best_residual_raw(tx_residual), .pl_cycles(tx_cycles),
        .overflow_count(tx_overflow), .saturation_count(tx_saturation),
        .error_detail(tx_error_detail), .busy(tx_busy),
        .m_axis_tdata(m_axis_tdata), .m_axis_tkeep(m_axis_tkeep),
        .m_axis_tvalid(m_axis_tvalid), .m_axis_tready(m_axis_tready),
        .m_axis_tlast(m_axis_tlast)
    );

    mimo_axi_lite_regs #(
        .ADDR_WIDTH(AXIL_ADDR_WIDTH), .BUILD_ID(BUILD_ID)
    ) u_regs (
        .aclk(aclk), .aresetn(aresetn),
        .s_axi_awaddr(s_axi_awaddr), .s_axi_awvalid(s_axi_awvalid),
        .s_axi_awready(s_axi_awready), .s_axi_wdata(s_axi_wdata),
        .s_axi_wstrb(s_axi_wstrb), .s_axi_wvalid(s_axi_wvalid),
        .s_axi_wready(s_axi_wready), .s_axi_bresp(s_axi_bresp),
        .s_axi_bvalid(s_axi_bvalid), .s_axi_bready(s_axi_bready),
        .s_axi_araddr(s_axi_araddr), .s_axi_arvalid(s_axi_arvalid),
        .s_axi_arready(s_axi_arready), .s_axi_rdata(s_axi_rdata),
        .s_axi_rresp(s_axi_rresp), .s_axi_rvalid(s_axi_rvalid),
        .s_axi_rready(s_axi_rready),
        .status_idle(!armed && !rx_receiving && !kernel_busy && !tx_busy),
        .status_armed(armed), .status_busy(rx_receiving || kernel_busy || tx_busy),
        .status_done(done_sticky), .status_error(error_sticky),
        .status_aborted(aborted_sticky), .error_code(status_error_code),
        .warning_bits(status_warning_bits), .input_beats(input_beats_q),
        .output_beats(output_beats_q), .frame_id(frame_id),
        .actual_iterations(actual_iterations), .cycle_count(pl_cycles),
        .overflow_count(overflow_count), .saturation_count(saturation_count),
        .debug_state(debug_state), .start_pulse(start_pulse),
        .abort_pulse(abort_pulse), .soft_reset_pulse(soft_reset_pulse),
        .clear_done_pulse(clear_done_pulse),
        .clear_error_pulse(clear_error_pulse),
        .clear_aborted_pulse(clear_aborted_pulse),
        .irq_enable(irq_enable), .irq(irq)
    );

    always_ff @(posedge aclk) begin
        if (!aresetn) begin
            armed              <= 1'b0;
            done_sticky        <= 1'b0;
            error_sticky       <= 1'b0;
            aborted_sticky     <= 1'b0;
            input_beats_q      <= 32'd0;
            output_beats_q     <= 32'd0;
            status_error_code  <= 16'd0;
            status_warning_bits<= 16'd0;
        end else begin
            if (soft_reset_pulse) begin
                armed               <= 1'b0;
                done_sticky         <= 1'b0;
                error_sticky        <= 1'b0;
                aborted_sticky      <= 1'b0;
                input_beats_q       <= 32'd0;
                output_beats_q      <= 32'd0;
                status_error_code   <= 16'd0;
                status_warning_bits <= 16'd0;
            end else begin
                if (clear_done_pulse)    done_sticky    <= 1'b0;
                if (clear_error_pulse)   error_sticky   <= 1'b0;
                if (clear_aborted_pulse) aborted_sticky <= 1'b0;

                if (accept_start) begin
                    armed          <= 1'b1;
                    done_sticky    <= 1'b0;
                    error_sticky   <= 1'b0;
                    aborted_sticky <= 1'b0;
                    input_beats_q  <= 32'd0;
                    output_beats_q <= 32'd0;
                end else if (start_pulse) begin
                    error_sticky      <= 1'b1;
                    status_error_code <= 16'h0201;
                end

                if (s_axis_tvalid && s_axis_tready)
                    input_beats_q <= input_beats_q + 32'd1;
                if (m_axis_tvalid && m_axis_tready)
                    output_beats_q <= output_beats_q + 32'd1;

                if (rx_frame_valid) begin
                    armed <= 1'b0;
                end
                if (rx_frame_error) begin
                    armed             <= 1'b0;
                    error_sticky      <= 1'b1;
                    status_error_code <= rx_error_code;
                end
                if (kernel_result_valid) begin
                    status_warning_bits <= kernel_warning_bits;
                end
                if (m_axis_tvalid && m_axis_tready && m_axis_tlast)
                    done_sticky <= 1'b1;

                if (abort_pulse) begin
                    armed          <= 1'b0;
                    aborted_sticky <= 1'b1;
                end
            end
        end
    end

endmodule
