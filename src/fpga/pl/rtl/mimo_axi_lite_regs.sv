`timescale 1ns/1ps

module mimo_axi_lite_regs #(
    parameter int ADDR_WIDTH = 6,
    parameter logic [31:0] BUILD_ID = 32'h0000_0001
) (
    input  logic                  aclk,
    input  logic                  aresetn,

    input  logic [ADDR_WIDTH-1:0] s_axi_awaddr,
    input  logic                  s_axi_awvalid,
    output logic                  s_axi_awready,
    input  logic [31:0]           s_axi_wdata,
    input  logic [3:0]            s_axi_wstrb,
    input  logic                  s_axi_wvalid,
    output logic                  s_axi_wready,
    output logic [1:0]            s_axi_bresp,
    output logic                  s_axi_bvalid,
    input  logic                  s_axi_bready,

    input  logic [ADDR_WIDTH-1:0] s_axi_araddr,
    input  logic                  s_axi_arvalid,
    output logic                  s_axi_arready,
    output logic [31:0]           s_axi_rdata,
    output logic [1:0]            s_axi_rresp,
    output logic                  s_axi_rvalid,
    input  logic                  s_axi_rready,

    input  logic                  status_idle,
    input  logic                  status_armed,
    input  logic                  status_busy,
    input  logic                  status_done,
    input  logic                  status_error,
    input  logic                  status_aborted,
    input  logic [15:0]           error_code,
    input  logic [15:0]           warning_bits,
    input  logic [31:0]           input_beats,
    input  logic [31:0]           output_beats,
    input  logic [31:0]           frame_id,
    input  logic [31:0]           actual_iterations,
    input  logic [63:0]           cycle_count,
    input  logic [31:0]           overflow_count,
    input  logic [31:0]           saturation_count,
    input  logic [31:0]           debug_state,

    output logic                  start_pulse,
    output logic                  abort_pulse,
    output logic                  soft_reset_pulse,
    output logic                  clear_done_pulse,
    output logic                  clear_error_pulse,
    output logic                  clear_aborted_pulse,
    output logic                  irq_enable,
    output logic                  irq
);

    localparam logic [31:0] CORE_ID      = 32'h4D48_4744; // MHGD
    localparam logic [31:0] CORE_VERSION = 32'h0001_0000;

    logic write_fire, read_fire;
    logic [ADDR_WIDTH-1:0] write_addr_q;
    logic [31:0] write_data_q, read_data_next;
    logic [3:0] write_strb_q;
    logic aw_pending, w_pending;
    logic [31:0] status_word;

    assign write_fire = aw_pending && w_pending && !s_axi_bvalid;
    assign read_fire  = s_axi_arvalid && s_axi_arready;

    // AXI-Lite address and data channels may arrive in either order.
    // Keep one outstanding write, with independent one-entry buffers.
    assign s_axi_awready = aresetn && !aw_pending && !s_axi_bvalid;
    assign s_axi_wready  = aresetn && !w_pending && !s_axi_bvalid;
    assign s_axi_arready = aresetn && !s_axi_rvalid;
    assign s_axi_bresp   = 2'b00;
    assign s_axi_rresp   = 2'b00;

    always_comb begin
        status_word = 32'd0;
        status_word[0] = status_idle;
        status_word[1] = status_armed;
        status_word[2] = status_busy;
        status_word[3] = status_done;
        status_word[4] = status_error;
        status_word[5] = status_aborted;

        case (s_axi_araddr[5:2])
            4'h0: read_data_next = CORE_ID;
            4'h1: read_data_next = CORE_VERSION;
            4'h2: read_data_next = {23'd0, irq_enable, 8'd0};
            4'h3: read_data_next = status_word;
            4'h4: read_data_next = {16'd0, error_code};
            4'h5: read_data_next = {16'd0, warning_bits};
            4'h6: read_data_next = input_beats;
            4'h7: read_data_next = output_beats;
            4'h8: read_data_next = frame_id;
            4'h9: read_data_next = actual_iterations;
            4'hA: read_data_next = cycle_count[31:0];
            4'hB: read_data_next = cycle_count[63:32];
            4'hC: read_data_next = overflow_count;
            4'hD: read_data_next = saturation_count;
            4'hE: read_data_next = debug_state;
            4'hF: read_data_next = BUILD_ID;
            default: read_data_next = 32'd0;
        endcase
    end

    assign irq = irq_enable && (status_done || status_error || status_aborted);

    always_ff @(posedge aclk) begin
        if (!aresetn) begin
            s_axi_bvalid        <= 1'b0;
            s_axi_rvalid        <= 1'b0;
            s_axi_rdata         <= '0;
            aw_pending          <= 1'b0;
            w_pending           <= 1'b0;
            write_addr_q        <= '0;
            write_data_q        <= '0;
            write_strb_q        <= '0;
            start_pulse         <= 1'b0;
            abort_pulse         <= 1'b0;
            soft_reset_pulse    <= 1'b0;
            clear_done_pulse    <= 1'b0;
            clear_error_pulse   <= 1'b0;
            clear_aborted_pulse <= 1'b0;
            irq_enable          <= 1'b0;
        end else begin
            start_pulse         <= 1'b0;
            abort_pulse         <= 1'b0;
            soft_reset_pulse    <= 1'b0;
            clear_done_pulse    <= 1'b0;
            clear_error_pulse   <= 1'b0;
            clear_aborted_pulse <= 1'b0;

            if (s_axi_awvalid && s_axi_awready) begin
                aw_pending <= 1'b1;
                write_addr_q <= s_axi_awaddr;
            end
            if (s_axi_wvalid && s_axi_wready) begin
                w_pending <= 1'b1;
                write_data_q <= s_axi_wdata;
                write_strb_q <= s_axi_wstrb;
            end
            if (write_fire) begin
                aw_pending <= 1'b0;
                w_pending <= 1'b0;
                s_axi_bvalid <= 1'b1;
                if (write_addr_q[5:2] == 4'h2) begin
                    if (write_strb_q[0]) begin
                        start_pulse      <= write_data_q[0];
                        abort_pulse      <= write_data_q[1];
                        soft_reset_pulse <= write_data_q[2];
                    end
                    if (write_strb_q[1])
                        irq_enable <= write_data_q[8];
                end else if (write_addr_q[5:2] == 4'h3 && write_strb_q[0]) begin
                    clear_done_pulse    <= write_data_q[3];
                    clear_error_pulse   <= write_data_q[4];
                    clear_aborted_pulse <= write_data_q[5];
                end
            end else if (s_axi_bvalid && s_axi_bready) begin
                s_axi_bvalid <= 1'b0;
            end

            if (read_fire) begin
                // Freeze the response at AR handshake, including live counters.
                // RDATA must remain stable for the entire RVALID stall.
                s_axi_rdata  <= read_data_next;
                s_axi_rvalid <= 1'b1;
            end else if (s_axi_rvalid && s_axi_rready) begin
                s_axi_rvalid <= 1'b0;
            end
        end
    end

endmodule
