// Two-entry AXI-stream bridge for the HDMI DMA to VDMA boundary.
//
// The DMA-facing ready and the VDMA-facing valid/data/last are clocked-only
// state.  In particular, raw reset and the HDMI epoch handshake never directly
// gate the ADI DMA store-and-forward BRAM read-enable through TREADY.
//
// A reset begins a DROP_TO_TLAST epoch.  The terminating stale TLAST is also
// discarded; PASS starts on the next beat, so a partial pre-reset frame cannot
// become the first post-reset display frame.
module axi_hdmi_tx_axis_epoch_bridge (
  input                   clk,
  input                   rst,
  input                   s_axis_tvalid,
  input       [63:0]      s_axis_tdata,
  input                   s_axis_tlast,
  output  reg             s_axis_tready = 1'b0,
  output  reg             m_axis_tvalid = 1'b0,
  output  reg [63:0]      m_axis_tdata = 64'd0,
  output  reg             m_axis_tlast = 1'b0,
  input                   m_axis_tready,
  output  reg             drop_to_tlast = 1'b1
);

  reg             skid_valid = 1'b0;
  reg [63:0]      skid_data = 64'd0;
  reg             skid_last = 1'b0;

  wire s_fire = s_axis_tvalid & s_axis_tready;
  wire m_fire = m_axis_tvalid & m_axis_tready;

  always @(posedge clk) begin
    if (rst == 1'b1) begin
      // Deliberately synchronous: raw reset may not asynchronously perturb
      // any DMA-facing TREADY or RAM-read enable path.
      s_axis_tready <= 1'b0;
      m_axis_tvalid <= 1'b0;
      m_axis_tdata <= 64'd0;
      m_axis_tlast <= 1'b0;
      skid_valid <= 1'b0;
      skid_data <= 64'd0;
      skid_last <= 1'b0;
      drop_to_tlast <= 1'b1;
    end else if (drop_to_tlast == 1'b1) begin
      // Drain, rather than reinterpret, a possibly truncated source frame.
      m_axis_tvalid <= 1'b0;
      m_axis_tdata <= 64'd0;
      m_axis_tlast <= 1'b0;
      skid_valid <= 1'b0;
      skid_data <= 64'd0;
      skid_last <= 1'b0;
      s_axis_tready <= 1'b1;
      if (s_fire && s_axis_tlast) begin
        // The observed delimiter belongs to the discarded epoch too.
        drop_to_tlast <= 1'b0;
      end
    end else begin
      // A two-entry registered skid buffer.  In steady state push+pop accepts
      // one beat every clock; its input ready is always a register.
      case ({m_fire, s_fire})
        2'b00: begin
          s_axis_tready <= ~skid_valid;
        end
        2'b01: begin
          if (m_axis_tvalid == 1'b0) begin
            m_axis_tvalid <= 1'b1;
            m_axis_tdata <= s_axis_tdata;
            m_axis_tlast <= s_axis_tlast;
            s_axis_tready <= 1'b1;
          end else begin
            skid_valid <= 1'b1;
            skid_data <= s_axis_tdata;
            skid_last <= s_axis_tlast;
            s_axis_tready <= 1'b0;
          end
        end
        2'b10: begin
          if (skid_valid == 1'b1) begin
            m_axis_tvalid <= 1'b1;
            m_axis_tdata <= skid_data;
            m_axis_tlast <= skid_last;
            skid_valid <= 1'b0;
          end else begin
            m_axis_tvalid <= 1'b0;
            m_axis_tlast <= 1'b0;
          end
          s_axis_tready <= 1'b1;
        end
        default: begin // push and pop in the same clock
          if (skid_valid == 1'b1) begin
            // Defensive full-buffer case; normally impossible because ready
            // is clocked low while skid_valid is set.
            m_axis_tdata <= skid_data;
            m_axis_tlast <= skid_last;
            skid_data <= s_axis_tdata;
            skid_last <= s_axis_tlast;
            skid_valid <= 1'b1;
            s_axis_tready <= 1'b0;
          end else begin
            m_axis_tvalid <= 1'b1;
            m_axis_tdata <= s_axis_tdata;
            m_axis_tlast <= s_axis_tlast;
            s_axis_tready <= 1'b1;
          end
        end
      endcase
    end
  end
endmodule
