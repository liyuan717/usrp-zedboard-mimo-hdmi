#!/usr/bin/env python3
"""Independent deque model for diagnostic head stability and latest-tail loss."""
import argparse,json,random,shutil,subprocess,hashlib
from pathlib import Path

def main(a):
 out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);rng=random.Random(1342026);q=[];drops=0;lines=[];overwrites=0;clear_losses=0;inputs=0;consumed=0
 for k in range(4096):
  reset=int(k%431==0);clear=int(k%137==0);valid=int(k<16 or rng.random()<.75);ready=int(k>=16 and rng.random()<.3);data=rng.getrandbits(134)
  if reset or clear:clear_losses+=len(q);q=[];drops=0
  else:
   if q and ready:q.pop(0);consumed+=1
   if valid:
    inputs+=1
    if len(q)==2:q[-1]=data;drops+=1;overwrites+=1
    else:q.append(data)
  lines.append(f'{reset} {clear} {valid} {ready} {data:034x} {int(bool(q))} {(q[0] if q else 0):034x} {drops}\n')
 assert inputs==consumed+overwrites+clear_losses+len(q)
 (out/'vectors.txt').write_text(''.join(lines));tb=r'''`timescale 1ns/1ps
module tb;
reg clk=0;always #10 clk=~clk;
reg rst=1,clear=0,iv=0,ready=0;reg [133:0] data=0;
wire ov;wire [133:0] result;wire [31:0] replaced;
diagnostic_latest_mailbox_cost_probe dut(clk,rst,clear,iv,data,ov,ready,result,replaced);
integer fd,n,r,c,v,b,expected_valid,expected_drops,count=0;reg [133:0] value,gold;
initial begin
fd=$fopen("vectors.txt","r");repeat(3) @(negedge clk);
while(!$feof(fd)) begin
 n=$fscanf(fd,"%d %d %d %d %h %d %h %d\n",r,c,v,b,value,expected_valid,gold,expected_drops);if(n!=8) $fatal(1,"parse");
 rst=r;clear=c;iv=v;ready=b;data=value;
 @(posedge clk);#1;
 if(ov!==expected_valid[0] || (ov && result!==gold) || replaced!==32'(expected_drops)) $fatal(1,"mailbox mismatch cycle%0d",count);
 count=count+1;@(negedge clk);
end
if(count!=4096) $fatal(1,"coverage");$display("G0_DIAGNOSTIC_MAILBOX_PASS cycles=%0d",count);$finish;
end
endmodule
''';(out/'tb.sv').write_text(tb);project=Path(__file__).resolve().parents[1];src=out/'diagnostic_latest_mailbox_cost_probe.sv';shutil.copyfile(project/'sim/g0'/src.name,src);bp=a.vivado_bin.resolve()
 def run(n,args):
  with (out/(n+'.log')).open('w') as f:subprocess.run(args,cwd=out,stdout=f,stderr=subprocess.STDOUT,check=True)
 run('compile',[str(bp/'xvlog'),'--sv',str(src),str(out/'tb.sv')]);run('elaborate',[str(bp/'xelab'),'tb','-s','mailbox_tb','-mt','off']);run('simulate',[str(bp/'xsim'),'mailbox_tb','-runall']);log=(out/'simulate.log').read_text();assert 'G0_DIAGNOSTIC_MAILBOX_PASS' in log and 'Fatal:' not in log
 for name,old,new in [('head','tail<=in_data;tail_valid<=1','out_data<=in_data;tail<=in_data;tail_valid<=1'),('counter',"replaced_count<=replaced_count+1'b1","replaced_count<=replaced_count")]:
  mutant=out/(name+'.sv');mutant.write_text(src.read_text().replace('module diagnostic_latest_mailbox_cost_probe','module '+name+'_mailbox').replace(old,new));fp=out/(name+'_tb.sv');fp.write_text(tb.replace('module tb;','module '+name+'_tb;').replace('diagnostic_latest_mailbox_cost_probe dut',name+'_mailbox dut'))
  run(name+'-compile',[str(bp/'xvlog'),'--sv',str(mutant),str(fp)]);run(name+'-elaborate',[str(bp/'xelab'),name+'_tb','-s',name+'_tb','-mt','off']);run(name+'-simulate',[str(bp/'xsim'),name+'_tb','-runall']);l=(out/(name+'-simulate.log')).read_text();assert 'Fatal: mailbox mismatch' in l and 'G0_DIAGNOSTIC_MAILBOX_PASS' not in l
 r={'scope':__doc__,'cycles':4096,'input_events':inputs,'consumed':consumed,'tail_events_replaced':overwrites,'events_cleared':clear_losses,'remaining_events':len(q),'negative_controls':['held head overwritten','replacement counter frozen'],'source_sha256':hashlib.sha256(src.read_bytes()).hexdigest(),'g0_passed':False};(out/'receipt.json').write_text(json.dumps(r,indent=2)+'\n');print(json.dumps(r))
if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);p.add_argument('--vivado-bin',type=Path,required=True);main(p.parse_args())
