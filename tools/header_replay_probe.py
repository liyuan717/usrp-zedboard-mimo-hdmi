#!/usr/bin/env python3
"""Actual shared XPM history/header replay ownership, errors and recovery.
No ADC-chain claim; complete live integration has a separate31-RTL probe.
"""
import argparse,hashlib,itertools,json,os,shutil,subprocess
from pathlib import Path
import numpy as np
from bpsk_header_probe import unpack,pack
from workspace_storage_guard import check as disk_check,GIB
SOURCES=['coherent_window_cost_probe.sv','interpolation_cost_probe.sv','timing_request_cost_probe.sv','bpsk_header_cost_probe.sv','header_replay_cost_probe.sv']
TB=r'''`timescale 1ns/1ps
module tb;
reg clk=0;always #10 clk=~clk;
reg rst=1,cancel=0,cv=0,rv=0,start=0,wr=1,ready=0;reg [31:0] ci=0,ctx=32'h80000000,cc=32'h80000000,cg=0,accepted_context=0,accepted_generation=0;reg [15:0] ep=16'hffff,ce=0,accepted_epoch=0;
reg [63:0] tag=0;reg [79:0] data=0,pos=0;reg [34:0] step=35'd17179869184;reg [103:0] h=104'h@H@;reg [31:0] phase=32'h@STEP@;
wire shared_ready,restart,re,active,valid;wire [11:0] ra;wire [79:0] word;wire [31:0] gen,oi,oc,og,sid,sf;wire [63:0] latest;wire [12:0] count;wire [15:0] oe,l0,l1;wire [2:0] error,detail;wire [191:0] raw;
coherent_window_cost_probe pool(.clk(clk),.rst(rst),.cancel(1'b0),.in_valid(rv),.in_ready(shared_ready),.in_start(start),.in_tag(tag),.in_data(data),
 .cfg_valid(1'b0),.cfg_ready(),.cfg_id(32'd0),.cfg_generation(32'd0),.cfg_first_tag(64'd0),.cfg_points(7'd1),
 .out_valid(),.out_ready(1'b1),.out_id(),.out_generation(),.out_tag(),.out_point(),.out_c(),.out_n(),.out_e(),.out_error(),.out_last(),.busy(),
 .history_generation(gen),.latest_tag(latest),.history_count(count),.history_restart(restart),
 .external_read_enable(re),.external_read_address(ra),.read_word(word));
header_replay_cost_probe dut(.clk(clk),.rst(rst),.cancel(cancel),.cfg_valid(cv),.cfg_ready(),.cfg_id(ci),.cfg_context(cc),.cfg_generation(cg),.cfg_epoch(ce),.cfg_training_position(pos),.cfg_symbol_step(step),.cfg_h(h),.cfg_phase_step(phase),
 .window_ready(wr),.current_context(ctx),.rrc_valid(rv),.rrc_start(start),.shared_ready(shared_ready),.rrc_tag(tag),.rrc_data(data),
 .history_generation(gen),.latest_tag(latest),.history_count(count),.history_restart(restart),.reader_active(active),.read_enable(re),.read_address(ra),.read_word(word),
 .out_valid(valid),.out_ready(ready),.out_id(oi),.out_context(oc),.out_generation(og),.out_epoch(oe),.out_error(error),.out_detail(detail),.out_raw_header(raw),.out_session(sid),.out_superframe(sf),.out_stream0_bytes(l0),.out_stream1_bytes(l1));
reg [79:0] rrc[0:@LAST@];reg [83:0] gold[0:383];reg [47:0] base;
integer j=0,cycle=0,cases=0,results=0,revoked=0,samples=0,held_checks=0,max_age=0,age=0,k,n,waited=0;reg [405:0] held;
wire [405:0] view={oi,oc,og,oe,error,detail,raw,sid,sf,l0,l1};
always @(posedge clk)begin
 cycle=cycle+1;
 if(!rst&&!cancel&&dut.sample_valid&&dut.sample_ready&&dut.sample_error==0&&!dut.sample_bad)begin
  if(dut.sample_data!==gold[dut.received])$fatal(1,"header replay actual F1 oracle case%0d idx%0d",j,dut.received);
  samples=samples+1;age=48'(latest[47:0]-dut.sample_tag[47:0]);if(age>max_age)max_age=age;
 end
end
task put(input integer i,input integer mode);
 begin
 rv=1;start=i==0||(mode==3&&i==@PRE@+20);tag={ep^(mode==5&&i>=@PRE@+20?16'd1:16'd0),base+48'(i)+48'(mode==4&&i>=@PRE@+20?1:0)};data=i<=@LAST@?rrc[i]:80'd0;
 @(posedge clk);while(!shared_ready)@(posedge clk);@(negedge clk);rv=0;start=0;
 end
endtask
task arm;
 begin
 cg=j==25?gen^32'h80000000:gen;ce=j==26?ep^16'h8000:ep;accepted_context=cc;accepted_generation=cg;accepted_epoch=ce;pos={base,32'd0}+80'h@TRAIN@;ci=32'hfffffff0+32'(j);cv=1;
 @(posedge clk);while(!dut.cfg_ready)@(posedge clk);@(negedge clk);cv=0;cc=~ctx;
 end
endtask
task check_result(input [2:0] expected_error,input [2:0] expected_detail);
 begin
 while(!valid)@(negedge clk);
 if(oi!==32'hfffffff0+32'(j)||oe!==accepted_epoch||oc!==accepted_context||og!==accepted_generation||error!==expected_error||detail!==expected_detail)$fatal(1,"header replay result case%0d error%0d:%0d expected%0d:%0d",j,error,detail,expected_error,expected_detail);
 if(error==0)begin if(raw!==192'h@RAW@||sid!=32'h56335001||sf!=8||l0!=4384||l1!=4384)$fatal(1,"header replay trusted metadata");end
 else if({sid,sf,l0,l1}!==96'd0)$fatal(1,"header replay untrusted metadata");
 held=view;results=results+1;
 repeat(53)begin @(negedge clk);if(!valid||view!==held||active||re)$fatal(1,"header replay held ownership");held_checks=held_checks+1;end
 ready=1;@(negedge clk);ready=0;repeat(2)@(negedge clk);
 if(valid||!dut.cfg_ready||re||active)$fatal(1,"header replay completion not idle");
 end
endtask
task cancel_check;
 begin
 cancel=1;#1;if(valid||re)$fatal(1,"header replay cancel edge");repeat(2)@(negedge clk);cancel=0;revoked=revoked+1;
 repeat(64)begin @(negedge clk);if(valid||re||active)$fatal(1,"header replay cancelled work revived");end
 end
endtask
initial begin
 $readmemh("rrc_rows.hex",rrc);$readmemh("gold.hex",gold);repeat(4)@(negedge clk);rst=0;
 for(j=0;j<28;j=j+1)begin
  cases=cases+1;base=48'hfffffffff000+48'(j*111);ep=16'hffff+16'(j);ctx=32'h80000000^32'(j);cc=ctx;wr=1;step=35'd@SYMBOLSTEP@;h=104'h@H@;phase=32'h@STEP@;
  for(k=0;k<@PRE@;k=k+1)put(k,0);
  if(j==1)cc=ctx^32'h80000000;
  if(j==6)step=35'd17176433209;
  if(j==7)step=35'd17183305159;
  if(j==8)h=0;
  if(j==27)wr=0;
  if(j==10)for(k=@PRE@;k<@PRE@+6000;k=k+1)put(k,0);
  arm();
  if(j==11)force dut.decoder.in_ready=0;
  fork
   begin
    if(j!=9&&j!=10)for(n=@PRE@;n<=@LAST@;n=n+1)begin put(n,j);repeat(7)@(negedge clk);end
   end
   begin
    if(j==2)begin while(dut.received!=8)@(negedge clk);ctx=ctx^32'h80000000;end
    if(j>=12&&j<=18)begin while(!(dut.sampler.state==j-11))@(negedge clk);cancel_check();end
    if(j==19)begin while(!(dut.decoder.state==2&&dut.decoder.issued==9))@(negedge clk);cancel_check();end
    if(j==20)begin while(!(dut.decoder.state==3&&dut.decoder.issued==4))@(negedge clk);cancel_check();end
    if(j==21)begin while(!dut.decoder.crc_busy)@(negedge clk);cancel_check();end
    if(j==22)begin while(dut.decoder.state!=4)@(negedge clk);cancel_check();end
   end
  join
  if(j>=12&&j<=22)begin
   repeat(64)begin @(negedge clk);if(valid||active||re)$fatal(1,"header replay old job after producer drain");end
   ctx=32'h80000000^32'(j);cc=ctx;arm();check_result(0,0);
  end else begin
   case(j)
    1,2,3,4,5,25,26,27:check_result(4,0);
    6,7:check_result(5,2);
    8:check_result(5,0);
    9:check_result(3,3);
    10:check_result(3,2);
    11:begin while(!valid)@(negedge clk);release dut.decoder.in_ready;check_result(6,0);end
    23:begin while(!valid)@(negedge clk);cancel_check();cc=ctx;arm();check_result(0,0);end
    default:check_result(0,0);
   endcase
  end
 end
 if(cases!=28||results!=28||revoked!=12||samples<2000||max_age>=4096)$fatal(1,"header replay coverage");
 $display("G0_HEADER_REPLAY_PASS cases=%0d results=%0d cancelled_then_recovered=%0d exact_samples=%0d maximum_read_age=%0d held_checks=%0d",cases,results,revoked,samples,max_age,held_checks);$finish;
end
initial begin #100000000;$fatal(1,"header replay timeout case%0d state%0d",j,dut.state);end
endmodule
'''
def main(a):
 project=Path(__file__).resolve().parents[1];out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);ref=a.fixture_directory.resolve();hr=a.header_reference.resolve();bp=a.vivado_bin.resolve()
 before=disk_check('before-header-replay-control');(out/'disk-before.json').write_text(json.dumps(before,indent=2)+'\n')
 if before['disk']['available_bytes']<5*GIB:raise SystemExit('Under5GiB: handle storage first.')
 try:
  meta=json.loads((ref/'fixture_metadata.json').read_text());model=meta['models'][0];w=model['windows'][1];first=(w['peak']+1152*w['step']+32768)//(1<<32);start=first-3072;end=(w['peak']+1535*w['step']+32768)//(1<<32)+2;pre=3072+264
  stream=np.memmap(ref/'filtered.hex',dtype='S21',mode='r');rows=[int(stream[model['offset']+i+20],16) for i in range(start,end+1)]
  (out/'rrc_rows.hex').write_text(''.join(f'{v:020x}\n' for v in rows))
  with (hr/'vectors.hex').open() as f:(out/'gold.hex').write_text(''.join(itertools.islice(f,384)))
  for name in ['sine.hex','coherent_signs.hex']:os.link(ref/name,out/name)
  actual=json.loads((hr/'actual_headers.json').read_text())[0];text=TB
  replacements={'H':f'{w["H"]%(1<<104):026x}','STEP':f'{w["phase_steps"]% (1<<32):08x}','LAST':str(len(rows)-1),'PRE':str(pre),'TRAIN':f'{(w["training"]-(start<<32))%(1<<80):020x}','RAW':f'{int.from_bytes(bytes.fromhex(actual["raw_header"]),"little"):048x}','SYMBOLSTEP':str(w['step'])}
  for k,v in replacements.items():text=text.replace('@'+k+'@',v)
  (out/'tb.sv').write_text(text);sources=[]
  for name in SOURCES:shutil.copyfile(project/'sim/g0'/name,out/name);sources.append(out/name)
  def run(step,args):
   with (out/(step+'.log')).open('w') as f:subprocess.run(args,cwd=out,stdout=f,stderr=subprocess.STDOUT,check=True)
  xpm=bp.parent/'data/ip/xpm';run('compile',[str(bp/'xvlog'),'--sv',str(xpm/'xpm_cdc/hdl/xpm_cdc.sv'),str(xpm/'xpm_memory/hdl/xpm_memory.sv'),*map(str,sources),str(out/'tb.sv')]);run('elaborate',[str(bp/'xelab'),'tb','-s','header_replay_tb','-mt','off']);run('simulate',[str(bp/'xsim'),'header_replay_tb','-runall'])
  log=(out/'simulate.log').read_text();assert 'G0_HEADER_REPLAY_PASS' in log and 'Fatal:' not in log,log[-3500:]
  result={'scope':__doc__,'simulation':next(s for s in log.splitlines() if s.startswith('G0_HEADER_REPLAY_PASS')),'source_sha256':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sources},'g0_passed':False};(out/'receipt.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result),flush=True)
 finally:(out/'disk-after.json').write_text(json.dumps(disk_check('after-header-replay-control'),indent=2)+'\n')
if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);p.add_argument('--fixture-directory',type=Path,required=True);p.add_argument('--header-reference',type=Path,required=True);p.add_argument('--vivado-bin',type=Path,required=True);main(p.parse_args())
