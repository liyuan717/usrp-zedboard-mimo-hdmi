#!/usr/bin/env python3
"""Independent Fraction OLS oracle for four-past-peak SFO and predictions.
No ADC or full tracking claim: the arithmetic consumes measured peak positions.
"""
import argparse,hashlib,json,random,shutil,subprocess
from fractions import Fraction
from pathlib import Path
from fec_inverse_cost_probe import routed_measurements
from workspace_storage_guard import check as disk_check,GIB

Q=1<<32;MASK=(1<<80)-1;PERIOD=80000*Q
def nearest(x):return (2*x.numerator+x.denominator)//(2*x.denominator)
def pack(values,widths):
 value=0
 for v,w in zip(values,widths):value=(value<<w)|(int(v)&((1<<w)-1))
 return value

class Oracle:
 def __init__(self):self.history=[];self.generation=None;self.epoch=None
 def update(self,owner,generation,epoch,position,error):
  def failed(code,keep=False):
   self.history=[(0,position)] if keep else [];return [owner,generation,epoch,0,0,0,0,0,0,code]
  if error or (self.history and (generation!=self.generation or epoch!=self.epoch)):
   return failed(1)
  self.generation=generation;self.epoch=epoch
  if not self.history:
   self.history=[(0,position)];step=4*Q;locked=0
  else:
   delta=(position-self.history[-1][1])&MASK
   if not delta or delta>=Fraction(17,2)*PERIOD:return failed(2)
   gap=nearest(Fraction(delta,PERIOD))
   if not 1<=gap<=8:return failed(2)
   self.history.append((self.history[-1][0]+gap,position));self.history=self.history[-4:]
   x=[i-self.history[0][0] for i,p in self.history];y=[(p-self.history[0][1])&MASK for i,p in self.history]
   n=len(x);den=n*sum(v*v for v in x)-sum(x)**2
   num=n*sum(a*b for a,b in zip(x,y))-sum(x)*sum(y)
   exact=Fraction(num,den*20000)
   if abs(exact-4*Q)>Fraction(4*Q,5000):return failed(3,True)
   step=nearest(exact);locked=1
  return [owner,generation,epoch,position,(position+160*step)&MASK,(position+20000*step)&MASK,step,locked,len(self.history),0]

