#!/usr/bin/env python3
"""Actual captured pilot vectors -> shared header/pilot DSP -> normalized CORDIC.
Direct unbounded complex arithmetic is the oracle; no phase truth reaches DUT.
Headers and pilots alternate on the SAME implemented multiplier/ROM. Numerical
island only; full ADC/pilot scheduling/filtering/detector integration is pending.
"""
import argparse,hashlib,itertools,json,math,os,random,shutil,subprocess
from pathlib import Path
import numpy as np
from bpsk_header_probe import unpack,pack,SINE,COS
from phase_angle_cost_probe import cordic_phase
from fec_inverse_cost_probe import routed_measurements
from workspace_storage_guard import check as disk_check,GIB
SOURCES=['bpsk_header_cost_probe.sv','phase_angle_cost_probe.sv','pilot_angle_cost_probe.sv','header_pilot_math_cost_probe.sv']

def observation(h,x,phase):
 zr=sum(h[2*r]*x[2*r]+h[2*r+1]*x[2*r+1] for r in (0,1));zi=sum(h[2*r]*x[2*r+1]-h[2*r+1]*x[2*r] for r in (0,1));b=((phase+0x80000)&0xffffffff)>>20
 real=zr*COS[b]-zi*SINE[b];imag=zr*SINE[b]+zi*COS[b]
 shift=max(0,max(abs(real),abs(imag)).bit_length()-25);bias=1<<(shift-1) if shift else 0
 nx,ny=(real+bias)>>shift,(imag+bias)>>shift;p,error=cordic_phase(nx,ny)
 angle=math.atan2(imag,real)*(1<<32)/(2*math.pi) if real or imag else 0
 angle_error=abs((p-angle+(1<<31))%(1<<32)-(1<<31)) if not error else 0
 assert angle_error<=61.01,(angle_error,real,imag,shift,nx,ny)
 if not any(h):real=imag=nx=ny=shift=p=0;error=2
 return (real,imag,nx,ny,shift,p,error),angle_error

