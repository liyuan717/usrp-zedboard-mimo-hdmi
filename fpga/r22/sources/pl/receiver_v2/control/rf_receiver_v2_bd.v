`timescale 1ns/1ps
// Vivado2025.2 module-reference tops must be Verilog/VHDL, not SystemVerilog.
// This wire-only boundary instantiates the real SV core, never a black box.
module rf_receiver_v2_bd #(
    parameter [31:0] BUILD_ID=32'h20261027,
    parameter integer PROPOSAL_POLICY=0,
    parameter NCO_MEM_FILE="pl/receiver_v2/dsp/rtl/rxv2_nco_q15.mem"
) (
    (* X_INTERFACE_INFO="xilinx.com:signal:clock:1.0 aclk CLK" *)
    (* X_INTERFACE_PARAMETER="ASSOCIATED_BUSIF s_axi:s_axis:m_axis, ASSOCIATED_RESET aresetn, FREQ_HZ 50000000" *)
    input wire aclk,
    (* X_INTERFACE_INFO="xilinx.com:signal:reset:1.0 aresetn RST" *)
    (* X_INTERFACE_PARAMETER="POLARITY ACTIVE_LOW" *) input wire aresetn,
    input wire [15:0] s_axi_awaddr,input wire s_axi_awvalid,output wire s_axi_awready,
    input wire [31:0] s_axi_wdata,input wire [3:0] s_axi_wstrb,input wire s_axi_wvalid,output wire s_axi_wready,
    output wire [1:0] s_axi_bresp,output wire s_axi_bvalid,input wire s_axi_bready,
    input wire [15:0] s_axi_araddr,input wire s_axi_arvalid,output wire s_axi_arready,
    output wire [31:0] s_axi_rdata,output wire [1:0] s_axi_rresp,output wire s_axi_rvalid,input wire s_axi_rready,
    input wire [63:0] s_axis_tdata,input wire [7:0] s_axis_tkeep,
    input wire s_axis_tvalid,output wire s_axis_tready,input wire s_axis_tlast,
    output wire [63:0] m_axis_tdata,output wire [7:0] m_axis_tkeep,
    output wire m_axis_tvalid,input wire m_axis_tready,output wire m_axis_tlast,
    (* X_INTERFACE_IGNORE="true" *) input wire ingress_overflow,
    (* X_INTERFACE_IGNORE="true" *) output wire ingress_flush,
    (* X_INTERFACE_IGNORE="true" *) input wire rf_path_ready,
    (* X_INTERFACE_IGNORE="true" *) input wire capture_epoch_fence
);
    // Build identity is the deployed implementation-policy discriminator.
    // Keep the baseline default available for regression, but fail elaboration
    // for a mixed solver-policy/Build-ID image.
    generate
        if (!(((PROPOSAL_POLICY == 0) && (BUILD_ID == 32'h20261027)) ||
              ((PROPOSAL_POLICY == 1) && (BUILD_ID == 32'h20261028)))) begin : g_bad_policy_identity
            initial $error("rf_receiver_v2_bd PROPOSAL_POLICY/BUILD_ID mismatch");
        end
    endgenerate

    rf_receiver_v2_core #(.BUILD_ID(BUILD_ID),.PROPOSAL_POLICY(PROPOSAL_POLICY),.NCO_MEM_FILE(NCO_MEM_FILE)) core(
        .aclk(aclk),.aresetn(aresetn),
        .s_axi_awaddr(s_axi_awaddr),.s_axi_awvalid(s_axi_awvalid),.s_axi_awready(s_axi_awready),
        .s_axi_wdata(s_axi_wdata),.s_axi_wstrb(s_axi_wstrb),.s_axi_wvalid(s_axi_wvalid),.s_axi_wready(s_axi_wready),
        .s_axi_bresp(s_axi_bresp),.s_axi_bvalid(s_axi_bvalid),.s_axi_bready(s_axi_bready),
        .s_axi_araddr(s_axi_araddr),.s_axi_arvalid(s_axi_arvalid),.s_axi_arready(s_axi_arready),
        .s_axi_rdata(s_axi_rdata),.s_axi_rresp(s_axi_rresp),.s_axi_rvalid(s_axi_rvalid),.s_axi_rready(s_axi_rready),
        .s_axis_tdata(s_axis_tdata),.s_axis_tkeep(s_axis_tkeep),.s_axis_tvalid(s_axis_tvalid),.s_axis_tready(s_axis_tready),.s_axis_tlast(s_axis_tlast),
        .m_axis_tdata(m_axis_tdata),.m_axis_tkeep(m_axis_tkeep),.m_axis_tvalid(m_axis_tvalid),.m_axis_tready(m_axis_tready),.m_axis_tlast(m_axis_tlast),
        .ingress_overflow(ingress_overflow),.ingress_flush(ingress_flush),.rf_path_ready(rf_path_ready),
        .capture_epoch_fence(capture_epoch_fence));
endmodule
