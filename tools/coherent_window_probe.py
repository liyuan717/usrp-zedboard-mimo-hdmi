#!/usr/bin/env python3
"""Exact192-symbol correlation and full768-sample energy on bounded RRC history.
Independent direct dot products/sums, priority sampler reads, fences/expiry and
continuous writes. Configured search window only; no peak/SFO estimator claim.
"""
import argparse,hashlib,json,shutil,subprocess
from pathlib import Path
import numpy as np
from g0_packet_probe import public_prefix
from fec_inverse_cost_probe import routed_measurements
from workspace_storage_guard import check as disk_check,GIB

def pack(row,bits):return sum((int(v)&((1<<bits)-1))<<(bits*j) for j,v in enumerate(row))

def metric(values,signs,start):
 # Direct192 terms and fresh768-term sum: no RTL rolling-energy recurrence.
 x=values[start+4*np.arange(192)].astype(np.int64);si=signs[:,0];sq=signs[:,1]
 c=[]
 for rx in (0,1):
  i=x[:,rx*2];q=x[:,rx*2+1];c.extend((int(np.sum(i*si+q*sq)),int(np.sum(q*si-i*sq))))
 n=sum(v*v for v in c);e=sum(int(v)*int(v) for row in values[start:start+768] for v in row)
 assert all(-(1<<28)<=v<1<<28 for v in c) and n<1<<60 and e<1<<52
 return pack(c,29)|(n<<116)|(e<<176)

def prepare(out):
 pre=public_prefix()[256:448,0];signs=np.column_stack((np.where(pre.real>0,1,-1),np.where(pre.imag>0,1,-1)))
 (out/'coherent_signs.hex').write_text(''.join(f'{int(si<0)+2*int(sq<0):x}\n' for si,sq in signs))
 rng=np.random.default_rng(0x192768);values=rng.integers(-(1<<19),1<<19,(12500,4),dtype=np.int64)
 values[:1300]//=32
 # Exact signed20 extrema and both independent complex channel directions.
 for m,(si,sq) in enumerate(signs):
  values[232+4*m]=[524287 if si>0 else -524288,524287 if sq>0 else -524288,
                   -524288 if sq>0 else 524287,524287 if si>0 else -524288]
 (out/'words.hex').write_text(''.join(f'{pack(row,20):020x}\n' for row in values))
 starts=list(range(200,265))+[4000,6000]
 (out/'expected.hex').write_text(''.join(f'{metric(values,signs,s):057x}\n' for s in starts))
 return {'normal_metric_windows':67,'source_vectors':12500,'signed20_extrema':True,'direct_oracle':'192 public sign complex products, four fresh768-component energy sums','oracle_start_indices':starts}