EXTRA=r'''
reg cp=0,pr=0;reg [31:0] cpp=0;wire pv;wire [31:0] pi,pp;wire [2:0] pe;wire [5:0] ps;wire signed [26:0] px,py;
reg [219:0] pinput[0:@PLAST@];reg [230:0] pgold[0:@PLAST@];
integer pilot_case=0,pilot_results=0,pilot_revoked=0,pilot_complex_checks=0,pilot_hold_checks=0,pilot_latency=0,pilot_max_latency=0,pilot_start=0,pilot_file;
reg pilot_seen=0,pilot_monitor=0,hold_pilot=0;reg [31:0] expected_pid;reg [126:0] held_pilot;
wire [126:0] pilot_view={pi,pp,pe,ps,px,py};
always @(posedge clk)begin
 if(!rst&&!cancel)begin
  if(iv&&ir&&dut.math_core.pilot)pilot_start=cycle;
  if(dut.math_valid&&dut.math_pilot&&dut.math_error==0)begin
   if({dut.math_imag,dut.math_real}!==pgold[pilot_case][135:0])$fatal(1,"pilot full complex oracle case%0d",pilot_case);
   pilot_complex_checks=pilot_complex_checks+1;
  end
  if(pilot_monitor&&pv&&!pilot_seen)begin
   if(pi!==expected_pid||{pe,pp,ps,py,px}!=={pgold[pilot_case][230:228],pgold[pilot_case][227:196],pgold[pilot_case][195:190],pgold[pilot_case][189:163],pgold[pilot_case][162:136]})$fatal(1,"pilot normalized phase oracle case%0d got%h err%0d",pilot_case,pp,pe);
   pilot_results=pilot_results+1;pilot_seen=1;
   if(pe==0||pe==1)begin pilot_latency=cycle-pilot_start;if(pilot_latency>pilot_max_latency)pilot_max_latency=pilot_latency;end
   $fdisplay(pilot_file,"%0d %08h %0d",pilot_case,pp,pe);
  end
  if(hold_pilot)begin if(!pv||pilot_view!==held_pilot)$fatal(1,"pilot held across header changed");pilot_hold_checks=pilot_hold_checks+1;end
 end
end
'''
TASK=r'''
task run_pilot(input integer n,input integer kind,input integer target);
 integer k;reg [31:0] id;begin
 pilot_case=n;pilot_seen=0;pilot_monitor=1;previous=-1;id=32'hfffffff0+32'(n);expected_pid=id;pr=0;cp=1;
 ci=id;h=pinput[n][187:84];cpp=pinput[n][219:188];step=32'hbeefddaa;cv=1;
 @(posedge clk);while(!cr)@(posedge clk);@(negedge clk);cv=0;ci=~id;cp=0;cpp=~cpp;h=~h;
 if(pinput[n][187:84]!=0)begin
  iv=1;ii=id;ix=0;ie=0;data=pinput[n][83:0];@(posedge clk);while(!ir)@(posedge clk);@(negedge clk);iv=0;
 end
 if(kind>=10&&kind<=14)begin
  if(kind==10)while(!(dut.math_core.state==5&&dut.math_core.issued==target))@(negedge clk);
  if(kind==11)while(dut.observation.state!=target)@(negedge clk);
  if(kind==12)while(!(dut.observation.angle.busy&&dut.observation.angle.iteration==target))@(negedge clk);
  if(kind==13)while(dut.math_core.state!=6)@(negedge clk);
  if(kind==14)while(!(dut.observation.angle.busy&&dut.observation.angle.iteration==15))@(negedge clk);
  pilot_monitor=0;
  if(kind==14)rst=1;else cancel=1;
  #1;if(pv||valid)$fatal(1,"pilot cancel edge leaked result");repeat(2)@(negedge clk);rst=0;cancel=0;pilot_revoked=pilot_revoked+1;
  repeat(40)begin @(negedge clk);if(pv||valid||!cr)$fatal(1,"pilot cancelled result revival");end
 end else begin
  while(!pv)@(negedge clk);
  @(negedge clk);
  if(!pilot_seen)$fatal(1,"pilot result invisible at active edge");
  if(valid||{sid,sf,l0,l1}!==96'd0)$fatal(1,"pilot leaked trusted header metadata");
  held_pilot=pilot_view;hold_pilot=1;
  repeat(n%5+3)@(negedge clk);
  if(kind==4)begin
   cp=1;ci=~id;cv=1;repeat(16)begin @(negedge clk);if(cr)$fatal(1,"second pilot overwrote held angle");end cv=0;cp=0;
  end else begin hold_pilot=0;pr=1;@(negedge clk);pr=0;repeat(2)@(negedge clk);pilot_monitor=0;if(pv)$fatal(1,"pilot result not consumed");end
 end
 end endtask
'''

