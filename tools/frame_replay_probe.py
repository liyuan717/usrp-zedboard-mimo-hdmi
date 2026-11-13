#!/usr/bin/env python3
"""Actual ONE shared XPM pool/F1 sampler -> protected header + pilot phase tracking.
Independent capture sample/complex/OLS oracles, missing/expired history and fences.
No live ADC or payload detector claim; live acquisition is a separate probe.
"""
import argparse,hashlib,json,os,shutil,subprocess
from pathlib import Path
import numpy as np
from bpsk_header_probe import unpack,pack
from pilot_smoothing_probe import smooth
from pilot_tracking_probe import SOURCES as TRACKING
from workspace_storage_guard import check as disk_check,GIB
SOURCES=['coherent_window_cost_probe.sv','interpolation_cost_probe.sv','timing_request_cost_probe.sv']+TRACKING+['frame_replay_cost_probe.sv']
TB=r'''`timescale 1ns/1ps
module tb;
reg clk=0;always #10 clk=~clk;
reg rst=1,cancel=0,cv=0,rv=0,start=0,wr=1,hr=0,pr=0,sr=0,hold_phase=0;
reg [31:0] ci=0,ctx=0,cc=0,cg=0;reg [15:0] ep=0,ce=0;reg [47:0] base=0;
reg [63:0] tag=0;reg [79:0] data=0,pos=0;reg [34:0] step=0;reg [207:0] h=0;reg [63:0] steps=0;
wire pool_ready,restart,re,active,cr,hv,pv,sv,ptx,fabort;wire [11:0] ra;wire [79:0] word;
wire [31:0] gen,hi,hc,hg,sid,sf,pi,pc,pg,si,sc,sg;wire [63:0] latest;wire [12:0] count;
wire [15:0] hepoch,l0,l1,pepoch,sepoch;wire [2:0] herr,hd,serr,sd;wire [191:0] raw;
wire [8:0] point,known,sp;wire signed [40:0] phi;
coherent_window_cost_probe pool(.clk(clk),.rst(rst),.cancel(1'b0),.in_valid(rv),.in_ready(pool_ready),.in_start(start),.in_tag(tag),.in_data(data),
.cfg_valid(1'b0),.cfg_ready(),.cfg_id(32'd0),.cfg_generation(32'd0),.cfg_first_tag(64'd0),.cfg_points(7'd1),
.out_valid(),.out_ready(1'b1),.out_id(),.out_generation(),.out_tag(),.out_point(),.out_c(),.out_n(),.out_e(),.out_error(),.out_last(),.busy(),
.history_generation(gen),.latest_tag(latest),.history_count(count),.history_restart(restart),.external_read_enable(re),.external_read_address(ra),.read_word(word));
frame_replay_cost_probe dut(.clk(clk),.rst(rst),.cancel(cancel),.cfg_valid(cv),.cfg_ready(cr),.cfg_id(ci),.cfg_context(cc),.cfg_generation(cg),.cfg_epoch(ce),.cfg_training_position(pos),.cfg_symbol_step(step),.cfg_h(h),.cfg_phase_steps(steps),
.window_ready(wr),.current_context(ctx),.rrc_valid(rv),.rrc_start(start),.shared_ready(pool_ready),.rrc_tag(tag),.rrc_data(data),.history_generation(gen),.latest_tag(latest),.history_count(count),.history_restart(restart),
.reader_active(active),.read_enable(re),.read_address(ra),.read_word(word),.header_valid(hv),.header_ready(hr),.header_id(hi),.header_context(hc),.header_generation(hg),.header_epoch(hepoch),.header_error(herr),.header_detail(hd),.header_raw(raw),.header_session(sid),.header_superframe(sf),.header_stream0_bytes(l0),.header_stream1_bytes(l1),
.phase_valid(pv),.phase_ready(pr),.phase_id(pi),.phase_context(pc),.phase_generation(pg),.phase_epoch(pepoch),.phase_point(point),.phase_known_point(known),.phase_tx(ptx),.phase_value(phi),
.frame_abort(fabort),.status_valid(sv),.status_ready(sr),.status_id(si),.status_context(sc),.status_generation(sg),.status_epoch(sepoch),.status_error(serr),.status_detail(sd),.status_point(sp));
reg [79:0] rrc[0:@RLAST@];reg [83:0] vectors[0:19571];reg [230:0] complex_gold[0:11507];reg [59:0] phase_gold[0:11507];
integer off[0:20],lo[0:20],pre[0:20],last[0:20];reg [79:0] training[0:20],peak[0:20];reg [34:0] st[0:20];reg [207:0] ch[0:20];reg [63:0] ph[0:20];reg [191:0] headers[0:20];
integer rrc_writes=0;integer j=0,model=0,mode=0,cycle=0,k,n,index=0,phase_count=0,phase_consumed=0,header_count=0,total_samples=0,total_phases=0,total_headers=0,total_status=0,total_cancelled=0,held_checks=0,max_read_age=0,age=0,hhwait=0;
reg [31:0] accepted_id,accepted_context,accepted_gen;reg [15:0] accepted_epoch;
reg seen=0,hseen=0,held=0,hheld=0;reg [405:0] held_header;wire [405:0] header_view={hi,hc,hg,hepoch,herr,hd,raw,sid,sf,l0,l1};reg [171:0] held_view;wire [171:0] view={pepoch,pg,pc,pi,ptx,known,point,phi};
reg [79:0] coordinate;reg [80:0] rounded;integer relative;
always @(negedge clk)begin
 if(!hv||rst||cancel||mode==9)begin hr=0;hhwait=0;end
 else if(hhwait==37)hr=1;else begin hr=0;hhwait=hhwait+1;end
 pr=!hold_phase && cycle%7>2;
end
always @(posedge clk)begin
 cycle=cycle+1;if(rv&&pool_ready)rrc_writes=rrc_writes+1;
 if(!rst&&!cancel&&held&&!pv&&!fabort)$fatal(1,"frame held phase withdrawn without public abort");
 if(!rst&&!cancel&&hheld&&!hv&&!fabort)$fatal(1,"frame held header withdrawn without public abort");
 if(!rst&&!cancel&&mode==6&&dut.piloting)$fatal(1,"frame rejected header admitted pilots");
 if(rst||cancel||dut.abort_work||dut.smooth_abort)begin seen=0;held=0;hheld=0;end
 else begin
  if(dut.sample_valid&&dut.sample_ready&&dut.sampling&&dut.sample_error==0)begin
   if(dut.state==2)begin index=model*932+dut.received;coordinate=training[model]+80'(992+dut.received)*st[model];end
   else begin index=model*932+384+2*dut.point+dut.tx;coordinate=peak[model]+80'(1536+66*dut.point+dut.tx)*st[model];end
   rounded={1'b0,coordinate}+81'd32768;
   if(dut.sample_bad||dut.sample_tag!=={ep,base+rounded[79:32]}||dut.sample_mu!==rounded[31:16]||dut.sample_data!==vectors[index])$fatal(1,"frame shared F1 oracle case%0d state%0d index%0d",j,dut.state,index);
   total_samples=total_samples+1;age=48'(latest[47:0]-dut.sample_tag[47:0]);if(age>max_read_age)max_read_age=age;
  end
  if(mode!=6&&mode!=7&&dut.tracking.math.math_valid&&dut.tracking.math.math_pilot&&dut.tracking.math.math_error==0)begin
   index=model*548+2*dut.point+dut.tx;
   if({dut.tracking.math.math_imag,dut.tracking.math.math_real}!==complex_gold[index][135:0])$fatal(1,"frame shared full complex oracle");
  end
  if(hv&&!hseen)begin
   if(hi!==accepted_id||hc!==accepted_context||hg!==accepted_gen||hepoch!==accepted_epoch)$fatal(1,"frame header identity");
   if(mode==6)begin if(herr!=1||{sid,sf,l0,l1}!==96'd0)$fatal(1,"frame rejected header metadata");end
   else if(herr||hd||raw!==headers[model]||sid!==raw[63:32]||sf!==raw[95:64]||l0!=4384||l1!=4384)$fatal(1,"frame header oracle");
   hseen=1;header_count=header_count+1;total_headers=total_headers+1;
  end
  if(hheld&&(!hv||header_view!==held_header))$fatal(1,"frame held header changed");
  hheld=hv&&!hr;if(hheld)begin held_header=header_view;held_checks=held_checks+1;end
  if(fabort)begin seen=0;held=0;end else begin
  if(pv&&!seen)begin
   if(phase_count>=548||{ptx,known,point,phi}!==phase_gold[model*548+phase_count]||pi!==accepted_id||pc!==accepted_context||pg!==accepted_gen||pepoch!==accepted_epoch)$fatal(1,"frame shared phase Fraction oracle case%0d point%0d",j,phase_count);
   seen=1;phase_count=phase_count+1;total_phases=total_phases+1;
  end
  if(held&&(!pv||view!==held_view))$fatal(1,"frame held phase changed");
  if(pv&&pr)begin seen=0;phase_consumed=phase_consumed+1;end
  held=pv&&!pr;if(held)begin held_view=view;held_checks=held_checks+1;end
  end
 end
end
task put(input integer r);
 begin
 rv=1;start=r==0;tag={ep,base+48'(lo[model]+r)};data=rrc[off[model]+r];
 @(posedge clk);while(!pool_ready)@(posedge clk);@(negedge clk);rv=0;start=0;
 end endtask
task run_case(input integer number,input integer source,input integer kind);
 integer expected_error,expected_detail,stale_writes;reg [126:0] held_status;reg [47:0] stale_tag;begin
 j=number;model=source;mode=kind;ctx=32'h80000000^number;cc=ctx;ep=16'hffff+number;base=48'hffffffff0000+48'(111*number);wr=1;sr=0;hold_phase=(mode==2||mode==8||mode==20);phase_count=0;phase_consumed=0;header_count=0;hseen=0;seen=0;held=0;hheld=0;
 for(k=0;k<pre[model];k=k+1)put(k);
 ci=32'hfffffff0+number;cg=mode==5?gen^32'h80000000:gen;ce=ep;step=mode==4?35'd17176433209:st[model];h=ch[model];steps=ph[model];pos={base,32'd0}+training[model];
 if(mode==6)for(integer v=0;v<4;v=v+1)h[v*26+:26]=-$signed(h[v*26+:26]);
 if(mode==7)h[207:104]=0;
 accepted_id=ci;accepted_context=cc;accepted_gen=cg;accepted_epoch=ce;cv=1;
 @(posedge clk);while(!cr)@(posedge clk);@(negedge clk);cv=0;ci=~ci;cc=~cc;cg=~cg;ce=~ce;pos=~pos;step=~step;h=~h;steps=~steps;
 fork
  begin if(mode!=3)for(n=pre[model];n<=last[model];n=n+1)begin put(n);repeat(j%2?11+n%2:7)@(negedge clk);end end
  begin
   if(mode==1)begin while(!(dut.state==4&&dut.point==8))@(negedge clk);ctx=~ctx;end
   if(mode==2)begin while(!pv)@(negedge clk);stale_tag=dut.pilot_position[79:32];stale_writes=rrc_writes;while(rrc_writes-stale_writes<4600)@(negedge clk);if(48'(latest[47:0]-stale_tag)<4096)$fatal(1,"frame stale fault not induced");$display("FRAME_STALE_RELEASE distance=%0d point=%0d tx=%0d",48'(latest[47:0]-stale_tag),dut.point,dut.tx);hold_phase=0;end
   if(mode>=10)begin
    if(mode<=16)while(dut.sampler.state!=mode-9)@(negedge clk);
    if(mode==17)while(!(dut.tracking.math.math_core.state==5&&dut.tracking.math.math_core.issued==4))@(negedge clk);
    if(mode==18)while(!(dut.tracking.math.observation.angle.busy&&dut.tracking.math.observation.angle.iteration==15))@(negedge clk);
    if(mode==19)while(!(dut.tracking.smoothing.state==4&&dut.tracking.smoothing.iteration==24))@(negedge clk);
    if(mode==20)while(!pv)@(negedge clk);
    cancel=1;#1;if(hv||pv||sv||re)$fatal(1,"frame cancellation edge");repeat(2)@(negedge clk);cancel=0;total_cancelled=total_cancelled+1;
   end
  end
 join
 if(mode>=10)begin repeat(64)begin @(negedge clk);if(hv||pv||sv||active||re||!cr)$fatal(1,"frame cancelled state revived");end end
 else begin
  while(!sv)@(negedge clk);
  expected_error=0;expected_detail=0;
  case(mode)
   1,5:expected_error=4;
   2:begin expected_error=2;expected_detail=2;end
   3:begin expected_error=2;expected_detail=3;end
   4:begin expected_error=5;expected_detail=2;end
   6:begin expected_error=1;expected_detail=1;end
   7:begin expected_error=3;expected_detail=1;end
   8:begin expected_error=3;expected_detail=3;end
   9:expected_error=6;
  endcase
  if(si!==accepted_id||sc!==accepted_context||sg!==accepted_gen||sepoch!==accepted_epoch||serr!=expected_error||sd!=expected_detail||active||re||pv)$fatal(1,"frame terminal status case%0d actual%0d:%0d expected%0d:%0d",j,serr,sd,expected_error,expected_detail);
  if(mode==0&&(phase_count!=548||phase_consumed!=548||header_count!=1))$fatal(1,"frame complete census");
  held_status={si,sc,sg,sepoch,serr,sd,sp};repeat(53)begin @(negedge clk);if(!sv||{si,sc,sg,sepoch,serr,sd,sp}!==held_status||re||active)$fatal(1,"frame held status changed");end
  sr=1;@(negedge clk);sr=0;repeat(3)@(negedge clk);if(!cr||hv||pv||sv||active||re)$fatal(1,"frame terminal ownership not idle");total_status=total_status+1;
 end
 $display("FRAME_REPLAY_CASE case=%0d model=%0d mode=%0d phases=%0d consumed=%0d headers=%0d",j,model,mode,phase_count,phase_consumed,header_count);
 end endtask
initial begin
 $readmemh("rrc_rows.hex",rrc);$readmemh("vectors.hex",vectors);$readmemh("pilot_gold.hex",complex_gold,0,11507);$readmemh("phase_gold.hex",phase_gold);
 @INIT@
 repeat(4)@(negedge clk);rst=0;
 @CASES@
 if(total_status!=31||total_cancelled!=11||total_phases<22*548||total_samples<22*932||max_read_age>=4096||held_checks<100000)$fatal(1,"frame shared coverage status%0d cancelled%0d phases%0d samples%0d age%0d",total_status,total_cancelled,total_phases,total_samples,max_read_age);
 $display("G0_FRAME_REPLAY_PASS complete_frames=22 terminal_status=%0d cancelled=%0d exact_samples=%0d exact_phases=%0d headers=%0d maximum_read_age=%0d held_checks=%0d",total_status,total_cancelled,total_samples,total_phases,total_headers,max_read_age,held_checks);$finish;
end
initial begin #2000000000;$fatal(1,"frame replay timeout case%0d mode%0d state%0d point%0d",j,mode,dut.state,dut.point);end
endmodule
'''
def main(a):
 project=Path(__file__).resolve().parents[1];out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);ref=a.fixture_directory.resolve();mathref=a.math_reference.resolve();headerref=a.header_reference.resolve();bp=a.vivado_bin.resolve()
 before=disk_check('before-frame-replay');(out/'disk-before.json').write_text(json.dumps(before,indent=2)+'\n')
 if before['disk']['available_bytes']<5*GIB:raise SystemExit('Under5GiB: handle storage first.')
 try:
  meta=json.loads((ref/'fixture_metadata.json').read_text());headers=json.loads((headerref/'receipt.json').read_text())['actual_capture_headers'];f=np.memmap(ref/'filtered.hex',dtype='S21',mode='r');init=[];rr=[];vectors=[];smoothgold=[];count=0
  observed=[s.split() for s in (mathref/'observed_pilots.txt').read_text().splitlines()]
  for m,model in enumerate(meta['models']):
   for w in range(1,4):
    n=m*3+w-1;e=model['windows'][w];first=(e['peak']+1536*e['step']+32768)//(1<<32);left=(e['peak']+1152*e['step'])//(1<<32)-512;right=(e['peak']+(1536+66*273+1)*e['step']+32768)//(1<<32)+100
    offset=len(rr);rr.extend(int(v,16) for v in f[model['offset']+left+20:model['offset']+right+21])
    init.append(f"off[{n}]={offset};lo[{n}]={left};pre[{n}]={first+9-left};last[{n}]={right-left};training[{n}]=80'h{e['training']:020x};peak[{n}]=80'h{e['peak']:020x};st[{n}]=35'd{e['step']};ch[{n}]=208'h{e['H']:052x};ph[{n}]=64'h{e['phase_steps']:016x};")
    hdr=next(h for h in headers if h['model']==m and h['window']==w);init.append(f"headers[{n}]=192'h{int.from_bytes(bytes.fromhex(hdr['raw_header']),'little'):048x};")
    coordinates=[e['peak']+(1152+i)*e['step'] for i in range(384)]+[e['peak']+(1536+66*g+tx)*e['step'] for g in range(274) for tx in (0,1)]
    for c in coordinates:
     tag,mu=divmod((c+32768)//65536,65536);aa=unpack(int(f[model['offset']+tag+20],16),20);bb=unpack(int(f[model['offset']+tag+21],16),20);vectors.append(pack([(2*(x*(65536-mu)+y*mu)+32768)//65536 for x,y in zip(aa,bb)],21))
    rows,_=smooth([[int(observed[n*548+2*g+tx][1],16) for tx in (0,1)] for g in range(274)])
    smoothgold.extend((phi%(1<<41))+(i<<41)+(known<<50)+(tx<<59) for known,tx,i,phi in rows)
  for name,rows,width in [('rrc_rows',rr,20),('vectors',vectors,21),('phase_gold',smoothgold,15)]: (out/(name+'.hex')).write_text(''.join(f'{v:0{width}x}\n' for v in rows))
  for p in ref.glob('*.hex'):
   if p.name in ('sine.hex','coherent_signs.hex'):os.link(p,out/p.name)
  (out/'pilot_gold.hex').write_text(''.join((mathref/'pilot_gold.hex').read_text().splitlines(keepends=True)[:11508]))
  calls=[f'run_case({n},{n},0);' for n in range(21)]+[f'run_case({20+mode},0,{mode});' for mode in range(1,21)]+['run_case(41,0,0);']
  text=TB.replace('@RLAST@',str(len(rr)-1)).replace('@INIT@','\n'.join(init)).replace('@CASES@','\n'.join(calls));(out/'tb.sv').write_text(text)
  for name in SOURCES:shutil.copyfile(project/'sim/g0'/name,out/name)
  xpm=bp.parent/'data/ip/xpm'
  def run(step,args):
   with (out/(step+'.log')).open('w') as g:subprocess.run(args,cwd=out,stdout=g,stderr=subprocess.STDOUT,check=True)
  run('compile',[str(bp/'xvlog'),'--sv',str(xpm/'xpm_cdc/hdl/xpm_cdc.sv'),str(xpm/'xpm_memory/hdl/xpm_memory.sv'),*[str(out/n) for n in SOURCES],str(out/'tb.sv')]);run('elaborate',[str(bp/'xelab'),'tb','-s','frame_replay_tb','-mt','off']);run('simulate',[str(bp/'xsim'),'frame_replay_tb','-runall'])
  log=(out/'simulate.log').read_text();assert 'G0_FRAME_REPLAY_PASS' in log and 'Fatal:' not in log,log[-4000:]
  result={'scope':__doc__,'simulation':next(s for s in log.splitlines() if s.startswith('G0_FRAME_REPLAY_PASS')),'source_sha256':{n:hashlib.sha256((out/n).read_bytes()).hexdigest() for n in SOURCES},'fixtures':str(ref.relative_to(project)),'g0_passed':False};(out/'receipt.json').write_text(json.dumps(result,indent=2)+'\n');print(result['simulation'],flush=True)
 finally:(out/'disk-after.json').write_text(json.dumps(disk_check('after-frame-replay'),indent=2)+'\n')
if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);p.add_argument('--fixture-directory',type=Path,required=True);p.add_argument('--math-reference',type=Path,required=True);p.add_argument('--header-reference',type=Path,required=True);p.add_argument('--vivado-bin',type=Path,required=True);main(p.parse_args())
