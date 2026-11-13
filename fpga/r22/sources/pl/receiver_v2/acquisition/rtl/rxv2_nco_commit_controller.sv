`timescale 1ns/1ps
// CFO commit controller in the 4-MSPS matched-scan coordinate domain.
// DSP rotates x*exp(-j phase); positive received CFO is passed through.
module rxv2_nco_commit_controller(
 input wire clk,input wire resetn,input wire soft_flush,
 input wire acq_valid,input wire [31:0] acq_epoch,input wire [63:0] acq_origin,input wire [31:0] acq_rx_inc,
 input wire now_valid,input wire [31:0] now_epoch,input wire [63:0] now_sample,
 input wire fb_valid,input wire [31:0] fb_epoch,input wire [63:0] fb_origin,input wire signed [31:0] fb_delta_hz_q16,
 output reg cfg_commit,output reg [31:0] cfg_rx_phase_inc_q0_32,output reg [31:0] cfg_phase_q0_32,
 output reg [31:0] cfg_apply_epoch,output reg [63:0] cfg_apply_sample,
 output reg lock_ok,output reg stale_feedback_sticky
);
 // One restoring divider services the slow frame-rate control updates.
 // Phase products are modulo 2^32, so only low coordinate bits are needed.
 localparam [3:0] IDLE=0,DIV_HZ=1,ROUND_HZ=2,BOUND_INIT=3,DIV_BOUND=4,
                  BOUND_APPLY=5,MULTIPLY=6,PHASE=7,EMIT=8;
 reg [3:0] state;
 reg [31:0] epoch_q,inc_q,phase_offset_q;
 reg [63:0] last_fb_origin;
 reg [31:0] pending_epoch,pending_inc,old_inc,old_offset;
 reg [63:0] pending_origin,apply_q,limit_q;
 reg signed [31:0] delta_q;
 reg [31:0] old_product,new_product,phase_q,offset_q;
 reg [63:0] dividend,quotient;
 reg [15:0] denominator;
 reg [16:0] remainder;
 reg [6:0] count;
 reg negative;
 wire [16:0] shifted_remainder={remainder[15:0],dividend[63]};
 wire subtract_enable=shifted_remainder>={1'b0,denominator};
 wire [16:0] next_remainder=subtract_enable ? shifted_remainder-{1'b0,denominator} : shifted_remainder;
 wire [63:0] next_quotient={quotient[62:0],subtract_enable};
 wire [31:0] rounded_hz=quotient[31:0]+(remainder>=17'd7813);
 wire signed [39:0] extended_delta={{8{fb_delta_hz_q16[31]}},fb_delta_hz_q16};
 wire [39:0] scaled_magnitude=(fb_delta_hz_q16[31] ? -extended_delta : extended_delta)<<8;
 wire [64:0] boundary_sum={1'b0,pending_origin}+({1'b0,quotient}+65'd1)*65'd3840;
 always @(posedge clk) begin
  if(!resetn||soft_flush) begin
    cfg_commit<=0;cfg_rx_phase_inc_q0_32<=0;cfg_phase_q0_32<=0;cfg_apply_epoch<=0;cfg_apply_sample<=0;
    lock_ok<=0;stale_feedback_sticky<=0;epoch_q<=0;inc_q<=0;phase_offset_q<=0;last_fb_origin<=0;
    state<=IDLE;pending_epoch<=0;pending_inc<=0;old_inc<=0;old_offset<=0;
    pending_origin<=0;apply_q<=0;limit_q<=0;delta_q<=0;old_product<=0;new_product<=0;
    phase_q<=0;offset_q<=0;dividend<=0;quotient<=0;denominator<=0;remainder<=0;count<=0;negative<=0;
  end else begin
    cfg_commit<=0;
    // Acquisition supersedes an unfinished feedback job. Only committed
    // state is used; no half-applied phase/frequency survives cancellation.
    if(acq_valid) begin
      if(acq_origin>64'hfffffffffffff0ff) begin state<=IDLE;stale_feedback_sticky<=1;end
      else begin
        pending_epoch<=acq_epoch;pending_origin<=acq_origin;apply_q<=acq_origin+64'd3840;
        if(lock_ok && acq_epoch==epoch_q) begin
          pending_inc<=inc_q+acq_rx_inc;old_inc<=inc_q;old_offset<=phase_offset_q;
        end else begin pending_inc<=acq_rx_inc;old_inc<=acq_rx_inc;old_offset<=0;end
        state<=MULTIPLY;
      end
    end else begin
      if(fb_valid && (state!=IDLE || !lock_ok || !now_valid || fb_epoch!=epoch_q || now_epoch!=epoch_q || fb_origin<last_fb_origin))
        stale_feedback_sticky<=1;
      case(state)
       IDLE: if(fb_valid && lock_ok && now_valid && fb_epoch==epoch_q && now_epoch==epoch_q && fb_origin>=last_fb_origin) begin
         if(now_sample>64'hfffffffffffffd7f) stale_feedback_sticky<=1;
         else begin
           pending_epoch<=epoch_q;pending_origin<=fb_origin;old_inc<=inc_q;old_offset<=phase_offset_q;
           limit_q<=now_sample+64'd640;
           dividend<={24'd0,scaled_magnitude};denominator<=16'd15625;
           quotient<=0;remainder<=0;count<=0;negative<=fb_delta_hz_q16[31];state<=DIV_HZ;
         end
       end
       DIV_HZ,DIV_BOUND: begin
         dividend<={dividend[62:0],1'b0};quotient<=next_quotient;remainder<=next_remainder;
         if(count==63) state<=state==DIV_HZ ? ROUND_HZ : BOUND_APPLY;
         else count<=count+1'b1;
       end
       ROUND_HZ: begin
         // Odd denominator: exact half-way remainder is impossible.
         delta_q<=negative ? -$signed(rounded_hz) : $signed(rounded_hz);state<=BOUND_INIT;
       end
       BOUND_INIT: begin
         pending_inc<=old_inc+delta_q;
         if(limit_q<pending_origin) begin
           if(pending_origin>64'hfffffffffffff0ff) begin stale_feedback_sticky<=1;state<=IDLE;end
           else begin apply_q<=pending_origin+64'd3840;state<=MULTIPLY;end
         end else begin
           dividend<=limit_q-pending_origin;denominator<=16'd3840;quotient<=0;remainder<=0;count<=0;state<=DIV_BOUND;
         end
       end
       BOUND_APPLY: begin
         if(boundary_sum[64]) begin stale_feedback_sticky<=1;state<=IDLE;end
         else begin apply_q<=boundary_sum[63:0];state<=MULTIPLY;end
       end
       MULTIPLY: begin
         old_product<=old_inc*apply_q[31:0];new_product<=pending_inc*apply_q[31:0];state<=PHASE;
       end
       PHASE: begin phase_q<=old_product+old_offset;offset_q<=old_product+old_offset-new_product;state<=EMIT;end
       EMIT: begin
         epoch_q<=pending_epoch;inc_q<=pending_inc;phase_offset_q<=offset_q;last_fb_origin<=pending_origin;
         cfg_rx_phase_inc_q0_32<=pending_inc;cfg_phase_q0_32<=phase_q;
         cfg_apply_epoch<=pending_epoch;cfg_apply_sample<=apply_q;cfg_commit<=1;lock_ok<=1;state<=IDLE;
       end
       default: state<=IDLE;
      endcase
    end
  end
 end
endmodule
