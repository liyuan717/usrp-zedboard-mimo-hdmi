// SPDX-License-Identifier: MIT
// Passive third endpoint on the existing HDMI AXI3 read interface.
// No output belongs to the observed bus; only separate observation outputs
// feed the diagnostic HPO register bank.
module hdmi_axi_read_bus_monitor (
  (* X_INTERFACE_INFO = "xilinx.com:signal:clock:1.0 aclk CLK" *)
  (* X_INTERFACE_PARAMETER = "ASSOCIATED_BUSIF READ_MONITOR, FREQ_HZ 50000000" *) input wire aclk,
  (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 READ_MONITOR ARADDR" *)
  (* X_INTERFACE_PARAMETER = "XIL_INTERFACENAME READ_MONITOR, PROTOCOL AXI3, READ_WRITE_MODE READ_ONLY, ADDR_WIDTH 29, DATA_WIDTH 64, ID_WIDTH 1, FREQ_HZ 50000000" *)
  (* X_INTERFACE_MODE = "monitor" *) input wire [28:0] araddr,
  (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 READ_MONITOR ARVALID" *) input wire arvalid,
  (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 READ_MONITOR ARREADY" *) input wire arready,
  (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 READ_MONITOR ARLEN" *) input wire [3:0] arlen,
  (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 READ_MONITOR ARSIZE" *) input wire [2:0] arsize,
  (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 READ_MONITOR ARBURST" *) input wire [1:0] arburst,
  (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 READ_MONITOR ARCACHE" *) input wire [3:0] arcache,
  (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 READ_MONITOR ARPROT" *) input wire [2:0] arprot,
  (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 READ_MONITOR ARLOCK" *) input wire [1:0] arlock,
  (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 READ_MONITOR ARID" *) input wire arid,
  (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 READ_MONITOR RVALID" *) input wire rvalid,
  (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 READ_MONITOR RREADY" *) input wire rready,
  (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 READ_MONITOR RLAST" *) input wire rlast,
  (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 READ_MONITOR RRESP" *) input wire [1:0] rresp,
  (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 READ_MONITOR RDATA" *) input wire [63:0] rdata,
  (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 READ_MONITOR RID" *) input wire rid,
  (* X_INTERFACE_IGNORE = "true" *) output wire observed_arvalid,
  (* X_INTERFACE_IGNORE = "true" *) output wire observed_arready,
  (* X_INTERFACE_IGNORE = "true" *) output wire [28:0] observed_araddr,
  (* X_INTERFACE_IGNORE = "true" *) output wire observed_rvalid,
  (* X_INTERFACE_IGNORE = "true" *) output wire observed_rready,
  (* X_INTERFACE_IGNORE = "true" *) output wire observed_rlast,
  (* X_INTERFACE_IGNORE = "true" *) output wire [1:0] observed_rresp
);
  assign observed_arvalid = arvalid;
  assign observed_arready = arready;
  assign observed_araddr = araddr;
  assign observed_rvalid = rvalid;
  assign observed_rready = rready;
  assign observed_rlast = rlast;
  assign observed_rresp = rresp;
endmodule
