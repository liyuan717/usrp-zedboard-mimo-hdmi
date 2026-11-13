#!/usr/bin/env python3
"""Bounded two-TX seven-point phase smoothing against independent Fraction OLS.
Capture inputs are ACTUAL pilot-phase RTL outputs from the passed math probe.
Tests numerical/ownership/expiry service only, not full ADC/pilot/detector PHY.
"""
import argparse,hashlib,json,random,shutil,subprocess
from fractions import Fraction
from pathlib import Path
import numpy as np
from scipy.signal import savgol_filter
from fec_inverse_cost_probe import routed_measurements
from workspace_storage_guard import check as disk_check,GIB
Q=1<<32;LIMIT=100000

def signed(v,b):return (int(v)+(1<<(b-1)))%(1<<b)-(1<<(b-1))
def nearest(v):return (2*v.numerator+v.denominator)//(2*v.denominator)
def smooth(raw):
 unwrapped=[]
 for tx in (0,1):
  prev=None;y=[]
  for row in raw:
   p=signed(row[tx],32)
   if prev is None:y.append(p)
   else:
    d=(p-prev+(1<<31))%Q-(1<<31)
    if d==-(1<<31) and p-prev>0:d=1<<31
    if abs(d)>= (1<<31)-122:raise ValueError('ambiguous')
    y.append(y[-1]+d)
   prev=p
  unwrapped.append(y)
 out=[];max_float_error=0
 for tx,y in enumerate(unwrapped):
  fp=savgol_filter(np.array(y,dtype=float),7,1)
  for i in range(274):
   start=max(0,min(i-3,267));window=y[start:start+7]
   fit=Fraction(sum(window),7)+Fraction(sum((j-3)*v for j,v in enumerate(window)),28)*(i-start-3)
   value=nearest(fit);assert -(1<<40)<=value<1<<40
   max_float_error=max(max_float_error,abs(value-fp[i]));out.append((start+6,tx,i,value))
 # A TX completes its fit before the next TX observation is accepted.
 out.sort(key=lambda r:(r[0],r[1],r[2]))
 assert max_float_error<=.501,max_float_error
 return out,max_float_error

def input_word(row):
 id,ctx,gen,ep,i,tx,p,error=row
 return (p&0xffffffff)+(tx<<32)+(i<<33)+(id<<42)+(ctx<<74)+(gen<<106)+(ep<<138)+(error<<154)
def output_word(row,id,ctx,gen,ep):
 known,tx,i,phi=row
 return (phi%(1<<41))+(i<<41)+(known<<50)+(tx<<59)+(id<<60)+(ctx<<92)+(gen<<124)+(ep<<156)