def prepare(out):
 rng=random.Random(0xCA05F0);rows=[];model=Oracle();cases={};owner=0;reset_pending=True
 def add(p,g=9,e=0xffff,err=0,op=0,target=0,iteration=0,name='random'):
  nonlocal owner,reset_pending
  owner=(owner+0x1000001)&0xffffffff;fields=[owner,g,e,p&MASK,err];expected=model.update(*fields)
  rows.append((int(reset_pending),op,target,iteration,pack(fields,[32,32,16,80,3]),pack(expected,[32,32,16,80,80,80,35,1,3,3])))
  reset_pending=False
  cases[name]=cases.get(name,0)+1
  if op:model.history=[]
 def fresh():
  nonlocal reset_pending
  model.history=[];reset_pending=True
 for sign in (-1,0,1):
  for offset in (-1,0,1):
   fresh();p=MASK-123456;add(p,name='exact200ppm')
   add(p+PERIOD+sign*16*Q+offset,name='exact200ppm')
   add(p+2*PERIOD+sign*16*Q+offset,name='after_bound_recovery')
 for d in (-10000,10000):
  fresh();p=MASK-160*Q;add(p,name='Q32half');add(p+PERIOD+d,name='Q32half')
 for trial in range(120):
  fresh();p=rng.randrange(1<<80);sfo=Fraction(rng.randrange(-190000,190001),1000*1000000)
  frame=0;base=p
  for k in range(9):
   if k:frame+=rng.randrange(1,9)
   position=base+nearest(Fraction(PERIOD*frame)*(1+sfo))+rng.randrange(-30000,30001)
   add(position,g=100+trial,e=trial,name='random_gap_rolling')
 for bad in ['duplicate','backward','zero_gap','max_gap_tie','huge_gap','generation','epoch','upstream']:
  fresh();p=MASK-4*Q;add(p,name=bad);add(p+PERIOD,name=bad)
  delta={'duplicate':0,'backward':-1,'zero_gap':PERIOD//2-1,'max_gap_tie':17*PERIOD//2,'huge_gap':1<<70}.get(bad,PERIOD)
  add(p+PERIOD+delta,g=10 if bad=='generation' else 9,e=0 if bad=='epoch' else 0xffff,err=1 if bad=='upstream' else 0,name=bad)
  add(p+2*PERIOD,name='after_invalid');add(p+3*PERIOD,name='after_invalid')
 # Prefix causality and bounded memory: two different discarded first peaks.
 for poison in (-Q//4,Q//4):
  fresh();p=1234*Q;add(p+poison,name='discarded_old_peak')
  for k in range(1,7):add(p+k*PERIOD+17*k*Q//32,name='discarded_old_peak')
 # Revoke first/middle/final iteration in each reachable arithmetic state.
 phases={1:[0],2:[0],3:[0],4:[0,3,7],5:[0],6:[0],7:[0],8:[0,31,63],9:[0],10:[0,7,14],11:[0]}
 for op in (1,2):
  for state,iterations in phases.items():
   for iteration in iterations:
    fresh();p=rng.randrange(1<<80);add(p,name='revoke_seed')
    add(p+PERIOD+12345,op=op,target=state,iteration=iteration,name='active_revoke')
    add(p+2*PERIOD,name='revoke_recovery')
 for op in (3,4):
  fresh();p=rng.randrange(1<<80);add(p,op=op,name='held_revoke');add(p+PERIOD,name='held_recovery')
 with (out/'vectors.txt').open('w') as f:
  for restart,op,state,iteration,source,gold in rows:f.write(f'{restart} {op} {state} {iteration} {source:041x} {gold:091x}\n')
 return rows,cases

TB=r'''`timescale 1ns/1ps
module tb;
reg clk=0;always #10 clk=~clk;
reg rst=1,cancel=0,iv=0,ready=0;reg [162:0] source=0;
wire ir,ov;wire [31:0] iid,igen,oid,ogen;wire [15:0] ie,oe;wire [79:0] ip,pos,train,nextpos;wire [34:0] step;wire [2:0] ierr,used,error;wire locked;
assign {iid,igen,ie,ip,ierr}=source;
causal_sfo_cost_probe dut(clk,rst,cancel,iv,ir,iid,igen,ie,ip,ierr,ov,ready,oid,ogen,oe,pos,train,nextpos,step,locked,used,error);
wire [361:0] observed={oid,ogen,oe,pos,train,nextpos,step,locked,used,error};
integer fd,scan,restart,op,target,iteration,accepted=0,results=0,revoked=0,held_revoked=0,cycle=0,at=0,maxlat=0,lat=0,checks=0;
reg [361:0] gold,held;reg measured=0,published=0;
always @(posedge clk) begin
 cycle=cycle+1;
 if(rst || cancel)begin measured=0;published=0;end
 else begin
  if(iv&&ir)begin accepted=accepted+1;at=cycle;measured=1;published=0;end
  if(ov&&!published&&measured)begin lat=cycle-at;if(lat>maxlat)maxlat=lat;if(lat>160)$fatal(1,"SFO unbounded calculation");published=1;end
 end
end
initial begin
 fd=$fopen("vectors.txt","r");repeat(4)@(negedge clk);rst=0;
 while(!$feof(fd))begin
  scan=$fscanf(fd,"%d %d %d %d %h %h\n",restart,op,target,iteration,source,gold);
  if(scan!=6)$fatal(1,"SFO fixture parse");
  if(restart)begin rst=1;repeat(2)@(negedge clk);rst=0;end
  iv=1;
  @(posedge clk);while(!ir)@(posedge clk);@(negedge clk);iv=0;source=~source;
  if(op==1||op==2)begin
   while(dut.state!=target || ((target==4||target==10) && dut.bit_count!=iteration) || (target==8&&dut.division_bit!=iteration))@(negedge clk);
   if(op==1)cancel=1;else rst=1;#1;if(ov||ir)$fatal(1,"SFO revoke port fence");
   repeat(2)@(negedge clk);cancel=0;rst=0;revoked=revoked+1;
   repeat(4)begin @(negedge clk);if(ov)$fatal(1,"SFO cancelled result revived");end
  end else begin
   while(!ov)@(negedge clk);
   if(observed!==gold)begin
    $display("SFO debug N=%0d D=%0d sumx=%0d frames=%0d/%0d/%0d/%0d scaled=%0d nominal=%0d",dut.numerator,dut.frame_denominator,dut.sum_x,dut.frames[0],dut.frames[1],dut.frames[2],dut.frames[3],dut.scaled_error,dut.nominal_bound);
    $fatal(1,"causal SFO oracle result%0d got%h expected%h",results,observed,gold);
   end
   held=observed;repeat(11)begin @(negedge clk);if(!ov||observed!==held||ir)$fatal(1,"SFO held ownership");checks=checks+1;end
   if(op==3||op==4)begin
    if(op==3)cancel=1;else rst=1;#1;if(ov||ir)$fatal(1,"SFO held revoke fence");
    repeat(2)@(negedge clk);cancel=0;rst=0;held_revoked=held_revoked+1;
   end else begin ready=1;@(negedge clk);ready=0;end
   results=results+1;
  end
 end
 if(accepted!=@ROWS@ || accepted!=results+revoked || revoked!=34 || held_revoked!=2)$fatal(1,"SFO coverage accepted%0d results%0d revoked%0d",accepted,results,revoked);
 $display("G0_CAUSAL_SFO_PASS accepted=%0d results=%0d active_revoked=%0d held_revoked=%0d hold_checks=%0d maximum_response_cycles=%0d",accepted,results,revoked,held_revoked,checks,maxlat);$finish;
end
initial begin #100000000;$fatal(1,"SFO timeout");end
endmodule
'''

def execute(out,bp,source,rows):
 (out/'tb.sv').write_text(TB.replace('@ROWS@',str(rows)))
 def run(step,args):
  with (out/(step+'.log')).open('w') as f:subprocess.run(args,cwd=out,stdout=f,stderr=subprocess.STDOUT,check=True)
 run('compile',[str(bp/'xvlog'),'--sv',str(source),str(out/'tb.sv')]);run('elaborate',[str(bp/'xelab'),'tb','-s','sfo_tb','-mt','off']);run('simulate',[str(bp/'xsim'),'sfo_tb','-runall'])
 log=(out/'simulate.log').read_text();assert 'G0_CAUSAL_SFO_PASS' in log and 'Fatal:' not in log,log[-2500:]
 return next(s for s in log.splitlines() if s.startswith('G0_CAUSAL_SFO_PASS'))

def main(a):
 out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);bp=a.vivado_bin.resolve();project=Path(__file__).resolve().parents[1]
 before=disk_check('before-causal-sfo');(out/'disk-before.json').write_text(json.dumps(before,indent=2)+'\n')
 if before['disk']['available_bytes']<5*GIB:raise SystemExit('Under5GiB: handle storage first.')
 try:
  rows,cases=prepare(out);source=out/'causal_sfo_cost_probe.sv';shutil.copyfile(project/'sim/g0'/source.name,source)
  simulation=execute(out,bp,source,len(rows));receipt={'scope':__doc__,'simulation':simulation,'case_counts':cases,'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),'routed_OOC':None,'negative_controls':[],'g0_passed':False}
  (out/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
  if a.negative_controls:
   mutations={'raw_bound':('if(scaled_error>{14\'d0,nominal_bound})','if(1\'b0)'),
    'rounding':("$unsigned(numerator)+{32'd0,divisor[31:1]}","$unsigned(numerator)"),
    'gap':("frames[used-1'b1]+gap_count","frames[used-1'b1]+5'd1"),
    'old_history':('positions[i]<=positions[i+1]','positions[i]<=positions[i]')}
   for name,(old,new) in mutations.items():
    mutant=out/name;mutant.mkdir();text=source.read_text();assert text.count(old)==1,(name,old)
    src=mutant/source.name;src.write_text(text.replace(old,new));shutil.copyfile(out/'vectors.txt',mutant/'vectors.txt')
    try:execute(mutant,bp,src,len(rows))
    except (subprocess.CalledProcessError,AssertionError):
     log=(mutant/'simulate.log').read_text() if (mutant/'simulate.log').exists() else ''
     assert 'Fatal: causal SFO oracle' in log,(name,log[-2000:])
     receipt['negative_controls'].append({'name':name,'detected_by':'Fatal: causal SFO oracle'})
    else:raise AssertionError('mutation escaped '+name)
  if not a.sim_only:
   tcl='create_project -in_memory -part xc7z020clg484-1\nset_param general.maxThreads 4\nread_verilog -sv {'+str(source)+'}\n'+'''synth_design -top causal_sfo_cost_probe -part xc7z020clg484-1 -mode out_of_context
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
write_checkpoint -force causal_sfo.dcp
'''
   (out/'ooc.tcl').write_text(tcl)
   with (out/'ooc.log').open('w') as f:subprocess.run([str(bp/'vivado'),'-mode','batch','-source',str(out/'ooc.tcl'),'-nolog','-nojournal'],cwd=out,stdout=f,stderr=subprocess.STDOUT,check=True)
   receipt['routed_OOC']=routed_measurements(out)
  (out/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n');print(json.dumps(receipt),flush=True)
 finally:(out/'disk-after.json').write_text(json.dumps(disk_check('after-causal-sfo'),indent=2)+'\n')

if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);p.add_argument('--vivado-bin',type=Path,required=True);p.add_argument('--sim-only',action='store_true');p.add_argument('--negative-controls',action='store_true');main(p.parse_args())
