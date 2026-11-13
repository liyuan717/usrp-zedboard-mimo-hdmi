#!/usr/bin/env python3
"""Actual observed ML nibbles -> dual VRL3/CRC buffers -> atomic packet bytes.
Uses passed RTL decisions, independently packed/zlib-parsed packet oracles.
No source/gold bytes configure the DUT. Not FEC/DMA or full G0.
"""
import argparse,hashlib,json,os,re,shutil,subprocess
from pathlib import Path
import numpy as np
from g0_packet_probe import parse_link
from workspace_storage_guard import check as disk_check,GIB
SOURCES=['link_packet_cost_probe.sv','payload_link_cost_probe.sv']

TB=r'''`timescale 1ns/1ps
module tb;
reg clk=0;always #10 clk=~clk;reg rst=1,cancel=0,cv=0,iv=0,rr=0,ready=0;
wire cr,ir,rv,ov,last,done,ab;wire [2:0] detail,slot,err;wire tx;wire [255:0] header;wire [15:0] size,oi,ep;wire [7:0] od;wire [31:0] rid,rctx,rgen;wire [4:0] good,bad;
reg [31:0] id,ctx,gen,iid,ictx,igen;reg [15:0] epoch,iep,l0=4384,l1=4384;reg [31:0] sid,sf;reg [14:0] symbol;reg [3:0] decision;
payload_link_cost_probe dut(clk,rst,cancel,cv,cr,id,ctx,gen,epoch,sid,sf,l0,l1,iv,ir,iid,ictx,igen,iep,symbol,decision,
 rv,rr,rid,rctx,rgen,ep,tx,slot,err,header,size,ov,ready,od,oi,last,done,ab,detail,good,bad);
reg [3:0] nibbles[0:368255];reg [7:0] packets[0:184127];reg [255:0] headers[0:335];reg [2:0] errors[0:335];reg [31:0] sessions[0:20],superframes[0:20];
integer j=0,f=0,mode=0,k,cycle=0,begin_cycle,elapsed,idx,di=0,result_count=0,case_good=0,case_bad=0,total_good=0,total_bad=0,checked=0,holds=0,cancelled=0,complete=0,hold_started=-1;
reg [15:0] seen;reg metadata=0,rheld=0,dheld=0,cut=0;reg [390:0] rview;reg [24:0] dview;
wire [390:0] current_rview={rid,rctx,rgen,ep,tx,slot,err,header,size};
always @(negedge clk)begin
 rr=cycle%13>3;ready=cycle%17>5;
 if(mode==2&&hold_started>=0&&cycle-hold_started<6000)rr=0;
 if(mode==5&&rv)begin rr=0;cut=1;end
 if((mode==6&&ov)||(mode==7&&ov&&di==128))begin ready=0;cut=1;end
end
always @(posedge clk)begin
 cycle=cycle+1;
 if(rst||cancel)begin metadata=0;rheld=0;dheld=0;end
 else begin
  if(mode==2&&rv&&hold_started<0)hold_started=cycle;
  if(rheld&&(!rv||current_rview!==rview))$fatal(1,"payload link held packet owner changed");
  if(dheld&&(!ov||{last,oi,od}!==dview))$fatal(1,"payload link held byte changed");
  if(rv)begin
   if({rid,rctx,rgen,ep}!=={id,ctx,gen,epoch}||err!==errors[f*16+tx*8+slot]||header!==headers[f*16+tx*8+slot]||size!=(err==0?512:0))$fatal(1,"payload link metadata/CRC oracle case%0d TX%0d slot%0d err%0d",j,tx,slot,err);
   if(rr)begin
    if(seen[tx*8+slot]||metadata)$fatal(1,"payload link packet overlap/duplicate");seen[tx*8+slot]=1;
    idx=f*16+tx*8+slot;di=0;metadata=err==0;result_count=result_count+1;
    if(err==0)begin case_good=case_good+1;total_good=total_good+1;end else begin case_bad=case_bad+1;total_bad=total_bad+1;end
   end
  end
  if(ov)begin
   if(!metadata||oi!=di||od!==packets[idx*548+32+di]||last!==(di==511))$fatal(1,"payload link byte oracle case%0d packet%0d byte%0d",j,idx,di);
   if(ready)begin di=di+1;checked=checked+1;if(last)metadata=0;end
  end
  rheld=rv&&!rr;if(rheld)begin rview=current_rview;holds=holds+1;end
  dheld=ov&&!ready;if(dheld)begin dview={last,oi,od};holds=holds+1;end
 end
end
task revoke;
 begin cancel=1;iv=0;cv=0;#1;if(cr||ir||rv||ov||done)$fatal(1,"payload link current cancellation fence");
 @(negedge clk);cancel=0;repeat(2)@(negedge clk);if(!cr||rv||ov||done||ab)$fatal(1,"payload link dirty restart");cancelled=cancelled+1;end
endtask
task run_case(input integer number,input integer fixture,input integer kind);
 begin
 j=number;f=fixture;mode=kind;seen=0;result_count=0;case_good=0;case_bad=0;hold_started=-1;cut=0;
 id=32'hfffffff0+32'(j);ctx=32'h80000000^32'(j);gen=32'hc0000000+32'(j);epoch=16'hfff0+16'(j);sid=sessions[f];sf=superframes[f];l0=mode==8?4383:4384;l1=4384;
 while(!cr)@(negedge clk);cv=1;@(negedge clk);cv=0;begin_cycle=cycle;
 for(k=0;k<17536;k=k+1)begin
  if(mode==3&&k==3||mode==4&&dut.pair_pending||cut)break;
  if(mode==8)break;
  iid=id;ictx=ctx;igen=gen;iep=epoch;symbol=15'(k);decision=nibbles[f*17536+k];
  if(k==32&&mode>=20&&mode<=24)case(mode)
   20:iid=id^32'h80000000;21:ictx=ctx^32'h80000000;22:igen=gen^32'h80000000;23:iep=epoch^16'h8000;24:symbol=15'(k+1);
  endcase
  iv=1;@(posedge clk);while(!ir&&!cut)@(posedge clk);@(negedge clk);iv=0;
  if(ab||cut)break;
 end
 if(mode>=3)begin
  if(mode==3&&dut.dibit!=3||mode==4&&!dut.pair_pending||mode==5&&(!rv||rr)||mode==6&&(!ov||di!=0)||mode==7&&(!ov||di!=128))$fatal(1,"payload link cancel target missing mode%0d",mode);
  $display("PAYLOAD_LINK_CANCEL mode=%0d accepted_symbols=%0d packets=%0d payload_index=%0d",mode,dut.symbols,result_count,di);
  if(mode==8||mode>=20)begin if(!ab||detail!=(mode==8?2:1))$fatal(1,"payload link framing identity fault");end
  revoke();return;
 end
 while(!done)@(negedge clk);elapsed=cycle-begin_cycle;
 if(ab||seen!=16'hffff||result_count!=16||metadata||good!=case_good||bad!=case_bad||case_good+case_bad!=16||elapsed>100000)$fatal(1,"payload link full frame census/service case%0d",j);
 complete=complete+1;$display("PAYLOAD_LINK_CASE case=%0d fixture=%0d good=%0d erased=%0d whole_frame_cycles=%0d",j,f,case_good,case_bad,elapsed);
 end
endtask
initial begin
 $readmemh("nibbles.hex",nibbles);$readmemh("packets.hex",packets);$readmemh("headers.hex",headers);$readmemh("errors.hex",errors);$readmemh("sessions.hex",sessions);$readmemh("superframes.hex",superframes);
 repeat(4)@(negedge clk);rst=0;
 for(integer n=0;n<21;n=n+1)run_case(n,n,0);
 run_case(21,0,2);for(integer n=3;n<=8;n=n+1)run_case(19+n,0,n);
 for(integer n=20;n<=24;n=n+1)run_case(8+n,0,n);run_case(33,0,0);
 if(complete!=23||cancelled!=11)$fatal(1,"payload link global census");
 $display("G0_PAYLOAD_LINK_PASS complete_frames=%0d cancelled=%0d accepted_packets=%0d erased_packets=%0d exact_payload_bytes=%0d held_checks=%0d",complete,cancelled,total_good,total_bad,checked,holds);$finish;
end
initial begin #2000000000;$fatal(1,"payload link timeout case%0d symbols%0d slots%0d/%0d",j,dut.symbols,dut.slot0,dut.slot1);end
endmodule
'''