TB=r'''`timescale 1ns/1ps
module tb;
reg clk=0;always #10 clk=~clk;
reg rst=1,cancel=0,cv=0,iv=0,ready=0;reg [31:0] ci=0,cc=0,cg=0,ii=0,ic=0,ig=0,phase=0;reg [15:0] ce=0,ie=0;reg [8:0] point=0;reg tx=0,error=0;
wire cr,ir,valid,otx,abort,active;wire [31:0] oi,oc,og;wire [15:0] oe;wire [8:0] op,known,ap;wire [1:0] reason;wire signed [40:0] phi;
pilot_smoothing_cost_probe #(.FRAME_LIMIT(@LIMIT@)) dut(clk,rst,cancel,cv,cr,ci,cc,cg,ce,iv,ir,ii,ic,ig,ie,point,tx,error,phase,valid,ready,oi,oc,og,oe,op,known,otx,phi,abort,reason,ap,active);
reg [154:0] inputs[0:@ILAST@];reg [171:0] gold[0:@OLAST@];wire [171:0] view={oe,og,oc,oi,otx,known,op,phi};
integer cycle=0,j=0,goffset=0,published=0,consumed=0,revoked=0,aborts=0,total_published=0,total_consumed=0,total_aborts=0,total_revoked=0,total_cancelled=0,held_checks=0,source_stalls=0,ready_policy=0,ready_wait=0,deadline_successes=0,last0=-1,last1=-1;
reg seen=0,held=0,aborted=0;reg [1:0] aborted_reason;reg [171:0] held_view;
always @(negedge clk)begin
 if(rst||cancel||abort||!valid)begin ready=0;ready_wait=0;end
 else if(ready_policy==1)ready=0;
 else if(ready_policy==2&&otx&&op==273)ready=dut.age==@LIMIT@-1;
 else if(ready_wait==3+j%5)ready=1;
 else begin ready=0;ready_wait=ready_wait+1;end
end
always @(posedge clk)begin
 cycle=cycle+1;
 if(rst||cancel||abort)begin
  if(seen)begin revoked=revoked+1;total_revoked=total_revoked+1;end seen=0;held=0;
  if(abort)begin aborted=1;aborted_reason=reason;aborts=aborts+1;total_aborts=total_aborts+1;end
 end else begin
  if(iv&&!ir)source_stalls=source_stalls+1;
  if(iv&&ir)begin if(tx)last1=point;else last0=point;end
  if(valid&&!seen)begin
   if(view!==gold[goffset+published])$fatal(1,"pilot smoothing Fraction oracle case%0d pub%0d actual%h expected%h",j,published,view,gold[goffset+published]);
   if(known!=(otx?last1:last0))$fatal(1,"pilot smoothing future observation used");
   seen=1;published=published+1;total_published=total_published+1;
  end
  if(held&&(!valid||view!==held_view))$fatal(1,"pilot smoothing held point changed");
  if(valid&&ready)begin
   if(dut.work_last&&dut.work_tx&&dut.fit_offset==dut.fit_end&&dut.age==@LIMIT@-1)deadline_successes=deadline_successes+1;
   consumed=consumed+1;total_consumed=total_consumed+1;seen=0;
  end
  held=valid&&!ready;if(held)begin held_view=view;held_checks=held_checks+1;end
 end
end
task put(input integer k);
 begin
 {error,ie,ig,ic,ii,point,tx,phase}=inputs[k];iv=1;
 @(posedge clk);while(!ir)@(posedge clk);@(negedge clk);iv=0;
 end
endtask
task run_frame(input integer number,input integer offset,input integer length,input integer gout,input [31:0] id,ctx,gen,input [15:0] epoch,input integer mode,input integer target,input integer expected_pub,input integer expected_cons,input integer expected_rev,input integer expected_abort);
 integer k,n;begin
 j=number;goffset=gout;published=0;consumed=0;revoked=0;aborts=0;aborted=0;seen=0;held=0;last0=-1;last1=-1;
 ready_policy=(mode==21||mode==23||mode==15)?1:(mode==22?2:0);
 ci=id;cc=ctx;cg=gen;ce=epoch;cv=1;@(posedge clk);while(!cr)@(posedge clk);@(negedge clk);cv=0;ci=~id;cc=~ctx;cg=~gen;ce=~epoch;
 n=length;if(mode==20)n=2;if(mode>=10&&mode<=15)n=mode==10?12:13;if(mode==21)n=13;
 for(k=0;k<n;k=k+1)begin
  put(offset+k);
  if(mode==23&&k==12)begin
   while(!valid)@(negedge clk);cv=1;
   repeat(16)begin @(negedge clk);if(cr||ir||abort)$fatal(1,"pilot smoothing busy descriptor/point admitted");end
   cv=0;ready_policy=0;
  end
 end
 if(mode>=10&&mode<=15)begin
  if(mode==11)while(dut.state!=2)@(negedge clk);
  if(mode==12)while(dut.state!=3)@(negedge clk);
  if(mode==13||mode==14)while(!(dut.state==4&&dut.iteration==target))@(negedge clk);
  if(mode==15)begin while(!valid)@(negedge clk);@(negedge clk);end
  if(mode==14)rst=1;else cancel=1;
  #1;if(valid||abort)$fatal(1,"pilot smoothing cancel edge");repeat(2)@(negedge clk);rst=0;cancel=0;total_cancelled=total_cancelled+1;
  repeat(64)begin @(negedge clk);if(valid||abort||active||!cr)$fatal(1,"pilot smoothing cancelled frame revival");end
 end else if(expected_abort)begin while(!aborted)@(negedge clk);if(aborted_reason!=expected_abort)$fatal(1,"pilot smoothing abort reason");repeat(3)@(negedge clk);end
 else begin while(active||valid)@(negedge clk);repeat(2)@(negedge clk);end
 if(published!=expected_pub||consumed!=expected_cons||revoked!=expected_rev||aborts!=(expected_abort?1:0)||valid||active||!cr)$fatal(1,"pilot smoothing census case%0d p%0d c%0d r%0d a%0d",j,published,consumed,revoked,aborts);
 end endtask
initial begin
 $readmemh("input.hex",inputs);$readmemh("expected.hex",gold);repeat(4)@(negedge clk);rst=0;
 @CASES@
 if(deadline_successes!=1||total_published<20000||total_aborts<10||total_cancelled!=8||held_checks<100000)$fatal(1,"pilot smoothing coverage");
 $display("G0_PILOT_SMOOTHING_PASS frames=@FRAMES@ published=%0d consumed=%0d revoked=%0d aborts=%0d cancelled=%0d held_checks=%0d source_stalls=%0d final_cycle_admissions=%0d",total_published,total_consumed,total_revoked,total_aborts,total_cancelled,held_checks,source_stalls,deadline_successes);$finish;
end
initial begin #2000000000;$fatal(1,"pilot smoothing timeout case%0d state%0d",j,dut.state);end
endmodule
'''

