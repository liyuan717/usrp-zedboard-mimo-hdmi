#!/usr/bin/env python3
"""Restart, cancel and reset during every coherent-window phase, including held publication."""
import argparse,hashlib,json,shutil,subprocess
from pathlib import Path
from workspace_storage_guard import check as disk_check,GIB

TB=r'''`timescale 1ns/1ps
module tb;
reg clk=0;always #10 clk=~clk;
reg rst=1,cancel=0,iv=0,start=0,cv=0,ready=1,ext=0;reg [63:0] tag=0,first=0;reg [79:0] data={20'sd12345,-20'sd23456,-20'sd32123,20'sd4567};reg [31:0] id=0,gen=0;
wire ir,cr,ov,last,restart,busy;wire [79:0] word;wire [31:0] oid,ogen,hgen;wire [63:0] ot,latest;wire [12:0] hc;wire [6:0] point;wire [2:0] error;wire [115:0] c;wire [59:0] n;wire [51:0] e;
coherent_window_cost_probe #(.WAIT_LIMIT(5000)) dut(clk,rst,cancel,iv,ir,start,tag,data,ext,12'd0,word,cv,cr,id,gen,first,7'd3,ov,ready,oid,ogen,ot,point,last,error,c,n,e,hgen,latest,hc,restart,busy);
integer mode,target,k,checks=0,held_heads=0;reg [366:0] held;wire [366:0] view={oid,ogen,ot,point,last,error,c,n,e};
localparam [47:0] BASE=48'hfffffffffe0c;
initial begin
 for(mode=0;mode<3;mode=mode+1) for(target=1;target<=10;target=target+1) begin
  rst=1;cancel=0;iv=0;cv=0;ready=target==8?0:1;repeat(3) @(negedge clk);rst=0;
  for(k=0;k<1000;k=k+1) begin
   tag={16'h3210,BASE+48'(k)};iv=1;start=k==0;@(negedge clk);iv=0;start=0;repeat(7) @(negedge clk);
  end
  id=mode*100+target;gen=hgen;first={16'h3210,BASE+48'd100};cv=1;
  @(posedge clk);while(!cr) @(posedge clk);@(negedge clk);cv=0;
  while(dut.state!=target) @(negedge clk);
  ready=0;if(target==8)begin if(!ov || error) $fatal(1,"missing held head");held=view;held_heads=held_heads+1;end
  if(mode==0) begin
   tag={16'h3210,BASE+48'd1000};iv=1;start=1;@(negedge clk);iv=0;start=0;
   if(target==8) begin
    repeat(17) begin @(negedge clk);if(!ov || view!==held) $fatal(1,"source fence mutated held metric");end
    ready=1;@(negedge clk);ready=0;
   end
   while(!ov) @(negedge clk);
   if(error!=1 || !last || n || e || c || oid!=id || ogen!=gen) $fatal(1,"phase ownership error mode%0d phase%0d err%0d",mode,target,error);
   held=view;repeat(19) begin @(negedge clk);if(!ov || view!==held) $fatal(1,"held phase error");end
   ready=1;@(negedge clk);ready=0;
  end else begin
   if(mode==1)cancel=1;else rst=1;
   #1;if(ov) $fatal(1,"global barrier transfer");@(negedge clk);cancel=0;rst=0;
  end
  repeat(16) begin @(negedge clk);if(ov || busy) $fatal(1,"aborted coherent phase revived");end
  checks=checks+1;
 end
 // Permanent priority occupation is a bounded error, not an immortal job.
 rst=1;iv=0;ready=0;repeat(3) @(negedge clk);rst=0;
 for(k=0;k<1000;k=k+1) begin
  tag={16'h3210,BASE+48'(k)};iv=1;start=k==0;@(negedge clk);iv=0;start=0;repeat(7) @(negedge clk);
 end
 ext=1;id=999;gen=hgen;first={16'h3210,BASE+48'd100};cv=1;
 @(posedge clk);while(!cr) @(posedge clk);@(negedge clk);cv=0;
 while(!ov) @(negedge clk);
 if(error!=3 || !last || n || e || c) $fatal(1,"unbounded sampler starvation");
 ready=1;@(negedge clk);ready=0;ext=0;
 if(checks!=30 || held_heads!=3) $fatal(1,"phase coverage");
 $display("G0_COHERENT_BOUNDARY_PASS phase_barriers=%0d held_publication_cases=%0d priority_starvation_timeout=1",checks,held_heads);$finish;
end
initial begin #20000000;$fatal(1,"coherent boundary timeout");end
endmodule
'''

def main(a):
 out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);bp=a.vivado_bin.resolve();project=Path(__file__).resolve().parents[1]
 before=disk_check('before-coherent-boundaries');(out/'disk-before.json').write_text(json.dumps(before,indent=2)+'\n')
 if before['disk']['available_bytes']<5*GIB:raise SystemExit('Under5GiB: handle storage first.')
 try:
  src=out/'coherent_window_cost_probe.sv';shutil.copyfile(project/'sim/g0'/src.name,src);shutil.copyfile(a.reference.resolve()/'coherent_signs.hex',out/'coherent_signs.hex');(out/'tb.sv').write_text(TB)
  xpm=bp.parent/'data/ip/xpm'
  for step,args in [('compile',[str(bp/'xvlog'),'--sv',str(xpm/'xpm_cdc/hdl/xpm_cdc.sv'),str(xpm/'xpm_memory/hdl/xpm_memory.sv'),str(src),str(out/'tb.sv')]),('elaborate',[str(bp/'xelab'),'tb','-s','boundary_tb','-mt','off']),('simulate',[str(bp/'xsim'),'boundary_tb','-runall'])]:
   with (out/(step+'.log')).open('w') as log:subprocess.run(args,cwd=out,stdout=log,stderr=subprocess.STDOUT,check=True)
  log=(out/'simulate.log').read_text();assert 'G0_COHERENT_BOUNDARY_PASS' in log and 'Fatal:' not in log,log[-2500:]
  r={'scope':__doc__,'simulation':next(s for s in log.splitlines() if s.startswith('G0_COHERENT_BOUNDARY_PASS')),'source_sha256':hashlib.sha256(src.read_bytes()).hexdigest(),'g0_passed':False};(out/'receipt.json').write_text(json.dumps(r,indent=2)+'\n');print(json.dumps(r))
 finally:(out/'disk-after.json').write_text(json.dumps(disk_check('after-coherent-boundaries'),indent=2)+'\n')

if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);p.add_argument('--reference',type=Path,required=True);p.add_argument('--vivado-bin',type=Path,required=True);main(p.parse_args())