TB=r'''`timescale 1ns/1ps
module tb;
reg clk=0;always #10 clk=~clk;
reg rst=1,cancel=0,iv=0,start=0,cv=0,ready=1,ext=0;
reg [63:0] tag=0,first=0;reg [79:0] data=0;reg [31:0] id=0,gen=0;reg [6:0] points=0;reg [11:0] addr=0;
wire ir,cr,ov,last,restart,busy;wire [79:0] word;wire [31:0] oid,ogen,hgen;wire [63:0] ot,latest;wire [12:0] hc;
wire [6:0] point;wire [2:0] error;wire [115:0] c;wire [59:0] n;wire [51:0] e;
coherent_window_cost_probe #(.WAIT_LIMIT(40000)) dut(clk,rst,cancel,iv,ir,start,tag,data,ext,addr,word,cv,cr,id,gen,first,points,ov,ready,oid,ogen,ot,point,last,error,c,n,e,hgen,latest,hc,restart,busy);
reg [79:0] words[0:12499];reg [227:0] expected[0:66];
localparam [47:0] BASE=48'hfffffffffe70;
integer k=0,cycle=0,metrics=0,errors=0,external_reads=0,admitted=0,reference_index,lookup,max_age=0,age;
reg source_done=0,held=0,was_ext=0;reg [424:0] held_view;reg [79:0] ext_expected;
wire [424:0] view={oid,ogen,ot,point,last,error,c,n,e};
always @(negedge clk) begin
 ext=!rst && !cancel && hc>=4 && cycle%11==0;
 addr=latest[11:0]-12'd3;
end
always @(posedge clk) begin
 cycle=cycle+1;
 if(rst || cancel) held=0;
 else begin
  if(!ir) $fatal(1,"continuous writer backpressured");
  if(held && (!ov || view!==held_view)) $fatal(1,"held metric changed");
  held=ov&&!ready;if(held) held_view=view;
  if(iv && ir) admitted=admitted+1;
  if(dut.own_read) begin age=48'(latest[47:0]-dut.own_read_tag[47:0]);if(age>max_age)max_age=age;if(age>=4096) $fatal(1,"expired read issued");end
  if(ov && ready) begin
   if(error) begin if(!last || c || n || e) $fatal(1,"error leaked numeric data");errors=errors+1;end
   else begin
    if(oid==100) begin reference_index=point;if(point>64 || last!==(point==64) || ot!=={16'h40,BASE+48'd200+48'(point)}) $fatal(1,"metric point identity");end
    else if(oid==101) begin reference_index=65;if(point || !last) $fatal(1,"single metric identity");end
    else if(oid==102) begin reference_index=66;if(point || last) $fatal(1,"held batch identity");end
    else $fatal(1,"unexpected successful descriptor");
    if({e,n,c}!==expected[reference_index]) $fatal(1,"coherent metric mismatch id%0d point%0d",oid,point);
    metrics=metrics+1;
   end
  end
 end
 was_ext=ext && !rst && !cancel;
 if(was_ext) begin
  lookup=48'(latest[47:0]-BASE-48'd3);
  if(lookup<0 || lookup>=12500) $fatal(1,"external oracle index");ext_expected=words[lookup];
 end
 #1;if(was_ext) begin if(word!==ext_expected) $fatal(1,"priority sampler read");external_reads=external_reads+1;end
end
task configure(input [31:0] owner,input integer offset,input [6:0] length,input [31:0] generation);
 begin
  @(negedge clk);id=owner;first={owner==108 ? 16'h41 : 16'h40,BASE+48'(offset)};points=length;gen=generation;cv=1;
  @(posedge clk);while(!cr) @(posedge clk);@(negedge clk);cv=0;id=~id;gen=~gen;first=0;points=0;
 end
endtask
task expect_error(input [31:0] owner,input [2:0] code);
 begin
  while(!ov) @(negedge clk);
  if(oid!=owner || error!=code || !last || n || e || c) $fatal(1,"wrong terminal error %0d/%0d",error,code);
  ready=1;@(posedge clk);@(negedge clk);ready=0;
 end
endtask
initial begin
 $readmemh("words.hex",words);$readmemh("expected.hex",expected);
 repeat(5) @(negedge clk);rst=0;
 fork
  begin
   for(k=0;k<12500;k=k+1) begin
    tag={16'h40,BASE+48'(k)};data=words[k];start=k==0 || k==7000;iv=1;
    @(negedge clk);iv=0;start=0;repeat(7) @(negedge clk);
   end
   source_done=1;
  end
  begin
   while(hc==0) @(negedge clk);configure(100,200,65,hgen);
   while(metrics<65) @(negedge clk);ready=0;
   configure(101,4000,1,hgen);while(!ov) @(negedge clk);
   repeat(83) @(negedge clk);ready=1;@(posedge clk);@(negedge clk);ready=0;
   configure(102,6000,3,hgen);while(!ov) @(negedge clk);
   // A source restart cannot mutate this already published first metric.
   while(hgen==ogen) @(negedge clk);
   repeat(17) @(negedge clk);ready=1;@(posedge clk);@(negedge clk);ready=0;
   expect_error(102,1);
   configure(103,8000,66,hgen);expect_error(103,4);
   configure(107,8000,3,hgen+32'd1);expect_error(107,4);
   configure(108,8000,3,hgen);expect_error(108,4);
   configure(104,0,1,hgen);expect_error(104,2);
   configure(105,100000,1,hgen);expect_error(105,3);
   configure(106,10000,3,hgen);
   while(dut.state!=4 || dut.read_index<20) @(negedge clk);
   cancel=1;#1;if(ov) $fatal(1,"cancel publication edge");@(negedge clk);cancel=0;
   repeat(100) begin @(negedge clk);if(ov || busy) $fatal(1,"cancelled batch survived");end
  end
 join
 repeat(10) @(negedge clk);
 if(metrics!=67 || errors!=6 || external_reads<1000 || max_age>=4096 || ov) $fatal(1,"coherent coverage %0d/%0d/%0d/%0d",metrics,errors,external_reads,max_age);
 $display("G0_COHERENT_WINDOW_PASS metrics=%0d errors=%0d priority_reads=%0d admitted=%0d maximum_read_age=%0d",metrics,errors,external_reads,admitted,max_age);$finish;
end
initial begin #5000000;$fatal(1,"coherent timeout");end
endmodule
'''

