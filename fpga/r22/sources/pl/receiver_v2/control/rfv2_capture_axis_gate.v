`timescale 1ns/1ps
// Zero-storage, same-clock capture fence. Both sides observe the SAME accepted
// beat: masking TVALID alone would let the upstream FIFO pop unseen data.
// The enable is an epoch/reset control, not per-word flow control; removing it
// during a held beat deliberately cancels the old epoch, followed by FIFO flush.
module rfv2_capture_axis_gate (
    (* X_INTERFACE_INFO="xilinx.com:signal:clock:1.0 aclk CLK" *)
    (* X_INTERFACE_PARAMETER="ASSOCIATED_BUSIF s_axis:m_axis, FREQ_HZ 50000000" *)
    input wire aclk,
    (* X_INTERFACE_IGNORE="true" *) input wire enable,
    input wire [127:0] s_axis_tdata,
    input wire [15:0] s_axis_tkeep,
    input wire [15:0] s_axis_tstrb,
    input wire s_axis_tlast,
    input wire s_axis_tvalid,
    output wire s_axis_tready,
    output wire [127:0] m_axis_tdata,
    output wire [15:0] m_axis_tkeep,
    output wire [15:0] m_axis_tstrb,
    output wire m_axis_tlast,
    output wire m_axis_tvalid,
    input wire m_axis_tready
);
    assign m_axis_tdata = s_axis_tdata;
    assign m_axis_tkeep = s_axis_tkeep;
    assign m_axis_tstrb = s_axis_tstrb;
    assign m_axis_tlast = s_axis_tlast;
    assign m_axis_tvalid = s_axis_tvalid & enable;
    assign s_axis_tready = m_axis_tready & enable;
endmodule
