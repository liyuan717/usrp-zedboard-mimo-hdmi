`timescale 1ns/1ps

// Candidate-only AXI-Lite control shell.  It deliberately has a distinct
// identity and register map from the deployed detector, so no released Linux
// driver can accidentally program the batch-envelope experiment.
//
// Register map (32-bit accesses only):
//   0x00 ID           RO  "IMBC"
//   0x04 VERSION      RO  1.0
//   0x08 CAPABILITIES RO  bit0=batch envelope, bit1=software tag reorder,
//                            bit2=single simple-DMA transaction
//   0x0c MAX_TASKS    RO
//   0x10 CONTROL      WO  bit0 START, bit1 CANCEL
//   0x14 BATCH_COUNT  RW  staged 1..MAX_TASKS, only while idle
//   0x18 STATUS       RO  bit0 busy, bit1 done(sticky), bit2 error
//   0x1c ERROR_CODE   RO
//   0x20 ACCEPTED     RO
//   0x24 EMITTED      RO
// Any unsupported, partial-strobe, invalid-count, or busy-time configuration
// write returns SLVERR and has no side effect.  START while idle with a valid
// staged count is accepted; CANCEL is a pulse and must be paired by software
// with resetting both simple-DMA channels before another START.
module mimo_mhgd_interleaved_axis_candidate_axil #(
    parameter int MAX_CONTEXTS = 10,
    parameter int MAX_BATCH_TASKS = 528,
    parameter logic [31:0] BUILD_ID = 32'h0000_0001,
    parameter int AXIL_ADDR_WIDTH = 6
) (
    input logic aclk,
    input logic aresetn,
    input logic [AXIL_ADDR_WIDTH-1:0] s_axi_awaddr,
    input logic s_axi_awvalid,
    output logic s_axi_awready,
    input logic [31:0] s_axi_wdata,
    input logic [3:0] s_axi_wstrb,
    input logic s_axi_wvalid,
    output logic s_axi_wready,
    output logic [1:0] s_axi_bresp,
    output logic s_axi_bvalid,
    input logic s_axi_bready,
    input logic [AXIL_ADDR_WIDTH-1:0] s_axi_araddr,
    input logic s_axi_arvalid,
    output logic s_axi_arready,
    output logic [31:0] s_axi_rdata,
    output logic [1:0] s_axi_rresp,
    output logic s_axi_rvalid,
    input logic s_axi_rready,
    input logic [63:0] s_axis_tdata,
    input logic [7:0] s_axis_tkeep,
    input logic s_axis_tvalid,
    output logic s_axis_tready,
    input logic s_axis_tlast,
    output logic [63:0] m_axis_tdata,
    output logic [7:0] m_axis_tkeep,
    output logic m_axis_tvalid,
    input logic m_axis_tready,
    output logic m_axis_tlast
);
    localparam logic [31:0] CORE_ID = 32'h494D_4243; // "IMBC"
    localparam logic [31:0] CORE_VERSION = 32'h0001_0000;
    localparam logic [31:0] CAPABILITIES = 32'h0000_0007;
    localparam logic [1:0] AXI_OKAY = 2'b00;
    localparam logic [1:0] AXI_SLVERR = 2'b10;

    logic aw_pending, w_pending, write_fire, read_fire;
    logic [AXIL_ADDR_WIDTH-1:0] write_addr_q;
    logic [31:0] write_data_q, read_data_next;
    logic [3:0] write_strb_q;
    logic [15:0] batch_count_q;
    logic start_pulse, cancel_pulse;
    logic batch_busy, batch_done, batch_error;
    logic [15:0] batch_error_code, accepted_tasks, emitted_tasks;
    logic done_sticky;

    assign write_fire = aw_pending && w_pending && !s_axi_bvalid;
    assign read_fire = s_axi_arvalid && s_axi_arready;
    assign s_axi_awready = aresetn && !aw_pending && !s_axi_bvalid;
    assign s_axi_wready = aresetn && !w_pending && !s_axi_bvalid;
    assign s_axi_arready = aresetn && !s_axi_rvalid;
    assign s_axi_rresp = AXI_OKAY;

    always_comb begin
        unique case (s_axi_araddr[5:2])
            4'h0: read_data_next = CORE_ID;
            4'h1: read_data_next = CORE_VERSION;
            4'h2: read_data_next = CAPABILITIES;
            4'h3: read_data_next = MAX_BATCH_TASKS;
            4'h4: read_data_next = 32'd0;
            4'h5: read_data_next = {16'd0, batch_count_q};
            4'h6: read_data_next = {29'd0, batch_error, done_sticky, batch_busy};
            4'h7: read_data_next = {16'd0, batch_error_code};
            4'h8: read_data_next = {16'd0, accepted_tasks};
            4'h9: read_data_next = {16'd0, emitted_tasks};
            4'hf: read_data_next = BUILD_ID;
            default: read_data_next = 32'd0;
        endcase
    end

    mimo_mhgd_interleaved_axis_candidate #(
        .MAX_CONTEXTS(MAX_CONTEXTS), .MAX_BATCH_TASKS(MAX_BATCH_TASKS), .BUILD_ID(BUILD_ID)
    ) u_candidate (
        .aclk(aclk), .aresetn(aresetn), .batch_start(start_pulse),
        .batch_count(batch_count_q), .batch_cancel(cancel_pulse),
        .s_axis_tdata(s_axis_tdata), .s_axis_tkeep(s_axis_tkeep),
        .s_axis_tvalid(s_axis_tvalid), .s_axis_tready(s_axis_tready), .s_axis_tlast(s_axis_tlast),
        .m_axis_tdata(m_axis_tdata), .m_axis_tkeep(m_axis_tkeep),
        .m_axis_tvalid(m_axis_tvalid), .m_axis_tready(m_axis_tready), .m_axis_tlast(m_axis_tlast),
        .batch_busy(batch_busy), .batch_done(batch_done), .batch_error(batch_error),
        .batch_error_code(batch_error_code), .accepted_tasks(accepted_tasks), .emitted_tasks(emitted_tasks)
    );

    always_ff @(posedge aclk) begin
        if (!aresetn) begin
            s_axi_bvalid <= 1'b0;
            s_axi_bresp <= AXI_OKAY;
            s_axi_rvalid <= 1'b0;
            s_axi_rdata <= 32'd0;
            aw_pending <= 1'b0;
            w_pending <= 1'b0;
            write_addr_q <= '0;
            write_data_q <= 32'd0;
            write_strb_q <= 4'd0;
            batch_count_q <= 16'd0;
            start_pulse <= 1'b0;
            cancel_pulse <= 1'b0;
            done_sticky <= 1'b0;
        end else begin
            start_pulse <= 1'b0;
            cancel_pulse <= 1'b0;
            if (batch_done)
                done_sticky <= 1'b1;
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
                s_axi_bresp <= AXI_SLVERR;
                unique case (write_addr_q[5:2])
                    4'h4: begin
                        if (write_strb_q == 4'hf &&
                            ((write_data_q == 32'h1 && !batch_busy) || write_data_q == 32'h2)) begin
                            if (write_data_q[0]) begin
                                start_pulse <= 1'b1;
                                done_sticky <= 1'b0;
                            end else begin
                                cancel_pulse <= 1'b1;
                            end
                            s_axi_bresp <= AXI_OKAY;
                        end
                    end
                    4'h5: begin
                        if (write_strb_q == 4'hf && !batch_busy &&
                            write_data_q[31:16] == 16'd0 && write_data_q[15:0] != 16'd0 &&
                            write_data_q[15:0] <= MAX_BATCH_TASKS) begin
                            batch_count_q <= write_data_q[15:0];
                            s_axi_bresp <= AXI_OKAY;
                        end
                    end
                    default: begin end
                endcase
            end else if (s_axi_bvalid && s_axi_bready) begin
                s_axi_bvalid <= 1'b0;
            end
            if (read_fire) begin
                s_axi_rdata <= read_data_next;
                s_axi_rvalid <= 1'b1;
            end else if (s_axi_rvalid && s_axi_rready) begin
                s_axi_rvalid <= 1'b0;
            end
        end
    end
endmodule
