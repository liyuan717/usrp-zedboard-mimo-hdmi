`timescale 1ns/1ps
module adc_record_tb;
 reg clk=0;always #10 clk=~clk;
 reg resetn=0;
 reg [15:0] awaddr=0,araddr=0;
 reg av=0,wv=0,bready=1,arv=0,rready=1;
 reg [31:0] wdata=0;reg [3:0] wstrb=15;
 wire awr,wr,bv,arr,rv;wire [1:0] br,rr;wire [31:0] rdata;
 reg [127:0] data=0;reg [15:0] keep=16'hffff;reg valid=0;
 wire ready;wire [63:0] odata;wire [7:0] okeep;wire oval,olast;
 reg oready=1,overflow=0,fence=0;
 wire flush;
 g0_adc_record dut(clk,resetn,awaddr,av,awr,wdata,wstrb,wv,wr,br,bv,bready,
  araddr,arv,arr,rdata,rr,rv,rready,data,keep,valid,ready,1'b0,
  odata,okeep,oval,oready,olast,overflow,flush,1'b1,fence);
 reg [127:0] expected[0:4095];integer put=0,get=0,part=0,cycle=0;
 reg [63:0] held;reg stalled=0;
 always @(posedge clk) begin
  if(!resetn)begin put=0;get=0;part=0;stalled=0;end
  else begin
   if(stalled && (!oval || odata!==held || okeep!==8'hff || olast))$fatal(1,"held AXIS beat changed");
   stalled=oval && !oready;held=odata;
   if(valid && ready && keep==16'hffff)begin expected[put]=data;put=put+1;end
   if(oval && oready)begin
    if(get>=put)$fatal(1,"unowned output");
    if(odata !== (part ? expected[get][63:0] : expected[get][127:64]))$fatal(1,"record data/tag mismatch");
    if(okeep!==8'hff || olast)$fatal(1,"bad raw diagnostic sideband");
    if(part)begin get=get+1;part=0;end else part=1;
   end
  end
 end
 always @(negedge clk)begin
  cycle=cycle+1;
  if(cycle%251<150 && cycle%251>75)oready=0;
  else oready=($urandom_range(0,9)>2);
 end
 task write_run(input bit run,input bit data_first);
  begin
   @(negedge clk);awaddr=8;wdata=run;wstrb=15;
   if(data_first)begin
    wv=1;do @(posedge clk);while(!wr);@(negedge clk);wv=0;
    repeat(3)@(negedge clk);av=1;do @(posedge clk);while(!awr);@(negedge clk);av=0;
   end else begin
    av=1;do @(posedge clk);while(!awr);@(negedge clk);av=0;
    repeat(3)@(negedge clk);wv=1;do @(posedge clk);while(!wr);@(negedge clk);wv=0;
   end
   while(!bv)@(negedge clk);
   @(negedge clk);
  end
 endtask
 task read_check(input [15:0] address,input [31:0] value);
  begin
   @(negedge clk);araddr=address;arv=1;
   do @(posedge clk);while(!arr);
   @(negedge clk);arv=0;
   while(!rv)@(negedge clk);
   if(rdata!==value)$fatal(1,"CSR %h got %h expected %h",address,rdata,value);
   @(negedge clk);
  end
 endtask
 task scans(input integer count,input integer epoch);
  integer i;
  begin
   for(i=0;i<count;i=i+1)begin
    repeat(i%2 ? 12:13)@(negedge clk);
    data={16'(epoch),48'(i),64'(i*37+123)};valid=1;
    do @(posedge clk);while(!ready);
    @(negedge clk);valid=0;
   end
  end
 endtask
 initial begin
  repeat(5)@(negedge clk);resetn=1;
  read_check(0,32'h44304756);read_check(8,0);
  write_run(1,0);read_check(8,1);
  scans(1000,7);
  while(get<1000)@(negedge clk);
  write_run(0,1);read_check(8,0);
  write_run(1,1);scans(300,8);
  while(get<1300)@(negedge clk);
  // Source epoch/loss events must not rewrite an AXIS beat held by DMA.
  @(negedge clk);overflow=1;fence=1;
  @(negedge clk);overflow=0;fence=0;
  read_check(24,1);read_check(32,1);read_check(16,1300);
  write_run(0,0);
  $display("G0_ADC_RECORD_PASS records=%0d tags_and_data_exact=1 independent_aw_w=1 stalls=1",get);
  $finish;
 end
 initial begin #1000000;$fatal(1,"test timeout");end
endmodule
