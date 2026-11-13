`timescale 1ns/1ps

// Frame-atomic context boundary. No PS transaction is needed per RF frame or
// detector task: the channel engine drives ctx + MIPs, and output slots drain
// independently to the receive DMA. Only one context owns the shared solver.
module rfv2_solver_delivery #(
    parameter logic [31:0] BUILD_ID=32'h20261027,
    parameter integer PROPOSAL_POLICY=0
) (
    input wire clk,input wire resetn,input wire flush,
    input wire ctx_cancel,input wire [15:0] ctx_cancel_code,
    input wire ctx_valid,output wire ctx_ready,
    input wire [31:0] ctx_frame_id,input wire [31:0] ctx_epoch,
    input wire [63:0] ctx_first_sample,input wire [319:0] ctx_headers,
    input wire [63:0] s_mip_data,input wire [7:0] s_mip_keep,
    input wire s_mip_valid,output wire s_mip_ready,input wire s_mip_last,
    output wire [63:0] m_data,output wire [7:0] m_keep,
    output wire m_valid,input wire m_ready,output wire m_last,
    output wire busy,output wire cancel_input,output logic ctx_done,
    output wire rejected,output wire [15:0] fault_code,
    output logic terminal_protocol_fault
);
    logic busy_q,start;
    logic [31:0] epoch_q;
    logic [63:0] first_sample_q;
    logic [15:0] terminal_code;
    wire [63:0] mop_data,record_data;
    wire [7:0] mop_keep,record_keep;
    wire mop_valid,mop_ready,mop_last,record_valid,record_ready,record_last;
    wire collector_ready,collector_abort,collector_done,collector_rejected,slot_fault,slot_busy,slot_rejected;
    wire [15:0] collector_code;
    wire solver_ready,solver_error,solver_busy,solver_done;
    wire [15:0] solver_code,accepted_tasks,emitted_tasks;
    wire live=resetn&&!flush;
    assign ctx_ready=live&&!ctx_cancel&&!slot_fault&&!busy_q&&collector_ready&&record_ready;
    // Soft flush clears the compute context, but a committed slot still
    // drains. STOP must not report idle until its final DMA beat completes.
    assign busy=busy_q||slot_busy;
    // The solver latches ERR_BATCH while the previous rejected frame keeps
    // CANCEL asserted. START clears it one clock after context admission;
    // that previous-frame status must not cancel the new context.
    assign cancel_input=busy_q&&(ctx_cancel||collector_abort||(solver_error&&!start)||slot_fault);
    assign s_mip_ready=live&&busy_q&&!cancel_input&&solver_ready;
    assign rejected=slot_rejected;
    assign fault_code=terminal_code;
    // Once committed, RFE has no reason field of its own. Preserve the
    // terminal diagnostic across compute flush until the NEXT context owns
    // the pipeline, just as the old DMA slot retains its epoch and sequence.
    always_ff @(posedge clk)begin
        if(!resetn)begin terminal_code<=0;terminal_protocol_fault<=0;ctx_done<=0;end
        else begin
            ctx_done<=m_valid&&m_ready&&m_last;
            // Predecode on the same owning edge as the diagnostic register.
            // The core's high-fanout compute reset must not decode 16 bits
            // after that edge. No added fault latency or altered priority.
            if(ctx_valid&&ctx_ready)begin terminal_code<=0;terminal_protocol_fault<=0;end
            else if(slot_fault)begin terminal_code<=16'h0300;terminal_protocol_fault<=1;end
            else if(busy_q&&ctx_cancel)begin terminal_code<=ctx_cancel_code;terminal_protocol_fault<=(ctx_cancel_code==16'h0300);end
            else if(busy_q&&solver_error&&!start)begin terminal_code<=solver_code;terminal_protocol_fault<=(solver_code==16'h0300);end
            else if(collector_code!=0)begin terminal_code<=collector_code;terminal_protocol_fault<=(collector_code==16'h0300);end
        end
    end
    always_ff @(posedge clk)begin
        if(!resetn||flush)begin busy_q<=0;start<=0;epoch_q<=0;first_sample_q<=0;end
        else begin
            start<=0;
            if(ctx_valid&&ctx_ready)begin
                busy_q<=1;start<=1;epoch_q<=ctx_epoch;first_sample_q<=ctx_first_sample;
            end
            if(m_valid&&m_ready&&m_last)begin busy_q<=0;end
        end
    end
    mimo_mhgd_interleaved_axis_candidate #(.BUILD_ID(BUILD_ID),.PROPOSAL_POLICY(PROPOSAL_POLICY)) solver(
        .aclk(clk),.aresetn(resetn),.batch_start(start),.batch_count(16'd528),
        .batch_cancel(flush||ctx_cancel||collector_abort||slot_fault),
        .s_axis_tdata(s_mip_data),.s_axis_tkeep(s_mip_keep),
        .s_axis_tvalid(s_mip_valid&&busy_q&&!cancel_input),.s_axis_tready(solver_ready),.s_axis_tlast(s_mip_last),
        .m_axis_tdata(mop_data),.m_axis_tkeep(mop_keep),.m_axis_tvalid(mop_valid),.m_axis_tready(mop_ready),.m_axis_tlast(mop_last),
        .batch_busy(solver_busy),.batch_done(solver_done),.batch_error(solver_error),.batch_error_code(solver_code),
        .accepted_tasks(accepted_tasks),.emitted_tasks(emitted_tasks));
    rfv2_packet_collector #(.BUILD_ID(BUILD_ID)) collector(
        .clk(clk),.resetn(resetn),.flush(flush),
        .cancel_frame(ctx_cancel||(busy_q&&solver_error&&!start)),
        .cancel_code(ctx_cancel ? ctx_cancel_code : solver_code),
        .frame_valid(ctx_valid&&ctx_ready),.frame_ready(collector_ready),
        .frame_id(ctx_frame_id),.public_headers(ctx_headers),
        .s_data(mop_data),.s_keep(mop_keep),.s_valid(mop_valid),.s_ready(mop_ready),.s_last(mop_last),
        .m_data(record_data),.m_keep(record_keep),.m_valid(record_valid),.m_ready(record_ready),.m_last(record_last),
        .abort_batch(collector_abort),.frame_done(collector_done),.frame_rejected(collector_rejected),.error_code(collector_code));
    rfv2_delivery_slot slots(.clk(clk),.resetn(resetn),.flush(flush),.epoch(epoch_q),.first_sample(first_sample_q),
        .s_data(record_data),.s_keep(record_keep),.s_valid(record_valid),.s_ready(record_ready),.s_last(record_last),
        .m_data(m_data),.m_keep(m_keep),.m_valid(m_valid),.m_ready(m_ready),.m_last(m_last),
        .protocol_error(slot_fault),.busy(slot_busy),.rejected_record(slot_rejected));
endmodule
