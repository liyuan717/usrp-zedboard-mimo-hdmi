#!/usr/bin/env python3
"""Actual timing controller admission, cancellation and source-fence boundaries."""
import argparse,hashlib,json,shutil,subprocess
from pathlib import Path
from causal_tracking_timing_probe import SOURCES
from workspace_storage_guard import check as disk_check,GIB

TB=r'''`timescale 1ns/1ps
module tb;
reg clk=0;always #10 clk=~clk;
reg rst=1,cancel=0,enable=0,inject=0,av=0;integer cycle=0,writes=0;reg first_word=1;reg [47:0] index=48'hfffffffffff0;
wire iv=enable&&(cycle%8==0||inject);wire start=first_word||inject;wire [63:0] tag={16'hffff,index};
reg [31:0] aid=32'hffffffff,agen=0;reg [63:0] afirst=0;
wire ir,ar,tu,dv,locked,request,restart,active;wire [31:0] tid,tgen,replaced,wstarted,missed,hgen;wire [15:0] te;wire [79:0] tp,train,nextpos,word;wire [34:0] step;wire [2:0] used,reason;wire [121:0] diag;wire [63:0] latest;wire [12:0] hc;
causal_tracking_timing_cost_probe dut(clk,rst,cancel,iv,ir,start,tag,80'd0,1'b0,12'd0,word,av,ar,aid,agen,afirst,tu,tid,tgen,te,tp,train,nextpos,step,used,dv,1'b0,diag,replaced,locked,request,reason,wstarted,missed,hgen,latest,hc,restart,active);
always @(posedge clk)begin
 cycle<=cycle+1;
 if(rst||cancel)first_word<=1;
 else if(iv&&ir)begin index<=index+1'b1;writes=writes+1;first_word<=0;end
end
task arm(input integer bad);
 begin
  while(!ar)@(negedge clk);agen=hgen^(bad==1?32'h80000000:32'd0);
  afirst={(latest[63:48]^(bad==2?16'h8000:16'd0)),latest[47:0]+48'd100000};av=1;
  @(posedge clk);while(!ar)@(posedge clk);@(negedge clk);av=0;
 end
endtask
integer oldwrites,oldgen,oldwindows,checks=0,invalid=0,fences=0,cancels=0;
initial begin
 repeat(4)@(negedge clk);rst=0;enable=1;while(hc<4)@(negedge clk);
 arm(1);if(active||!request||reason!=1)$fatal(1,"tracking high-generation admission");invalid=invalid+1;
 arm(2);if(active||!request||reason!=1)$fatal(1,"tracking high-epoch admission");invalid=invalid+1;
 arm(0);if(dut.state!=1)$fatal(1,"tracking ISSUE boundary");
 cancel=1;#1;if(tu||dv||ir||ar)$fatal(1,"tracking ISSUE cancel fence");
 repeat(2)@(negedge clk);cancel=0;cancels=cancels+1;
 repeat(32)begin @(negedge clk);if(active||locked||request||tu||dv)$fatal(1,"tracking cancelled auto revival");checks=checks+1;end
 // A source restart cancels only timing work: its sample must still be written.
 arm(0);oldwrites=writes;oldgen=hgen;inject=1;#1;if(!ir||!restart)$fatal(1,"tracking ISSUE source write gate");
 @(negedge clk);inject=0;
 if(writes!=oldwrites+1||hgen!=oldgen+1||hc!=1||active||!request||reason!=1)$fatal(1,"tracking ISSUE source fence");fences=fences+1;
 arm(0);while(dut.state!=2)@(negedge clk);oldwindows=wstarted;
 // Holding a second arm cannot replace this active owner.
 av=1;aid=32'd17;agen=~agen;afirst=~afirst;
 repeat(16)begin @(negedge clk);if(ar||dut.window_id!=32'hffffffff||wstarted!=oldwindows)$fatal(1,"tracking active rearm ownership");checks=checks+1;end
 av=0;oldwrites=writes;oldgen=hgen;inject=1;#1;if(!ir||!restart)$fatal(1,"tracking WAIT source write gate");
 @(negedge clk);inject=0;
 if(writes!=oldwrites+1||hgen!=oldgen+1||hc!=1||active||!request||reason!=1)$fatal(1,"tracking WAIT source fence");fences=fences+1;
 repeat(64)begin @(negedge clk);if(active||locked||tu||dv||wstarted!=oldwindows)$fatal(1,"tracking old generation revival");checks=checks+1;end
 if(!ar||invalid!=2||fences!=2||cancels!=1)$fatal(1,"tracking boundary coverage");
 $display("G0_CAUSAL_TRACKING_BOUNDARY_PASS invalid_arms=%0d source_fences=%0d ISSUE_cancels=%0d stopped_checks=%0d writes=%0d",invalid,fences,cancels,checks,writes);$finish;
end
initial begin #100000;$fatal(1,"tracking boundary timeout");end
endmodule
'''

def main(a):
 out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);bp=a.vivado_bin.resolve();project=Path(__file__).resolve().parents[1]
 before=disk_check('before-causal-tracking-boundaries');(out/'disk-before.json').write_text(json.dumps(before,indent=2)+'\n')
 if before['disk']['available_bytes']<5*GIB:raise SystemExit('Under5GiB: handle storage first.')
 try:
  sources=[]
  for name in SOURCES:
   target=out/name;shutil.copyfile(project/'sim/g0'/name,target);sources.append(target)
  shutil.copyfile(a.reference.resolve()/'coherent_signs.hex',out/'coherent_signs.hex');(out/'tb.sv').write_text(TB)
  def run(step,args):
   with (out/(step+'.log')).open('w') as f:subprocess.run(args,cwd=out,stdout=f,stderr=subprocess.STDOUT,check=True)
  xpm=bp.parent/'data/ip/xpm';run('compile',[str(bp/'xvlog'),'--sv',str(xpm/'xpm_cdc/hdl/xpm_cdc.sv'),str(xpm/'xpm_memory/hdl/xpm_memory.sv'),*map(str,sources),str(out/'tb.sv')]);run('elaborate',[str(bp/'xelab'),'tb','-s','tracking_boundary_tb','-mt','off']);run('simulate',[str(bp/'xsim'),'tracking_boundary_tb','-runall'])
  log=(out/'simulate.log').read_text();assert 'G0_CAUSAL_TRACKING_BOUNDARY_PASS' in log and 'Fatal:' not in log,log[-2500:]
  result={'scope':__doc__,'simulation':next(s for s in log.splitlines() if s.startswith('G0_CAUSAL_TRACKING_BOUNDARY_PASS')),'source_sha256':{s.name:hashlib.sha256(s.read_bytes()).hexdigest() for s in sources},'g0_passed':False}
  (out/'receipt.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result),flush=True)
 finally:(out/'disk-after.json').write_text(json.dumps(disk_check('after-causal-tracking-boundaries'),indent=2)+'\n')

if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);p.add_argument('--reference',type=Path,required=True);p.add_argument('--vivado-bin',type=Path,required=True);main(p.parse_args())
