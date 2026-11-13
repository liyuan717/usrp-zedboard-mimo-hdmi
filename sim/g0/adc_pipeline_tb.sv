`timescale 1ns/1ps
// G0 measurement pipeline: actual vendor WFIFO/CPACK and XPM elastic RAM.
module adc_pipeline_tb;
 reg clk=0,src_clk=0,src_running=1,por_n=0,raw_reset=1,source_ready=0;
 always #10 clk=~clk;
 always begin #31.25; if(src_running)src_clk=~src_clk;end
 wire wr_reset,rd_resetn,pack_reset,elastic_resetn,write_enable,pack_enable,reader_enable,fence,capture_ready;
 wire flush,flush_ack;
 rfv2_capture_reset_guard guard(.din_clk(src_clk),.dout_clk(clk),
  .din_raw_reset(raw_reset),.dout_raw_reset(raw_reset),.pll_locked(1'b1),.source_ready(source_ready),
  .elastic_flush_ack(flush_ack),.wfifo_din_rst(wr_reset),.wfifo_dout_rstn(rd_resetn),
  .cpack_reset(pack_reset),.elastic_resetn(elastic_resetn),.source_write_enable(write_enable),
  .cpack_write_enable(pack_enable),.reader_data_enable(reader_enable),.capture_epoch_fence(fence),.capture_ready(capture_ready));
 reg [1:0] phase=0;wire sample_valid=phase==0;
 always @(negedge src_clk)phase<=phase+1'b1;
 wire [63:0] tag;
 g0_adc_counter counter(src_clk,por_n,wr_reset,sample_valid,tag);
 // Independent source coordinate and sample payload. A source reset creates
 // a new epoch without resetting the lifetime index.
 reg [47:0] ref_index=0;reg [15:0] ref_epoch=0;reg ref_was_reset=1;
 integer init_edges=0;
 wire [63:0] payload={16'(ref_index*17+7),16'(ref_index^16'hcdef),16'(ref_index*3+11),16'(ref_index^16'habcd)};
 wire [127:0] din={tag,payload};wire vin=sample_valid && write_enable;
 reg [127:0] expected[0:19999];reg [19999:0] present=0;
 integer injected=0;
 always @(posedge src_clk) begin
  if(!por_n)begin
   ref_index=0;ref_epoch=0;ref_was_reset=1;init_edges=0;
  end else if(init_edges<2)begin
   ref_index=0;ref_epoch=0;ref_was_reset=1;init_edges=init_edges+1;
  end else begin
   if(tag!=={ref_epoch,ref_index})$fatal(1,"source coordinate mismatch %h != %h",tag,{ref_epoch,ref_index});
   if(vin)begin expected[ref_index]={ref_epoch,ref_index,payload};present[ref_index]=1;injected=injected+1;end
   #1;
   if(wr_reset && !ref_was_reset)ref_epoch=ref_epoch+1'b1;
   ref_was_reset=wr_reset;
   if(sample_valid && !wr_reset)ref_index=ref_index+1'b1;
  end
 end
 wire [127:0] wfdata;wire [7:0] wfvalid,wfenable;wire wfovf,overflow;
 util_wfifo #(.NUM_OF_CHANNELS(8),.DIN_DATA_WIDTH(16),.DOUT_DATA_WIDTH(16),.DIN_ADDRESS_WIDTH(4)) wfifo(
  .din_clk(src_clk),.din_rst(wr_reset),.dout_clk(clk),.dout_rstn(rd_resetn),.din_ovf(wfovf),.dout_ovf(overflow),
`include "g0_wfifo_ports.vh"
 );
 wire [127:0] packed_data;wire packed_en;
 util_cpack2 #(.NUM_OF_CHANNELS(8),.SAMPLE_DATA_WIDTH(16),.INTERFACE_TYPE(1)) cpack(
  .clk(clk),.reset(pack_reset),.fifo_wr_en(wfvalid[0]),.packed_fifo_wr_data(packed_data),
  .packed_fifo_wr_en(packed_en),.packed_fifo_wr_overflow(overflow),.m_axis_ready(1'b0),
`include "g0_cpack_ports.vh"
 );
 wire [127:0] edata,gdata;wire [15:0] ekeep,gkeep;wire ev,er,gv,gr;
 rf_rx_elastic_fifo #(.DATA_WIDTH(128)) elastic(.clk(clk),.resetn(elastic_resetn),
  .flush_sync(flush),.flush_ack(flush_ack),.packed_fifo_wr_en(packed_en && pack_enable),
  .packed_fifo_wr_data(packed_data),.packed_fifo_wr_overflow(overflow),
  .m_axis_tdata(edata),.m_axis_tkeep(ekeep),.m_axis_tvalid(ev),.m_axis_tready(er));
 rfv2_capture_axis_gate gate(.aclk(clk),.enable(reader_enable),.s_axis_tdata(edata),.s_axis_tkeep(ekeep),
  .s_axis_tstrb(ekeep),.s_axis_tlast(1'b0),.s_axis_tvalid(ev),.s_axis_tready(er),
  .m_axis_tdata(gdata),.m_axis_tkeep(gkeep),.m_axis_tvalid(gv),.m_axis_tready(gr));
 reg av=0,wv=0;wire awready,wready,bvalid;reg [31:0] wdata=0;reg [15:0] awaddr=8;
 wire [63:0] odata;wire ov;reg oready=1;
 g0_adc_record recorder(.aclk(clk),.aresetn(por_n),.s_axi_awaddr(awaddr),.s_axi_awvalid(av),.s_axi_awready(awready),
  .s_axi_wdata(wdata),.s_axi_wstrb(4'hf),.s_axi_wvalid(wv),.s_axi_wready(wready),.s_axi_bvalid(bvalid),.s_axi_bready(1'b1),
  .s_axi_araddr(16'b0),.s_axi_arvalid(1'b0),.s_axi_rready(1'b1),
  .s_axis_tdata(gdata),.s_axis_tkeep(gkeep),.s_axis_tvalid(gv),.s_axis_tready(gr),.s_axis_tlast(1'b0),
  .m_axis_tdata(odata),.m_axis_tvalid(ov),.m_axis_tready(oready),
  .ingress_overflow(overflow),.ingress_flush(flush),.rf_path_ready(capture_ready),.capture_epoch_fence(fence));
 reg part=0,held_valid=0;reg [63:0] out_tag,held_data;integer received=0,last_index=-1,overflow_events=0,fences=0,gaps=0;
 always @(posedge clk)if(por_n)begin
  if(held_valid && (!ov || odata!==held_data))$fatal(1,"held DMA output changed");
  held_valid=ov && !oready;held_data=odata;
  if(overflow)overflow_events=overflow_events+1;
  if(fence)fences=fences+1;
  if(ov && oready)begin
   if(!part)begin out_tag=odata;part=1;end
   else begin
    if(out_tag[47:0]>=20000 || !present[out_tag[47:0]])$fatal(1,"unknown source coordinate");
    if({out_tag,odata}!==expected[out_tag[47:0]])$fatal(1,"data/tag alignment mismatch %h",out_tag);
    if(int'(out_tag[47:0])<=last_index)$fatal(1,"duplicate/reordered source coordinate");
    if(last_index>=0 && int'(out_tag[47:0])!=last_index+1)gaps=gaps+1;
    last_index=int'(out_tag[47:0]);received=received+1;part=0;
   end
  end
 end
 task csr_write(input [15:0] addr,input [31:0] value);
  begin
   @(negedge clk);av=1;wv=1;awaddr=addr;wdata=value;
   do @(posedge clk);while(!(awready && wready));
   @(negedge clk);av=0;wv=0;
   wait(bvalid);@(negedge clk);
  end
 endtask
 task run(input bit value);csr_write(8,value);endtask
 integer finite_start;
 initial begin
  repeat(8)@(negedge clk);por_n=1;
  repeat(8)@(negedge clk);raw_reset=0;source_ready=1;
  wait(capture_ready);run(1);
  // Initial startup must complete the real flush ACK protocol.
  wait(received>=3000);
  if(gaps || overflow_events)$fatal(1,"clean pipeline lost data");
  // Fill beyond elastic capacity: retained records must still have exact
  // source coordinates; dropped scans must remain visible as index gaps.
  @(negedge clk);oready=0;repeat(25000)@(negedge clk);oready=1;
  repeat(20000)@(negedge clk);
  if(!overflow_events || !gaps)$fatal(1,"overflow not observable");
  // Stop the ADC clock while records remain queued, fault both interfaces,
  // then restart through the existing reset/epoch fence protocol.
  @(negedge src_clk);src_running=0;source_ready=0;
  repeat(50)@(negedge clk);
  if(capture_ready)$fatal(1,"stopped source remained ready");
  src_running=1;repeat(8)@(negedge src_clk);source_ready=1;
  wait(capture_ready);repeat(25000)@(negedge clk);run(0);
  repeat(3000)@(negedge clk);
  if(fences<2 || part || received<5000)$fatal(1,"recovery incomplete");
  // Continuous ADC input must not create a prefetch tail after finite DMA.
  finite_start=received;csr_write(44,1000);run(1);
  wait(received==finite_start+1000);repeat(1000)@(negedge clk);
  if(received!=finite_start+1000 || recorder.pending || recorder.run || recorder.remaining)$fatal(1,"finite capture did not quiesce exactly");
  finite_start=received;run(1);wait(received==finite_start+1000);repeat(1000)@(negedge clk);
  if(received!=finite_start+1000 || recorder.pending || recorder.run)$fatal(1,"finite reentry leaked data");
  $display("G0_ADC_PIPELINE_PASS received=%0d injected=%0d overflow_events=%0d gaps=%0d fences=%0d",received,injected,overflow_events,gaps,fences);
  $finish;
 end
 initial begin #5000000;$fatal(1,"pipeline timeout");end
endmodule
