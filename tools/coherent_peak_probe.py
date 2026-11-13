#!/usr/bin/env python3
"""Independent rational oracle for coherent N/E ranking and Q16 peak refinement."""
import argparse,json,hashlib,shutil,subprocess
from fractions import Fraction
from pathlib import Path
import numpy as np
from workspace_storage_guard import check as disk_check,GIB

def refine(metrics):
 eligible=[k for k,(n,e) in enumerate(metrics) if e]
 if not eligible:return 0,0,4
 best=max(eligible,key=lambda k:Fraction(*metrics[k]))
 n,e=metrics[best]
 if n<=48*e:return 0,0,4
 if best==0 or best==len(metrics)-1:return 0,0,3
 q=[((n<<24)+e//2)//e if e else 0 for n,e in metrics[best-1:best+2]]
 den=q[0]-2*q[1]+q[2]
 fraction=Fraction((q[0]-q[2])<<15,den) if den else Fraction(0)
 rounded=(2*fraction.numerator+fraction.denominator)//(2*fraction.denominator)
 if abs(rounded)>65536:rounded=0
 return best,rounded,0

def stimulus(tag,gen,owner,error,last,point,n,e):
 return e|(n<<52)|(point<<112)|(int(last)<<119)|(error<<120)|(owner<<123)|(gen<<155)|(tag<<187)

def prepare(out):
 rng=np.random.default_rng(0xC02416);cases=[]
 def add(name,metrics,**kwargs):cases.append(dict(name=name,metrics=metrics,**kwargs))
 add('left_half',[(96,1),(192,1),(192,1),(32,1)])
 add('right_half',[(0,1),(192,1),(192,1),(0,1)])
 add('fraction_negative',[(160,1),(200,1),(100,1)])
 add('fraction_positive',[(100,1),(200,1),(160,1)])
 add('cross_product_not_raw_power',[(1000,100),(200,1),(3000,100)])
 add('zero_left_energy',[(0,0),(100,1),(0,1)])
 add('zero_scores',[(0,0),(0,0),(0,0)])
 add('threshold_equal',[(1,1),(48,1),(1,1)])
 add('boundary_first',[(100,1),(99,1),(98,1)])
 add('boundary_last',[(98,1),(99,1),(100,1)])
 add('curvature_quantizes_zero',[(int((1<<51)*100),1<<51),(int((1<<51)*100)+1,1<<51),(int((1<<51)*100),1<<51)])
 q=100<<24
 add('negative_half_Q16_LSB',[(q+2,1<<24),(q+65537,1<<24),(q,1<<24)])
 add('positive_half_Q16_LSB',[(q-2,1<<24),(q+65535,1<<24),(q,1<<24)])
 for j in range(200):
  length=int(rng.integers(3,66));metrics=[]
  for k in range(length):
   e=int(rng.integers(1,1<<51));ratio=int(rng.integers(1,300));n=ratio*e+int(rng.integers(0,e));metrics.append((n,e))
  add('random_'+str(j),metrics)
 for fault in ('point','tag','epoch','id','generation','upstream','first_point','too_many'):
  add(fault,[(100,1),(200,1),(100,1)],fault=fault)
 rows=[];initial=[];reports=[]
 for j,case in enumerate(cases):
  owner=700+j;gen=40+j;base=(1<<48)-1 if j%2 else 1000;epoch=31+j
  metrics=case['metrics'];offset=len(rows);fault=case.get('fault');best,mu,error=refine(metrics)
  if fault:error=1 if fault=='upstream' else 2
  for k,(n,e) in enumerate(metrics):
   tag=(epoch<<48)|((base+k)&((1<<48)-1));ii=owner;gg=gen;p=k;ee=0
   if k==(0 if fault in ('first_point','too_many') else 1):
    if fault=='point':p+=1
    if fault=='tag':tag=(epoch<<48)|((base+k+1)&((1<<48)-1))
    if fault=='epoch':tag^=1<<48
    if fault=='id':ii+=1
    if fault=='generation':gg+=1
    if fault=='upstream':ee=2
    if fault=='first_point':p=1
    if fault=='too_many':p=65
   rows.append(stimulus(tag,gg,ii,ee,k==len(metrics)-1,p,n,e))
   if fault and k==(0 if fault in ('first_point','too_many') else 1):break
  count=len(rows)-offset;tag=(epoch<<48)|((base+best)&((1<<48)-1));position=(((base+best)<<32)+(mu<<16))&((1<<80)-1)
  expected_id=owner+(1 if fault=='id' else 0);expected_gen=gen+(1 if fault=='generation' else 0)
  initial.append(f"offsets[{j}]={offset};lengths[{j}]={count};ids[{j}]={expected_id};generations[{j}]={expected_gen};errors[{j}]={error};tags[{j}]=64'h{tag if not error else 0:016x};positions[{j}]=80'h{position if not error else 0:020x};fractions[{j}]=18'h{(mu if not error else 0)&0x3ffff:05x};best_indices[{j}]={best if not error else 0};")
  reports.append(dict(name=case['name'],expected_error=error,best=best,fraction_q16=mu))
 (out/'stimuli.hex').write_text(''.join(f'{row:063x}\n' for row in rows))
 return len(rows),len(cases),'\n'.join(initial),reports

TB=r'''`timescale 1ns/1ps
module tb;
reg clk=0;always #10 clk=~clk;
reg rst=1,cancel=0,iv=0,ready=0,last=0;reg [31:0] id=0,gen=0;reg [63:0] tag=0;reg [6:0] point=0;reg [2:0] error=0;reg [59:0] n=0;reg [51:0] e=0;
wire ir,ov;wire [31:0] oid,ogen;wire [63:0] ot;wire signed [17:0] fraction;wire [79:0] position;wire [2:0] err;wire [6:0] best;
coherent_peak_cost_probe dut(clk,rst,cancel,iv,ir,id,gen,tag,point,last,error,n,e,ov,ready,oid,ogen,ot,fraction,position,err,best);
reg [250:0] stimulus[0:@LAST@];integer offsets[0:@COUNT@],lengths[0:@COUNT@],ids[0:@COUNT@],generations[0:@COUNT@],errors[0:@COUNT@],best_indices[0:@COUNT@];
reg [63:0] tags[0:@COUNT@];reg [79:0] positions[0:@COUNT@];reg [17:0] fractions[0:@COUNT@];
integer j,k,target,results=0,cancels=0,accepted=0,cycle=0,latency=0,max_latency=0;reg [31:0] last_input_cycle;reg was_valid=0,measure=0;
reg [235:0] held;wire [235:0] view={oid,ogen,ot,fraction,position,err,best};
always @(posedge clk) begin
 cycle=cycle+1;
 if(rst || cancel) begin was_valid=0;measure=0;end
 else begin
  if(iv && ir) begin accepted=accepted+1;if(last && error==0) begin last_input_cycle=cycle;measure=1;end end
  if(ov && !was_valid && err==0 && measure) begin latency=cycle-last_input_cycle;if(latency>max_latency)max_latency=latency;measure=0;end
  was_valid=ov;
 end
end
task feed(input [250:0] row);
 begin
  @(negedge clk);{tag,gen,id,error,last,point,n,e}=row;iv=1;
  @(posedge clk);while(!ir) @(posedge clk);@(negedge clk);iv=0;
 end
endtask
initial begin
 $readmemh("stimuli.hex",stimulus);
 @INIT@
 repeat(4) @(negedge clk);rst=0;
 for(j=0;j<@NCASES@;j=j+1) begin
  for(k=0;k<lengths[j];k=k+1) feed(stimulus[offsets[j]+k]);
  while(!ov) @(negedge clk);
  if(oid!=ids[j] || ogen!=generations[j] || err!=errors[j] || ot!==tags[j] || position!==positions[j] || fraction!==fractions[j] || best!=best_indices[j]) $fatal(1,"coherent peak oracle j%0d err%0d/%0d index%0d/%0d mu%0d/%0d",j,err,errors[j],best,best_indices[j],fraction,$signed(fractions[j]));
  held=view;repeat(11) begin @(negedge clk);if(!ov || view!==held) $fatal(1,"held peak changed");end
  ready=1;@(negedge clk);ready=0;results=results+1;
 end
 // Cancel during every calculation phase; no old status can escape or revive.
 for(target=1;target<=9;target=target+1) begin
  fork
   begin
    feed({64'h0008ffffffffffff,32'd1,32'd1,3'd0,1'b0,7'd0,60'd100,52'd1});
    feed({64'h0008000000000000,32'd1,32'd1,3'd0,1'b0,7'd1,60'd200,52'd1});
    feed({64'h0008000000000001,32'd1,32'd1,3'd0,1'b1,7'd2,60'd160,52'd1});
   end
   begin while(dut.state!=target) @(negedge clk);end
  join_any
  // The producer finishing first is not a reason to stop waiting for late phases.
  if(dut.state!=target) begin while(dut.state!=target) @(negedge clk);end
  disable fork;
  cancel=1;iv=0;#1;if(ov) $fatal(1,"cancel edge peak");@(negedge clk);cancel=0;
  repeat(12) begin @(negedge clk);if(ov || dut.state!=0) $fatal(1,"cancelled peak revived");end
  cancels=cancels+1;
 end
 if(results!=@NCASES@ || cancels!=9) $fatal(1,"peak coverage");
 $display("G0_COHERENT_PEAK_PASS results=%0d phase_cancels=%0d metrics_accepted=%0d maximum_observed_response_cycles=%0d",results,cancels,accepted,max_latency);$finish;
end
initial begin #10000000;$fatal(1,"peak timeout");end
endmodule
'''

def main(a):
 out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);bp=a.vivado_bin.resolve();project=Path(__file__).resolve().parents[1]
 before=disk_check('before-coherent-peak');(out/'disk-before.json').write_text(json.dumps(before,indent=2)+'\n')
 if before['disk']['available_bytes']<5*GIB:raise SystemExit('Under5GiB: handle storage first.')
 try:
  n,count,init,reports=prepare(out);src=out/'coherent_peak_cost_probe.sv';shutil.copyfile(project/'sim/g0'/src.name,src)
  (out/'tb.sv').write_text(TB.replace('@LAST@',str(n-1)).replace('@COUNT@',str(count-1)).replace('@NCASES@',str(count)).replace('@INIT@',init))
  for step,args in [('compile',[str(bp/'xvlog'),'--sv',str(src),str(out/'tb.sv')]),('elaborate',[str(bp/'xelab'),'tb','-s','peak_tb','-mt','off']),('simulate',[str(bp/'xsim'),'peak_tb','-runall'])]:
   with (out/(step+'.log')).open('w') as log:subprocess.run(args,cwd=out,stdout=log,stderr=subprocess.STDOUT,check=True)
  log=(out/'simulate.log').read_text();assert 'G0_COHERENT_PEAK_PASS' in log and 'Fatal:' not in log,log[-2500:]
  r={'scope':__doc__,'simulation':next(s for s in log.splitlines() if s.startswith('G0_COHERENT_PEAK_PASS')),'cases':reports,'source_sha256':hashlib.sha256(src.read_bytes()).hexdigest(),'g0_passed':False};(out/'receipt.json').write_text(json.dumps(r,indent=2)+'\n');print(json.dumps({k:v for k,v in r.items() if k!='cases'}))
 finally:(out/'disk-after.json').write_text(json.dumps(disk_check('after-coherent-peak'),indent=2)+'\n')

if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);p.add_argument('--vivado-bin',type=Path,required=True);main(p.parse_args())
