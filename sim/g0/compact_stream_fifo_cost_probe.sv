`timescale 1ns/1ps
// Common-clock, no-ready producer FIFO for the V3 ingress guard only.
// Total capacity includes the registered write command, RAM, registered read
// command, BRAM response and one spill register. Flush discards simultaneous
// input (the owner gates writes throughout flush); not a drop-in board FIFO.
module compact_stream_fifo_cost_probe #(
 parameter DATA_WIDTH=128,DEPTH=128,ADDR_WIDTH=7
)(
 input wire clk,resetn,flush_sync,output reg flush_ack,
 input wire packed_fifo_wr_en,input wire [DATA_WIDTH-1:0] packed_fifo_wr_data,
 output reg packed_fifo_wr_overflow,output wire accepted_write,ingress_drop,
 output wire [DATA_WIDTH-1:0] m_axis_tdata,
 output wire [DATA_WIDTH/8-1:0] m_axis_tkeep,m_axis_tstrb,
 output wire m_axis_tvalid,input wire m_axis_tready,output wire m_axis_tlast
);
 (* ASYNC_REG="TRUE",SHREG_EXTRACT="NO",DONT_TOUCH="TRUE" *) reg [1:0] resetn_sync=0;
 wire local_resetn=resetn_sync[1];
 reg flush_level=0,flush_cmd=0;
 wire flush_edge=flush_sync && !flush_level;
 (* DONT_TOUCH="TRUE" *) reg wr_valid=0;
 (* DONT_TOUCH="TRUE" *) reg [DATA_WIDTH-1:0] wr_data;
 reg [ADDR_WIDTH-1:0] write_ptr=0,read_ptr=0,read_address=0;
 reg [ADDR_WIDTH:0] ram_count=0,total_count=0;
 // ENB is driven by this registered command, never combinational READY.
 (* DONT_TOUCH="TRUE" *) reg read_command=0;
 reg response_valid=0,head_valid=0;
 reg [DATA_WIDTH-1:0] head_data;
 wire [DATA_WIDTH-1:0] ram_q;
 assign m_axis_tdata=head_valid?head_data:ram_q;
 assign m_axis_tvalid=(head_valid || response_valid) && resetn && local_resetn && !flush_sync && !flush_cmd;
 assign m_axis_tkeep={DATA_WIDTH/8{1'b1}};assign m_axis_tstrb=m_axis_tkeep;assign m_axis_tlast=0;
 wire pop=m_axis_tvalid && m_axis_tready;
 wire pop_head=pop && head_valid;
 wire pop_response=pop && !head_valid;
 wire [2:0] reserved={2'd0,head_valid}+{2'd0,response_valid}+{2'd0,read_command};
 // Reserve a slot now for next cycle's read. The already-reserved command
 // may replace ram_q only after its old response is consumed or spilled.
 wire plan_read=local_resetn && !flush_cmd && ram_count!=0 &&
                 (reserved-(pop?3'd1:3'd0)<2);
 wire spill=read_command && response_valid && !pop_response;
 wire mem_wren=local_resetn && !flush_cmd && wr_valid;
 wire mem_rden=local_resetn && !flush_cmd && read_command;
 wire producer_valid=packed_fifo_wr_en && resetn;
 assign accepted_write=producer_valid && local_resetn && !flush_sync && !flush_cmd && (total_count<DEPTH || pop);
 assign ingress_drop=producer_valid && !accepted_write;
 xpm_memory_sdpram #(
  .ADDR_WIDTH_A(ADDR_WIDTH),.ADDR_WIDTH_B(ADDR_WIDTH),.AUTO_SLEEP_TIME(0),
  .BYTE_WRITE_WIDTH_A(DATA_WIDTH),.CLOCKING_MODE("common_clock"),.ECC_MODE("no_ecc"),
  .MEMORY_INIT_FILE("none"),.MEMORY_INIT_PARAM("0"),.MEMORY_OPTIMIZATION("true"),
  .MEMORY_PRIMITIVE("block"),.MEMORY_SIZE(DATA_WIDTH*DEPTH),.MESSAGE_CONTROL(0),
  .READ_DATA_WIDTH_B(DATA_WIDTH),.READ_LATENCY_B(1),.READ_RESET_VALUE_B("0"),
  .RST_MODE_A("SYNC"),.RST_MODE_B("SYNC"),.SIM_ASSERT_CHK(1),
  .USE_EMBEDDED_CONSTRAINT(0),.USE_MEM_INIT(1),.WAKEUP_TIME("disable_sleep"),
  .WRITE_DATA_WIDTH_A(DATA_WIDTH),.WRITE_MODE_B("read_first")
 ) ram_i(
  .clka(clk),.ena(mem_wren),.wea(mem_wren),.addra(write_ptr),.dina(wr_data),
  .injectsbiterra(1'b0),.injectdbiterra(1'b0),.clkb(clk),.rstb(1'b0),
  .enb(mem_rden),.regceb(1'b1),.addrb(read_address),.doutb(ram_q),
  .sbiterrb(),.dbiterrb(),.sleep(1'b0));
 always @(posedge clk) begin
  resetn_sync<={resetn_sync[0],resetn};
  if(!resetn) begin flush_level<=0;flush_cmd<=0;end
  else begin flush_level<=flush_sync;flush_cmd<=flush_edge;end
  flush_ack<=local_resetn && flush_cmd;
  if(accepted_write) wr_data<=packed_fifo_wr_data;
  if(spill) head_data<=ram_q;
  if(!resetn || !local_resetn || flush_sync || flush_cmd) begin
   wr_valid<=0;read_command<=0;response_valid<=0;head_valid<=0;
  end else begin
   wr_valid<=accepted_write;
   read_command<=plan_read;
   if(plan_read) read_address<=read_ptr;
   if(read_command) response_valid<=1;
   else if(pop_response) response_valid<=0;
   if(spill) head_valid<=1;
   else if(pop_head) head_valid<=0;
  end
  if(!local_resetn || flush_cmd) begin write_ptr<=0;read_ptr<=0;ram_count<=0;end
  else begin
   if(mem_wren) write_ptr<=write_ptr+1'b1;
   if(plan_read) read_ptr<=read_ptr+1'b1;
   case({mem_wren,plan_read})
    2'b10:ram_count<=ram_count+1'b1;
    2'b01:ram_count<=ram_count-1'b1;
   endcase
  end
  if(!resetn || flush_edge || flush_cmd) total_count<=0;
  else case({accepted_write,pop})
   2'b10:total_count<=total_count+1'b1;
   2'b01:total_count<=total_count-1'b1;
  endcase
  if(!resetn || !local_resetn) packed_fifo_wr_overflow<=0;
  else packed_fifo_wr_overflow<=ingress_drop;
 end
endmodule
