`timescale 1ns/1ps
// G0 raw-flight-recorder tag. It travels through the same WFIFO/CPACK
// lanes as the ADC data; no asynchronous multibit sampling is used.
module g0_adc_counter (
    (* X_INTERFACE_INFO="xilinx.com:signal:clock:1.0 clk CLK" *)
    (* X_INTERFACE_PARAMETER="FREQ_HZ 16000000" *) input wire clk,
    // Raw power-on assertion enters ONLY the release synchronizer. The
    // counter state below uses a synchronous local reset; raw deassertion
    // never reaches its reset pins. A stopped source clock cannot emit data.
    (* X_INTERFACE_IGNORE="true" *) input wire por_n,
    input wire source_reset, input wire sample_valid,
    output wire [63:0] tag
);
    reg [47:0] index;
    reg [15:0] epoch;
    reg was_reset;
    (* ASYNC_REG="TRUE", SHREG_EXTRACT="NO" *) reg [1:0] reset_release;
    assign tag = {epoch,index};
    always @(posedge clk or negedge por_n) begin
        if(!por_n)reset_release<=0;
        else reset_release<={reset_release[0],1'b1};
    end
    always @(posedge clk) begin
        if(!reset_release[1]) begin index<=0;epoch<=0;was_reset<=1;end
        else begin
            was_reset <= source_reset;
            if (source_reset && !was_reset) epoch <= epoch + 1'b1;
            if (sample_valid && !source_reset) index <= index + 1'b1;
        end
    end
endmodule
