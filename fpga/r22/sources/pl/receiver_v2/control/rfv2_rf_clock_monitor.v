`timescale 1ns/1ps
// Verilog module-reference top: observe REAL l_clk, not generated PLL locks.
// Ready deasserts after a bounded stopped-clock interval even if the source
// reset controller cannot advance because its own clock has stopped.
module rfv2_rf_clock_monitor #(
    parameter integer FABRIC_CLOCK_HZ=50000000,
    parameter integer SOURCE_CLOCK_HZ=16000000,
    // Two worst-case nominal source periods (8 clk50 cycles at 16 MHz).
    // The old 4096-cycle default could conceal ~82 us of capture interruption.
    // This is a bounded heartbeat watchdog, not proof of subthreshold continuity.
    parameter integer TIMEOUT_CYCLES=2*((FABRIC_CLOCK_HZ+SOURCE_CLOCK_HZ-1)/SOURCE_CLOCK_HZ),
    parameter integer START_EDGES=3
) (
    input wire clk,input wire resetn,
    input wire source_clk,input wire source_reset,input wire source_ready,
    // This low-fanout health register is an explicitly audited async fault
    // launch root. Preserve its identity through phys_opt replication; the
    // release assertion XDC names its Q, and the structural gate names its C.
    // This does not add a timing exception or change the health latency.
    (* DONT_TOUCH="TRUE" *) output reg ready
);
    reg source_toggle=1'b0;
    always @(posedge source_clk or posedge source_reset)
        if(source_reset)source_toggle<=0;
        else source_toggle<=~source_toggle;
    (* ASYNC_REG="TRUE", SHREG_EXTRACT="NO" *) reg [1:0] toggle_sync;
    (* ASYNC_REG="TRUE", SHREG_EXTRACT="NO" *) reg [1:0] ready_sync;
    reg observed_toggle;
    reg [$clog2(TIMEOUT_CYCLES+1)-1:0] idle_cycles;
    reg [$clog2(START_EDGES+1)-1:0] edge_count;
    always @(posedge clk)begin
        if(!resetn)begin
            toggle_sync<=0;ready_sync<=0;observed_toggle<=0;
            idle_cycles<=0;edge_count<=0;ready<=0;
        end else begin
            toggle_sync<={toggle_sync[0],source_toggle};
            ready_sync<={ready_sync[0],source_ready};
            observed_toggle<=toggle_sync[1];
            if(!ready_sync[1])begin idle_cycles<=0;edge_count<=0;ready<=0;end
            else if(toggle_sync[1]!=observed_toggle)begin
                idle_cycles<=0;
                if(edge_count<START_EDGES)edge_count<=edge_count+1'b1;
                if(edge_count>=START_EDGES-1)ready<=1;
            end else if(idle_cycles>=TIMEOUT_CYCLES-1)begin
                idle_cycles<=TIMEOUT_CYCLES;edge_count<=0;ready<=0;
            end else idle_cycles<=idle_cycles+1'b1;
        end
    end
endmodule
