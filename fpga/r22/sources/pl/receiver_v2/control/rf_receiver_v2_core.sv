`timescale 1ns/1ps
// Clean RX-only BD boundary. This top intentionally references the COMPLETE
// acquisition frontend, never the cold-only engine or a known-frame fixture.
// Until all dependencies pass their gates this is NOT a deployable receiver.
module rf_receiver_v2_core #(
    parameter [31:0] BUILD_ID=32'h20261027,
    parameter integer PROPOSAL_POLICY=0,
    parameter NCO_MEM_FILE="pl/receiver_v2/dsp/rtl/rxv2_nco_q15.mem"
) (
    (* X_INTERFACE_PARAMETER="ASSOCIATED_BUSIF s_axi:s_axis:m_axis, ASSOCIATED_RESET aresetn, FREQ_HZ 50000000" *)
    input wire aclk,
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
    input wire ingress_overflow,output wire ingress_flush,
    input wire rf_path_ready,
    // Same-clock level request from the capture reset coordinator. Its rising
    // edge fences the ingress even before software RUN/clock-ready is enabled.
    input wire capture_epoch_fence
);
    wire run,ctrl_flush,terminal_fault,dsp_overflow,dsp_coordinate_fault;
    wire [31:0] epoch,overflow_count,format_errors,backpressure_cycles;
    wire [63:0] sample_count,scan_data,scan_sample;
    wire [31:0] scan_epoch;
    wire scan_disc,scan_valid,scan_ready;
    wire ingress_scan_ready;
    wire [127:0] filtered_data,symbol_data;
    wire [31:0] filtered_epoch,symbol_epoch;
    wire [63:0] filtered_sample,symbol_origin;
    wire filtered_disc,filtered_valid,filtered_ready;
    wire [9:0] symbol_index;
    wire symbol_begin,symbol_abort,locked,symbol_valid,symbol_ready;
    wire nco_commit;wire [31:0] nco_step,nco_phase,nco_epoch;wire [63:0] nco_sample;
    wire fb_valid;wire [31:0] fb_epoch;wire [63:0] fb_origin;wire signed [31:0] fb_delta;
    wire [319:0] ctx_headers;wire [31:0] ctx_id,ctx_epoch;wire [63:0] ctx_origin;
    wire signed [255:0] ctx_h_debug;
    wire ctx_valid,ctx_ready,channel_abort,channel_complete;
    wire [7:0] channel_code;wire [63:0] mip_data;wire [7:0] mip_keep;
    wire mip_valid,mip_ready,mip_last,solver_cancel,solver_busy,slot_done,slot_rejected;
    wire [15:0] solver_code;
    wire solver_protocol_fault;
    reg [31:0] frames_ok,frames_rejected,output_slots,last_fault;
    wire fatal=terminal_fault||solver_protocol_fault;
    // Do not feed ingress's own terminal fault back as a second new epoch.
    wire processing_fault=dsp_overflow||dsp_coordinate_fault||solver_protocol_fault;
    // Compute reset is independent of the slot/DMA owner. STOP/overflow may
    // reset NCO/channel, but must not revoke a committed output transaction.
    //
    // The capture guard has asynchronously-asserted local-reset registers.
    // Never let its ready/fence levels feed a BRAM EN/WE through combinational
    // ingress/DSP logic.  These are destination-clock command samplers; their
    // reset is synchronous to aclk, so their Qs are the only guard-derived
    // controls visible to the data path.
    // DONT_TOUCH is intentional: these are the physical cut points between
    // the guard's asynchronously-reset state and all BRAM-facing controls.
    // Without it, an OOC optimizer may propagate the guard level through the
    // command sampler and recreate an asynchronous RAM enable cone.
    (* ASYNC_REG="TRUE",SHREG_EXTRACT="NO",DONT_TOUCH="TRUE" *) reg [1:0] rf_path_ready_sync;
    (* ASYNC_REG="TRUE",SHREG_EXTRACT="NO",DONT_TOUCH="TRUE" *) reg [1:0] capture_fence_sync;
    (* DONT_TOUCH="TRUE" *) reg capture_fence_sync_q;
    wire capture_fence_cmd=capture_fence_sync[1]&&!capture_fence_sync_q;
    wire run_cmd=run&&rf_path_ready_sync[1];

    // Registered AXIS scan boundary.  It has no fall-through path: a guard-
    // gated capture TREADY/TVALID transition can at most be sampled here, and
    // never becomes a same-cycle BRAM control.  It accepts one scan per aclk
    // in steady state (pop/push may occur together), above 4 MSPS at 50 MHz.
    reg [63:0] ingress_scan_data;
    reg [7:0] ingress_scan_keep;
    reg ingress_scan_last,ingress_scan_valid;
    wire ingress_event;
    wire compute_request=run_cmd&&!ingress_event&&!fatal;
    // Kept as a read-only integration/diagnostic name.  It is deliberately
    // formed only from local synchronous command state; BRAM-facing logic
    // uses the registered barrier below.
    wire compute_enable=compute_request;
    wire ingress_scan_cancel=capture_fence_cmd||ingress_event||!run_cmd||fatal;
    // No sample/context can be admitted until the command sampler and this
    // registered compute barrier are both open.
    (* max_fanout=256,DONT_TOUCH="TRUE" *) reg compute_resetn;
    always @(posedge aclk)begin
        if(!aresetn)begin
            rf_path_ready_sync<=0;
            capture_fence_sync<=0;
            capture_fence_sync_q<=0;
            compute_resetn<=0;
            ingress_scan_data<=0;
            ingress_scan_keep<=0;
            ingress_scan_last<=0;
            ingress_scan_valid<=0;
        end else begin
            rf_path_ready_sync<={rf_path_ready_sync[0],rf_path_ready};
            capture_fence_sync<={capture_fence_sync[0],capture_epoch_fence};
            capture_fence_sync_q<=capture_fence_sync[1];
            compute_resetn<=compute_request;
            if(ingress_scan_cancel)ingress_scan_valid<=0;
            else if(s_axis_tvalid&&s_axis_tready)begin
                ingress_scan_data<=s_axis_tdata;
                ingress_scan_keep<=s_axis_tkeep;
                ingress_scan_last<=s_axis_tlast;
                ingress_scan_valid<=1;
            end else if(ingress_scan_valid&&ingress_scan_ready)
                ingress_scan_valid<=0;
        end
    end
    wire dsp_scan_ready;
    assign scan_ready=compute_resetn&&dsp_scan_ready;
    // Do not admit a post-fence raw beat into the registered boundary.  The
    // guard's source path can physically complete one earlier write; that
    // write is discarded by this cancel/epoch boundary and cannot create a
    // context in the new epoch.
    assign s_axis_tready=compute_resetn&&!ingress_scan_cancel&&
                         (!ingress_scan_valid||ingress_scan_ready);

    rfv2_control_regs #(.BUILD_ID(BUILD_ID)) regs(
        .clk(aclk),.resetn(aresetn),
        .s_axi_awaddr(s_axi_awaddr),.s_axi_awvalid(s_axi_awvalid),.s_axi_awready(s_axi_awready),
        .s_axi_wdata(s_axi_wdata),.s_axi_wstrb(s_axi_wstrb),.s_axi_wvalid(s_axi_wvalid),.s_axi_wready(s_axi_wready),
        .s_axi_bresp(s_axi_bresp),.s_axi_bvalid(s_axi_bvalid),.s_axi_bready(s_axi_bready),
        .s_axi_araddr(s_axi_araddr),.s_axi_arvalid(s_axi_arvalid),.s_axi_arready(s_axi_arready),
        .s_axi_rdata(s_axi_rdata),.s_axi_rresp(s_axi_rresp),.s_axi_rvalid(s_axi_rvalid),.s_axi_rready(s_axi_rready),
        .run(run),.flush_pulse(ctrl_flush),.locked(locked),.datapath_busy(solver_busy),.fault(fatal),.rf_path_ready(rf_path_ready_sync[1]),
        .epoch(epoch),.fault_code(last_fault),.sample_count(sample_count),.frames_ok(frames_ok),
        .frames_rejected(frames_rejected),.ingress_overflows(overflow_count),
        .backpressure_cycles(backpressure_cycles),.output_slots(output_slots),.format_errors(format_errors));
    rfv2_ingress ingress(.clk(aclk),.resetn(aresetn),.run(run_cmd),.soft_flush(ctrl_flush|capture_fence_cmd),
        .ingress_overflow(ingress_overflow),.processing_fault(processing_fault),
        .s_data(ingress_scan_data),.s_keep(ingress_scan_keep),.s_last(ingress_scan_last),.s_valid(ingress_scan_valid),.s_ready(ingress_scan_ready),
        .m_data(scan_data),.m_epoch(scan_epoch),.m_sample(scan_sample),.m_discontinuity(scan_disc),.m_valid(scan_valid),.m_ready(scan_ready),
        .flush(ingress_event),.fifo_flush(ingress_flush),.terminal_fault(terminal_fault),.epoch(epoch),.sample_count(sample_count),
        .overflow_count(overflow_count),.format_errors(format_errors),.backpressure_cycles(backpressure_cycles));
    rxv2_nco_rrc_frontend #(.NCO_MEM_FILE(NCO_MEM_FILE)) dsp(.clk(aclk),.resetn(compute_resetn),
        .s_data(scan_data),.s_epoch(scan_epoch),.s_sample(scan_sample),.s_discontinuity(scan_disc),
        .s_valid(scan_valid&&compute_resetn),.s_ready(dsp_scan_ready),
        // DSP's reset captures these configuration words. Acquisition resets
        // on the SAME edge, so gate them rather than sampling its previous
        // epoch's nonzero CFO through nonblocking-reset ordering.
        .cfg_phase_inc_q0_32(compute_resetn?nco_step:32'd0),
        .cfg_phase_q0_32(compute_resetn?nco_phase:32'd0),.cfg_commit(nco_commit&&compute_resetn),
        .cfg_apply_epoch(nco_epoch),.cfg_apply_sample(nco_sample),
        .m_data(filtered_data),.m_epoch(filtered_epoch),.m_sample(filtered_sample),.m_discontinuity(filtered_disc),.m_valid(filtered_valid),.m_ready(filtered_ready),
        .fifo_overflow_sticky(dsp_overflow),.coordinate_fault_sticky(dsp_coordinate_fault));
    rxv2_acquisition_frontend #(.NCO_MEM_FILE(NCO_MEM_FILE)) acquisition(.clk(aclk),.resetn(compute_resetn),.soft_flush(ingress_event),
        .s_data(filtered_data),.s_epoch(filtered_epoch),.s_sample(filtered_sample),.s_discontinuity(filtered_disc),.s_valid(filtered_valid),.s_ready(filtered_ready),
        .fb_valid(fb_valid),.fb_epoch(fb_epoch),.fb_origin(fb_origin),.fb_cfo_delta_hz_q16(fb_delta),
        .m_symbol_data(symbol_data),.m_symbol_index(symbol_index),.m_epoch(symbol_epoch),.m_origin(symbol_origin),
        .m_frame_begin(symbol_begin),.m_frame_abort(symbol_abort),.m_lock_ok(locked),.m_valid(symbol_valid),.m_ready(symbol_ready),
        .nco_cfg_commit(nco_commit),.nco_rx_phase_inc_q0_32(nco_step),.nco_phase_q0_32(nco_phase),.nco_apply_epoch(nco_epoch),.nco_apply_sample(nco_sample));
    mrf1_symbol_channel_backend channel(.clk50(aclk),.rst_n(compute_resetn),
        .s_symbol_data(symbol_data),.s_symbol_index(symbol_index),.s_epoch(symbol_epoch),.s_frame_origin(symbol_origin),
        .s_frame_begin(symbol_begin),.s_frame_abort(symbol_abort||solver_cancel),.s_lock_ok(locked),.s_symbol_valid(symbol_valid),.s_symbol_ready(symbol_ready),
        .m_ctx_headers(ctx_headers),.m_ctx_frame_id(ctx_id),.m_ctx_epoch(ctx_epoch),.m_ctx_origin(ctx_origin),.m_ctx_h_q15(ctx_h_debug),.m_ctx_valid(ctx_valid),.m_ctx_ready(ctx_ready),
        .m_mip_tdata(mip_data),.m_mip_tkeep(mip_keep),.m_mip_tvalid(mip_valid),.m_mip_tready(mip_ready),.m_mip_tlast(mip_last),
        .m_frame_abort(channel_abort),.m_error_code(channel_code),.m_frame_complete(channel_complete),
        .m_cfo_fb_valid(fb_valid),.m_cfo_fb_epoch(fb_epoch),.m_cfo_fb_origin(fb_origin),.m_cfo_fb_delta_hz_q16(fb_delta));
    rfv2_solver_delivery #(.BUILD_ID(BUILD_ID),.PROPOSAL_POLICY(PROPOSAL_POLICY)) delivery(.clk(aclk),.resetn(aresetn),.flush(ingress_event),
        .ctx_cancel(channel_abort||symbol_abort),.ctx_cancel_code(channel_abort?{8'd0,channel_code}:16'h0402),
        .ctx_valid(ctx_valid&&compute_resetn),.ctx_ready(ctx_ready),.ctx_frame_id(ctx_id),.ctx_epoch(ctx_epoch),.ctx_first_sample(ctx_origin),.ctx_headers(ctx_headers),
        .s_mip_data(mip_data),.s_mip_keep(mip_keep),.s_mip_valid(mip_valid&&compute_resetn),.s_mip_ready(mip_ready),.s_mip_last(mip_last),
        .m_data(m_axis_tdata),.m_keep(m_axis_tkeep),.m_valid(m_axis_tvalid),.m_ready(m_axis_tready),.m_last(m_axis_tlast),
        .busy(solver_busy),.cancel_input(solver_cancel),.ctx_done(slot_done),.rejected(slot_rejected),.fault_code(solver_code),
        .terminal_protocol_fault(solver_protocol_fault));
    always @(posedge aclk)begin
        if(!aresetn)begin frames_ok<=0;frames_rejected<=0;output_slots<=0;last_fault<=0;end
        else begin
            // This counter is not reset by compute flush or RUN=0. The DMA
            // driver's STOP fence compares its per-START modulo32 delta.
            if(m_axis_tvalid&&m_axis_tready&&m_axis_tlast)begin
                output_slots<=output_slots+1'b1;
                if(slot_rejected)begin
                    if(frames_rejected!=32'hffffffff)frames_rejected<=frames_rejected+1'b1;
                    if(solver_code!=0)last_fault<={16'd0,solver_code};
                end else if(frames_ok!=32'hffffffff)frames_ok<=frames_ok+1'b1;
            end
            if(ingress_overflow)last_fault<=32'h00000501;
            if(run&&s_axis_tvalid&&(s_axis_tkeep!=8'hff||s_axis_tlast))last_fault<=32'h00000504;
            if(dsp_overflow||dsp_coordinate_fault)last_fault<=32'h00000502;
            if(terminal_fault)last_fault<=32'h00000503;
            if(solver_protocol_fault)last_fault<=32'h00000300;
            if(run&&!rf_path_ready)last_fault<=32'h00000505;
        end
    end
endmodule
