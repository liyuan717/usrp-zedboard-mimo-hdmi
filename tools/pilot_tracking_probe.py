#!/usr/bin/env python3
"""Actual coherent pilot vectors -> shared header/pilot DSP/CORDIC -> seven-point OLS.
Joint ownership/backpressure/fence proof, not live ADC/shared-pool scheduling.
All expected phases are outputs only; DUT receives H, samples and nominal phase.
"""
import argparse,hashlib,json,os,re,shutil,subprocess
from pathlib import Path
from pilot_smoothing_probe import smooth,output_word
from pilot_math_probe import SOURCES as MATH_SOURCES
from fec_inverse_cost_probe import routed_measurements
from workspace_storage_guard import check as disk_check,GIB
SOURCES=MATH_SOURCES+['pilot_smoothing_cost_probe.sv','pilot_tracking_cost_probe.sv']
TB=r'''`timescale 1ns/1ps
module tb;
reg clk=0;always #10 clk=~clk;
reg rst=1,cancel=0,fv=0,cv=0,cp=1,iv=0,ie=0,hr=0,ready=0;
reg [31:0] fi=0,fc=0,fg=0,ci=0,cc=0,cg=0,step=0,nominal=0,ii=0;
reg [15:0] fe=0,ce=0;reg [8:0] point=0,ix=0;reg tx=0;
reg [103:0] h=0;reg [83:0] data=0;
wire fr,cr,ir,hv,valid,otx,abort,active;wire [31:0] hi,hs,hf,oi,oc,og;
wire [2:0] he;wire [191:0] raw;wire [15:0] l0,l1,oe;wire [8:0] op,known,ap;wire [1:0] reason;wire signed [40:0] phi;
pilot_tracking_cost_probe dut(.clk(clk),.rst(rst),.cancel(cancel),.frame_valid(fv),.frame_ready(fr),.frame_id(fi),.frame_context(fc),.frame_generation(fg),.frame_epoch(fe),
.cfg_valid(cv),.cfg_ready(cr),.cfg_pilot(cp),.cfg_id(ci),.cfg_context(cc),.cfg_generation(cg),.cfg_epoch(ce),.cfg_point(point),.cfg_tx(tx),.cfg_h(h),.cfg_phase_step(step),.cfg_pilot_phase(nominal),
.in_valid(iv),.in_ready(ir),.in_id(ii),.in_index(ix),.in_error(ie),.in_data(data),
.header_valid(hv),.header_ready(hr),.header_id(hi),.header_error(he),.header_raw(raw),.header_session(hs),.header_superframe(hf),.header_stream0_bytes(l0),.header_stream1_bytes(l1),
.out_valid(valid),.out_ready(ready),.out_id(oi),.out_context(oc),.out_generation(og),.out_epoch(oe),.out_point(op),.out_known_point(known),.out_tx(otx),.out_phase(phi),
.frame_abort(abort),.abort_reason(reason),.abort_point(ap),.active(active));
reg [219:0] samples[0:11507];reg [230:0] phasegold[0:11507];reg [83:0] header_samples[0:55295];reg [171:0] gold[0:@OLAST@];
wire [171:0] view={oe,og,oc,oi,otx,known,op,phi};
integer cycle=0,j=0,base=0,gout=0,pub=0,cons=0,rev=0,aborts=0,total_pub=0,total_cons=0,total_rev=0,total_aborts=0,held_checks=0,pilot_results=0,headers=0,cancelled=0;
integer math_index=0,last0=-1,last1=-1,start_cycle=0,max_frame_cycles=0,max_pending_cycles=0,pending_age=0;
reg [171:0] held_view;reg held=0,seen=0,aborted=0;reg [1:0] why;reg hold_all=0,allow_abort=0;
always @(negedge clk)ready=!hold_all && cycle%9!=0 && cycle%9!=1 && cycle%9!=2;
always @(posedge clk)begin
 cycle=cycle+1;
 if(rst||cancel||abort)begin
  if(seen)begin rev=rev+1;total_rev=total_rev+1;end
  held=0;seen=0;pending_age=0;
  if(abort)begin if(!allow_abort)$fatal(1,"joint pilot unexpected frame abort frame%0d reason%0d",j,reason);aborted=1;why=reason;aborts=aborts+1;total_aborts=total_aborts+1;end
 end else begin
  if(cv&&cr&&cp)math_index=base+2*point+tx;
  if(dut.pending)begin pending_age=pending_age+1;if(pending_age>max_pending_cycles)max_pending_cycles=pending_age;end else pending_age=0;
  if(dut.pilot_valid&&dut.pilot_ready)begin
   pilot_results=pilot_results+1;
   if(dut.pilot_error==0&&dut.pilot_phase!==phasegold[math_index][227:196])$fatal(1,"joint pilot phase oracle frame%0d sample%0d",j,math_index);
   if(dut.owner_tx)last1=dut.owner_point;else last0=dut.owner_point;
  end
  if(valid&&!seen)begin
   if(view!==gold[gout+pub])$fatal(1,"joint pilot Fraction oracle frame%0d pub%0d actual%h expected%h",j,pub,view,gold[gout+pub]);
   if(known!=(otx?last1:last0))$fatal(1,"joint pilot future observation");
   seen=1;pub=pub+1;total_pub=total_pub+1;
  end
  if(held&&(!valid||view!==held_view))$fatal(1,"joint pilot held result changed");
  if(valid&&ready)begin cons=cons+1;total_cons=total_cons+1;seen=0;end
  held=valid&&!ready;if(held)begin held_view=view;held_checks=held_checks+1;end
 end
end
task start_frame(input integer number,input integer source,input integer offset);
 begin
 j=number;base=source;gout=offset;pub=0;cons=0;rev=0;aborts=0;seen=0;held=0;aborted=0;last0=-1;last1=-1;hold_all=0;allow_abort=0;
 fi=32'hfffffff0+number;fc=32'h80000000^number;fg=32'hffffffff-3*number;fe=16'hffff-number;
 fv=1;@(posedge clk);while(!fr)@(posedge clk);@(negedge clk);fv=0;start_cycle=cycle;
 end endtask
task pilot(input integer n,input integer fault);
 reg [31:0] id;begin
 id=fi;cp=1;ci=id;cc=fc;cg=fg;ce=fe;point=9'(n/2);tx=1'(n%2);
 h=samples[base+n][187:84];nominal=samples[base+n][219:188];step=32'hafdfbadc;
 if(fault==1)cg=fg^32'h80000000;
 if(fault==3)h=0;
 cv=1;@(posedge clk);while(!cr)@(posedge clk);@(negedge clk);cv=0;
 cc=~fc;cg=~fg;ce=~fe;point=~point;tx=~tx;h=~h;nominal=~nominal;
 if(fault!=3)begin
  ii=fault==2?id^32'h80000000:id;ix=0;ie=0;data=fault==4?84'd0:samples[base+n][83:0];iv=1;
  @(posedge clk);while(!ir)@(posedge clk);@(negedge clk);iv=0;
 end
 end endtask
task header;
 integer k;reg [322:0] held_header;begin
 cp=0;ci=32'h12345678;h=104'h@HH@;step=32'h@STEP@;cv=1;
 @(posedge clk);while(!cr)@(posedge clk);@(negedge clk);cv=0;h=0;step=0;
 for(k=0;k<384;k=k+1)begin
  iv=1;ii=32'h12345678;ix=9'(k);data=header_samples[k];ie=0;
  @(posedge clk);while(!ir)@(posedge clk);@(negedge clk);iv=0;
 end
 while(!hv)@(negedge clk);
 if(hi!==32'h12345678||he!==0||raw!==192'h@RAW@||hs!==32'h@SID@||hf!==32'h@SF@||l0!=4384||l1!=4384)$fatal(1,"joint shared header oracle");
 headers=headers+1;held_header={hi,he,raw,hs,hf,l0,l1};cp=1;cv=1;
 repeat(19)begin @(negedge clk);if(cr||!hv||{hi,he,raw,hs,hf,l0,l1}!==held_header)$fatal(1,"joint held header changed");end cv=0;
 end endtask
task finish_frame;
 begin
 while(active||valid||dut.pending)@(negedge clk);repeat(3)@(negedge clk);
 if(pub!=548||cons!=548||rev||aborts||!fr||hv)$fatal(1,"joint pilot completed census frame%0d p%0d c%0d r%0d a%0d",j,pub,cons,rev,aborts);
 if(cycle-start_cycle>max_frame_cycles)max_frame_cycles=cycle-start_cycle;
 end endtask
task good_frame(input integer number,input integer source,input integer offset,input integer cadence);
 integer n,due;begin
 start_frame(number,source,offset);
 for(n=0;n<548;n=n+1)begin
  if(cadence>0)begin
   due=start_cycle+(n/2)*66*cadence+(n%2)*cadence;
   while(cycle<due)@(negedge clk);
  end
  if(n==12)hold_all=1;
  pilot(n,0);
  if(n==12)begin
   while(!valid)@(negedge clk);fv=1;repeat(17)begin @(negedge clk);if(fr)$fatal(1,"joint busy frame overwritten");end fv=0;
   header();hr=1;@(negedge clk);hr=0;hold_all=0;
  end
 end
 finish_frame();
 end endtask
task wait_abort(input integer expected_pub,input integer expected_cons,input integer expected_rev,input integer expected_reason);
 begin
 while(!aborted)@(negedge clk);
 if(why!=expected_reason)$fatal(1,"joint pilot abort reason");repeat(8)@(negedge clk);
 if(pub!=expected_pub||cons!=expected_cons||rev!=expected_rev||aborts!=1||active||valid||hv||dut.pending||!fr||cr)$fatal(1,"joint pilot abort/fence census frame%0d p%0d c%0d r%0d a%0d active%0d valid%0d header%0d pending%0d fr%0d cr%0d",j,pub,cons,rev,aborts,active,valid,hv,dut.pending,fr,cr);
 end endtask
task cancel_job(input integer number,input integer offset,input integer mode,input integer target);
 integer n;begin
 start_frame(number,0,offset);hold_all=1;
 if(mode<2)begin
  pilot(0,0);
  if(mode==0)while(!(dut.math.math_core.state==5&&dut.math.math_core.issued==target))@(negedge clk);
  else while(!(dut.math.observation.angle.busy&&dut.math.observation.angle.iteration==target))@(negedge clk);
 end else begin
  for(n=0;n<13;n=n+1)pilot(n,0);
  if(mode==2)while(!(dut.smoothing.state==4&&dut.smoothing.iteration==target))@(negedge clk);
  else begin while(!valid)@(negedge clk);pilot(13,0);while(!dut.pilot_valid)@(negedge clk);end
 end
 if(mode==4)rst=1;else cancel=1;#1;if(valid||hv||abort||cr||fr||ir)$fatal(1,"joint pilot cancel edge");
 repeat(2)@(negedge clk);rst=0;cancel=0;cancelled=cancelled+1;hold_all=0;
 repeat(96)begin @(negedge clk);if(valid||hv||abort||active||dut.pending||!fr||cr)$fatal(1,"joint pilot stale revival");end
 if(pub!=(mode>=3?1:0)||cons||rev!=(mode>=3?1:0)||aborts)$fatal(1,"joint pilot cancel census");
 end endtask
initial begin
 $readmemh("pilot_input.hex",samples,0,11507);$readmemh("pilot_gold.hex",phasegold,0,11507);$readmemh("vectors.hex",header_samples);$readmemh("expected.hex",gold);repeat(4)@(negedge clk);rst=0;
 @CASES@
 if(total_pub!=@PUB@||total_cons!=@CONS@||total_rev!=@REV@||total_aborts!=5||cancelled!=7||headers!=23||held_checks<100000)$fatal(1,"joint pilot coverage p%0d c%0d r%0d a%0d h%0d",total_pub,total_cons,total_rev,total_aborts,headers);
 $display("G0_PILOT_TRACKING_PASS complete_frames=22 exact_smoothed_points=%0d consumed=%0d revoked=%0d aborts=%0d cancelled=%0d headers=%0d pilot_results=%0d held_checks=%0d maximum_frame_cycles=%0d maximum_pending_cycles=%0d",total_pub,total_cons,total_rev,total_aborts,cancelled,headers,pilot_results,held_checks,max_frame_cycles,max_pending_cycles);$finish;
end
initial begin #2000000000;$fatal(1,"joint pilot timeout frame%0d pub%0d state%0d",j,pub,dut.smoothing.state);end
endmodule
'''
def main(a):
 project=Path(__file__).resolve().parents[1];out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);ref=a.math_reference.resolve();bp=a.vivado_bin.resolve()
 before=disk_check('before-pilot-tracking');(out/'disk-before.json').write_text(json.dumps(before,indent=2)+'\n')
 if before['disk']['available_bytes']<5*GIB:raise SystemExit('Under5GiB: handle storage first.')
 try:
  receipt=json.loads((ref/'receipt.json').read_text());assert receipt['simulation'].startswith('G0_PILOT_MATH_PASS');meta=json.loads((ref/'reference_frames.json').read_text());obs=[s.split() for s in (ref/'observed_pilots.txt').read_text().splitlines()]
  for name,sha in receipt['source_sha256'].items():assert hashlib.sha256((ref/name).read_bytes()).hexdigest()==sha==hashlib.sha256((project/'sim/g0'/name).read_bytes()).hexdigest()
  expected=[];calls=[];jobs=[];number=0
  def job(source,kind):
   nonlocal number
   n=number;number+=1;offset=len(expected);f=meta['frames'][source];raw=[[int(obs[f['first_case']+2*g+t][1],16) for t in (0,1)] for g in range(274)];rows,_=smooth(raw)
   expected.extend(output_word(r,(0xfffffff0+n)&0xffffffff,0x80000000^n,(0xffffffff-3*n)&0xffffffff,(0xffff-n)&0xffff) for r in rows)
   jobs.append({'number':n,'source':source,'kind':kind,'output_offset':offset});return n,f['first_case'],offset
  for source in range(21):
   n,s,g=job(source,'captured_nominal' if source<3 else 'captured_burst');calls.append(f'good_frame({n},{s},{g},{50 if source<3 else 0});')
  # Four explicit malformed coherent jobs abort only after the prior correct fit.
  for fault in (1,2,3,4):
   n,s,g=job(0,'pilot_error_fence');calls.append(f'start_frame({n},{s},{g});for(integer k=0;k<14;k=k+1)pilot(k,0);while(cons<8)@(negedge clk);allow_abort=1;pilot(14,{fault});wait_abort(8,8,0,1);')
  # Hold first fit, then a second actual angle, then a valid header on SAME DSP.
  n,s,g=job(0,'held_angle_header_deadline');calls.append(f'start_frame({n},{s},{g});hold_all=1;for(integer k=0;k<14;k=k+1)pilot(k,0);while(!dut.pilot_valid)@(negedge clk);header();allow_abort=1;wait_abort(1,0,1,3);hold_all=0;')
  for mode,target in [(0,0),(0,5),(1,15),(2,0),(2,48),(3,0),(4,0)]:
   n,s,g=job(0,'cancel_reset');calls.append(f'cancel_job({n},{g},{mode},{target});')
  n,s,g=job(0,'post_dirty_recovery');calls.append(f'good_frame({n},{s},{g},0);')
  (out/'expected.hex').write_text(''.join(f'{v:043x}\n' for v in expected));
  for p in ref.glob('*.hex'):
   if p.name in ('pilot_input.hex','pilot_gold.hex'):(out/p.name).write_text(''.join(p.read_text().splitlines(keepends=True)[:11508]))
   else:os.link(p,out/p.name)
  old=(ref/'tb.sv').read_text();line=next(s.strip() for s in old.splitlines() if s.lstrip().startswith('run_job(0,'));m=re.search(r"104'h([0-9a-f]+),32'h([0-9a-f]+),192'h([0-9a-f]+)",line);assert m
  hh,step,raw=m.groups();value=int(raw,16);subs={'@OLAST@':str(len(expected)-1),'@HH@':hh,'@STEP@':step,'@RAW@':raw,'@SID@':f'{(value>>32)&0xffffffff:08x}','@SF@':f'{(value>>64)&0xffffffff:08x}','@CASES@':'\n'.join(calls),'@PUB@':str(22*548+4*8+1+2),'@CONS@':str(22*548+4*8),'@REV@':'3'}
  text=TB
  for k,v in subs.items():text=text.replace(k,v)
  assert not re.search('@[A-Z]+@',text);(out/'tb.sv').write_text(text);sources=[]
  for name in SOURCES:shutil.copyfile(project/'sim/g0'/name,out/name);sources.append(out/name)
  def run(step,args):
   with (out/(step+'.log')).open('w') as f:subprocess.run(args,cwd=out,stdout=f,stderr=subprocess.STDOUT,check=True)
  run('compile',[str(bp/'xvlog'),'--sv',*map(str,sources),str(out/'tb.sv')]);run('elaborate',[str(bp/'xelab'),'tb','-s','pilot_tracking_tb','-mt','off']);run('simulate',[str(bp/'xsim'),'pilot_tracking_tb','-runall'])
  log=(out/'simulate.log').read_text();assert 'G0_PILOT_TRACKING_PASS' in log and 'Fatal:' not in log,log[-4000:]
  result={'scope':__doc__,'simulation':next(s for s in log.splitlines() if s.startswith('G0_PILOT_TRACKING_PASS')),'case_counts':{kind:sum(j['kind']==kind for j in jobs) for kind in sorted({j['kind'] for j in jobs})},'source_sha256':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sources},'routed_OOC':None,'g0_passed':False}
  if not a.sim_only:
   tcl='create_project -in_memory -part xc7z020clg484-1\nset_param general.maxThreads 4\n'+''.join('read_verilog -sv {'+str(p)+'}\n' for p in sources)+'''synth_design -top pilot_tracking_cost_probe -part xc7z020clg484-1 -mode out_of_context
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
'''
   (out/'ooc.tcl').write_text(tcl);run('ooc',[str(bp/'vivado'),'-mode','batch','-source',str(out/'ooc.tcl'),'-nolog','-nojournal']);result['routed_OOC']=routed_measurements(out);assert result['routed_OOC']['DSP']==1
  (out/'receipt.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result),flush=True)
 finally:(out/'disk-after.json').write_text(json.dumps(disk_check('after-pilot-tracking'),indent=2)+'\n')
if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);p.add_argument('--math-reference',type=Path,required=True);p.add_argument('--vivado-bin',type=Path,required=True);p.add_argument('--sim-only',action='store_true');main(p.parse_args())
