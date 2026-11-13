// SPDX-License-Identifier: Apache-2.0
//
// RX-only replacement for the retired axi_ad9361_tx instance.
//
// This module is intentionally not a DAC implementation.  It supplies the
// outputs which axi_ad9361_tx formerly drove, keeping the AD9361 digital TX
// pins quiescent, and acknowledges only the legacy TX register banks which
// were acknowledged by the disabled-DAC configuration.  Software therefore
// cannot hang while probing those old banks, but every read is zero and no
// write changes hardware state.
//
// `up_*addr` is the ADI up_axi *word* address (AXI byte address >> 2):
//   0x1000..0x107f : DAC common (COMMON_ID 6'h10)
//   0x1100..0x113f : DAC channels 0..3 (COMMON_ID 6'h11)
// The old delay-control bank 0x1200..0x12ff deliberately remains unclaimed:
// DAC_IODELAY_ENABLE=0 made up_delay_cntrl(DISABLE=1) return no acknowledge.

`timescale 1ns/100ps

module rfv2_tx_register_stub (
  // Retired DAC-facing outputs.  They remain deterministic safe values.
  output                    dac_valid,
  output  [47:0]            dac_data,
  output                    dac_clksel,
  output                    dac_r1_mode,
  output  [15:0]            up_dld,
  output  [79:0]            up_dwdata,
  output                    dac_sync_out,
  output                    dac_enable_i0,
  output                    dac_valid_i0,
  output                    dac_enable_q0,
  output                    dac_valid_q0,
  output                    dac_enable_i1,
  output                    dac_valid_i1,
  output                    dac_enable_q1,
  output                    dac_valid_q1,
  output                    up_pps_irq_mask,
  output  [31:0]            up_dac_gpio_out,

  // Same ADI internal processor interface used by axi_ad9361_tx.
  input                     up_rstn,
  input                     up_clk,
  input                     up_wreq,
  input   [13:0]            up_waddr,
  input   [31:0]            up_wdata,
  output                    up_wack,
  input                     up_rreq,
  input   [13:0]            up_raddr,
  output  [31:0]            up_rdata,
  output                    up_rack
);

  reg                       up_wack_int = 1'b0;
  reg                       up_rack_int = 1'b0;
  reg     [31:0]            up_rdata_int = 32'd0;

  wire tx_w_common;
  wire tx_r_common;
  wire tx_w_channel;
  wire tx_r_channel;

  // Exact copies of the former up_dac_common/up_dac_channel block selects.
  // There is no response for any RX, TDD, or old disabled-delay address.
  assign tx_w_common  = (up_waddr[13:7] == 7'h20);
  assign tx_r_common  = (up_raddr[13:7] == 7'h20);
  assign tx_w_channel = (up_waddr[13:8] == 6'h11) &&
                        (up_waddr[7:4] <= 4'h3);
  assign tx_r_channel = (up_raddr[13:8] == 6'h11) &&
                        (up_raddr[7:4] <= 4'h3);

  // This is intentionally an unsupported-register compatibility response, not
  // a cycle-identical recreation of the removed multi-level TX aggregator.
  // It is one up_clk registered response like the ADI leaf controls; up_axi
  // observes no hanging transaction. Byte strobes were consumed by up_axi
  // before this interface; retired writes are acknowledged and discarded.
  always @(negedge up_rstn or posedge up_clk) begin
    if (up_rstn == 1'b0) begin
      up_wack_int  <= 1'b0;
      up_rack_int  <= 1'b0;
      up_rdata_int <= 32'd0;
    end else begin
      up_wack_int  <= up_wreq && (tx_w_common || tx_w_channel);
      up_rack_int  <= up_rreq && (tx_r_common || tx_r_channel);
      up_rdata_int <= 32'd0;
    end
  end

  assign up_wack  = up_wack_int;
  assign up_rack  = up_rack_int;
  assign up_rdata = up_rdata_int;

  // No DAC datapath, digital loopback source, GPIO side effect, PPS state,
  // delay request, or TX sample-valid indication survives the RX-only build.
  assign dac_valid       = 1'b0;
  assign dac_data        = 48'd0;
  assign dac_clksel      = 1'b0;
  assign dac_r1_mode     = 1'b0;
  assign up_dld          = 16'd0;
  assign up_dwdata       = 80'd0;
  assign dac_sync_out    = 1'b0;
  assign dac_enable_i0   = 1'b0;
  assign dac_valid_i0    = 1'b0;
  assign dac_enable_q0   = 1'b0;
  assign dac_valid_q0    = 1'b0;
  assign dac_enable_i1   = 1'b0;
  assign dac_valid_i1    = 1'b0;
  assign dac_enable_q1   = 1'b0;
  assign dac_valid_q1    = 1'b0;
  assign up_pps_irq_mask = 1'b0;
  assign up_dac_gpio_out = 32'd0;

endmodule
