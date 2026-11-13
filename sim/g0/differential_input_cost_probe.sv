`timescale 1ns/1ps
// Finite G0 differential frontend; not acquisition or a complete PHY.
// ADC and z lane order, low to high: RX0 I/Q, RX1 I/Q.
// Exact z[n] = x[n] * conj(x[n-4]); no scaling, rounding or saturation.
// Four signed16 products share each RX's turn. Two signed33 squarers then
// share the RXs. One work slot plus one immutable held response gives II8
// without stalls; the first four ADC samples need no arithmetic work slot.
module differential_input_cost_probe(
 input wire clk,rst,cancel,
 input wire in_valid,output wire in_ready,input wire in_start,
 input wire [63:0] in_tag,input wire [63:0] in_data,
 output wire out_valid,input wire out_ready,
 output reg out_start,output reg [63:0] out_tag,
 output reg [31:0] out_generation,output reg [131:0] out_z,
 output reg [69:0] out_energy,output reg out_window_valid,
 output reg [31:0] fences,
 output wire [31:0] active_generation,output wire history_restart
);
 localparam IDLE=0,MUL_RX0=1,MUL_RX1=2,Z_RX1=3,
            SQUARE_RX1=4,POWER=5,ENERGY=6,PUBLISH=7;
 reg [2:0] state;
 reg response_valid,have_previous,first_z_pending;
 reg [31:0] generation;
 reg [63:0] expected_tag;
 reg [2:0] lag_count;
 // Previous z count saturates at508: subtract starts with z number509.
 reg [9:0] z_count;
 reg [63:0] lag_history[0:3];
 reg [69:0] phase_energy[0:3];
 (* ram_style="block" *) reg [63:0] power_history[0:511];
 reg [63:0] old_power;

 reg [63:0] current_adc,lag_adc,work_tag;
 reg [31:0] work_generation;
 reg work_start,work_window_valid,work_subtract;
 reg [69:0] work_base,work_energy;
 reg [131:0] work_z;
 reg [63:0] power_rx0,work_power;
 (* use_dsp="yes" *) reg signed [31:0] product_ii,product_qq,product_qi,product_iq;
 (* use_dsp="yes" *) reg signed [65:0] square_real,square_imag;

 assign in_ready=!rst && !cancel && state==IDLE;
 // Cancellation retracts a held response before the cancellation edge.
 assign out_valid=response_valid && !rst && !cancel;
 wire input_fire=in_valid && in_ready;
 wire new_window=input_fire && (!have_previous || in_start || in_tag!=expected_tag);
 // Current ownership and its same-edge transition are separate from the
 // immutable generation snapshot carried by a held output/work item.
 assign active_generation=generation;
 assign history_restart=new_window;
 wire start_work=input_fire && !new_window && lag_count==4;
 wire output_space=!response_valid || out_ready;
 wire [8:0] expired_address=in_tag[8:0]+9'd4;

 wire select_rx1=state==MUL_RX1;
 wire signed [15:0] adc_i=select_rx1?current_adc[47:32]:current_adc[15:0];
 wire signed [15:0] adc_q=select_rx1?current_adc[63:48]:current_adc[31:16];
 wire signed [15:0] lag_i=select_rx1?lag_adc[47:32]:lag_adc[15:0];
 wire signed [15:0] lag_q=select_rx1?lag_adc[63:48]:lag_adc[31:16];
 wire signed [32:0] z_real=$signed({product_ii[31],product_ii})+
                         $signed({product_qq[31],product_qq});
 wire signed [32:0] z_imag=$signed({product_qi[31],product_qi})-
                         $signed({product_iq[31],product_iq});
 wire select_square_rx1=state==SQUARE_RX1;
 wire signed [32:0] square_input_real=select_square_rx1?work_z[98:66]:work_z[32:0];
 wire signed [32:0] square_input_imag=select_square_rx1?work_z[131:99]:work_z[65:33];
 // Although stored as signed66 products, these squares are nonnegative.
 // Valid signed16 ADC implies |z_RX|^2<=2^62 and two-RX power<=2^63;
 // high square bits and the sum's carry are therefore zero, not saturation.
 wire [64:0] square_sum={1'b0,square_real[63:0]}+{1'b0,square_imag[63:0]};
 wire [64:0] total_power={1'b0,power_rx0}+square_sum;
 // Extend before add/subtract: a valid E is at most127*2^63 <2^70.
 wire [70:0] updated_energy={1'b0,work_base}+{7'd0,work_power}-
                            (work_subtract?{7'd0,old_power}:71'd0);
 integer i;

 // True single write + synchronous read RAM. n-508 == n+4 (mod512),
 // independent of window alignment; stale entries are masked by z_count.
 // RAM and lag samples do not need reset: their validity is metadata-only.
 always @(posedge clk) begin
  if(input_fire) lag_history[in_tag[1:0]]<=in_data;
  if(start_work && z_count>=508)
   old_power<=power_history[expired_address];
  if(!rst && !cancel && state==ENERGY)
   power_history[work_tag[8:0]]<=work_power;
 end

 // Shared arithmetic units; each product is captured before its RX mux
 // changes. These registers need no reset because FSM validity gates use.
 always @(posedge clk) begin
  if(!rst && !cancel && (state==MUL_RX0 || state==MUL_RX1)) begin
   product_ii<=adc_i*lag_i;product_qq<=adc_q*lag_q;
   product_qi<=adc_q*lag_i;product_iq<=adc_i*lag_q;
  end
  if(!rst && !cancel && (state==Z_RX1 || state==SQUARE_RX1)) begin
   square_real<=square_input_real*square_input_real;
   square_imag<=square_input_imag*square_input_imag;
  end
 end

 always @(posedge clk) begin
  if(rst) begin
   state<=IDLE;response_valid<=0;have_previous<=0;first_z_pending<=0;
   generation<=0;expected_tag<=0;lag_count<=0;z_count<=0;fences<=0;
   current_adc<=0;lag_adc<=0;work_tag<=0;work_generation<=0;
   work_start<=0;work_window_valid<=0;work_subtract<=0;work_base<=0;
   work_energy<=0;work_z<=0;power_rx0<=0;work_power<=0;
   out_start<=0;out_tag<=0;out_generation<=0;out_z<=0;out_energy<=0;out_window_valid<=0;
   for(i=0;i<4;i=i+1) phase_energy[i]<=0;
  end else if(cancel) begin
   // Every high cycle advances ownership. First subsequent ADC advances it
   // again, just as the first window after reset receives generation1.
   state<=IDLE;response_valid<=0;have_previous<=0;first_z_pending<=0;
   generation<=generation+32'd1;lag_count<=0;z_count<=0;
   for(i=0;i<4;i=i+1) phase_energy[i]<=0;
  end else begin
   if(out_valid && out_ready) response_valid<=0;
   if(input_fire) begin
    have_previous<=1;
    expected_tag<={in_tag[63:48],in_tag[47:0]+48'd1};
    if(new_window) begin
     generation<=generation+32'd1;
     if(have_previous) fences<=fences+32'd1;
     lag_count<=1;z_count<=0;first_z_pending<=1;
     for(i=0;i<4;i=i+1) phase_energy[i]<=0;
    end else if(lag_count<4) lag_count<=lag_count+1'b1;
    else begin
     current_adc<=in_data;lag_adc<=lag_history[in_tag[1:0]];
     work_tag<=in_tag;work_generation<=generation;work_start<=first_z_pending;
     first_z_pending<=0;work_window_valid<=z_count>=504;work_subtract<=z_count>=508;
     work_base<=phase_energy[in_tag[1:0]];
     if(z_count<508) z_count<=z_count+1'b1;
     state<=MUL_RX0;
    end
   end
   case(state)
    IDLE:begin end
    MUL_RX0:state<=MUL_RX1;
    MUL_RX1:begin work_z[65:0]<={z_imag,z_real};state<=Z_RX1;end
    Z_RX1:begin work_z[131:66]<={z_imag,z_real};state<=SQUARE_RX1;end
    SQUARE_RX1:begin power_rx0<=square_sum[63:0];state<=POWER;end
    POWER:begin work_power<=total_power[63:0];state<=ENERGY;end
    ENERGY:begin
     work_energy<=updated_energy[69:0];phase_energy[work_tag[1:0]]<=updated_energy[69:0];
     state<=PUBLISH;
    end
    PUBLISH:if(output_space) begin
     response_valid<=1;out_start<=work_start;out_tag<=work_tag;
     out_generation<=work_generation;out_z<=work_z;out_energy<=work_energy;
     out_window_valid<=work_window_valid;state<=IDLE;
    end
    default:state<=IDLE;
   endcase
  end
 end
endmodule
