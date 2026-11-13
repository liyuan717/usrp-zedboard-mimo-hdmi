`timescale 1ns/1ps
// SPDX-License-Identifier: Apache-2.0
// RXV2: two-RX, 4 MSPS NCO/CFO correction and 41-tap matched filter.
//
// This is deliberately a lossless *rate-converting service engine*, not an
// AXIS pass-through.  Four scalar symmetric FIRs use two real multipliers
// each and consume one IQ scan in 11 50-MHz clocks.  The ingress FIFO absorbs
// bursty DMA delivery; do not connect it directly to an ADC-rate valid source
// without retaining ready/backpressure semantics.
module rxv2_nco_rrc_frontend #(
  parameter integer FIFO_DEPTH = 128,
  parameter NCO_MEM_FILE = "pl/receiver_v2/dsp/rtl/rxv2_nco_q15.mem"
) (
  input  wire              clk,
  input  wire              resetn,

  input  wire [63:0]       s_data,
  input  wire [31:0]       s_epoch,
  input  wire [63:0]       s_sample,
  input  wire              s_discontinuity,
  input  wire              s_valid,
  output wire              s_ready,

  // Q0.32 received-CFO phase increment and restart phase.  The core applies
  // x * exp(-j*phase), so x[n]=A*exp(+j*omega*n) is corrected with +omega.
  // Upstream acquisition must not negate this value a second time.
  input  wire [31:0]       cfg_phase_inc_q0_32,
  input  wire [31:0]       cfg_phase_q0_32,
  // Synchronous one-cycle commit.  Both words become active atomically on
  // the next NCO input scan; commit never flushes RRC history or drops data.
  input  wire              cfg_commit,
  input  wire [31:0]       cfg_apply_epoch,
  input  wire [63:0]       cfg_apply_sample,

  output wire [127:0]      m_data,
  output wire [31:0]       m_epoch,
  output wire [63:0]       m_sample,
  output wire              m_discontinuity,
  output wire              m_valid,
  input  wire              m_ready,

  output reg               fifo_overflow_sticky,
  output reg               coordinate_fault_sticky
);
  // Kept repository-relative so non-project OOC synthesis and simulation use
  // the same frozen coefficients; an integrator may instead add rtl/ as an
  // include directory without changing this source.
  `include "pl/receiver_v2/dsp/rtl/rxv2_rrc_q15.svh"

  localparam integer FIFO_W = 161;
  // Bank the 161-bit scan into legal 7-series block-RAM port widths. A
  // monolithic 128x161 array cannot map to one RAMB port. Two 72-bit banks
  // and one 17-bit bank preserve the single-clock, single-read/write FIFO
  // semantics; the OOC report remains authoritative about LUTRAM vs RAMB.
  reg [6:0] wr_ptr, rd_ptr;
  reg [7:0] fifo_count;
  wire [71:0] fifo_q0, fifo_q1;
  wire [16:0] fifo_q2;
  wire [FIFO_W-1:0] rd_data = {fifo_q2,fifo_q1,fifo_q0};
  reg [6:0] rd_addr;
  reg rd_read_en, rd_meta_pending, rd_valid, rd_gap;
  // Registered NCO stage.  It prevents the NCO LUT/complex multiply from
  // feeding the FIR multipliers in one 50-MHz cycle; while FIR launches one
  // scan, this stage may be refilled with the next prefetched scan.
  reg nco_valid, nco_discontinuity;
  reg [31:0] nco_epoch;
  reg [63:0] nco_sample;
  // Rotation needs one guard bit: legal full-scale I and Q can rotate to
  // sqrt(2) times either component.  Do not silently saturate here.
  reg signed [16:0] nco0, nco1, nco2, nco3;

  reg prev_valid;
  reg [31:0] prev_epoch;
  reg [63:0] prev_sample;
  reg [31:0] phase_q0_32;
  reg [31:0] active_phase_inc_q0_32, active_phase_q0_32;
  reg cfg_pending;
  reg [31:0] pending_phase_inc_q0_32, pending_phase_q0_32, pending_apply_epoch;
  reg [63:0] pending_apply_sample;

  reg signed [16:0] history0 [0:39];
  reg signed [16:0] history1 [0:39];
  reg signed [16:0] history2 [0:39];
  reg signed [16:0] history3 [0:39];
  reg signed [16:0] new0, new1, new2, new3;
  reg signed [47:0] accum0, accum1, accum2, accum3;
  reg [3:0] fir_cycle;
  reg busy;
  reg work_discontinuity;
  reg [31:0] work_epoch;
  reg [63:0] work_sample;

  reg [127:0] out_data;
  reg [31:0] out_epoch;
  reg [63:0] out_sample;
  reg out_discontinuity, out_valid;

  // Frozen signed Q1.15 sin(2*pi*k/1024) ROM.  No real-number synthesis is
  // involved.  Configuration/build scripts must add the .mem file.
  reg signed [15:0] nco_sine [0:1023];
  initial $readmemh(NCO_MEM_FILE, nco_sine);
  wire push = s_valid && s_ready;

  // A small retained storage boundary prevents data-lane consumers from
  // absorbing only part of a bank's read register and defeating RAM mapping.
  // It adds no register: each bank still returns on the same synchronous edge.
  rxv2_fifo_bram_bank #(.WIDTH(72),.DEPTH(FIFO_DEPTH)) fifo_bank0(
    .clk(clk),.we(resetn&&push),.wa(wr_ptr),.wd({s_epoch[7:0],s_data}),
    .re(rd_read_en),.ra(rd_addr),.q(fifo_q0));
  rxv2_fifo_bram_bank #(.WIDTH(72),.DEPTH(FIFO_DEPTH)) fifo_bank1(
    .clk(clk),.we(resetn&&push),.wa(wr_ptr),.wd({s_sample[47:0],s_epoch[31:8]}),
    .re(rd_read_en),.ra(rd_addr),.q(fifo_q1));
  rxv2_fifo_bram_bank #(.WIDTH(17),.DEPTH(FIFO_DEPTH)) fifo_bank2(
    .clk(clk),.we(resetn&&push),.wa(wr_ptr),.wd({s_discontinuity,s_sample[63:48]}),
    .re(rd_read_en),.ra(rd_addr),.q(fifo_q2));

  // Reserve -> synchronous read -> metadata is still safely inside the
  // 11-clock FIR service interval.  It avoids mixed RAM/comparator logic.
  wire pop_issue = !rd_read_en && !rd_meta_pending && !rd_valid && (fifo_count != 0);
  assign s_ready = (fifo_count < FIFO_DEPTH-1);

  wire [63:0] launch_data = rd_data[63:0];
  wire [31:0] launch_epoch = rd_data[95:64];
  wire [63:0] launch_sample = rd_data[159:96];
  wire launch_mark = rd_data[160];
  // This continuity comparison is registered alongside the synchronous FIFO
  // read.  It must not sit in the NCO LUT/DSP timing path at 50 MHz.
  wire input_gap = rd_gap;
  // A commit is associated with an input coordinate, not a wall-clock edge:
  // scans already resident in the FIFO must retain their old CFO correction.
  wire nco_apply_commit = cfg_pending && launch_epoch == pending_apply_epoch &&
                          launch_sample == pending_apply_sample;
  wire [31:0] selected_phase_inc = nco_apply_commit ? pending_phase_inc_q0_32 : active_phase_inc_q0_32;
  wire [31:0] selected_phase_base = nco_apply_commit ? pending_phase_q0_32 : active_phase_q0_32;
  wire [31:0] launch_phase = (input_gap || nco_apply_commit) ? selected_phase_base : phase_q0_32;
  wire [31:0] launch_cos_phase = launch_phase + 32'h4000_0000;
  wire signed [15:0] launch_sin = nco_sine[launch_phase[31:22]];
  wire signed [15:0] launch_cos = nco_sine[launch_cos_phase[31:22]];
  wire launch_fire = nco_valid && !busy && (!out_valid || m_ready);
  wire nco_fire = rd_valid && (!nco_valid || launch_fire);
  wire signed [15:0] launch_i0 = launch_data[15:0];
  wire signed [15:0] launch_q0 = launch_data[31:16];
  wire signed [15:0] launch_i1 = launch_data[47:32];
  wire signed [15:0] launch_q1 = launch_data[63:48];

  // Eight explicit NCO products: two complex rotations for each scan.
  (* use_dsp = "yes" *) wire signed [31:0] nco_i0c = launch_i0 * launch_cos;
  (* use_dsp = "yes" *) wire signed [31:0] nco_q0s = launch_q0 * launch_sin;
  (* use_dsp = "yes" *) wire signed [31:0] nco_i0s = launch_i0 * launch_sin;
  (* use_dsp = "yes" *) wire signed [31:0] nco_q0c = launch_q0 * launch_cos;
  (* use_dsp = "yes" *) wire signed [31:0] nco_i1c = launch_i1 * launch_cos;
  (* use_dsp = "yes" *) wire signed [31:0] nco_q1s = launch_q1 * launch_sin;
  (* use_dsp = "yes" *) wire signed [31:0] nco_i1s = launch_i1 * launch_sin;
  (* use_dsp = "yes" *) wire signed [31:0] nco_q1c = launch_q1 * launch_cos;

  // Full Q1.15 complex rotation is bounded by +/-46341 for signed-16 input,
  // which fits signed17 exactly.  Keep it wide into the FIR history.
  wire signed [32:0] nco_rot0_i_full = (nco_i0c + nco_q0s) >>> 15;
  wire signed [32:0] nco_rot0_q_full = (nco_q0c - nco_i0s) >>> 15;
  wire signed [32:0] nco_rot1_i_full = (nco_i1c + nco_q1s) >>> 15;
  wire signed [32:0] nco_rot1_q_full = (nco_q1c - nco_i1s) >>> 15;
  wire signed [16:0] launch_rot0_i = nco_rot0_i_full[16:0];
  wire signed [16:0] launch_rot0_q = nco_rot0_q_full[16:0];
  wire signed [16:0] launch_rot1_i = nco_rot1_i_full[16:0];
  wire signed [16:0] launch_rot1_q = nco_rot1_q_full[16:0];

  function automatic signed [16:0] lag_sample;
    input integer component;
    input integer lag;
    begin
      if (lag == 0) begin
        // This function feeds next_acc*, which is sampled only by the
        // `else if (busy)` branch below.  A launch edge instead takes the
        // preceding branch and writes its first two terms from lm*.  Thus in
        // every sampled FIR steady cycle launch_fire is false and lag zero is
        // exactly the registered new sample.  Keeping launch_fire out of this
        // mux cuts an otherwise dead launch->FIR arithmetic timing arc.
        case (component)
          0: lag_sample = new0;
          1: lag_sample = new1;
          2: lag_sample = new2;
          default: lag_sample = new3;
        endcase
      end else begin
        // Discontinuities clear history[1:39] in the launch branch before the
        // next busy cycle, so the steady FIR never needs a launch-dependent
        // zero mux here.
        case (component)
          0: lag_sample = history0[lag];
          1: lag_sample = history1[lag];
          2: lag_sample = history2[lag];
          default: lag_sample = history3[lag];
        endcase
      end
    end
  endfunction
  // `fir_cycle` is observed only while busy; launch writes it to one and uses
  // the dedicated lm* h[0]/h[1] path, so no launch-cycle zero override is
  // required by the steady coefficient/address path.
  wire [4:0] term_a = {fir_cycle, 1'b0};
  wire [4:0] term_b = {fir_cycle, 1'b0} + 5'd1;
  wire signed [15:0] coeff_a = rxv2_rrc_coeff_q15(term_a);
  wire signed [15:0] coeff_b = (term_b <= 20) ? rxv2_rrc_coeff_q15(term_b) : 16'sd0;
  // Keep each delay-line read explicit.  Besides avoiding a simulator's
  // nested automatic-function corner case, this makes the two FIR read ports
  // per scalar engine visible to synthesis.
  wire signed [16:0] a0l=lag_sample(0,term_a), a0r=lag_sample(0,40-term_a);
  wire signed [16:0] a1l=lag_sample(1,term_a), a1r=lag_sample(1,40-term_a);
  wire signed [16:0] a2l=lag_sample(2,term_a), a2r=lag_sample(2,40-term_a);
  wire signed [16:0] a3l=lag_sample(3,term_a), a3r=lag_sample(3,40-term_a);
  wire signed [16:0] b0l=lag_sample(0,term_b), b0r=lag_sample(0,40-term_b);
  wire signed [16:0] b1l=lag_sample(1,term_b), b1r=lag_sample(1,40-term_b);
  wire signed [16:0] b2l=lag_sample(2,term_b), b2r=lag_sample(2,40-term_b);
  wire signed [16:0] b3l=lag_sample(3,term_b), b3r=lag_sample(3,40-term_b);
  wire signed [17:0] p0a = (term_a==20) ? {a0l[16],a0l} : {a0l[16],a0l}+{a0r[16],a0r};
  wire signed [17:0] p1a = (term_a==20) ? {a1l[16],a1l} : {a1l[16],a1l}+{a1r[16],a1r};
  wire signed [17:0] p2a = (term_a==20) ? {a2l[16],a2l} : {a2l[16],a2l}+{a2r[16],a2r};
  wire signed [17:0] p3a = (term_a==20) ? {a3l[16],a3l} : {a3l[16],a3l}+{a3r[16],a3r};
  wire signed [17:0] p0b = (term_b>20) ? 18'sd0 : ((term_b==20) ? {b0l[16],b0l} : {b0l[16],b0l}+{b0r[16],b0r});
  wire signed [17:0] p1b = (term_b>20) ? 18'sd0 : ((term_b==20) ? {b1l[16],b1l} : {b1l[16],b1l}+{b1r[16],b1r});
  wire signed [17:0] p2b = (term_b>20) ? 18'sd0 : ((term_b==20) ? {b2l[16],b2l} : {b2l[16],b2l}+{b2r[16],b2r});
  wire signed [17:0] p3b = (term_b>20) ? 18'sd0 : ((term_b==20) ? {b3l[16],b3l} : {b3l[16],b3l}+{b3r[16],b3r});
  // Eight real FIR multipliers.  Each scalar engine owns two lanes.
  (* use_dsp = "yes" *) wire signed [33:0] m0a = p0a * coeff_a;
  (* use_dsp = "yes" *) wire signed [33:0] m0b = p0b * coeff_b;
  (* use_dsp = "yes" *) wire signed [33:0] m1a = p1a * coeff_a;
  (* use_dsp = "yes" *) wire signed [33:0] m1b = p1b * coeff_b;
  (* use_dsp = "yes" *) wire signed [33:0] m2a = p2a * coeff_a;
  (* use_dsp = "yes" *) wire signed [33:0] m2b = p2b * coeff_b;
  (* use_dsp = "yes" *) wire signed [33:0] m3a = p3a * coeff_a;
  (* use_dsp = "yes" *) wire signed [33:0] m3b = p3b * coeff_b;
  wire signed [47:0] next_acc0 = accum0 + m0a + m0b;
  wire signed [47:0] next_acc1 = accum1 + m1a + m1b;
  wire signed [47:0] next_acc2 = accum2 + m2a + m2b;
  wire signed [47:0] next_acc3 = accum3 + m3a + m3b;
  // First two symmetric terms are captured on the launch edge from the NCO
  // register and pre-shift delay line.  Keeping them explicit avoids a
  // launch-valid-dependent function path and preserves the 11-clock cadence.
  wire signed [17:0] la0 = {nco0[16],nco0} + (nco_discontinuity ? 18'sd0 : {history0[39][16],history0[39]});
  wire signed [17:0] la1 = {nco1[16],nco1} + (nco_discontinuity ? 18'sd0 : {history1[39][16],history1[39]});
  wire signed [17:0] la2 = {nco2[16],nco2} + (nco_discontinuity ? 18'sd0 : {history2[39][16],history2[39]});
  wire signed [17:0] la3 = {nco3[16],nco3} + (nco_discontinuity ? 18'sd0 : {history3[39][16],history3[39]});
  wire signed [17:0] lb0 = nco_discontinuity ? 18'sd0 : ({history0[0][16],history0[0]} + {history0[38][16],history0[38]});
  wire signed [17:0] lb1 = nco_discontinuity ? 18'sd0 : ({history1[0][16],history1[0]} + {history1[38][16],history1[38]});
  wire signed [17:0] lb2 = nco_discontinuity ? 18'sd0 : ({history2[0][16],history2[0]} + {history2[38][16],history2[38]});
  wire signed [17:0] lb3 = nco_discontinuity ? 18'sd0 : ({history3[0][16],history3[0]} + {history3[38][16],history3[38]});
  wire signed [33:0] lm0a=la0*16'sd123,lm1a=la1*16'sd123,lm2a=la2*16'sd123,lm3a=la3*16'sd123;
  wire signed [33:0] lm0b=lb0*-16'sd39,lm1b=lb1*-16'sd39,lm2b=lb2*-16'sd39,lm3b=lb3*-16'sd39;

  integer index;
  always @(posedge clk) begin
    if (!resetn) begin
      wr_ptr <= 0; rd_ptr <= 0; fifo_count <= 0; rd_addr <= 0;
      rd_read_en <= 0; rd_meta_pending <= 0; rd_valid <= 0; rd_gap <= 0;
      nco_valid <= 0; nco_discontinuity <= 0; nco_epoch <= 0; nco_sample <= 0;
      nco0 <= 0; nco1 <= 0; nco2 <= 0; nco3 <= 0;
      prev_valid <= 0; prev_epoch <= 0; prev_sample <= 0; phase_q0_32 <= 0;
      active_phase_inc_q0_32 <= cfg_phase_inc_q0_32;
      active_phase_q0_32 <= cfg_phase_q0_32;
      cfg_pending <= 0; pending_phase_inc_q0_32 <= 0; pending_phase_q0_32 <= 0;
      pending_apply_epoch <= 0; pending_apply_sample <= 0;
      new0 <= 0; new1 <= 0; new2 <= 0; new3 <= 0;
      accum0 <= 0; accum1 <= 0; accum2 <= 0; accum3 <= 0; fir_cycle <= 0; busy <= 0;
      work_discontinuity <= 0; work_epoch <= 0; work_sample <= 0;
      out_data <= 0; out_epoch <= 0; out_sample <= 0; out_discontinuity <= 0; out_valid <= 0;
      fifo_overflow_sticky <= 0; coordinate_fault_sticky <= 0;
      for (index = 0; index < 40; index = index + 1) begin
        history0[index] <= 0; history1[index] <= 0;
        history2[index] <= 0; history3[index] <= 0;
      end
    end else begin
      if (out_valid && m_ready) out_valid <= 0;
      if (cfg_commit) begin
        cfg_pending <= 1;
        pending_phase_inc_q0_32 <= cfg_phase_inc_q0_32;
        pending_phase_q0_32 <= cfg_phase_q0_32;
        pending_apply_epoch <= cfg_apply_epoch;
        pending_apply_sample <= cfg_apply_sample;
      end
      // AXI-stream legal backpressure is `valid=1, ready=0` with the source
      // holding its scan stable.  It is not an overflow.  The non-backpressure
      // CPACK/RXTH ingress owns its own explicit overflow/epoch indication.
      if (push) begin
        wr_ptr <= wr_ptr + 1'b1;
      end
      if (pop_issue) begin
        rd_read_en <= 1;
        rd_addr <= rd_ptr;
        rd_ptr <= rd_ptr + 1'b1;
      end
      if (rd_read_en) begin
        // rd_data is loaded by the dedicated RAM process on this edge.
        rd_read_en <= 0;
        rd_meta_pending <= 1;
      end
      if (rd_meta_pending) begin
        rd_gap <= !prev_valid || rd_data[160] ||
                  rd_data[95:64] != prev_epoch ||
                  rd_data[159:96] != (prev_sample + 64'd1);
        rd_meta_pending <= 0;
        rd_valid <= 1;
      end
      case ({push, pop_issue})
        2'b10: fifo_count <= fifo_count + 1'b1;
        2'b01: fifo_count <= fifo_count - 1'b1;
        default: fifo_count <= fifo_count;
      endcase

      if (launch_fire) begin
        rd_valid <= 0;
        busy <= 1;
        // Consume h[0] and h[1] in the launch cycle; h[2]..h[20]
        // occupy the next ten clocks, giving a true 11-clock scan cadence.
        fir_cycle <= 1;
        accum0 <= lm0a + lm0b; accum1 <= lm1a + lm1b;
        accum2 <= lm2a + lm2b; accum3 <= lm3a + lm3b;
        new0 <= nco0; new1 <= nco1;
        new2 <= nco2; new3 <= nco3;
        for (index = 39; index > 0; index = index - 1) begin
          history0[index] <= history0[index-1]; history1[index] <= history1[index-1];
          history2[index] <= history2[index-1]; history3[index] <= history3[index-1];
        end
        history0[0] <= nco0; history1[0] <= nco1;
        history2[0] <= nco2; history3[0] <= nco3;
        work_discontinuity <= nco_discontinuity;
        work_epoch <= nco_epoch; work_sample <= nco_sample;
        if (nco_discontinuity) begin
          // The current scan must remain visible, but prior history cannot
          // contribute across a physical capture discontinuity.
          for (index = 1; index < 40; index = index + 1) begin
            history0[index] <= 0; history1[index] <= 0;
            history2[index] <= 0; history3[index] <= 0;
          end
        end
      end else if (busy) begin
        accum0 <= next_acc0; accum1 <= next_acc1; accum2 <= next_acc2; accum3 <= next_acc3;
        if (fir_cycle == 10) begin
          // All 21 unique coefficients have been consumed.  Keep Q*.15 in
          // signed32 rather than narrowing the matched-filter observation.
          out_data <= {next_acc3[46:15], next_acc2[46:15],
                       next_acc1[46:15], next_acc0[46:15]};
          out_epoch <= work_epoch; out_sample <= work_sample;
          out_discontinuity <= work_discontinuity; out_valid <= 1; busy <= 0;
        end else fir_cycle <= fir_cycle + 1'b1;
      end
      // NCO is a separate registered pipeline stage.  Its raw coordinate
      // continuity decision and phase advance happen exactly once per scan.
      // If a FIR launch consumes the old NCO word this same edge, nco_fire
      // refills it, so the 11-clock FIR cadence remains achievable.
      if (nco_fire) begin
        rd_valid <= 0;
        nco_valid <= 1;
        nco0 <= launch_rot0_i; nco1 <= launch_rot0_q;
        nco2 <= launch_rot1_i; nco3 <= launch_rot1_q;
        nco_discontinuity <= input_gap;
        nco_epoch <= launch_epoch; nco_sample <= launch_sample;
        // A deliberate discontinuity marker or an epoch transition resets
        // state but is not a transport-coordinate fault.  Flag only an
        // unmarked hole/reorder inside one epoch.  Keep this independent of
        // cfg_commit: a phase handoff must not mask a malformed coordinate.
        if (prev_valid && !launch_mark && launch_epoch == prev_epoch &&
            launch_sample != (prev_sample + 64'd1))
          coordinate_fault_sticky <= 1'b1;
        if (nco_apply_commit) begin
          active_phase_inc_q0_32 <= pending_phase_inc_q0_32;
          active_phase_q0_32 <= pending_phase_q0_32;
          cfg_pending <= 0;
          phase_q0_32 <= pending_phase_q0_32 + pending_phase_inc_q0_32;
        end else if (input_gap) begin
          phase_q0_32 <= selected_phase_base + selected_phase_inc;
        end else phase_q0_32 <= launch_phase + selected_phase_inc;
        prev_valid <= 1; prev_epoch <= launch_epoch; prev_sample <= launch_sample;
      end else if (launch_fire) begin
        nco_valid <= 0;
      end
    end
  end

  assign m_data = out_data;
  assign m_epoch = out_epoch;
  assign m_sample = out_sample;
  assign m_discontinuity = out_discontinuity;
  assign m_valid = out_valid;
endmodule

// Single-clock simple-dual-port storage. FIFO ownership/valid/reset are in
// the parent, so neither RAM contents nor read data need a reset mux.
(* keep_hierarchy = "yes" *)
module rxv2_fifo_bram_bank #(
  parameter integer WIDTH=72,
  parameter integer DEPTH=128
) (
  input wire clk,input wire we,input wire [6:0] wa,input wire [WIDTH-1:0] wd,
  input wire re,input wire [6:0] ra,output reg [WIDTH-1:0] q
);
  (* ram_style="block" *) reg [WIDTH-1:0] mem [0:DEPTH-1];
  always @(posedge clk)begin
    if(we)mem[wa]<=wd;
    if(re)q<=mem[ra];
  end
endmodule