def main(a):
 project=Path(__file__).resolve().parents[1];out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);ref=a.reference.resolve();bp=a.vivado_bin.resolve()
 before=disk_check('before-payload-link');(out/'disk-before.json').write_text(json.dumps(before,indent=2)+'\n')
 if before['disk']['available_bytes']<5*GIB:raise SystemExit('Under5GiB: handle storage first.')
 try:
  passed=json.loads((ref/'receipt.json').read_text());assert passed['simulation'].startswith('G0_FRAME_PAYLOAD_PASS')
  observed=[s.split() for s in (ref/'observed.txt').read_text().splitlines()];gold=[];packets=[];headers=[];errors=[];sessions=[];sfs=[]
  tb=(ref/'tb.sv').read_text();rows=[]
  for f in range(21):
   data=[v for v in observed if int(v[0])==f];assert len(data)==17536 and all(int(v[1])==i for i,v in enumerate(data))
   d=np.array([int(v[2],16) for v in data],dtype=np.uint8);gold.extend(d)
   raw=int(re.search(r'headers\['+str(f)+r"\]=192'h([0-9a-f]+)",tb)[1],16).to_bytes(24,'little');session=int.from_bytes(raw[4:8],'little');sf=int.from_bytes(raw[8:12],'little');sessions.append(session);sfs.append(sf);good=0
   for tx in (0,1):
    bits=np.stack([(d>>(2*tx))&1,(d>>(2*tx+1))&1],axis=1).ravel();payload=np.packbits(bits,bitorder='big').tobytes()
    for slot in range(8):
     p=payload[slot*548:(slot+1)*548];body,error=parse_link(p,session,sf,sf*16+slot*2+tx);packets.extend(p);headers.append(int.from_bytes(p[:32],'little'));errors.append(0 if error is None else (1 if error=='CRC' else 2));good+=body is not None
   rows.append({'fixture':f,'accepted':good,'erased':16-good})
  assert sum(x['accepted'] for x in rows)==310
  for name,values,width in [('nibbles',gold,1),('packets',packets,2),('headers',headers,64),('errors',errors,1),('sessions',sessions,8),('superframes',sfs,8)]:
   (out/(name+'.hex')).write_text(''.join(f'{int(v):0{width}x}\n' for v in values))
  (out/'tb.sv').write_text(TB)
  for n in SOURCES:shutil.copyfile(project/'sim/g0'/n,out/n)
  for label,args in [('compile',[str(bp/'xvlog'),'--sv',*SOURCES,'tb.sv']),('elaborate',[str(bp/'xelab'),'tb','-s','payload_link_tb','-mt','off']),('simulate',[str(bp/'xsim'),'payload_link_tb','-runall'])]:
   with (out/(label+'.log')).open('w') as f:subprocess.run(args,cwd=out,stdout=f,stderr=subprocess.STDOUT,check=True)
  log=(out/'simulate.log').read_text();assert 'G0_PAYLOAD_LINK_PASS' in log and 'Fatal:' not in log,log[-5000:]
  result={'scope':__doc__,'simulation':next(s for s in log.splitlines() if s.startswith('G0_PAYLOAD_LINK_PASS')),'cases':[s for s in log.splitlines() if s.startswith('PAYLOAD_LINK_CASE')],'fixture_packets':rows,'actual_ML_reference':str(ref.relative_to(project)),'source_sha256':{n:hashlib.sha256((out/n).read_bytes()).hexdigest() for n in SOURCES},'g0_passed':False}
  (out/'receipt.json').write_text(json.dumps(result,indent=2)+'\n');print(result['simulation'],flush=True)
 finally:(out/'disk-after.json').write_text(json.dumps(disk_check('after-payload-link'),indent=2)+'\n')

if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__)
 for name in ('output','reference','vivado-bin'):p.add_argument('--'+name,type=Path,required=True)
 main(p.parse_args())