def main(a):
 out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);bp=a.vivado_bin.resolve();project=Path(__file__).resolve().parents[1]
 before=disk_check('before-coherent-window');(out/'disk-before.json').write_text(json.dumps(before,indent=2)+'\n')
 if before['disk']['available_bytes']<5*GIB:raise SystemExit('Under5GiB: handle storage first.')
 try:
  fixture=prepare(out);src=out/'coherent_window_cost_probe.sv';shutil.copyfile(project/'sim/g0'/src.name,src);(out/'tb.sv').write_text(TB)
  def run(name,args):
   with (out/(name+'.log')).open('w') as log:subprocess.run(args,cwd=out,stdout=log,stderr=subprocess.STDOUT,check=True)
  xpm=bp.parent/'data/ip/xpm'
  run('compile',[str(bp/'xvlog'),'--sv',str(xpm/'xpm_cdc/hdl/xpm_cdc.sv'),str(xpm/'xpm_memory/hdl/xpm_memory.sv'),str(src),str(out/'tb.sv')]);run('elaborate',[str(bp/'xelab'),'tb','-s','coherent_tb','-mt','off']);run('simulate',[str(bp/'xsim'),'coherent_tb','-runall'])
  log=(out/'simulate.log').read_text();assert 'G0_COHERENT_WINDOW_PASS' in log and 'Fatal:' not in log,log[-2500:]
  receipt={'scope':__doc__,'simulation':next(s for s in log.splitlines() if s.startswith('G0_COHERENT_WINDOW_PASS')),'fixture':fixture,'source_sha256':hashlib.sha256(src.read_bytes()).hexdigest(),'g0_passed':False,'routed_OOC':None}
  (out/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
  if not a.sim_only:
   tcl='create_project -in_memory -part xc7z020clg484-1\nset_property XPM_LIBRARIES {XPM_MEMORY XPM_CDC} [current_project]\nset_param general.maxThreads 4\nread_verilog -sv {'+str(src)+'}\n'+'''synth_design -top coherent_window_cost_probe -part xc7z020clg484-1 -mode out_of_context
create_clock -name processing -period 20 [get_ports clk]
set_input_delay 2 -clock processing [get_ports -filter {DIRECTION == IN && NAME != clk}]
set_output_delay 2 -clock processing [get_ports -filter {DIRECTION == OUT}]
opt_design
place_design
route_design
report_utilization -file utilization.rpt
report_utilization -hierarchical -file hierarchy.rpt
report_timing_summary -file timing.rpt -report_unconstrained
report_drc -file drc.rpt
write_checkpoint -force coherent.dcp
'''
   (out/'ooc.tcl').write_text(tcl);run('ooc',[str(bp/'vivado'),'-mode','batch','-source',str(out/'ooc.tcl'),'-nolog','-nojournal']);receipt['routed_OOC']=routed_measurements(out);(out/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
  print(json.dumps(receipt))
 finally:(out/'disk-after.json').write_text(json.dumps(disk_check('after-coherent-window'),indent=2)+'\n')

if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);p.add_argument('--vivado-bin',type=Path,required=True);p.add_argument('--sim-only',action='store_true');main(p.parse_args())