def main(a):
 project=Path(__file__).resolve().parents[1];out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);ref=a.fixture_directory.resolve();headers=a.header_reference.resolve();bp=a.vivado_bin.resolve()
 before=disk_check('before-pilot-math');(out/'disk-before.json').write_text(json.dumps(before,indent=2)+'\n')
 if before['disk']['available_bytes']<5*GIB:raise SystemExit('Under5GiB: handle storage first.')
 try:
  meta=json.loads((ref/'fixture_metadata.json').read_text());f=np.memmap(ref/'filtered.hex',dtype='S21',mode='r');ins=[];gold=[];cases=[];frames=[];max_angle_error=0
  def add(h,x,phase,kind):
   nonlocal max_angle_error
   result,error=observation(h,x,phase);real,imag,nx,ny,shift,p,e=result;ins.append(pack(x,21)+(pack(h,26)<<84)+((phase&0xffffffff)<<188));gold.append((real%(1<<68))+((imag%(1<<68))<<68)+((nx%(1<<27))<<136)+((ny%(1<<27))<<163)+(shift<<190)+(p<<196)+(e<<228));cases.append({'kind':kind,'error':e});max_angle_error=max(max_angle_error,error);return p,e
  for m,model in enumerate(meta['models']):
   for w in range(1,4):
    entry=model['windows'][w];rows=[]
    for g in range(274):
     phases=[]
     for tx in (0,1):
      offset=1536+66*g+tx;p=entry['peak']+offset*entry['step'];tag,mu=divmod((p+32768)//65536,65536)
      aa=unpack(int(f[model['offset']+tag+20],16),20);bb=unpack(int(f[model['offset']+tag+21],16),20);x=[(2*(v*(65536-mu)+z*mu)+32768)//65536 for v,z in zip(aa,bb)]
      h=unpack(entry['H']>>(104*tx),26);step=(entry['phase_steps']>>(32*tx))&0xffffffff
      phase,error=add(h,x,(-step*offset)&0xffffffff,'capture');assert error==0;(phases.append(phase))
     rows.append(phases)
    frames.append({'model':m,'window':w,'first_case':len(cases)-548,'phases':rows})
  rng=random.Random(0x70696c6f74)
  for k in range(1024):
   h=[rng.randrange(-(1<<25),1<<25) for _ in range(4)];x=[rng.randrange(-(1<<20),1<<20) for _ in range(4)]
   h[k%4]=-(1<<25) if k%2 else (1<<25)-1;x[(k+1)%4]=-(1<<20) if k%2 else (1<<20)-1
   add(h,x,rng.getrandbits(32),'full_range')
  for phase in [0,0x80000-1,0x80000,0x80000+1,0x40000000,0x80000000,0xc0000000,0xffffffff]:
   for x in [[0,0,0,0],[1,0,0,0],[0,-1,0,0],[1,-1,0,0],[-(1<<20),(1<<20)-1,0,0]]:add([1,0,0,0],x,phase,'axes_ties')
  add([0]*4,[1,2,3,4],0,'zero_H');count=len(cases)
  for name,rows,width in [('pilot_input',ins,55),('pilot_gold',gold,58)]: (out/(name+'.hex')).write_text(''.join(f'{v:0{width}x}\n' for v in rows))
  for p in headers.glob('*.hex'):os.link(p,out/p.name)
  sources=[]
  for name in SOURCES:shutil.copyfile(project/'sim/g0'/name,out/name);sources.append(out/name)
  text=(headers/'tb.sv').read_text().replace('dut.','dut.math_core.')
  old="bpsk_header_cost_probe dut(clk,rst,cancel,cv,cr,ci,h,step,iv,ir,ii,ix,ie,data,valid,ready,oi,error,raw,sid,sf,l0,l1,1'b0,32'd0,,,);";assert text.count(old)==1
  decl=EXTRA.split('always @(posedge clk)',1)[0].replace('@PLAST@',str(count-1));logic='always @(posedge clk)'+EXTRA.split('always @(posedge clk)',1)[1]
  inst='''header_pilot_math_cost_probe dut(.clk(clk),.rst(rst),.cancel(cancel),.cfg_valid(cv),.cfg_ready(cr),.cfg_pilot(cp),.cfg_id(ci),.cfg_h(h),.cfg_phase_step(step),.cfg_pilot_phase(cpp),.in_valid(iv),.in_ready(ir),.in_id(ii),.in_index(ix),.in_error(ie),.in_data(data),.header_valid(valid),.header_ready(ready),.header_id(oi),.header_error(error),.header_raw(raw),.header_session(sid),.header_superframe(sf),.header_stream0_bytes(l0),.header_stream1_bytes(l1),.pilot_valid(pv),.pilot_ready(pr),.pilot_id(pi),.pilot_phase(pp),.pilot_error(pe),.pilot_shift(ps),.pilot_x(px),.pilot_y(py));'''
  text=text.replace(old,decl+'\n'+inst)
  text=text.replace('if(dut.math_core.multiply_enable)','if(!dut.math_core.pilot&&dut.math_core.multiply_enable)').replace('if(dut.math_core.state==3&&dut.math_core.issued==0)','if(!dut.math_core.pilot&&dut.math_core.state==3&&dut.math_core.issued==0)')
  text=text.replace('task run_job',logic+'\n'+TASK+'\ntask run_job')
  text=text.replace('$readmemh("vectors.hex",vectors);','$readmemh("pilot_input.hex",pinput);$readmemh("pilot_gold.hex",pgold);pilot_file=$fopen("observed_pilots.txt","w");\n $readmemh("vectors.hex",vectors);')
  first=next(s.strip() for s in text.splitlines() if s.lstrip().startswith('run_job(0,'))
  calls=f' for(integer p=0;p<{count};p=p+1)run_pilot(p,0,0);\n'
  calls+=' for(integer p=0;p<6;p=p+1)run_pilot(0,10,p);\n for(integer p=1;p<=4;p=p+1)run_pilot(0,11,p);\n'
  calls+=' run_pilot(0,12,0);run_pilot(0,12,15);run_pilot(0,12,31);run_pilot(0,13,0);run_pilot(0,14,0);\n'
  calls+=' run_pilot(0,4,0);\n'+first+'\n hold_pilot=0;pr=1;@(negedge clk);pr=0;pilot_monitor=0;repeat(2)@(negedge clk);\n'
  calls+=f' if(pilot_results!={count+1}||pilot_revoked!=15||pilot_max_latency>72||pilot_complex_checks<{count-1})$fatal(1,"pilot math coverage results%0d rev%0d latency%0d",pilot_results,pilot_revoked,pilot_max_latency);\n $fclose(pilot_file);$display("G0_PILOT_MATH_PASS results=%0d revoked=%0d complex_checks=%0d held_checks=%0d maximum_response_cycles=%0d",pilot_results,pilot_revoked,pilot_complex_checks,pilot_hold_checks,pilot_max_latency);\n'
  text=text.replace(' if(results!=104',calls+' if(results!=105');(out/'tb.sv').write_text(text)
  (out/'reference_frames.json').write_text(json.dumps({'frames':frames,'capture_cases':21*548,'case_count':count},indent=2)+'\n')
  def run(step,args):
   with (out/(step+'.log')).open('w') as f:subprocess.run(args,cwd=out,stdout=f,stderr=subprocess.STDOUT,check=True)
  run('compile',[str(bp/'xvlog'),'--sv',*map(str,sources),str(out/'tb.sv')]);run('elaborate',[str(bp/'xelab'),'tb','-s','pilot_math_tb','-mt','off']);run('simulate',[str(bp/'xsim'),'pilot_math_tb','-runall'])
  log=(out/'simulate.log').read_text();assert 'G0_PILOT_MATH_PASS' in log and 'G0_BPSK_HEADER_PASS' in log and 'Fatal:' not in log,log[-4000:]
  observed=[s.split() for s in (out/'observed_pilots.txt').read_text().splitlines()];assert len(observed)==count+1
  for number,p,e in observed[:21*548]:assert int(p,16)==(gold[int(number)]>>196)&0xffffffff and int(e)==0
  result={'scope':__doc__,'simulation':next(s for s in log.splitlines() if s.startswith('G0_PILOT_MATH_PASS')),'header_regression':next(s for s in log.splitlines() if s.startswith('G0_BPSK_HEADER_PASS')),'capture_frames':21,'capture_pilots':21*548,'maximum_total_angle_error_phase_LSB':max_angle_error,'source_sha256':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sources},'routed_OOC':None,'g0_passed':False}
  if not a.sim_only:
   tcl='create_project -in_memory -part xc7z020clg484-1\nset_param general.maxThreads 4\n'+''.join('read_verilog -sv {'+str(p)+'}\n' for p in sources)+'''synth_design -top header_pilot_math_cost_probe -part xc7z020clg484-1 -mode out_of_context
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
  (out/'receipt.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps({k:v for k,v in result.items() if k!='source_sha256'}),flush=True)
 finally:(out/'disk-after.json').write_text(json.dumps(disk_check('after-pilot-math'),indent=2)+'\n')
if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);p.add_argument('--fixture-directory',type=Path,required=True);p.add_argument('--header-reference',type=Path,required=True);p.add_argument('--vivado-bin',type=Path,required=True);p.add_argument('--sim-only',action='store_true');main(p.parse_args())
