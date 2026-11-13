#!/usr/bin/env python3
"""Actual timed NCO configurations with independent per-tag/full-circle oracle."""
import argparse,json,math,shutil,subprocess,hashlib
from fractions import Fraction
from pathlib import Path
from fec_inverse_cost_probe import routed_measurements

def pack(v,w):return sum((x&((1<<w)-1))<<(j*w) for j,x in enumerate(v))
def main(a):
 out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);project=Path(__file__).resolve().parents[1]
 (out/'sine.hex').write_text(''.join(f'{round(65536*math.sin(2*math.pi*k/4096)):05x}\n' for k in range(1024)))
 expected=[]
 def exact(case,boundary,index):
  num=[453236549588,482014294528,228910892524][case%3];den=41600
  value=Fraction(0x1234567+boundary)-Fraction(num,4*den)
  def rnd(v):return (2*v.numerator+v.denominator)//(2*v.denominator)
  origin=100+case*17;offset=(index-origin)&((1<<48)-1)
  return rnd(value)&0xffffffff,rnd(value*offset)&0xffffffff
 for case in range(8):
  epoch=20+case;base=((1<<48)-49) if case%2 else 1000
  first,second,third,end=(320,640,960,1024) if a.calculated else (8,64,96,128)
  for k in range(first,end):
   boundary=third if k>=third else (second if k>=second else first);sense=(case+int(boundary==second))%2
   phase=(0x23456789+boundary*1234567+(k-boundary)*(0x1234567+boundary))&0xffffffff
   if a.calculated:
    initial=(base+boundary)&((1<<48)-1);step,phase0=exact(case,boundary,initial)
    phase=(phase0+(k-boundary)*step)&0xffffffff
   b=((phase+0x80000)&0xffffffff)>>20;c=round(65536*math.cos(2*math.pi*b/4096));s=round(65536*math.sin(2*math.pi*b/4096))
   vals=[-32768,32767 if k%2 else -32768,12345,-32768];mixed=[]
   for rx in range(2):
    i,q=vals[2*rx:2*rx+2];q=-q if sense else q
    mixed += [(i*c-q*s+16384)//32768,(i*s+q*c+16384)//32768]
   tag=(epoch<<48)|((base+k)&((1<<48)-1));expected.append((tag,int(k==boundary),pack(mixed,18)))
 (out/'expected.txt').write_text(''.join(f'{t:016x} {s} {v:018x}\n' for t,s,v in expected))
 tb=r'''`timescale 1ns/1ps
module tb;
reg clk=0;always #10 clk=~clk;
reg rst=1,cv=0,iv=0,ready=1,sense=0;reg [63:0] ct=0,it=0,data=0;reg [31:0] cp=0,cs=0;
wire cr,ir,ov,os,armed,applied;wire [63:0] ot;wire [71:0] result;wire [31:0] fences;wire [1:0] error;
nco_cost_probe #(.ENABLE_CONJUGATE(1),.ENABLE_TIMED_CONFIG(1)) dut(clk,rst,cv,cr,ct,cp,cs,iv,ir,it,data,ov,ready,ot,os,result,armed,fences,sense,applied,error);
integer fd,n,case_no=0,k=0,received=0,commits=0,rejects=0,stalls=0,cycle=0,period,desc=0;
reg [47:0] base;reg [15:0] epoch;reg [63:0] et;integer start;reg [71:0] value,held_value;reg [63:0] held_tag;reg held_start,held=0;
function [63:0] tag(input integer index);tag={epoch,48'(base+index)};endfunction
always @(negedge clk) ready=!(case_no%2 && (cycle%107<24));
always @(posedge clk) begin
 cycle=cycle+1;
 if(rst) held=0;
 else begin
  if(held && (!ov || result!==held_value || ot!==held_tag || os!==held_start)) $fatal(1,"hold");
  held=ov&&!ready;
  if(held) begin held_value=result;held_tag=ot;held_start=os;end
  if(iv&&!ir) stalls=stalls+1;
  if(applied) begin
   if(it!==ct || !iv || !cr || ir) $fatal(1,"boundary mismatch");commits=commits+1;
  end
  if(ov&&ready) begin
   n=$fscanf(fd,"%h %d %h\n",et,start,value);
   if(n!=3 || ot!==et || os!==start[0] || result!==value) $fatal(1,"timed output mismatch %0d tag%h/%h got%h/%h",received,ot,et,result,value);
   received=received+1;
  end
 end
end
task configure(input integer boundary,input integer bad);
 begin
  ct=tag(boundary);if(bad==2) ct=ct+64'h0001000000000000;
  cp=32'h23456789+boundary*1234567;cs=32'h1234567+boundary;
  sense=(case_no+(boundary==64))%2;cv=1;
  @(posedge clk);while(!cr) @(posedge clk);
  if(bad && applied) $fatal(1,"invalid config applied");
  @(negedge clk);cv=0;cp=~cp;cs=~cs;sense=~sense;ct=~ct;
  if(error!==2'(bad)) $fatal(1,"config status %0d/%0d",error,bad);
  if(bad) rejects=rejects+1;
 end
endtask
initial begin
 fd=$fopen("expected.txt","r");
 for(case_no=0;case_no<8;case_no=case_no+1) begin
  rst=1;repeat(3) @(negedge clk);rst=0;epoch=20+case_no;base=case_no%2?48'hffffffffffcf:48'd1000;
  period=case_no%4==0?3:(case_no%4==1?8:(case_no%4==2?12:13));
  fork
   begin configure(8,0);configure(64,0);configure(32,1);configure(80,2);configure(96,0);end
   begin
    for(k=0;k<128;k=k+1) begin
     it=tag(k);data={16'h8000,16'd12345,(k%2?16'h7fff:16'h8000),16'h8000};iv=1;
     @(posedge clk);while(!ir) @(posedge clk);@(negedge clk);iv=0;
     repeat(period-1) @(negedge clk);
    end
   end
  join
  repeat(40) @(negedge clk);
  if(fences!=0) $fatal(1,"unexpected fence");
 end
 // Pending future descriptor is revoked by reset, with no sample commit.
 cv=1;ct=tag(1000);repeat(11) @(negedge clk);rst=1;cv=0;repeat(3) @(negedge clk);rst=0;
 if(armed || ov || error) $fatal(1,"reset pending");
 if(received!=960 || commits!=24 || rejects!=16 || stalls==0) $fatal(1,"coverage");
 $display("G0_TIMED_NCO_PASS outputs=%0d commits=%0d rejects=%0d stalls=%0d",received,commits,rejects,stalls);$finish;
end
initial begin #30000000;$fatal(1,"timeout");end
endmodule
'''
 if a.calculated:
  tb=tb.replace('wire cr,ir,ov', 'wire fready,fvalid;wire [63:0] ftag;wire [31:0] fstep,fphase,fid,fgen;wire [1:0] ferr;\nwire cr,ir,ov')
  tb=tb.replace('dut(clk,rst,cv,cr,ct,cp,cs,', 'dut(clk,rst,fvalid,cr,ftag,fphase,fstep,')
  i=tb.index('function [63:0] tag')
  tb=tb[:i]+"""fine_nco_config_cost_probe calculator(.clk(clk),.rst(rst),.cancel(1'b0),.in_valid(cv),.in_ready(fready),
.in_id(32'd1),.in_generation(32'd2),.in_numerator(case_no%3==0?48'd453236549588:(case_no%3==1?48'd482014294528:48'd228910892524)),
.in_divisor(16'd41600),.in_coarse_step(cs),.in_known_tag(ct),.in_phase_origin_index(48'(100+case_no*17)),
.out_valid(fvalid),.out_ready(cr),.out_id(fid),.out_generation(fgen),.out_known_tag(ftag),.out_step(fstep),.out_phase(fphase),.out_error(ferr));
"""+tb[i:]
  tb=tb.replace('if(it!==ct || !iv || !cr || ir)', 'if(it!==ftag || ferr || fid!=1 || fgen!=2 || !iv || !cr || ir)')
  tb=tb.replace('@(posedge clk);while(!cr) @(posedge clk);', """@(posedge clk);while(!fready) @(posedge clk);
  @(negedge clk);cv=0;
  @(posedge clk);while(!fvalid || !cr) @(posedge clk);""")
  tb=tb.replace('boundary==64','boundary==640').replace('configure(8,0);configure(64,0);','configure(320,0);configure(640,0);').replace('configure(96,0)','configure(960,0)').replace('k<128','k<1024').replace('received!=960','received!=5632')
  # Pending calculator input rather than an NCO descriptor is reset at end.
  tb=tb.replace('cv=1;ct=tag(1000);repeat(11)', 'cv=1;ct=tag(2000);repeat(11)')
 (out/'tb.sv').write_text(tb);src=out/'nco_cost_probe.sv';shutil.copyfile(project/'sim/g0/nco_cost_probe.sv',src);bp=a.vivado_bin.resolve()
 def run(n,args):
  with (out/(n+'.log')).open('w') as f:subprocess.run(args,cwd=out,stdout=f,stderr=subprocess.STDOUT,check=True)
 extra=[]
 if a.calculated:
  fine=out/'fine_nco_config_cost_probe.sv';shutil.copyfile(project/'sim/g0'/fine.name,fine);extra=[str(fine)]
 run('compile',[str(bp/'xvlog'),'--sv',*extra,str(src),str(out/'tb.sv')]);run('elaborate',[str(bp/'xelab'),'tb','-s','timed_tb','-mt','off']);run('simulate',[str(bp/'xsim'),'timed_tb','-runall'])
 log=(out/'simulate.log').read_text();assert 'G0_TIMED_NCO_PASS' in log and 'Fatal:' not in log,log[-1500:]
 for name,old,new in [('early','cfg_distance==0','cfg_distance>=0'),('sense','conjugate<=ENABLE_CONJUGATE && cfg_conjugate','conjugate<=0')]:
  mutant=out/(name+'.sv');mutant.write_text(src.read_text().replace('module nco_cost_probe','module '+name+'_nco').replace(old,new));fault=out/(name+'_tb.sv');fault.write_text(tb.replace('module tb;','module '+name+'_tb;').replace('nco_cost_probe #',name+'_nco #'))
  run(name+'-compile',[str(bp/'xvlog'),'--sv',str(mutant),str(fault)]);run(name+'-elaborate',[str(bp/'xelab'),name+'_tb','-s',name+'_tb','-mt','off']);run(name+'-simulate',[str(bp/'xsim'),name+'_tb','-runall'])
  l=(out/(name+'-simulate.log')).read_text();assert 'Fatal:' in l and 'G0_TIMED_NCO_PASS' not in l
 receipt={'actual_calculator_drives_NCO':a.calculated,'scope':__doc__,'simulation':log[log.index('G0_TIMED_NCO_PASS'):].splitlines()[0],'negative_controls':['early commit','conjugation removed'],'source_sha256':hashlib.sha256(src.read_bytes()).hexdigest(),'g0_passed':False}
 (out/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n');print(json.dumps(receipt))
if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--calculated',action='store_true');p.add_argument('--output',type=Path,required=True);p.add_argument('--vivado-bin',type=Path,required=True);main(p.parse_args())
