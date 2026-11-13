#!/usr/bin/env python3
"""Continuous no-ready CPACK input, compact common-clock XPM FIFO, fail-stop guard."""
import argparse,json,hashlib,shutil,subprocess
from pathlib import Path
from fec_inverse_cost_probe import routed_measurements
TB=r'''`timescale 1ns/1ps
module tb;
reg clk=0;always #10 clk=~clk;
reg rst=1,rearm=0,raw_valid=0,ready=1,loss=0;reg [63:0] tag=0,data=0;
wire ov,start,cancel,running,admitted,dropped;wire [63:0] ot,od;wire [31:0] generation,faults;wire [2:0] reason;wire [7:0] occupancy,peak;
continuous_adc_ingress_cost_probe dut(clk,rst,rearm,raw_valid,tag,data,loss,ov,ready,ot,od,start,cancel,running,generation,faults,reason,occupancy,peak,admitted,dropped);
reg [127:0] queue[0:19999];integer head=0,tail=0,accepted=0,received=0,revoked_words=0,fault_cycles=0,starts=0,cycle=0,full_pop_push=0,max_peak=0;
reg hold=0;reg [127:0] held;reg held_start;
always @(posedge clk) begin
 cycle=cycle+1;if(peak>max_peak) max_peak=peak;
 if(rst) begin revoked_words=revoked_words+tail-head;head=0;tail=0;hold=0;end
 else if(cancel) begin
  revoked_words=revoked_words+tail-head;head=0;tail=0;hold=0;fault_cycles=fault_cycles+1;
  if(ov) $fatal(1,"fault output");
 end else begin
  if(hold && (!ov || {ot,od}!==held || start!==held_start)) $fatal(1,"held output");
  hold=ov&&!ready;held={ot,od};held_start=start;
  if(admitted) begin queue[tail]={tag,data};tail=tail+1;accepted=accepted+1;end
  if(ov && ready) begin
   if(head>=tail || {ot,od}!==queue[head]) $fatal(1,"FIFO data mismatch");head=head+1;received=received+1;
   if(start) starts=starts+1;
  end
  if(occupancy==128 && raw_valid && ov&&ready&&admitted) full_pop_push=full_pop_push+1;
  if(dut.elastic.total_count!==occupancy || occupancy>128) $fatal(1,"credit accounting %0d/%0d",occupancy,dut.elastic.total_count);
 end
end
task arm;
 begin
  raw_valid=0;rearm=1;@(negedge clk);rearm=0;
  while(!running) @(negedge clk);repeat(2) @(negedge clk);
  if(occupancy || dut.elastic.total_count) $fatal(1,"rearm not empty");
 end
endtask
task emit(input integer index,input integer gap);
 begin
  tag={16'd7,48'(48'hffffffffff00+index+gap)};data=64'(index*1234567)^64'hfedcba9876543210;raw_valid=1;
  @(negedge clk);raw_valid=0;#1;
 end
endtask
task drain;
 begin ready=1;repeat(170) @(negedge clk);if(head!=tail || occupancy) $fatal(1,"drain");end
endtask
integer k,previous_faults;
initial begin
 repeat(5) @(negedge clk);rst=0;repeat(8) @(negedge clk);arm;
 // Continuous one-word-per-cycle burst with downstream bubbles, no input READY.
 for(k=0;k<3000;k=k+1) begin ready=k%17>=3;emit(k,0);if(k%4==0) repeat(2) @(negedge clk);end
 drain;
 // Filling exactly the usable capacity then simultaneous full pop/push.
 ready=0;for(k=3000;k<3128;k=k+1) emit(k,0);
 if(occupancy!=128 || cancel) $fatal(1,"exact capacity");
 ready=1;emit(3128,0);if(cancel) $fatal(1,"full pop push falsely dropped");drain;
 // Overflow rejects one new word and atomically invalidates all queued words.
 ready=0;for(k=3129;k<3257;k=k+1) emit(k,0);previous_faults=faults;emit(3257,0);@(negedge clk);#1;
 if(running || faults!=previous_faults+1 || reason!=2) $fatal(1,"overflow not fenced");
 for(k=3258;k<3290;k=k+1) emit(k,0);
 if(ov || admitted) $fatal(1,"halted admitted");arm;
 for(k=4000;k<4050;k=k+1) emit(k,0);drain;
 previous_faults=faults;emit(4050,1);
 if(faults!=previous_faults+1 || reason!=4 || ov) $fatal(1,"gap not fenced");arm;
 emit(5000,0);loss=1;previous_faults=faults;emit(5001,0);
 if(faults!=previous_faults+1 || reason!=1) $fatal(1,"upstream loss");
 // Held upstream loss prevents rearm from exposing any input.
 rearm=1;repeat(12) @(negedge clk);rearm=0;if(running || ov || admitted) $fatal(1,"loss rearm");loss=0;
 while(!running) @(negedge clk);
 for(k=6000;k<6080;k=k+1) emit(k,0);drain;
 // Reset held output and pipeline; stale RAM data must not escape.
 ready=0;for(k=6080;k<6090;k=k+1) emit(k,0);rst=1;repeat(5) @(negedge clk);rst=0;repeat(6) @(negedge clk);arm;
 for(k=7000;k<7050;k=k+1) emit(k,0);drain;
 if(accepted!=received+revoked_words) $fatal(1,"word conservation");
 if(received<3000 || starts!=4 || full_pop_push!=1 || max_peak<128) $fatal(1,"coverage %0d/%0d/%0d",received,starts,full_pop_push);
 $display("G0_CONTINUOUS_INGRESS_PASS accepted=%0d received=%0d revoked_words=%0d first_words=%0d full_pop_push=%0d fault_cycles=%0d",accepted,received,revoked_words,starts,full_pop_push,fault_cycles);$finish;
end
initial begin #2000000;$fatal(1,"timeout");end
endmodule
'''
def main(a):
 out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);project=Path(__file__).resolve().parents[1];bp=a.vivado_bin.resolve();sources=[]
 for f in ['continuous_adc_ingress_cost_probe.sv','compact_stream_fifo_cost_probe.sv','rf_rx_elastic_fifo.v']:
  p=out/f;shutil.copyfile(project/'sim/g0'/f,p);sources.append(p)
 (out/'tb.sv').write_text(TB);xpm=bp.parent/'data/ip/xpm'
 def run(n,args):
  with (out/(n+'.log')).open('w') as f:subprocess.run(args,cwd=out,stdout=f,stderr=subprocess.STDOUT,check=True)
 run('compile',[str(bp/'xvlog'),'--sv',str(xpm/'xpm_cdc/hdl/xpm_cdc.sv'),str(xpm/'xpm_memory/hdl/xpm_memory.sv'),*map(str,sources),str(out/'tb.sv')]);run('elaborate',[str(bp/'xelab'),'tb','-s','ingress_tb','-mt','off']);run('simulate',[str(bp/'xsim'),'ingress_tb','-runall'])
 log=(out/'simulate.log').read_text();assert 'G0_CONTINUOUS_INGRESS_PASS' in log and 'Fatal:' not in log,log[-2000:]
 for name,old,new in [('fault','assign receiver_cancel=!running || fault','assign receiver_cancel=1\'b0'),('data','out_data=fifo_data[63:0]','out_data=fifo_data[63:0]^64\'d1')]:
  src=out/(name+'.sv');src.write_text(sources[0].read_text().replace('module continuous_adc_ingress_cost_probe','module '+name+'_guard').replace(old,new));tb=out/(name+'_tb.sv');tb.write_text(TB.replace('module tb;','module '+name+'_tb;').replace('continuous_adc_ingress_cost_probe dut',name+'_guard dut'))
  run(name+'-compile',[str(bp/'xvlog'),'--sv',str(src),str(tb)]);run(name+'-elaborate',[str(bp/'xelab'),name+'_tb','-s',name+'_tb','-mt','off']);run(name+'-simulate',[str(bp/'xsim'),name+'_tb','-runall']);l=(out/(name+'-simulate.log')).read_text();assert 'Fatal:' in l and 'G0_CONTINUOUS_INGRESS_PASS' not in l
 receipt={'scope':__doc__,'simulation':log[log.index('G0_CONTINUOUS_INGRESS_PASS'):].splitlines()[0],'source_sha256':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sources},'routed_OOC':None,'g0_passed':False}
 (out/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
 print(json.dumps(receipt))
if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);p.add_argument('--vivado-bin',type=Path,required=True);main(p.parse_args())
