// SPDX-License-Identifier: MIT
// Passive HDMI presentation observer, ABI 1.
//
// This block never drives AXIS TREADY and therefore cannot change the display
// data path.  The AXIS tap and the physical HDMI VSYNC input are observed in
// s_axi_aclk (FCLK0, 50 MHz in the ZedBoard integration).  DMA identity is
// queued by software before dma_async_issue_pending(); it is bound only when the
// first accepted AXIS beat of that descriptor is observed.
`timescale 1ns/1ps
module hdmi_presentation_observer #(
  // 64 KiB aperture requires all 16 address bits.  Narrower ports alias
  // offsets (for example 0x0034 and 0x8034) and are deliberately forbidden.
  parameter integer C_S_AXI_ADDR_WIDTH = 16
) (
  (* X_INTERFACE_INFO = "xilinx.com:signal:clock:1.0 s_axi_aclk CLK" *)
  (* X_INTERFACE_PARAMETER = "ASSOCIATED_BUSIF S_AXI, ASSOCIATED_RESET s_axi_aresetn, FREQ_HZ 50000000" *)
  input  wire                         s_axi_aclk,
  (* X_INTERFACE_INFO = "xilinx.com:signal:reset:1.0 s_axi_aresetn RST" *)
  (* X_INTERFACE_PARAMETER = "POLARITY ACTIVE_LOW" *)
  input  wire                         s_axi_aresetn,
  (* X_INTERFACE_INFO = "xilinx.com:interface:aximm_rtl:1.0 S_AXI AWADDR" *)
  (* X_INTERFACE_PARAMETER = "XIL_INTERFACENAME S_AXI, PROTOCOL AXI4LITE, ADDR_WIDTH 16, DATA_WIDTH 32" *)
  input  wire [C_S_AXI_ADDR_WIDTH-1:0] s_axi_awaddr,
  (* X_INTERFACE_INFO = "xilinx.com:interface:aximm_rtl:1.0 S_AXI AWVALID" *) input wire s_axi_awvalid,
  (* X_INTERFACE_INFO = "xilinx.com:interface:aximm_rtl:1.0 S_AXI AWREADY" *) output reg s_axi_awready,
  (* X_INTERFACE_INFO = "xilinx.com:interface:aximm_rtl:1.0 S_AXI WDATA" *) input wire [31:0] s_axi_wdata,
  (* X_INTERFACE_INFO = "xilinx.com:interface:aximm_rtl:1.0 S_AXI WSTRB" *) input wire [3:0] s_axi_wstrb,
  (* X_INTERFACE_INFO = "xilinx.com:interface:aximm_rtl:1.0 S_AXI WVALID" *) input wire s_axi_wvalid,
  (* X_INTERFACE_INFO = "xilinx.com:interface:aximm_rtl:1.0 S_AXI WREADY" *) output reg s_axi_wready,
  (* X_INTERFACE_INFO = "xilinx.com:interface:aximm_rtl:1.0 S_AXI BRESP" *) output reg [1:0] s_axi_bresp,
  (* X_INTERFACE_INFO = "xilinx.com:interface:aximm_rtl:1.0 S_AXI BVALID" *) output reg s_axi_bvalid,
  (* X_INTERFACE_INFO = "xilinx.com:interface:aximm_rtl:1.0 S_AXI BREADY" *) input wire s_axi_bready,
  (* X_INTERFACE_INFO = "xilinx.com:interface:aximm_rtl:1.0 S_AXI ARADDR" *) input wire [C_S_AXI_ADDR_WIDTH-1:0] s_axi_araddr,
  (* X_INTERFACE_INFO = "xilinx.com:interface:aximm_rtl:1.0 S_AXI ARVALID" *) input wire s_axi_arvalid,
  (* X_INTERFACE_INFO = "xilinx.com:interface:aximm_rtl:1.0 S_AXI ARREADY" *) output reg s_axi_arready,
  (* X_INTERFACE_INFO = "xilinx.com:interface:aximm_rtl:1.0 S_AXI RDATA" *) output reg [31:0] s_axi_rdata,
  (* X_INTERFACE_INFO = "xilinx.com:interface:aximm_rtl:1.0 S_AXI RRESP" *) output reg [1:0] s_axi_rresp,
  (* X_INTERFACE_INFO = "xilinx.com:interface:aximm_rtl:1.0 S_AXI RVALID" *) output reg s_axi_rvalid,
  (* X_INTERFACE_INFO = "xilinx.com:interface:aximm_rtl:1.0 S_AXI RREADY" *) input wire s_axi_rready,

  // A wide pulse from the HDMI transmitter pixel domain.  It must be at
  // least three FCLK0 periods in the selected integration; otherwise a
  // toggle/acknowledged CDC must replace this pulse synchronizer.
  input  wire                         hdmi_vsync_async,

  // Passive AXI-stream monitor: connect these pins to an existing AXIS net.
  // Do NOT insert this module as an AXIS source/sink and do NOT route TREADY
  // through it.
  (* X_INTERFACE_IGNORE = "true" *) input wire axis_tvalid,
  (* X_INTERFACE_IGNORE = "true" *) input wire axis_tready,
  (* X_INTERFACE_IGNORE = "true" *) input wire axis_tlast,

  // Debug-only passive observation of the HDMI DMAC read master. These
  // inputs are sinks only: neither AXI nor AXIS flow control is driven here.
  (* X_INTERFACE_IGNORE = "true" *) input wire probe_arvalid,
  (* X_INTERFACE_IGNORE = "true" *) input wire probe_arready,
  (* X_INTERFACE_IGNORE = "true" *) input wire [28:0] probe_araddr,
  (* X_INTERFACE_IGNORE = "true" *) input wire probe_rvalid,
  (* X_INTERFACE_IGNORE = "true" *) input wire probe_rready,
  (* X_INTERFACE_IGNORE = "true" *) input wire probe_rlast,
  (* X_INTERFACE_IGNORE = "true" *) input wire [1:0] probe_rresp,

  output wire                         irq
);
  localparam [15:0] REG_ID             = 16'h0000;
  localparam [5:0] REG_VERSION        = 6'h04;
  localparam [5:0] REG_CONTROL        = 6'h08;
  localparam [5:0] REG_IRQ_STATUS     = 6'h0c;
  localparam [5:0] REG_IRQ_ENABLE     = 6'h10;
  localparam [5:0] REG_VSYNC_COUNT    = 6'h14;
  localparam [5:0] REG_LAST_EOF_SEQ   = 6'h18;
  localparam [5:0] REG_LAST_EOF_BEATS = 6'h1c;
  localparam [5:0] REG_EOF_VSYNC      = 6'h20;
  localparam [5:0] REG_LAST_PRESENT   = 6'h24;
  localparam [5:0] REG_PRESENT_VSYNC  = 6'h28;
  localparam [5:0] REG_QUEUE_STATUS   = 6'h2c;
  localparam [5:0] REG_ACTIVE_SEQ     = 6'h30;
  localparam [5:0] REG_QUEUE_SUBMIT   = 6'h34;
  localparam [15:0] REG_ERROR_STATUS   = 16'h0038;
  localparam [15:0] REG_EVENT_COUNT    = 16'h003c;
  localparam [15:0] REG_EVENT_SERIAL   = 16'h0040;
  localparam [15:0] REG_EVENT_BEATS    = 16'h0044;
  localparam [15:0] REG_EVENT_EOF_VSYNC= 16'h0048;
  localparam [15:0] REG_EVENT_TX_VSYNC = 16'h004c;
  localparam [15:0] REG_EVENT_POP      = 16'h0050;
  // Debug-only, read-only probe window. Keep the production ABI at
  // 0x0000..0x0050 byte-for-byte unchanged.
  localparam [15:0] REG_PROBE_AR_FIRE    = 16'h0080;
  localparam [15:0] REG_PROBE_AR_STALL   = 16'h0084;
  localparam [15:0] REG_PROBE_R_FIRE     = 16'h0088;
  localparam [15:0] REG_PROBE_R_STALL    = 16'h008c;
  localparam [15:0] REG_PROBE_R_LAST     = 16'h0090;
  localparam [15:0] REG_PROBE_AXIS_FIRE  = 16'h0094;
  localparam [15:0] REG_PROBE_AXIS_STALL = 16'h0098;
  localparam [15:0] REG_PROBE_AXIS_LAST  = 16'h009c;
  localparam [15:0] REG_PROBE_LAST_ARADDR= 16'h00a0;
  localparam [15:0] REG_PROBE_RRESP      = 16'h00a4;
  localparam [15:0] REG_PROBE_LIVE       = 16'h00a8;
  localparam [15:0] REG_PROBE_SEEN       = 16'h00ac;

  localparam [31:0] OBSERVER_ID = 32'h4850_4f31; // "HPO1"
  localparam [31:0] OBSERVER_VERSION = 32'h0001_0000;
  localparam [31:0] CAPABILITIES = 32'h0000_003f;

  // IRQ_STATUS / ERROR_STATUS bit assignments.
  localparam [4:0] IRQ_VSYNC       = 5'b00001;
  localparam [4:0] IRQ_DMA_EOF     = 5'b00010;
  localparam [4:0] IRQ_PRESENT     = 5'b00100;
  localparam [4:0] IRQ_QUEUE_FULL  = 5'b01000;
  localparam [4:0] IRQ_PROTOCOL    = 5'b10000;

  reg aw_hold, w_hold;
  reg [C_S_AXI_ADDR_WIDTH-1:0] awaddr_hold;
  reg [31:0] wdata_hold;
  reg [3:0] wstrb_hold;
  reg control_enable;
  reg [4:0] irq_status, irq_enable, error_status;

  (* ASYNC_REG = "TRUE", SHREG_EXTRACT = "NO" *) reg vsync_m1, vsync_m2, vsync_m3;
  reg vsync_m3_d;
  reg [31:0] vsync_count;

  reg [30:0] q0_seq, q1_seq;
  reg q0_new, q1_new;
  reg [1:0] q_count;
  reg axis_active, axis_active_new;
  reg [30:0] axis_active_seq;
  reg [31:0] axis_beat_count;
  reg [30:0] last_eof_seq;
  reg last_eof_new, last_eof_valid, eof_presented;
  reg [31:0] last_eof_beats, eof_vsync_count;
  reg [30:0] last_present_seq;
  reg last_present_new, last_present_valid;
  reg [31:0] present_vsync_count;
  reg [30:0] event_serial [0:3]; reg event_new [0:3];
  reg [31:0] event_beats [0:3], event_eof_vsync [0:3], event_tx_vsync [0:3];
  reg [2:0] event_count;

  wire axis_fire = axis_tvalid & axis_tready;
  wire vsync_rise = vsync_m3 & ~vsync_m3_d;
  wire write_commit = aw_hold & w_hold & ~s_axi_bvalid;
  wire [15:0] write_addr = awaddr_hold[15:0];
  wire [31:0] write_data = wdata_hold;
  wire write_bad_addr = (|write_addr[15:7]) || (|write_addr[1:0]) || (write_addr > REG_EVENT_POP);
  wire probe_read_addr = 1'b0; // Diagnostic window absent: SLVERR.
  wire read_bad_addr = (|s_axi_araddr[15:8]) || (|s_axi_araddr[1:0]) ||
                       ((s_axi_araddr > REG_EVENT_POP) && !probe_read_addr);
  wire queue_partial = write_commit && (write_addr == REG_QUEUE_SUBMIT) &&
                       ((wstrb_hold != 4'hf) || !(|write_data[30:0]));
  wire push_id = write_commit && (write_addr == REG_QUEUE_SUBMIT) &&
                 (wstrb_hold == 4'hf) && (|write_data[30:0]);
  wire push_new = write_data[31];
  wire pop_id = axis_fire && !axis_active && (q_count != 0);
  wire push_rejected = push_id && (q_count == 2) && !pop_id;
  wire protocol_fault = axis_fire && !axis_active && (q_count == 0);
  wire control_write = write_commit && (write_addr == REG_CONTROL) && wstrb_hold[0];
  wire flush_write = control_write && write_data[1];
  // Existing CONTROL[1] flush is an explicit, session-scoped probe clear.
  // CONTROL[0] gates counting, exactly like the existing observer state.
  wire probe_clear = flush_write;
  wire pop_write = write_commit && write_addr == REG_EVENT_POP;
  wire pop_valid = pop_write && wstrb_hold == 4'hf && write_data == 32'd1 && event_count != 0 && control_enable;
  wire pop_invalid = pop_write && !pop_valid;
  wire [4:0] irq_w1c = (write_commit && (write_addr == REG_IRQ_STATUS) && wstrb_hold[0]) ? write_data[4:0] : 5'd0;
  wire [4:0] error_w1c = (write_commit && (write_addr == REG_ERROR_STATUS) && wstrb_hold[0]) ? write_data[4:0] : 5'd0;
  wire event_vsync = vsync_rise && control_enable;
  wire event_present = event_vsync && last_eof_valid && !eof_presented &&
                       (eof_vsync_count != (vsync_count + 1'b1));
  wire event_full = (event_count == 4);
  wire event_push = event_present && (!event_full || pop_valid);
  wire event_overflow = event_present && event_full && !pop_valid;
  wire [2:0] event_push_index = event_count - (pop_valid ? 3'd1 : 3'd0);
  wire event_eof = control_enable && axis_fire &&
                   ((!axis_active && (q_count != 0) && axis_tlast) || (axis_active && axis_tlast));
  wire event_queue = control_enable && push_rejected;
  wire eof_conflict = event_eof && last_eof_valid && !eof_presented && !event_present;
  wire event_protocol = pop_invalid || queue_partial || (write_commit && write_bad_addr) ||
                        (control_enable && (protocol_fault || event_overflow || eof_conflict));
  wire [4:0] visible_irq_status = (irq_status & ~IRQ_PRESENT) |
                                 ((event_count != 0) ? IRQ_PRESENT : 5'd0);
  wire [4:0] irq_events = (event_vsync ? IRQ_VSYNC : 5'd0) |
                          (event_eof ? IRQ_DMA_EOF : 5'd0) |
                          (event_queue ? IRQ_QUEUE_FULL : 5'd0) |
                          (event_protocol ? IRQ_PROTOCOL : 5'd0);
  wire [4:0] error_events = (event_queue ? IRQ_QUEUE_FULL : 5'd0) |
                            (event_protocol ? IRQ_PROTOCOL : 5'd0);

  assign irq = |(visible_irq_status & irq_enable);

  wire [31:0] probe_ar_fire_count, probe_ar_stall_cycle_count;
  wire [31:0] probe_r_fire_count, probe_r_stall_cycle_count, probe_r_last_count;
  wire [31:0] probe_axis_fire_count, probe_axis_stall_cycle_count, probe_axis_last_count;
  wire [28:0] probe_last_araddr;
  wire [3:0] probe_rresp_seen;
  wire [7:0] probe_live_status, probe_seen_status;
  assign probe_ar_fire_count = 0;
  assign probe_ar_stall_cycle_count = 0;
  assign probe_r_fire_count = 0;
  assign probe_r_stall_cycle_count = 0;
  assign probe_r_last_count = 0;
  assign probe_axis_fire_count = 0;
  assign probe_axis_stall_cycle_count = 0;
  assign probe_axis_last_count = 0;
  assign probe_last_araddr = 0;
  assign probe_rresp_seen = 0;
  assign probe_live_status = 0;
  assign probe_seen_status = 0;

  // READY is combinational from the holding state.  Registering it delayed
  // admission by a cycle and could accept a second AW/W/AR before the hold
  // bit became visible.
  always @(*) begin
    s_axi_awready = s_axi_aresetn && !aw_hold && !s_axi_bvalid;
    s_axi_wready  = s_axi_aresetn && !w_hold  && !s_axi_bvalid;
    s_axi_arready = s_axi_aresetn && !s_axi_rvalid;
  end

  always @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      s_axi_bvalid <= 1'b0; s_axi_bresp <= 2'b00;
      s_axi_rvalid <= 1'b0; s_axi_rresp <= 2'b00;
      s_axi_rdata <= 32'd0;
      aw_hold <= 1'b0; w_hold <= 1'b0;
      awaddr_hold <= {C_S_AXI_ADDR_WIDTH{1'b0}};
      wdata_hold <= 32'd0; wstrb_hold <= 4'd0;
    end else begin
      if (s_axi_awvalid && s_axi_awready) begin
        aw_hold <= 1'b1;
        awaddr_hold <= s_axi_awaddr;
      end
      if (s_axi_wvalid && s_axi_wready) begin
        w_hold <= 1'b1;
        wdata_hold <= s_axi_wdata;
        wstrb_hold <= s_axi_wstrb;
      end
      if (write_commit) begin
        aw_hold <= 1'b0;
        w_hold <= 1'b0;
        s_axi_bvalid <= 1'b1;
        s_axi_bresp <= (write_bad_addr || queue_partial || pop_invalid) ? 2'b10 : 2'b00;
      end else if (s_axi_bvalid && s_axi_bready) begin
        s_axi_bvalid <= 1'b0;
      end

      if (s_axi_arvalid && s_axi_arready) begin
        s_axi_rvalid <= 1'b1;
        s_axi_rresp <= read_bad_addr ? 2'b10 : 2'b00;
        case (s_axi_araddr[15:0])
          REG_ID:             s_axi_rdata <= OBSERVER_ID;
          REG_VERSION:        s_axi_rdata <= OBSERVER_VERSION | CAPABILITIES;
          REG_CONTROL:        s_axi_rdata <= {31'd0, control_enable};
          REG_IRQ_STATUS:     s_axi_rdata <= {27'd0, visible_irq_status};
          REG_IRQ_ENABLE:     s_axi_rdata <= {27'd0, irq_enable};
          REG_VSYNC_COUNT:    s_axi_rdata <= vsync_count;
          REG_LAST_EOF_SEQ:   s_axi_rdata <= {last_eof_new, last_eof_seq};
          REG_LAST_EOF_BEATS: s_axi_rdata <= last_eof_beats;
          REG_EOF_VSYNC:      s_axi_rdata <= eof_vsync_count;
          REG_LAST_PRESENT:   s_axi_rdata <= {last_present_new, last_present_seq};
          REG_PRESENT_VSYNC:  s_axi_rdata <= present_vsync_count;
          REG_QUEUE_STATUS:   s_axi_rdata <= {24'd0, last_present_valid,
                                last_eof_valid, axis_active, q_count, 2'd0};
          REG_ACTIVE_SEQ:     s_axi_rdata <= {axis_active_new, axis_active_seq};
          REG_ERROR_STATUS:   s_axi_rdata <= {27'd0, error_status};
          REG_EVENT_COUNT:    s_axi_rdata <= {29'd0,event_count};
          REG_EVENT_SERIAL:   s_axi_rdata <= event_count != 0 ? {event_new[0],event_serial[0]} : 32'd0;
          REG_EVENT_BEATS:    s_axi_rdata <= event_count != 0 ? event_beats[0] : 32'd0;
          REG_EVENT_EOF_VSYNC:s_axi_rdata <= event_count != 0 ? event_eof_vsync[0] : 32'd0;
          REG_EVENT_TX_VSYNC: s_axi_rdata <= event_count != 0 ? event_tx_vsync[0] : 32'd0;
          REG_PROBE_AR_FIRE:    s_axi_rdata <= probe_ar_fire_count;
          REG_PROBE_AR_STALL:   s_axi_rdata <= probe_ar_stall_cycle_count;
          REG_PROBE_R_FIRE:     s_axi_rdata <= probe_r_fire_count;
          REG_PROBE_R_STALL:    s_axi_rdata <= probe_r_stall_cycle_count;
          REG_PROBE_R_LAST:     s_axi_rdata <= probe_r_last_count;
          REG_PROBE_AXIS_FIRE:  s_axi_rdata <= probe_axis_fire_count;
          REG_PROBE_AXIS_STALL: s_axi_rdata <= probe_axis_stall_cycle_count;
          REG_PROBE_AXIS_LAST:  s_axi_rdata <= probe_axis_last_count;
          REG_PROBE_LAST_ARADDR:s_axi_rdata <= {3'd0, probe_last_araddr};
          REG_PROBE_RRESP:      s_axi_rdata <= {28'd0, probe_rresp_seen};
          REG_PROBE_LIVE:       s_axi_rdata <= {24'd0, probe_live_status};
          REG_PROBE_SEEN:       s_axi_rdata <= {24'd0, probe_seen_status};
          default:            s_axi_rdata <= 32'd0;
        endcase
      end else if (s_axi_rvalid && s_axi_rready) begin
        s_axi_rvalid <= 1'b0;
      end
    end
  end

  always @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      control_enable <= 1'b0;
      irq_status <= 5'd0; irq_enable <= 5'd0; error_status <= 5'd0;
      vsync_m1 <= 1'b0; vsync_m2 <= 1'b0; vsync_m3 <= 1'b0; vsync_m3_d <= 1'b0;
      vsync_count <= 32'd0;
      q0_seq <= 31'd0; q1_seq <= 31'd0; q0_new <= 1'b0; q1_new <= 1'b0; q_count <= 2'd0;
      axis_active <= 1'b0; axis_active_new <= 1'b0; axis_active_seq <= 31'd0;
      axis_beat_count <= 32'd0;
      last_eof_seq <= 31'd0; last_eof_new <= 1'b0; last_eof_valid <= 1'b0; eof_presented <= 1'b0;
      last_eof_beats <= 32'd0; eof_vsync_count <= 32'd0;
      last_present_seq <= 31'd0; last_present_new <= 1'b0; last_present_valid <= 1'b0;
      present_vsync_count <= 32'd0;
      event_count <= 0;
    end else begin
      vsync_m1 <= hdmi_vsync_async;
      vsync_m2 <= vsync_m1;
      vsync_m3 <= vsync_m2;
      vsync_m3_d <= vsync_m3;

      // CONTROL[0]=enable; CONTROL[1]=flush all identity/event state (W1P).
      // Flush has absolute priority over events and W1C in this clock.
      if (control_write) begin
        control_enable <= write_data[0];
        if (flush_write) begin
          q_count <= 2'd0; axis_active <= 1'b0; axis_beat_count <= 32'd0;
          last_eof_valid <= 1'b0; eof_presented <= 1'b0; last_present_valid <= 1'b0;
          event_count <= 0;
        end
      end
      if (write_commit && write_addr == REG_IRQ_ENABLE && wstrb_hold[0])
        irq_enable <= write_data[4:0];
      if (!flush_write && vsync_rise && control_enable) begin
        vsync_count <= vsync_count + 1'b1;
        // EOF at VSYNC N becomes a transmitter-frame-boundary only at a
        // subsequent physical VSYNC.  This deliberately does not claim that
        // a panel/LCD has visually accepted the pixels.
        if (last_eof_valid && !eof_presented &&
            (eof_vsync_count != (vsync_count + 1'b1))) begin
          last_present_seq <= last_eof_seq;
          last_present_new <= last_eof_new;
          last_present_valid <= 1'b1;
          present_vsync_count <= vsync_count + 1'b1;
          // A full FIFO loses this boundary visibly; never relabel it later.
          eof_presented <= 1'b1;
        end
      end

      if (!flush_write && control_enable) begin
        if (pop_valid) begin
          event_serial[0]<=event_serial[1]; event_new[0]<=event_new[1]; event_beats[0]<=event_beats[1]; event_eof_vsync[0]<=event_eof_vsync[1]; event_tx_vsync[0]<=event_tx_vsync[1];
          event_serial[1]<=event_serial[2]; event_new[1]<=event_new[2]; event_beats[1]<=event_beats[2]; event_eof_vsync[1]<=event_eof_vsync[2]; event_tx_vsync[1]<=event_tx_vsync[2];
          event_serial[2]<=event_serial[3]; event_new[2]<=event_new[3]; event_beats[2]<=event_beats[3]; event_eof_vsync[2]<=event_eof_vsync[3]; event_tx_vsync[2]<=event_tx_vsync[3];
        end
        // Shift before append, so full simultaneous POP+push remains ordered.
        // Exactly one count assignment covers all push/pop combinations.
        if (event_push) begin
          event_serial[event_push_index] <= last_eof_seq;
          event_new[event_push_index] <= last_eof_new;
          event_beats[event_push_index] <= last_eof_beats;
          event_eof_vsync[event_push_index] <= eof_vsync_count;
          event_tx_vsync[event_push_index] <= vsync_count + 1'b1;
        end
        case ({event_push, pop_valid})
          2'b10: event_count <= event_count + 1'b1;
          2'b01: event_count <= event_count - 1'b1;
          default: begin end
        endcase

        // Two-entry identity FIFO.  A same-cycle pop/push is deliberately
        // lossless; a full FIFO rejects an unaccompanied push rather than
        // silently reusing any shadow identity.
        case ({pop_id, push_id && !push_rejected})
          2'b01: begin
            if (q_count == 0) begin
              q0_seq <= write_data[30:0]; q0_new <= push_new; q_count <= 1;
            end else if (q_count == 1) begin
              q1_seq <= write_data[30:0]; q1_new <= push_new; q_count <= 2;
            end
          end
          2'b10: begin
            if (q_count == 2) begin
              q0_seq <= q1_seq; q0_new <= q1_new;
            end
            q_count <= q_count - 1'b1;
          end
          2'b11: begin
            if (q_count == 1) begin
              q0_seq <= write_data[30:0]; q0_new <= push_new;
            end else if (q_count == 2) begin
              q0_seq <= q1_seq; q0_new <= q1_new;
              q1_seq <= write_data[30:0]; q1_new <= push_new;
            end
          end
          default: begin end
        endcase

        if (axis_fire) begin
          if (!axis_active) begin
            if (q_count != 0) begin
              axis_active_seq <= q0_seq;
              axis_active_new <= q0_new;
              axis_beat_count <= 32'd1;
              if (axis_tlast) begin
                if (!eof_conflict) begin
                  last_eof_seq <= q0_seq; last_eof_new <= q0_new; last_eof_valid <= 1'b1; eof_presented <= 1'b0;
                  last_eof_beats <= 32'd1;
                  eof_vsync_count <= vsync_count + (event_vsync ? 32'd1 : 32'd0);
                end
                axis_active <= 1'b0;
              end else begin
                axis_active <= 1'b1;
              end
            end
          end else if (axis_tlast) begin
            if (!eof_conflict) begin
              last_eof_seq <= axis_active_seq; last_eof_new <= axis_active_new;
              last_eof_valid <= 1'b1; eof_presented <= 1'b0; last_eof_beats <= axis_beat_count + 1'b1;
              eof_vsync_count <= vsync_count + (event_vsync ? 32'd1 : 32'd0);
            end
            axis_active <= 1'b0;
            axis_beat_count <= 32'd0;
          end else begin
            axis_beat_count <= axis_beat_count + 1'b1;
          end
        end
      end
      if (flush_write) begin
        irq_status <= 5'd0;
        error_status <= 5'd0;
      end else begin
        // W1C clears old bits, concurrent events win and remain observable.
        irq_status <= (irq_status & ~irq_w1c) | irq_events;
        error_status <= (error_status & ~error_w1c) | error_events;
      end
    end
  end
endmodule
