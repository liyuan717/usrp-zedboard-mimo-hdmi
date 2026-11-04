`timescale 1ns/1ps
// SPDX-License-Identifier: Apache-2.0
//
// Passive AXI4-Stream monitor for RX9 diagnostics.
//
// S_AXIS is explicitly an IP-Integrator *monitor* interface: it has no
// READY/VALID/data outputs and therefore cannot source, throttle, or otherwise
// modify the existing producer-to-consumer AXIS path.  The independent scalar
// outputs are deliberately marked non-interface so they may feed the read-only
// status bank without inference reconnecting the observed stream.
module rf_axis_status_monitor (
  (* X_INTERFACE_INFO = "xilinx.com:interface:axis:1.0 S_AXIS TDATA" *)
  (* X_INTERFACE_PARAMETER = "XIL_INTERFACENAME S_AXIS, TDATA_NUM_BYTES 8, HAS_TKEEP 1, HAS_TLAST 1, HAS_TREADY 1, FREQ_HZ 50000000" *)
  (* X_INTERFACE_MODE = "monitor" *) input wire [63:0] s_axis_tdata,
  (* X_INTERFACE_INFO = "xilinx.com:interface:axis:1.0 S_AXIS TKEEP" *)
  input wire [7:0] s_axis_tkeep,
  (* X_INTERFACE_INFO = "xilinx.com:interface:axis:1.0 S_AXIS TVALID" *)
  input wire s_axis_tvalid,
  (* X_INTERFACE_INFO = "xilinx.com:interface:axis:1.0 S_AXIS TREADY" *)
  input wire s_axis_tready,
  (* X_INTERFACE_INFO = "xilinx.com:interface:axis:1.0 S_AXIS TLAST" *)
  input wire s_axis_tlast,

  (* X_INTERFACE_IGNORE = "true" *) output wire observed_tvalid,
  (* X_INTERFACE_IGNORE = "true" *) output wire observed_tready,
  (* X_INTERFACE_IGNORE = "true" *) output wire observed_tlast
);
  // TDATA/TKEEP are part of the monitor interface so the attachment contract
  // matches the 64-bit RXTH stream.  They are intentionally not exported.
  assign observed_tvalid = s_axis_tvalid;
  assign observed_tready = s_axis_tready;
  assign observed_tlast  = s_axis_tlast;
endmodule
