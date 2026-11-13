`timescale 1ns/1ps
module g0_iic_standard_tb;
  localparam [8:0] CR='h100, SR='h104, TX='h108, RX='h10c;
  reg clk=0, reset_n=0;
  always #10 clk=~clk;
  reg [8:0] awaddr=0, araddr=0;
  reg [31:0] wdata=0;
  reg awvalid=0, wvalid=0, arvalid=0;
  wire awready, wready, bvalid, arready, rvalid;
  wire [1:0] raw_bresp, raw_rresp;
  wire [1:0] bresp, rresp;
  wire [31:0] rdata;
  wire scl_in, scl_out, scl_release, sda_in, sda_out, sda_release;
  wire raw_scl_out, raw_sda_out;
`ifdef R22_ROUTED_IIC
  // This isolated hierarchy omits external constants and uses ARADDR for
  // both transaction directions, as driven by the routed SASD crossbar.
  assign bresp={raw_bresp[1],1'b0}, rresp={raw_rresp[1],1'b0};
  assign scl_out=1'b0, sda_out=1'b0;
`else
  assign bresp=raw_bresp, rresp=raw_rresp;
  assign scl_out=raw_scl_out, sda_out=raw_sda_out;
`endif
  wire scl_t, sda_t;
  wire [1:0] scl_drive, sda_drive;
  tri1 [1:0] scl, sda;
  reg [1:0] target_scl_low=0, target_sda_low=0;

  receiver_v2_axi_iic_main_0 dut (
    .s_axi_aclk(clk), .s_axi_aresetn(reset_n),
    .s_axi_awaddr(awaddr), .s_axi_awvalid(awvalid), .s_axi_awready(awready),
    .s_axi_wdata(wdata), .s_axi_wstrb(4'hf), .s_axi_wvalid(wvalid), .s_axi_wready(wready),
    .s_axi_bresp(raw_bresp), .s_axi_bvalid(bvalid), .s_axi_bready(1'b1),
    .s_axi_araddr(araddr), .s_axi_arvalid(arvalid), .s_axi_arready(arready),
    .s_axi_rdata(rdata), .s_axi_rresp(raw_rresp), .s_axi_rvalid(rvalid), .s_axi_rready(1'b1),
    .scl_i(scl_in), .scl_o(raw_scl_out), .scl_t(scl_release),
    .sda_i(sda_in), .sda_o(raw_sda_out), .sda_t(sda_release), .gpo(), .iic2intc_irpt()
  );
  util_i2c_mixer mixer (
    .upstream_scl_T(scl_release), .upstream_scl_I(scl_out), .upstream_scl_O(scl_in),
    .upstream_sda_T(sda_release), .upstream_sda_I(sda_out), .upstream_sda_O(sda_in),
    .downstream_scl_T(scl_t), .downstream_scl_I(scl), .downstream_scl_O(scl_drive),
    .downstream_sda_T(sda_t), .downstream_sda_I(sda), .downstream_sda_O(sda_drive)
  );
  for (genvar branch=0; branch<2; branch++) begin : wires
    assign scl[branch]=scl_t ? 1'bz : scl_drive[branch];
    assign sda[branch]=sda_t ? 1'bz : sda_drive[branch];
    assign scl[branch]=target_scl_low[branch] ? 1'b0 : 1'bz;
    assign sda[branch]=target_sda_low[branch] ? 1'b0 : 1'bz;
  end
  always @(negedge clk) if (reset_n &&
      ((!scl_t && scl_drive!==2'b00) || (!sda_t && sda_drive!==2'b00)))
    $fatal(1,"Controller actively drove a high level");

  task automatic wr(input [8:0] addr,input [31:0] value);
    @(negedge clk);
    awaddr=addr; wdata=value; awvalid=1; wvalid=1;
`ifdef R22_ROUTED_IIC
    araddr=addr;
`endif
    fork
      begin do @(posedge clk); while (!awready); @(negedge clk); awvalid=0; end
      begin do @(posedge clk); while (!wready); @(negedge clk); wvalid=0; end
      begin do @(posedge clk); while (!bvalid); if (bresp!==0) $fatal(1,"AXI write"); end
    join
  endtask
  task automatic rd(input [8:0] addr,output reg [31:0] value);
    @(negedge clk);
    araddr=addr; arvalid=1;
    fork
      begin do @(posedge clk); while (!arready); @(negedge clk); arvalid=0; end
      begin do @(posedge clk); while (!rvalid); if (rresp!==0) $fatal(1,"AXI read"); value=rdata; end
    join
  endtask

  realtime last_rise,last_fall,min_high=1e9,min_low=1e9,stretched_low;
  bit measuring=0, saw_fall=0;
  always @(posedge scl_in) if (measuring) begin
    if (saw_fall && $realtime-last_fall<min_low) min_low=$realtime-last_fall;
    last_rise=$realtime;
  end
  always @(negedge scl_in) if (measuring) begin
    if (last_rise!=0 && $realtime-last_rise<min_high) min_high=$realtime-last_rise;
    last_fall=$realtime;
    saw_fall=1;
  end

  task automatic reply(input int branch,input [7:0] payload,input bit stretch);
    reg [7:0] address;
    realtime start_stretch;
    @(negedge sda_in);
    if (scl[branch]!==1'b1) $fatal(1,"Missing START");
    @(negedge scl_in);
    if (stretch) begin
      start_stretch=$realtime;
      target_scl_low[branch]=1;
      #12000;
      if (scl_in!==1'b0 || scl[1-branch]!==1'b1)
        $fatal(1,"Stretch did not propagate through the mixer after controller released");
      target_scl_low[branch]=0;
    end
    for (int bit_index=7; bit_index>=0; bit_index--) begin
      @(posedge scl_in);
      if (stretch && bit_index==7) stretched_low=$realtime-start_stretch;
      address[bit_index]=sda[branch];
      @(negedge scl_in);
    end
    if (address!==8'h73) $fatal(1,"Wrong target address/direction %h",address);
    #100; target_sda_low[branch]=1;
    @(posedge scl_in);
    if (sda_in!==1'b0) $fatal(1,"Target ACK did not reach controller");
    @(negedge scl_in);
    for (int bit_index=7; bit_index>=0; bit_index--) begin
      #100; target_sda_low[branch]=!payload[bit_index];
      @(posedge scl_in);
      @(negedge scl_in);
    end
    #100; target_sda_low[branch]=0;
    @(posedge scl_in);
    if (sda_in!==1'b1) $fatal(1,"Missing controller NACK on final byte");
  endtask

  task automatic transfer(input int branch,input [7:0] payload,input bit stretch);
    reg [31:0] status,value;
    @(negedge clk);
    last_rise=0; saw_fall=0; measuring=1;
    fork
      reply(branch,payload,stretch);
      begin
        wr(TX,32'h173);
        wr(TX,32'h201);
        do rd(SR,status); while (status[6]!==1'b0);
        rd(RX,value);
        if (value[7:0]!==payload) $fatal(1,"Wrong FIFO byte %h expected %h",value,payload);
        do rd(SR,status); while (status[2]!==1'b0);
      end
    join
    measuring=0;
    if (scl!==2'b11 || sda!==2'b11) $fatal(1,"Bus did not release after STOP");
    $display("IIC_BRANCH=%0d PAYLOAD=%02h STRETCH=%0d",branch,payload,stretch);
  endtask

  initial begin
    repeat (15) @(negedge clk);
    reset_n=1;
    repeat (15) @(negedge clk);
    wr(CR,1);
    transfer(0,8'h96,0);
    transfer(1,8'h69,1);
    transfer(0,8'h3c,0);
    $display("IIC_EDGE_NS HIGH=%0.1f LOW=%0.1f STRETCHED_LOW=%0.1f",min_high,min_low,stretched_low);
    if (min_high<4000 || min_high>=1e9 || min_low<4700 || min_low>=1e9 || stretched_low<12000)
      $fatal(1,"Standard-mode digital SCL timing or stretch check failed");
    $display("IIC_STANDARD_MIXED_BRANCH_READBACK_PASS");
    $finish;
  end
  initial #2_000_000 $fatal(1,"IIC simulation watchdog");
endmodule
