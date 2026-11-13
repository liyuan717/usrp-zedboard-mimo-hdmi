`timescale 1ns/1ps

// Small AXI-Lite control plane. All high-rate receive data stays in PL;
// this interface is only start/stop, bounded fault recovery and statistics.
module rfv2_control_regs #(
    parameter logic [31:0] BUILD_ID=32'h20261027
) (
    input wire clk,input wire resetn,
    input wire [15:0] s_axi_awaddr,input wire s_axi_awvalid,output wire s_axi_awready,
    input wire [31:0] s_axi_wdata,input wire [3:0] s_axi_wstrb,input wire s_axi_wvalid,output wire s_axi_wready,
    output logic [1:0] s_axi_bresp,output logic s_axi_bvalid,input wire s_axi_bready,
    input wire [15:0] s_axi_araddr,input wire s_axi_arvalid,output wire s_axi_arready,
    output logic [31:0] s_axi_rdata,output logic [1:0] s_axi_rresp,output logic s_axi_rvalid,input wire s_axi_rready,
    output logic run,output logic flush_pulse,
    input wire locked,input wire datapath_busy,input wire fault,input wire rf_path_ready,
    input wire [31:0] epoch,input wire [31:0] fault_code,
    input wire [63:0] sample_count,input wire [31:0] frames_ok,
    input wire [31:0] frames_rejected,input wire [31:0] ingress_overflows,
    input wire [31:0] backpressure_cycles,
    // Number of complete AXIS slots accepted by the output DMAC. A driver
    // must ALSO fence completed-period callbacks before terminating DMA.
    input wire [31:0] output_slots,input wire [31:0] format_errors
);
    logic aw_pending,w_pending;
    logic [15:0] aw_q;
    logic [31:0] wd_q;
    logic [3:0] ws_q;
    logic [63:0] sample_snapshot;
    wire [31:0] byte_mask={{8{ws_q[3]}},{8{ws_q[2]}},{8{ws_q[1]}},{8{ws_q[0]}}};
    wire [31:0] write_bits=wd_q&byte_mask;
    assign s_axi_awready=resetn&&!aw_pending&&!s_axi_bvalid;
    assign s_axi_wready=resetn&&!w_pending&&!s_axi_bvalid;
    assign s_axi_arready=resetn&&!s_axi_rvalid;
    always_ff @(posedge clk)begin
        if(!resetn)begin
            aw_pending<=0;w_pending<=0;aw_q<=0;wd_q<=0;ws_q<=0;
            s_axi_bresp<=0;s_axi_bvalid<=0;s_axi_rdata<=0;s_axi_rresp<=0;s_axi_rvalid<=0;
            run<=0;flush_pulse<=0;sample_snapshot<=0;
        end else begin
            flush_pulse<=0;
            if(s_axi_bvalid&&s_axi_bready)s_axi_bvalid<=0;
            if(s_axi_rvalid&&s_axi_rready)s_axi_rvalid<=0;
            if(s_axi_awvalid&&s_axi_awready)begin aw_pending<=1;aw_q<=s_axi_awaddr;end
            if(s_axi_wvalid&&s_axi_wready)begin w_pending<=1;wd_q<=s_axi_wdata;ws_q<=s_axi_wstrb;end
            if(aw_pending&&w_pending&&!s_axi_bvalid)begin
                aw_pending<=0;w_pending<=0;s_axi_bvalid<=1;
                if(aw_q!=16'h0010||(write_bits&32'hfffffefe)!=0)begin
                    s_axi_bresp<=2'b10;
                end else begin
                    s_axi_bresp<=2'b00;
                    if(ws_q[0])begin
                        run<=wd_q[0];
                        if(run&&!wd_q[0])flush_pulse<=1;
                    end
                    if(ws_q[1]&&wd_q[8])flush_pulse<=1;
                end
            end
            if(s_axi_arvalid&&s_axi_arready)begin
                s_axi_rvalid<=1;s_axi_rresp<=2'b00;
                case(s_axi_araddr)
                    16'h0000:s_axi_rdata<=32'h32564652;
                    16'h0004:s_axi_rdata<=32'h00020000;
                    16'h0008:s_axi_rdata<=BUILD_ID;
                    16'h000c:s_axi_rdata<=32'h0000000f;
                    16'h0010:s_axi_rdata<={31'd0,run};
                    16'h0014:s_axi_rdata<={27'd0,rf_path_ready,fault,datapath_busy,locked,run};
                    16'h0018:s_axi_rdata<=epoch;
                    16'h001c:s_axi_rdata<=fault_code;
                    16'h0020:begin sample_snapshot<=sample_count;s_axi_rdata<=sample_count[31:0];end
                    16'h0024:s_axi_rdata<=sample_snapshot[63:32];
                    16'h0028:s_axi_rdata<=frames_ok;
                    16'h002c:s_axi_rdata<=frames_rejected;
                    16'h0030:s_axi_rdata<=ingress_overflows;
                    16'h0034:s_axi_rdata<=backpressure_cycles;
                    16'h0038:s_axi_rdata<=output_slots;
                    16'h003c:s_axi_rdata<=format_errors;
                    default:begin s_axi_rdata<=0;s_axi_rresp<=2'b10;end
                endcase
            end
        end
    end
endmodule
