#!/usr/bin/env python3
"""Scheduler domain: all512 peak offsets, modulo tags, errors and cancellation."""
import argparse,json,shutil,subprocess,hashlib
from pathlib import Path
TB=r'''`timescale 1ns/1ps
module tb;
reg clk=0;always #10 clk=~clk;
reg rst=1,cancel=0,cv=0,cfgready=0,applied=0,window=0,restart=0,jready=0,rv=0,ready=0,committed=1;
reg [31:0] cid=0,cgen=0,cp=0,cs=0,rid=0,rctx=0,rgen=0;reg [63:0] first=0,peak=0,known=0;reg [47:0] origin=0;
reg sense=0;reg [2:0] cerr=0,rerr=0,rdetail=0;reg [1:0] cfgerr=0;
wire cr,cfgvalid,jsense,jvalid,rr,ov,idle;wire [63:0] cfg_tag,commit;wire [31:0] cfg_phase,cfg_step,jid,jctx,jgen,oid,ogen,ostep,ophase;wire [15:0] epoch;wire [79:0] position;wire [34:0] step;wire [8:0] count;wire [9:0] symbol;wire [47:0] jorigin;wire [2:0] error,detail;
coarse_startup_schedule_cost_probe dut(clk,rst,cancel,cv,cr,cid,cgen,first,peak,known,cs,cp,sense,cerr,origin,
cfgvalid,cfgready,applied,cfgerr,cfg_tag,cfg_phase,cfg_step,jsense,window,restart,32'd23,32'd17,jvalid,jready,jid,jctx,jgen,epoch,position,step,count,symbol,commit,jorigin,
rv,rr,rid,rctx,rgen,rerr,rdetail,committed,32'h11223344,32'h55667788,ov,ready,oid,ogen,error,detail,ostep,ophase,idle);
integer fd,n,mode,k=0,expected_symbol,expected_count,expected_error,expected_detail,outputs=0,cancels=0,jobs=0;
reg [63:0] f,p,q,expected_commit;reg [79:0] expected_position;
reg [31:0] held_id,held_gen;reg [2:0] held_error,held_detail;
task revoke;
 begin
  cancel=1;@(posedge clk);if(cfgvalid || jvalid || cr) $fatal(1,"cancel admission");
  @(negedge clk);cancel=0;cv=0;rv=0;cfgready=0;jready=0;repeat(3) @(negedge clk);
  if(!idle || ov || cfgvalid || jvalid) $fatal(1,"cancel revoke");cancels=cancels+1;
 end
endtask
initial begin
fd=$fopen("vectors.txt","r");repeat(4) @(negedge clk);rst=0;
while(!$feof(fd)) begin
 n=$fscanf(fd,"%d %h %h %h %d %d %h %h %d %d\n",mode,f,p,q,expected_symbol,expected_count,expected_position,expected_commit,expected_error,expected_detail);if(n!=10) $fatal(1,"parse");
 cv=1;cid=k+100;cgen=32'hfffffe00+k;first=f;peak=p;known=q;cp=k*12345;cs=~cp;sense=k%2;cerr=mode==1?2:0;origin=48'h123456789abc;
 cfgready=0;applied=0;cfgerr=0;window=0;restart=0;jready=0;rv=0;ready=0;committed=1;rerr=0;rdetail=0;
 @(posedge clk);while(!cr) @(posedge clk);@(negedge clk);cv=0;first=~f;peak=~p;known=~q;origin=0;
 if(mode==1 || mode==2) begin end
 else begin
  if(!cfgvalid || cfg_tag!==q || cfg_phase!==32'(k*12345) || cfg_step!==~32'(k*12345) || jsense!==1'(k%2)) $fatal(1,"coarse descriptor latch");
  if(mode==10) begin revoke;k=k+1;continue;end
  repeat(3) @(negedge clk);cfgready=1;applied=!(mode==3 || mode==4 || mode==11);
  @(posedge clk);@(negedge clk);cfgready=0;applied=0;cfgerr=mode==4?2:1;
  if(mode==11) begin revoke;k=k+1;continue;end
  if(mode!=3 && mode!=4) begin
   if(mode==12) begin revoke;k=k+1;continue;end
   window=1;restart=1;repeat(3) @(negedge clk);if(jvalid) $fatal(1,"restart bound too soon");restart=0;
   while(!jvalid) @(negedge clk);
   repeat(4) begin @(posedge clk);
    if(!jvalid || symbol!=expected_symbol || count!=expected_count || position!==expected_position || commit!==expected_commit || jctx!=23 || jgen!=17 || jid!=k+100 || epoch!==p[63:48] || jorigin!==48'h123456789abc || step!=35'd17179869184) $fatal(1,"job crop/vector %0d sym%0d/%0d count%0d/%0d",k,symbol,expected_symbol,count,expected_count);
    @(negedge clk);
   end
   if(mode==13) begin revoke;k=k+1;continue;end
   jready=1;@(posedge clk);@(negedge clk);jready=0;jobs=jobs+1;
   if(mode==14) begin revoke;k=k+1;continue;end
   rid=k+100;rctx=23;rgen=17;rv=1;
   if(mode==5) rid=rid^1;if(mode==6) rctx=24;if(mode==7) rgen=18;
   if(mode==8) begin rerr=4;rdetail=3;end
   if(mode==9) committed=0;
   @(posedge clk);while(!rr) @(posedge clk);@(negedge clk);rv=0;
  end
 end
 while(!ov) @(negedge clk);
 if(oid!=k+100 || ogen!==32'(32'hfffffe00+k) || error!=expected_error || detail!=expected_detail ||
    ostep!==(expected_error?32'd0:32'h11223344) || ophase!==(expected_error?32'd0:32'h55667788)) $fatal(1,"terminal %0d error%0d:%0d/%0d:%0d",k,error,detail,expected_error,expected_detail);
 held_id=oid;held_gen=ogen;held_error=error;held_detail=detail;
 repeat(7) begin @(posedge clk);if(!ov || cr || oid!==held_id || ogen!==held_gen || error!==held_error || detail!==held_detail) $fatal(1,"held");@(negedge clk);end
 outputs=outputs+1;
 if(mode==15) revoke;
 else begin ready=1;@(negedge clk);ready=0;end
 k=k+1;
end
if(k!=527 || outputs!=522 || cancels!=6 || jobs!=519) $fatal(1,"coverage %0d/%0d/%0d/%0d",k,outputs,cancels,jobs);
$display("G0_COARSE_STARTUP_SCHEDULE_PASS descriptors=%0d outputs=%0d cancellations=%0d jobs=%0d",k,outputs,cancels,jobs);$finish;
end
initial begin #10000000;$fatal(1,"timeout");end
endmodule
'''
def main(a):
 out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);project=Path(__file__).resolve().parents[1];rows=[]
 for n in range(527):
  mode=0 if n<512 else n-511;offset=n%512;first=((1<<48)-300+n*7919)&((1<<48)-1);peak=(first+offset)&((1<<48)-1);known=(first+1152)&((1<<48)-1);epoch=(n%7+20)<<48
  valid=[m for m in range(416,864) if 20+4*(m-256)>1152-offset+82];symbol=valid[0];count=len(valid);position=((peak+4*(symbol-256))&((1<<48)-1))<<32;commit=(epoch|((peak+2960)&((1<<48)-1)))
  if mode==2:known=(known+1)&((1<<48)-1)
  err,detail={1:(1,2),2:(2,1),3:(3,1),4:(3,2),5:(5,1),6:(5,1),7:(5,1),8:(4,3),9:(5,2)}.get(mode,(0,0))
  rows.append(f'{mode} {epoch|first:016x} {epoch|peak:016x} {epoch|known:016x} {symbol} {count} {position:020x} {commit:016x} {err} {detail}\n')
 (out/'vectors.txt').write_text(''.join(rows));(out/'tb.sv').write_text(TB);src=out/'coarse_startup_schedule_cost_probe.sv';shutil.copyfile(project/'sim/g0'/src.name,src);bp=a.vivado_bin.resolve()
 def run(n,args):
  with (out/(n+'.log')).open('w') as f:subprocess.run(args,cwd=out,stdout=f,stderr=subprocess.STDOUT,check=True)
 run('compile',[str(bp/'xvlog'),'--sv',str(src),str(out/'tb.sv')]);run('elaborate',[str(bp/'xelab'),'tb','-s','schedule_tb','-mt','off']);run('simulate',[str(bp/'xsim'),'schedule_tb','-runall']);log=(out/'simulate.log').read_text();assert 'G0_COARSE_STARTUP_SCHEDULE_PASS' in log and 'Fatal:' not in log,log[-1800:]
 for name,old,new in [('crop',"49'd257","49'd256"),('commit',"48'd2960","48'd2959")]:
  mutant=out/(name+'.sv');mutant.write_text(src.read_text().replace('module coarse_startup_schedule_cost_probe','module '+name+'_schedule').replace(old,new));tb=out/(name+'_tb.sv');tb.write_text(TB.replace('module tb;','module '+name+'_tb;').replace('coarse_startup_schedule_cost_probe dut',name+'_schedule dut'))
  run(name+'-compile',[str(bp/'xvlog'),'--sv',str(mutant),str(tb)]);run(name+'-elaborate',[str(bp/'xelab'),name+'_tb','-s',name+'_tb','-mt','off']);run(name+'-simulate',[str(bp/'xsim'),name+'_tb','-runall']);l=(out/(name+'-simulate.log')).read_text();assert 'Fatal: job crop/vector' in l and 'G0_COARSE_STARTUP_SCHEDULE_PASS' not in l
 r={'scope':__doc__,'simulation':log[log.index('G0_COARSE_STARTUP_SCHEDULE_PASS'):].splitlines()[0],'all512_offsets':True,'M_range':[304,432],'negative_controls':['warmup strict-bound off-by-one','commit off-by-one'],'source_sha256':hashlib.sha256(src.read_bytes()).hexdigest(),'g0_passed':False};(out/'receipt.json').write_text(json.dumps(r,indent=2)+'\n');print(json.dumps(r))
if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);p.add_argument('--vivado-bin',type=Path,required=True);main(p.parse_args())
