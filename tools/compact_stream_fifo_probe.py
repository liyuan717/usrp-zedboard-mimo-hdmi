#!/usr/bin/env python3
"""128-bit compact FIFO: independent queue, throughput, reservations and collisions."""
import argparse,json,shutil,subprocess,hashlib
from pathlib import Path
TB=r'''`timescale 1ns/1ps
module tb;
reg clk=0;always #10 clk=~clk;
reg resetn=0,flush=0,iv=0,ready=1;reg [127:0] data=0;
wire ack,overflow,accepted,drop,ov;wire [127:0] result;wire [15:0] keep,strb;wire last;
compact_stream_fifo_cost_probe dut(clk,resetn,flush,ack,iv,data,overflow,accepted,drop,result,keep,strb,ov,ready,last);
reg [127:0] queue[0:65535];integer head=0,tail=0,accepted_n=0,popped=0,revoked=0,drops=0,collisions_checked=0,cycle=0;
integer seed=1282026,dummy,k,mode=0,steady=0,full_edges=0,peak=0;reg held=0;reg [127:0] held_data;
always @(posedge clk) begin
 cycle=cycle+1;
 if(!resetn || flush) begin
  revoked=revoked+tail-head;head=0;tail=0;held=0;
  if(ov || accepted) $fatal(1,"barrier admission");
 end else if(dut.local_resetn && !dut.flush_cmd) begin
  if(held && (!ov || result!==held_data)) $fatal(1,"output hold");
  held=ov&&!ready;held_data=result;
  if(keep!==16'hffff || strb!==16'hffff || last) $fatal(1,"sideband");
  if(ov && ready) begin
   if(head>=tail || result!==queue[head]) $fatal(1,"compact data mismatch cycle%0d",cycle);
   head=head+1;popped=popped+1;
  end
  if(accepted!==(iv && tail-head<128)) $fatal(1,"independent credit");
  if(accepted) begin queue[tail]=data;tail=tail+1;accepted_n=accepted_n+1;end
  if(drop) drops=drops+1;
  if(tail-head>peak) peak=tail-head;
  if(mode==1 && k>8 && k<1000) begin
   if(!ov || !accepted) $fatal(1,"one-word-per-clock throughput");steady=steady+1;
  end
  if(tail-head==128 && accepted && ov&&ready) full_edges=full_edges+1;
  if(dut.mem_wren && dut.mem_rden) begin
   if(dut.write_ptr==dut.read_address) $fatal(1,"BRAM same-address collision");collisions_checked=collisions_checked+1;
  end
 end else begin revoked=revoked+tail-head;head=0;tail=0;held=0;end
 #1;
 if(resetn && dut.local_resetn && !flush && !dut.flush_cmd) begin
  if(dut.total_count!==tail-head) $fatal(1,"total credit");
  if(dut.total_count!=dut.ram_count+dut.wr_valid+dut.read_command+dut.response_valid+dut.head_valid) $fatal(1,"token conservation");
  if(dut.head_valid+dut.response_valid+dut.read_command>2) $fatal(1,"read reservation");
 end
end
task random_data;
 begin data={$urandom,$urandom,$urandom,$urandom};end
endtask
task drain;
 begin mode=0;iv=0;ready=1;repeat(150) @(negedge clk);if(tail!=head || ov || dut.total_count) $fatal(1,"drain");end
endtask
task barrier;
 begin
  flush=1;iv=1;random_data;
  while(!ack) @(negedge clk);
  repeat(3) begin random_data;@(negedge clk);if(accepted) $fatal(1,"held flush input");end
  iv=0;flush=0;while(ack) @(negedge clk);repeat(3) @(negedge clk);
 end
endtask
initial begin
 dummy=$urandom(seed);repeat(5) @(negedge clk);resetn=1;repeat(4) @(negedge clk);barrier;
 mode=1;ready=1;
 for(k=0;k<1024;k=k+1) begin random_data;iv=1;@(negedge clk);end drain;
 // Precisely fill, then pop and push at full capacity.
 ready=0;
 for(k=0;k<128;k=k+1) begin random_data;iv=1;@(negedge clk);end
 ready=1;random_data;@(negedge clk);iv=0;drain;
 // Arbitrary output stalls, writes while full and immediate recovery.
 for(k=0;k<12000;k=k+1) begin
  random_data;iv=$urandom_range(0,3)!=0;ready=$urandom_range(0,3)==0;
  @(negedge clk);
  if(k%977==976) barrier;
 end drain;
 // Withdraw an in-flight registered read and a held output.
 iv=1;ready=0;random_data;@(negedge clk);iv=0;
 while(!dut.read_command) @(negedge clk);resetn=0;repeat(5) @(negedge clk);resetn=1;repeat(4) @(negedge clk);barrier;
 for(k=0;k<16;k=k+1) begin random_data;iv=1;@(negedge clk);end
 barrier;drain;
 for(k=0;k<512;k=k+1) begin random_data;iv=1;@(negedge clk);end drain;
 if(accepted_n!=popped+revoked || steady<900 || full_edges==0 || peak!=128 || drops<100 || collisions_checked<100) $fatal(1,"coverage");
 $display("G0_COMPACT_FIFO_PASS admitted=%0d consumed=%0d revoked=%0d dropped=%0d saturated_outputs=%0d full_pop_push=%0d distinct_address_readwrites=%0d",accepted_n,popped,revoked,drops,steady,full_edges,collisions_checked);$finish;
end
initial begin #5000000;$fatal(1,"timeout");end
endmodule
'''
def main(a):
 out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);project=Path(__file__).resolve().parents[1];src=out/'compact_stream_fifo_cost_probe.sv';shutil.copyfile(project/'sim/g0'/src.name,src);(out/'tb.sv').write_text(TB);bp=a.vivado_bin.resolve();xpm=bp.parent/'data/ip/xpm'
 def run(name,args):
  with (out/(name+'.log')).open('w') as log:subprocess.run(args,cwd=out,stdout=log,stderr=subprocess.STDOUT,check=True)
 run('compile',[str(bp/'xvlog'),'--sv',str(xpm/'xpm_cdc/hdl/xpm_cdc.sv'),str(xpm/'xpm_memory/hdl/xpm_memory.sv'),str(src),str(out/'tb.sv')]);run('elaborate',[str(bp/'xelab'),'tb','-s','compact_tb','-mt','off']);run('simulate',[str(bp/'xsim'),'compact_tb','-runall']);log=(out/'simulate.log').read_text();assert 'G0_COMPACT_FIFO_PASS' in log and 'Fatal:' not in log,log[-2000:]
 for name,old,new,expected in [('spill','if(spill) head_data<=ram_q','if(spill) head_data<=ram_q^128\'d1','Fatal: output hold'),('credit','total_count<DEPTH || pop','total_count<DEPTH','Fatal: independent credit')]:
  assert old in src.read_text()
  m=out/(name+'.sv');m.write_text(src.read_text().replace('module compact_stream_fifo_cost_probe','module '+name+'_fifo').replace(old,new));tb=out/(name+'_tb.sv');tb.write_text(TB.replace('module tb;','module '+name+'_tb;').replace('compact_stream_fifo_cost_probe dut',name+'_fifo dut'))
  run(name+'-compile',[str(bp/'xvlog'),'--sv',str(m),str(tb)]);run(name+'-elaborate',[str(bp/'xelab'),name+'_tb','-s',name+'_tb','-mt','off']);run(name+'-simulate',[str(bp/'xsim'),name+'_tb','-runall']);l=(out/(name+'-simulate.log')).read_text();assert expected in l and 'G0_COMPACT_FIFO_PASS' not in l
 r={'scope':__doc__,'simulation':log[log.index('G0_COMPACT_FIFO_PASS'):].splitlines()[0],'source_sha256':hashlib.sha256(src.read_bytes()).hexdigest(),'negative_controls':['spill bit corrupted','full pop/push credit removed'],'g0_passed':False};(out/'receipt.json').write_text(json.dumps(r,indent=2)+'\n');print(json.dumps(r))
if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);p.add_argument('--vivado-bin',type=Path,required=True);main(p.parse_args())
