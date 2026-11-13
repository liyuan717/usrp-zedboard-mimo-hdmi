`timescale 1ns/1ps
// G0 diagnostic only: raw ADC records, not the production V3 source-symbol ABI.
// One record is tag64 LE then I0,Q0,I1,Q1 as four LE16 values (16 bytes).
// Tag upper16=source-reset epoch, lower48=ADC-domain accepted sample index.
// AXIS TLAST stays low: ADI S2MM terminates finite captures by byte count.
module g0_adc_record (
    (* X_INTERFACE_INFO="xilinx.com:signal:clock:1.0 aclk CLK" *)
    (* X_INTERFACE_PARAMETER="ASSOCIATED_BUSIF s_axi:s_axis:m_axis, ASSOCIATED_RESET aresetn, FREQ_HZ 50000000" *) input wire aclk,
    (* X_INTERFACE_INFO="xilinx.com:signal:reset:1.0 aresetn RST" *)
    (* X_INTERFACE_PARAMETER="POLARITY ACTIVE_LOW" *) input wire aresetn,
    input wire [15:0] s_axi_awaddr, input wire s_axi_awvalid, output wire s_axi_awready,
    input wire [31:0] s_axi_wdata, input wire [3:0] s_axi_wstrb, input wire s_axi_wvalid, output wire s_axi_wready,
    output wire [1:0] s_axi_bresp, output reg s_axi_bvalid, input wire s_axi_bready,
    input wire [15:0] s_axi_araddr, input wire s_axi_arvalid, output wire s_axi_arready,
    output reg [31:0] s_axi_rdata, output wire [1:0] s_axi_rresp, output reg s_axi_rvalid, input wire s_axi_rready,
    input wire [127:0] s_axis_tdata, input wire [15:0] s_axis_tkeep,
    input wire s_axis_tvalid, output wire s_axis_tready, input wire s_axis_tlast,
    output wire [63:0] m_axis_tdata, output wire [7:0] m_axis_tkeep,
    output wire m_axis_tvalid, input wire m_axis_tready, output wire m_axis_tlast,
    (* X_INTERFACE_IGNORE="true" *) input wire ingress_overflow,
    (* X_INTERFACE_IGNORE="true" *) output wire ingress_flush,
    (* X_INTERFACE_IGNORE="true" *) input wire rf_path_ready,
    (* X_INTERFACE_IGNORE="true" *) input wire capture_epoch_fence
);
    reg run;
    reg [31:0] limit_records,remaining;
    reg [127:0] record;
    reg pending, beat;
    reg [63:0] delivered, accepted;
    reg [31:0] loss_events, bad_keep, fence_events;
    reg fence_prev;
    reg aw_pending,w_pending;
    reg [15:0] awaddr;
    reg [31:0] wdata;
    reg [3:0] wstrb;
    assign s_axi_awready = !aw_pending && !s_axi_bvalid;
    assign s_axi_wready = !w_pending && !s_axi_bvalid;
    assign s_axi_bresp=0;
    assign s_axi_arready = !s_axi_rvalid || s_axi_rready;
    assign s_axi_rresp=0;
    // STOP stops admitting new records but never changes a held AXIS beat.
    // The owner drains the final complete record before DMA termination.
    assign s_axis_tready = !run || !pending || (beat && m_axis_tready);
    assign m_axis_tvalid = pending;
    assign m_axis_tdata = beat ? record[63:0] : record[127:64];
    assign m_axis_tkeep=8'hff;
    assign m_axis_tlast=1'b0;
    // The reset guard waits for the REAL elastic-FIFO flush acknowledgement
    // before it reopens the writer. Return its FCLK-domain fence, otherwise
    // startup deadlocks in DST_WAIT_ACK. Overflow is recorded, not hidden by
    // an automatic flush: source tags expose the resulting missing scans.
    assign ingress_flush=capture_epoch_fence;
    wire take = s_axis_tvalid && s_axis_tready;
    wire send = m_axis_tvalid && m_axis_tready;
    always @(posedge aclk) begin
        if(!aresetn) begin
            run<=0;limit_records<=0;remaining<=0;record<=0;pending<=0;beat<=0;delivered<=0;accepted<=0;
            loss_events<=0;bad_keep<=0;fence_events<=0;fence_prev<=0;
            aw_pending<=0;w_pending<=0;awaddr<=0;wdata<=0;wstrb<=0;
            s_axi_bvalid<=0;s_axi_rvalid<=0;s_axi_rdata<=0;
        end else begin
            if(s_axi_awvalid && s_axi_awready) begin aw_pending<=1;awaddr<=s_axi_awaddr;end
            if(s_axi_wvalid && s_axi_wready) begin w_pending<=1;wdata<=s_axi_wdata;wstrb<=s_axi_wstrb;end
            if(s_axi_bvalid && s_axi_bready)s_axi_bvalid<=0;
            if(aw_pending && w_pending && !s_axi_bvalid) begin
                aw_pending<=0;w_pending<=0;s_axi_bvalid<=1;
                if(awaddr[15:2]==14'h2 && wstrb[0]) begin
                    if(!wdata[0])run<=0;
                    else if(!run && !pending)begin run<=1;remaining<=limit_records;end
                end
                // Finite capture admits exactly this many complete records,
                // then drains the last one without prefetching another. Zero
                // selects the unbounded serializer mode used by RTL tests.
                if(awaddr[15:2]==14'hb && !run && !pending && wstrb==4'hf)limit_records<=wdata;
            end
            if(s_axi_rvalid && s_axi_rready)s_axi_rvalid<=0;
            if(s_axi_arvalid && s_axi_arready) begin
                s_axi_rvalid<=1;
                case(s_axi_araddr[15:2])
                    0:s_axi_rdata<=32'h44304756; // "VG0D"
                    1:s_axi_rdata<=32'h20261102;
                    2:s_axi_rdata<={31'b0,run};
                    3:s_axi_rdata<={28'b0,beat,pending,run,rf_path_ready};
                    4:s_axi_rdata<=delivered[31:0];
                    5:s_axi_rdata<=delivered[63:32];
                    6:s_axi_rdata<=loss_events;
                    7:s_axi_rdata<=bad_keep;
                    8:s_axi_rdata<=fence_events;
                    9:s_axi_rdata<=accepted[31:0];
                    10:s_axi_rdata<=accepted[63:32];
                    11:s_axi_rdata<=limit_records;
                    12:s_axi_rdata<=remaining;
                    default:s_axi_rdata<=0;
                endcase
            end
            if(ingress_overflow)loss_events<=loss_events+1'b1;
            fence_prev<=capture_epoch_fence;
            if(capture_epoch_fence && !fence_prev)fence_events<=fence_events+1'b1;
            if(send) begin
                if(beat)begin pending<=0;beat<=0;delivered<=delivered+1'b1;end
                else beat<=1;
            end
            if(take && run) begin
                if(s_axis_tkeep==16'hffff)begin
                    record<=s_axis_tdata;pending<=1;beat<=0;accepted<=accepted+1'b1;
                    if(limit_records!=0)begin
                        remaining<=remaining-1'b1;
                        if(remaining==1)run<=0;
                    end
                end else bad_keep<=bad_keep+1'b1;
            end
        end
    end
endmodule