def main(a):
 project=Path(__file__).resolve().parents[1];out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);ref=a.math_reference.resolve();bp=a.vivado_bin.resolve()
 before=disk_check('before-pilot-smoothing');(out/'disk-before.json').write_text(json.dumps(before,indent=2)+'\n')
 if before['disk']['available_bytes']<5*GIB:raise SystemExit('Under5GiB: handle storage first.')
 try:
  positive=json.loads((ref/'receipt.json').read_text());assert positive['simulation'].startswith('G0_PILOT_MATH_PASS')
  observed=[s.split() for s in (ref/'observed_pilots.txt').read_text().splitlines()];meta=json.loads((ref/'reference_frames.json').read_text());jobs=[];input_rows=[];expected_rows=[];max_float_error=0
  def add(raw,kind,mode=0,target=0,fault=None):
   nonlocal max_float_error
   n=len(jobs);id=(0xfffffff8+n)&0xffffffff;ctx=0x80000000^n;gen=(0xfffffff0+3*n)&0xffffffff;ep=(0xffff+n)&0xffff
   offset=len(input_rows);gout=len(expected_rows);rows=[(id,ctx,gen,ep,i,tx,raw[i][tx],0) for i in range(274) for tx in (0,1)]
   smooth_rows,float_error=smooth(raw);max_float_error=max(max_float_error,float_error);err=0;cut=len(rows)
   if fault:
    where,key,value=fault;item=list(rows[where]);item[key]=value(item[key]) if callable(value) else value;rows[where]=tuple(item);cut=where+1;err=2 if key==6 else 1
   if mode in (20,21):err=3
   kept=[]
   if fault:
    bad_i,bad_tx=divmod(cut-1,2)
    kept=[r for r in smooth_rows if (r[0],r[1])<(bad_i,bad_tx)]
   elif mode==20:kept=[]
   else:kept=smooth_rows
   input_rows.extend(input_word(r) for r in rows[:cut]);expected_rows.extend(output_word(r,id,ctx,gen,ep) for r in kept)
   pub=cons=len(kept);rev=0
   if mode in (10,11,12,13,14):pub=cons=0
   if mode==15:pub=1;cons=0;rev=1
   if mode==21:pub=1;cons=0;rev=1
   jobs.append(dict(number=n,offset=offset,length=cut,gout=gout,id=id,ctx=ctx,gen=gen,ep=ep,mode=mode,target=target,pub=pub,cons=cons,rev=rev,error=err,kind=kind))
  for f in meta['frames']:
   raw=[]
   for g in range(274):raw.append([int(observed[f['first_case']+2*g+tx][1],16) for tx in (0,1)])
   add(raw,'actual_RTL_pilot_phase')
  zero=[[0,0] for _ in range(274)];add(zero,'zero_phase_valid')
  for step in [1,-1,0x123456,-0x123456,(1<<31)-123,-((1<<31)-123)]:add([[(0x80000000+step*i)&0xffffffff,(0x7fffffff-step*i)&0xffffffff] for i in range(274)],'wrapped_linear')
  ties=[x[:] for x in zero];ties[1]=[7,(-7)&0xffffffff];add(ties,'positive_negative_half_ties')
  rng=random.Random(0x53473731)
  for k in range(16):
   p=[rng.getrandbits(32),rng.getrandbits(32)];raw=[]
   for i in range(274):
    p=[(x+rng.randrange(-30000000,30000000))&0xffffffff for x in p];raw.append(p[:])
   add(raw,'random_walk')
  for key in [0,1,2,3,4,5,7]:add(zero,'bad_identity',fault=(0,key,(lambda x:x^0x80000000) if key in (0,1,2) else ((lambda x:x^0x8000) if key==3 else ((lambda x:x^1) if key in (4,5) else 1))))
  for where in [47,200,546]:add(zero,'late_index_loss',fault=(where,4,lambda x:x^1))
  for delta in [1<<31,(1<<31)-122,(-(1<<31)+122)&0xffffffff]:add(zero,'ambiguous_half_turn',fault=(2,6,delta))
  add(zero,'missing_observation_deadline',20);add(zero,'held_output_deadline',21);add(zero,'final_cycle_success',22);add(zero,'busy_cfg_ignored',23)
  for mode,target in [(10,0),(11,0),(12,0),(13,0),(13,24),(13,48),(14,24),(15,0)]:add(ties,'cancel_reset',mode,target)
  add(zero,'post_dirty_recovery')
  for name,rows,width in [('input',input_rows,39),('expected',expected_rows,43)]: (out/(name+'.hex')).write_text(''.join(f'{v:0{width}x}\n' for v in rows))
  calls=[]
  for j in jobs:calls.append(f"run_frame({j['number']},{j['offset']},{j['length']},{j['gout']},32'h{j['id']:08x},32'h{j['ctx']:08x},32'h{j['gen']:08x},16'h{j['ep']:04x},{j['mode']},{j['target']},{j['pub']},{j['cons']},{j['rev']},{j['error']});")
  text=TB.replace('@LIMIT@',str(LIMIT)).replace('@ILAST@',str(len(input_rows)-1)).replace('@OLAST@',str(len(expected_rows)-1)).replace('@CASES@','\n'.join(calls)).replace('@FRAMES@',str(len(jobs)))
  (out/'tb.sv').write_text(text);src=out/'pilot_smoothing_cost_probe.sv';shutil.copyfile(project/'sim/g0'/src.name,src)
  def run(step,args):
   with (out/(step+'.log')).open('w') as f:subprocess.run(args,cwd=out,stdout=f,stderr=subprocess.STDOUT,check=True)
  run('compile',[str(bp/'xvlog'),'--sv',str(src),str(out/'tb.sv')]);run('elaborate',[str(bp/'xelab'),'tb','-s','pilot_smoothing_tb','-mt','off']);run('simulate',[str(bp/'xsim'),'pilot_smoothing_tb','-runall'])
  log=(out/'simulate.log').read_text();assert 'G0_PILOT_SMOOTHING_PASS' in log and 'Fatal:' not in log,log[-4000:]
  result={'scope':__doc__,'simulation':next(s for s in log.splitlines() if s.startswith('G0_PILOT_SMOOTHING_PASS')),'actual_RTL_input_frames':len(meta['frames']),'maximum_difference_from_scipy_phase_LSB':max_float_error,'case_counts':{kind:sum(j['kind']==kind for j in jobs) for kind in sorted({j['kind'] for j in jobs})},'source_sha256':hashlib.sha256(src.read_bytes()).hexdigest(),'routed_OOC':None,'g0_passed':False}
  if not a.sim_only:
   tcl='create_project -in_memory -part xc7z020clg484-1\nset_param general.maxThreads 4\nread_verilog -sv {'+str(src)+'}\nsynth_design -top pilot_smoothing_cost_probe -part xc7z020clg484-1 -mode out_of_context\ncreate_clock -name processing -period 20 [get_ports clk]\nset_input_delay 2 -clock processing [get_ports -filter {DIRECTION == IN && NAME != clk}]\nset_output_delay 2 -clock processing [get_ports -filter {DIRECTION == OUT}]\nopt_design\nplace_design\nroute_design\nreport_utilization -file utilization.rpt\nreport_timing_summary -file timing.rpt -report_unconstrained\nreport_drc -file drc.rpt\n'
   (out/'ooc.tcl').write_text(tcl);run('ooc',[str(bp/'vivado'),'-mode','batch','-source',str(out/'ooc.tcl'),'-nolog','-nojournal']);result['routed_OOC']=routed_measurements(out);assert result['routed_OOC']['DSP']==0 and result['routed_OOC']['BRAM36_equivalent']==0
  (out/'receipt.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result),flush=True)
 finally:(out/'disk-after.json').write_text(json.dumps(disk_check('after-pilot-smoothing'),indent=2)+'\n')
if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);p.add_argument('--math-reference',type=Path,required=True);p.add_argument('--vivado-bin',type=Path,required=True);p.add_argument('--sim-only',action='store_true');main(p.parse_args())
