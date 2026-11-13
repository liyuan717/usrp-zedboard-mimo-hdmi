`timescale 1ns/1ps
// Receive-only ingress ownership. Coordinates count accepted scans in an
// epoch, NEVER claim to be an ADC clock counter across overflow/drop events.
// All signals are in the aclk domain; async ADC CDC belongs to the BD FIFO.
module rfv2_ingress (
    input wire clk, input wire resetn, input wire run,
    input wire soft_flush, input wire ingress_overflow, input wire processing_fault,
    input wire [63:0] s_data, input wire [7:0] s_keep,
    input wire s_last, input wire s_valid, output wire s_ready,
    output wire [63:0] m_data, output wire [31:0] m_epoch,
    output wire [63:0] m_sample, output wire m_discontinuity,
    output wire m_valid, input wire m_ready,
    output wire flush, output reg fifo_flush, output reg terminal_fault,
    output reg [31:0] epoch, output reg [63:0] sample_count,
    output reg [31:0] overflow_count, output reg [31:0] format_errors,
    output reg [31:0] backpressure_cycles
);
    reg run_q, flush_q, overflow_q, processing_q, first_scan;
    reg sample_at_max;
    wire malformed=s_valid && (s_keep!=8'hff || s_last);
    // CPACK keeps running while software is stopped. Idle FIFO loss must not
    // consume active epochs or poison a later START. The run-edge fence owns
    // that boundary; delayed FIFO flushing may still expose an old loss pulse.
    wire overflow_edge=run&&run_q&&!fifo_flush&&ingress_overflow&&!overflow_q;
    wire flush_edge=soft_flush&&!flush_q;
    wire run_edge=run!=run_q;
    // Precompute alongside the accepted-scan increment. This preserves the
    // immediate terminal fence at MAX, without a 64-bit compare feeding all
    // compute-reset muxes and ultimately the FIR DSP inputs in one cycle.
    wire coordinate_exhausted=run&&s_valid&&sample_at_max;
    wire event_now=run_edge||flush_edge||overflow_edge||(processing_fault&&!processing_q)||
                   (run&&malformed)||coordinate_exhausted;
    assign flush=resetn&&event_now;
    // Immediate internal quiesce must NOT be fed combinationally back to a
    // FIFO that masks TVALID with its flush input. Keep that feedback clocked.
    always @(posedge clk) begin
        if(!resetn) fifo_flush<=0;
        else fifo_flush<=flush;
    end
    assign s_ready=resetn&&run&&!terminal_fault&&!event_now&&m_ready;
    assign m_valid=resetn&&run&&!terminal_fault&&!event_now&&s_valid;
    // ADI CPACK uses signed low twelve bits in each sixteen-bit lane.
    // Convert exactly once to the MSB-aligned signed16 DSP input contract.
    genvar lane;
    generate for(lane=0;lane<4;lane=lane+1)begin: normalize
        assign m_data[lane*16+:16]={s_data[lane*16+:12],4'b0000};
    end endgenerate
    assign m_epoch=epoch;
    assign m_sample=sample_count;
    assign m_discontinuity=first_scan;
    always @(posedge clk)begin
        if(!resetn)begin
            run_q<=0;flush_q<=0;overflow_q<=0;processing_q<=0;first_scan<=1;
            epoch<=0;sample_count<=0;sample_at_max<=0;terminal_fault<=0;
            overflow_count<=0;format_errors<=0;backpressure_cycles<=0;
        end else begin
            run_q<=run;flush_q<=soft_flush;overflow_q<=ingress_overflow;processing_q<=processing_fault;
            if(run&&s_valid&&!m_ready&&!event_now&&!terminal_fault &&
               backpressure_cycles!=32'hffffffff)
                backpressure_cycles<=backpressure_cycles+1'b1;
            if(overflow_edge&&overflow_count!=32'hffffffff)
                overflow_count<=overflow_count+1'b1;
            // A malformed upstream beat is flushed out of its owning FIFO,
            // not acknowledged as an accepted sample or allowed into the FIR.
            if(run&&malformed&&format_errors!=32'hffffffff)
                format_errors<=format_errors+1'b1;
            if(event_now)begin
                sample_count<=0;sample_at_max<=0;first_scan<=1;
                if(epoch==32'hffffffff||coordinate_exhausted)
                    terminal_fault<=1;
                else epoch<=epoch+1'b1;
            end else if(m_valid&&m_ready)begin
                sample_count<=sample_count+1'b1;sample_at_max<=(sample_count==64'hfffffffffffffffe);first_scan<=0;
            end
        end
    end
endmodule
