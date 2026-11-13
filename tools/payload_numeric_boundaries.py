#!/usr/bin/env python3
"""Independent full-width numeric/held/cancel boundaries for payload arithmetic.
Unbounded integer/Fraction references, including every ML candidate metric.
"""
import argparse,hashlib,json,os,random,shutil,subprocess
from fractions import Fraction
from pathlib import Path
import numpy as np
from bpsk_header_probe import pack
from payload_reference import rotate
from workspace_storage_guard import check as disk_check,GIB
SOURCES=['payload_phase_cost_probe.sv','payload_rotate_cost_probe.sv','payload_ml_cost_probe.sv']

def nearest(v):return (2*v.numerator+v.denominator)//(2*v.denominator)
def metrics(y,h):
 result=[]
 for c in range(16):
  s=[1-2*((c>>k)&1) for k in range(4)];e=[]
  for rx in (0,1):
   a,b,d,f=h[rx*4:rx*4+4];e.extend([y[2*rx]-(a*s[0]-b*s[1]+d*s[2]-f*s[3]),y[2*rx+1]-(b*s[0]+a*s[1]+f*s[2]+d*s[3])])
  result.append(sum(v*v for v in e))
 return result
TB=r'''`timescale 1ns/1ps
module tb;
reg clk=0;always #10 clk=~clk;reg rst=1,cancel=0;
reg pcv=0,pr=0;wire pcr,pv,pe,pl,pbusy;reg [31:0] pid=0;reg [8:0] pg=0;reg [63:0] pn=0,pstep=0;reg signed [40:0] a=0,b=0,c=0,d=0;wire [31:0] po,px,py;wire [15:0] pep;wire [8:0] pog;wire [5:0] poi;wire [14:0] pos;wire [63:0] pp;wire signed [40:0] ra,rb;
payload_phase_cost_probe phase_dut(clk,rst,cancel,pcv,pcr,pid,32'h80000000,32'hffffffff,16'hffff,pg,pn,pstep,a,b,c,d,pv,pr,po,px,py,pep,pog,poi,pos,pp,ra,rb,pe,pl,pbusy);
reg rcv=0,riv=0,rr=0;wire rcr,rir,rv,rbusy;reg [207:0] rh=0;reg [83:0] ry=0;reg [63:0] rp=0;wire [31:0] ro,rc,rg;wire [15:0] rep;wire [14:0] rs;wire [2:0] re;wire [103:0] roy;wire [215:0] roh;
payload_rotate_cost_probe rotation_dut(clk,rst,cancel,rcv,rcr,32'hffffffff,32'h80000000,32'hffffffff,16'hffff,rh,riv,rir,32'hffffffff,32'h80000000,32'hffffffff,16'hffff,15'h7fff,3'd0,ry,rp,rv,rr,ro,rc,rg,rep,rs,re,roy,roh,rbusy);
reg miv=0,mr=0;wire mir,mv,mbusy;reg [103:0] my=0;reg [215:0] mh=0;wire [31:0] mo,mc,mg;wire [15:0] mep;wire [14:0] ms;wire [2:0] me;wire [3:0] md;wire [61:0] mm;
payload_ml_cost_probe ml_dut(clk,rst,cancel,miv,mir,32'hffffffff,32'h80000000,32'hffffffff,16'hffff,15'h7fff,3'd0,my,mh,mv,mr,mo,mc,mg,mep,ms,me,md,mm,mbusy);
reg [300:0] pconfig[0:@PN@];reg [145:0] pgold[0:@PLAST@];reg [355:0] rinput[0:@RN@];reg [319:0] rgold[0:@RN@];reg [319:0] minput[0:@MN@];reg [65:0] mgold[0:@MN@];reg [61:0] allmetric[0:@MALL@];
integer n,i,phase_count=0,rotations=0,mlresults=0,metric_checks=0,held_checks=0,pbase=0,pidx=0;reg mcheck=0;reg [177:0] pview;reg [319:0] rview;reg [65:0] mview;
always @(posedge clk)if(mcheck&&ml_dut.metric_valid&&!rst&&!cancel)begin if(ml_dut.metric!==allmetric[n*16+ml_dut.metric_candidate])$fatal(1,"wide ML all-candidate metric oracle case%0d candidate%0d",n,ml_dut.metric_candidate);metric_checks=metric_checks+1;end
initial begin
 $readmemh("phase_cfg.hex",pconfig);$readmemh("phase_gold.hex",pgold);$readmemh("rotate_input.hex",rinput);$readmemh("rotate_gold.hex",rgold);$readmemh("ml_input.hex",minput);$readmemh("ml_gold.hex",mgold);$readmemh("all_metrics.hex",allmetric);
 repeat(5)@(negedge clk);rst=0;
 for(n=0;n<=@PN@;n=n+1)begin
  {pg,pstep,pn,d,c,b,a}=pconfig[n];pid=32'hfffffff0+n;pcv=1;@(posedge clk);while(!pcr)@(posedge clk);@(negedge clk);pcv=0;pg=~pg;pstep=~pstep;pn=~pn;a=~a;b=~b;c=~c;d=~d;
  for(i=0;i<(n<@PVALID@?64:1);i=i+1)begin
   while(!pv)@(negedge clk);
   if({rb,ra,pp}!==pgold[pbase+i]||pe!==(n>=@PVALID@)||po!==32'hfffffff0+n||px!==32'h80000000||py!==32'hffffffff||pep!==16'hffff||poi!==6'(i)||pos!=={pog,poi}||pl!==(i==63||n>=@PVALID@))$fatal(1,"exact Fraction phase boundary case%0d index%0d",n,i);
   pview={po,px,py,pep,pog,poi,pos,pe,pl};pr=0;repeat(3+n%5)begin @(negedge clk);if(!pv||pview!=={po,px,py,pep,pog,poi,pos,pe,pl}||{rb,ra,pp}!==pgold[pbase+i])$fatal(1,"phase boundary held changed");held_checks=held_checks+1;end
   pr=1;@(negedge clk);pr=0;phase_count=phase_count+1;
  end
  pbase=pbase+(n<@PVALID@?64:1);if(pv||pbusy||!pcr)$fatal(1,"phase boundary not idle");
 end
 for(n=0;n<=@RN@;n=n+1)begin
  {rp,rh,ry}=rinput[n];rcv=1;@(posedge clk);while(!rcr)@(posedge clk);@(negedge clk);rcv=0;rh=~rh;
  riv=1;@(posedge clk);while(!rir)@(posedge clk);@(negedge clk);riv=0;ry=~ry;rp=~rp;
  while(!rv)@(negedge clk);
  if(re||{roh,roy}!==rgold[n]||{ro,rc,rg,rep,rs}!=={32'hffffffff,32'h80000000,32'hffffffff,16'hffff,15'h7fff})$fatal(1,"full H26 rotation boundary case%0d",n);
  rview={roh,roy};repeat(3+n%3)begin @(negedge clk);if(!rv||{roh,roy}!==rview||rcr)$fatal(1,"rotation boundary held changed");held_checks=held_checks+1;end
  rr=1;@(negedge clk);rr=0;rotations=rotations+1;
 end
 for(n=0;n<=@MN@;n=n+1)begin
  {mh,my}=minput[n];mcheck=1;miv=1;@(posedge clk);while(!mir)@(posedge clk);@(negedge clk);miv=0;mh=~mh;my=~my;
  while(!mv)@(negedge clk);
  if(me||{mm,md}!==mgold[n]||{mo,mc,mg,mep,ms}!=={32'hffffffff,32'h80000000,32'hffffffff,16'hffff,15'h7fff})$fatal(1,"wide ML boundary result case%0d",n);
  mview={mm,md};repeat(3+n%3)begin @(negedge clk);if(!mv||{mm,md}!==mview||mir)$fatal(1,"ML boundary held changed");held_checks=held_checks+1;end
  mr=1;@(negedge clk);mr=0;mlresults=mlresults+1;mcheck=0;
 end
 if(phase_count!=@PCOUNT@||rotations!=@RCOUNT@||mlresults!=@MCOUNT@||metric_checks!=@METRICS@)$fatal(1,"payload numeric boundary census");
 $display("G0_PAYLOAD_NUMERIC_PASS phase_results=%0d full_range_rotations=%0d wide_ML_results=%0d all_candidate_metrics=%0d held_checks=%0d",phase_count,rotations,mlresults,metric_checks,held_checks);$finish;
end
initial begin #2000000000;$fatal(1,"payload numeric boundary timeout n%0d i%0d",n,i);end
endmodule
'''
def main(a):
 project=Path(__file__).resolve().parents[1];out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);bp=a.vivado_bin.resolve();before=disk_check('before-payload-numeric');(out/'disk-before.json').write_text(json.dumps(before,indent=2)+'\n')
 if before['disk']['available_bytes']<5*GIB:raise SystemExit('Under5GiB: handle storage first.')
 try:
  rng=random.Random(0x5041594c);pc=[];pg=[];rc=[];rg=[];mi=[];mg=[];allm=[];rails=[-(1<<40),(1<<40)-1]
  for n in range(256):
   group=273 if n%7==0 else rng.randrange(273);left=[rng.randrange(-(1<<40),1<<40) for _ in range(2)];right=[rng.randrange(-(1<<40),1<<40) for _ in range(2)]
   if n<16:left=[rails[n%2],rails[1-n%2]];right=left[::-1]
   if 16<=n<32:left=[0,0];right=[[-1,1,33,-33,65,-65,66,-66][n%8]]*2
   nominal=[rng.getrandbits(32) for _ in range(2)];steps=[rng.getrandbits(32) for _ in range(2)]
   # RTL order left0,right0,left1,right1,nominal,steps,group.
   pc.append(pack([left[0],right[0],left[1],right[1]],41)+(pack(nominal,32)<<164)+(pack(steps,32)<<228)+(group<<292))
   for i in range(64):
    residual=[left[t] if group==273 else nearest(Fraction(left[t]*66+(right[t]-left[t])*(i+2-t),66)) for t in (0,1)];phase=[(nominal[t]+steps[t]*i+residual[t])&0xffffffff for t in (0,1)];pg.append(pack(phase,32)+(pack(residual,41)<<64))
  valid=len(pc)
  for group in (274,511):pc.append(group<<292);pg.append(0)
  for n in range(4160):
   h=[rng.randrange(-(1<<25),1<<25) for _ in range(8)];y=[rng.randrange(-(1<<20),1<<20) for _ in range(4)];h[n%8]=-(1<<25) if n%2 else (1<<25)-1;y[n%4]=-(1<<20) if n%2 else (1<<20)-1
   phase=(n%4096)<<20;phase=(phase+([-1,0,1][n%3]+0x80000 if n>=4096 else 0))&0xffffffff;ph=[phase,(-phase)&0xffffffff];sy,hh=rotate(np.array(h,dtype=np.int64).reshape(2,2,2),np.array(y,dtype=np.int64).reshape(1,2,2),np.array([ph],dtype=np.int64));rc.append(pack(y,21)+(pack(h,26)<<84)+(pack(ph,32)<<292));rg.append(pack(sy[0],26)+(pack(hh[0],27)<<104))
  for n in range(2048):
   y=[rng.randrange(-(1<<25),1<<25) for _ in range(4)];h=[rng.randrange(-(1<<26),1<<26) for _ in range(8)]
   if n<3:y=[[0]*4,[-(1<<25)]*4,[(1<<25)-1]*4][n];h=[[0]*8,[-(1<<26)]*8,[(1<<26)-1]*8][n]
   else:y[n%4]=-(1<<25) if n%2 else (1<<25)-1;h[n%8]=-(1<<26) if n%2 else (1<<26)-1
   m=metrics(y,h);decision=min(range(16),key=lambda c:m[c]);mi.append(pack(y,26)+(pack(h,27)<<104));mg.append(decision+(m[decision]<<4));allm.extend(m)
  for name,rows,width in [('phase_cfg',pc,76),('phase_gold',pg,37),('rotate_input',rc,89),('rotate_gold',rg,80),('ml_input',mi,80),('ml_gold',mg,17),('all_metrics',allm,16)]: (out/(name+'.hex')).write_text(''.join(f'{v:0{width}x}\n' for v in rows))
  text=TB
  for key,value in {'PN':len(pc)-1,'PLAST':len(pg)-1,'RN':len(rc)-1,'MN':len(mi)-1,'MALL':len(allm)-1,'PVALID':valid,'PCOUNT':len(pg),'RCOUNT':len(rc),'MCOUNT':len(mi),'METRICS':len(allm)}.items():text=text.replace('@'+key+'@',str(value))
  (out/'tb.sv').write_text(text);shutil.copyfile(a.rom_directory/'sine.hex',out/'sine.hex')
  for n in SOURCES:shutil.copyfile(project/'sim/g0'/n,out/n)
  def run(step,args):
   with (out/(step+'.log')).open('w') as f:subprocess.run(args,cwd=out,stdout=f,stderr=subprocess.STDOUT,check=True)
  run('compile',[str(bp/'xvlog'),'--sv',*[str(out/n) for n in SOURCES],str(out/'tb.sv')]);run('elaborate',[str(bp/'xelab'),'tb','-s','payload_numeric_tb','-mt','off']);run('simulate',[str(bp/'xsim'),'payload_numeric_tb','-runall'])
  log=(out/'simulate.log').read_text();assert 'G0_PAYLOAD_NUMERIC_PASS' in log and 'Fatal:' not in log,log[-3500:];result={'scope':__doc__,'simulation':next(s for s in log.splitlines() if s.startswith('G0_PAYLOAD_NUMERIC_PASS')),'source_sha256':{n:hashlib.sha256((out/n).read_bytes()).hexdigest() for n in SOURCES},'g0_passed':False};(out/'receipt.json').write_text(json.dumps(result,indent=2)+'\n');print(result['simulation'],flush=True)
 finally:(out/'disk-after.json').write_text(json.dumps(disk_check('after-payload-numeric'),indent=2)+'\n')
if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);p.add_argument('--rom-directory',type=Path,required=True);p.add_argument('--vivado-bin',type=Path,required=True);main(p.parse_args())
