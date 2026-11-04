`timescale 1ns/1ps
// SPDX-License-Identifier: Apache-2.0
//
// RXTH-only IQ representation adapter.
//
// The legacy ADI/IIO RX DMAC deliberately remains untouched and exposes its
// proven `le:S12/16>>0` scan ABI: each 16-bit lane is sign-extended low-12
// data.  RXTH has a different, explicit contract: signed-12 in bits [15:4]
// (the low nibble is zero), so host code can normalize each component by
// 32768.  This transparent AXIS adapter makes that conversion only on the
// independent transport-DMA branch.  It neither changes framing nor creates
// buffering/backpressure.
module rf_rx_msb12_normalizer (
  (* X_INTERFACE_INFO = "xilinx.com:signal:clock:1.0 clk CLK" *)
  (* X_INTERFACE_PARAMETER = "ASSOCIATED_BUSIF S_AXIS:M_AXIS, ASSOCIATED_RESET resetn, FREQ_HZ 50000000" *)
  input  wire        clk,
  (* X_INTERFACE_INFO = "xilinx.com:signal:reset:1.0 resetn RST" *)
  (* X_INTERFACE_PARAMETER = "POLARITY ACTIVE_LOW" *)
  input  wire        resetn,

  (* X_INTERFACE_INFO = "xilinx.com:interface:axis:1.0 S_AXIS TDATA" *)
  (* X_INTERFACE_PARAMETER = "XIL_INTERFACENAME S_AXIS, TDATA_NUM_BYTES 8, HAS_TKEEP 1, HAS_TLAST 1, HAS_TREADY 1" *)
  input  wire [63:0] s_axis_tdata,
  (* X_INTERFACE_INFO = "xilinx.com:interface:axis:1.0 S_AXIS TKEEP" *)
  input  wire [7:0]  s_axis_tkeep,
  (* X_INTERFACE_INFO = "xilinx.com:interface:axis:1.0 S_AXIS TSTRB" *)
  input  wire [7:0]  s_axis_tstrb,
  (* X_INTERFACE_INFO = "xilinx.com:interface:axis:1.0 S_AXIS TVALID" *)
  input  wire        s_axis_tvalid,
  (* X_INTERFACE_INFO = "xilinx.com:interface:axis:1.0 S_AXIS TREADY" *)
  output wire        s_axis_tready,
  (* X_INTERFACE_INFO = "xilinx.com:interface:axis:1.0 S_AXIS TLAST" *)
  input  wire        s_axis_tlast,

  (* X_INTERFACE_INFO = "xilinx.com:interface:axis:1.0 M_AXIS TDATA" *)
  (* X_INTERFACE_PARAMETER = "XIL_INTERFACENAME M_AXIS, TDATA_NUM_BYTES 8, HAS_TKEEP 1, HAS_TLAST 1, HAS_TREADY 1" *)
  output wire [63:0] m_axis_tdata,
  (* X_INTERFACE_INFO = "xilinx.com:interface:axis:1.0 M_AXIS TKEEP" *)
  output wire [7:0]  m_axis_tkeep,
  (* X_INTERFACE_INFO = "xilinx.com:interface:axis:1.0 M_AXIS TSTRB" *)
  output wire [7:0]  m_axis_tstrb,
  (* X_INTERFACE_INFO = "xilinx.com:interface:axis:1.0 M_AXIS TVALID" *)
  output wire        m_axis_tvalid,
  (* X_INTERFACE_INFO = "xilinx.com:interface:axis:1.0 M_AXIS TREADY" *)
  input  wire        m_axis_tready,
  (* X_INTERFACE_INFO = "xilinx.com:interface:axis:1.0 M_AXIS TLAST" *)
  output wire        m_axis_tlast
);
  // Each source lane is an ADI sign-extended S12/16 word.  Selecting its
  // least-significant twelve bits and appending four zeroes preserves its
  // two's-complement value exactly while moving it to the documented MSBs.
  assign m_axis_tdata = {
      s_axis_tdata[59:48], 4'b0000,
      s_axis_tdata[43:32], 4'b0000,
      s_axis_tdata[27:16], 4'b0000,
      s_axis_tdata[11:0],  4'b0000
  };
  assign m_axis_tkeep  = s_axis_tkeep;
  assign m_axis_tstrb  = s_axis_tstrb;
  assign m_axis_tvalid = s_axis_tvalid;
  assign s_axis_tready = m_axis_tready;
  assign m_axis_tlast  = s_axis_tlast;
endmodule
