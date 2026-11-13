#!/usr/bin/env python3
"""Finite G0 history/interpolation RTL simulation and routed 50 MHz cost probe.

The scoreboard retains an insertion-ordered queue of full tags, independent of
RTL address mapping, and computes signed integer interpolation directly.
"""
import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess
import numpy as np
from fec_inverse_cost_probe import routed_measurements


def pack(values):
    return sum((int(v) & 0xfffff) << (20*i) for i, v in enumerate(values))


TB = r'''`timescale 1ns/1ps
module tb;
 localparam F=@FRACTION@, O=20+F, W=4*O;
 reg clk=0; always #10 clk=~clk;
 reg rst=1,cancel=0,iv=0,istart=0,rv=0,ready=1;
 reg [63:0] it=0,rt=0;reg [79:0] idata=0;
 reg [31:0] rg=0,rid=0;reg [15:0] mu=0;
 wire ir,rr,ov;wire [31:0] og,oid,hgen,fences;wire [63:0] ot,latest;
 wire [15:0] omu;wire [2:0] err;wire [W-1:0] raw_result;
 wire [W-1:0] result=raw_result ^ @FAULT@;
 wire [9:0] count;
 interpolation_cost_probe #(.OUT_FRACTION_BITS(F)) dut(.clk(clk),.rst(rst),.cancel(cancel),.request_cancel(1'b0),.request_idle(),.history_restart(),
 .in_valid(iv),.in_ready(ir),.in_start(istart),.in_tag(it),.in_data(idata),
 .req_valid(rv),.req_ready(rr),.req_generation(rg),.req_id(rid),.req_tag(rt),.req_mu(mu),
 .out_valid(ov),.out_ready(ready),.out_generation(og),.out_id(oid),.out_tag(ot),
 .out_mu(omu),.out_error(err),.out_data(raw_result),.history_generation(hgen),
 .latest_tag(latest),.history_count(count),.fence_count(fences));
 // The reference queue is addressed by insertion order, not sample tag bits.
 reg [63:0] qtag[0:511];reg [79:0] qdata[0:511];integer qhead=0,qcount=0;
 reg [63:0] mlatest=0;reg [31:0] mg=0,mf=0;
 reg pending=0,seen=0,held=0,reqheld=0,auto_expect=0,data_known=0;
 reg [31:0] eg,eid;reg [63:0] et;reg [15:0] emu;reg [W-1:0] edata;
 integer eerror=0,expect_override=-1,issued=0,received=0,aborted=0,inputs=0;
 integer good=0,e1=0,e2=0,e3=0,cycle=0,wait_cycles=0,out_stalls=0,req_stalls=0;
 integer writes_blocked_output=0,writes_ii1=0,lastwrite=-10,max_service=0,min_service=999;
 integer service_intervals=0,concurrent_intervals=0,lastservice=-1,service_count=0;
 integer reset_events=0,cancel_cycles=0,windows=0,wraps=0,cadenced=0;
 integer accepted_cycle=0,timeout_latency=0;
 integer tie_case=0,expected_tie=0,tie_component_checks=0;
 integer wrap_neighbor_checks=0;
 integer cindex,search0,search1,idx;reg service=0,cadence=0;integer last_cadence=-1;
 reg [63:0] heldt,reqheldt;reg [31:0] heldg,heldid,reqheldg,reqheldid;
 reg [15:0] heldmu,reqheldmu;reg [2:0] helderr;reg [W-1:0] helddata;
 reg newwindow;reg [63:0] nexttag;reg [47:0] distance;
 reg [79:0] found0,found1;
 function automatic [63:0] inc_tag(input [63:0] t,input integer increment);
  inc_tag={t[63:48],t[47:0]+48'(increment)};
 endfunction
 function automatic [79:0] sample(input integer seed);
  reg [31:0] s;integer l;begin
   s=32'h812371ab ^ seed;
   for(l=0;l<4;l=l+1) begin s=s^(s<<13);s=s^(s>>17);s=s^(s<<5);sample[l*20+:20]=s[19:0];end
  end
 endfunction
 function automatic [W-1:0] mix(input [79:0] a,b,input [15:0] u);
  longint signed x,y,v;integer l;begin
   for(l=0;l<4;l=l+1) begin
    x=$signed(a[l*20+:20]);y=$signed(b[l*20+:20]);
    v=(x<<<F)+(((y-x)*longint'(u)+(1<<(15-F)))>>>(16-F));mix[l*O+:O]=v[O-1:0];
   end
  end
 endfunction
 task lookup;
  begin
   search0=-1;search1=-1;
   for(cindex=0;cindex<qcount;cindex=cindex+1) begin
    idx=(qhead-cindex-1+512)%512;
    if(qtag[idx]==et) search0=idx;
    if(qtag[idx]==inc_tag(et,1)) search1=idx;
   end
   if(search0>=0 && search1>=0) begin edata=mix(qdata[search0],qdata[search1],emu);data_known=1;end
  end
 endtask
 always @(posedge clk) begin
  cycle=cycle+1;
  if(rst) begin
   if(pending) aborted=aborted+1;
   if(mg!=0 || pending || seen) reset_events=reset_events+1;
   qhead=0;qcount=0;mlatest=0;mg=0;mf=0;pending=0;seen=0;held=0;reqheld=0;
  end else if(cancel) begin
   if(pending) aborted=aborted+1;
   qhead=0;qcount=0;mg=mg+1;pending=0;seen=0;held=0;reqheld=0;cancel_cycles=cancel_cycles+1;
  end else begin
   if(!ir) $fatal(1,"input backpressure at cycle %0d",cycle);
   if(held && (!ov || {og,oid,ot,omu,err,result}!=={heldg,heldid,heldt,heldmu,helderr,helddata}))
    $fatal(1,"held output changed");
   if(reqheld && (!rv || {rg,rid,rt,mu}!=={reqheldg,reqheldid,reqheldt,reqheldmu}))
    $fatal(1,"held request changed");
   if(ov && !seen) begin
    if(!pending) $fatal(1,"unexpected output id%0d",oid);
    if({og,oid,ot,omu}!=={eg,eid,et,emu} || err!==3'(eerror) ||
       result!==(eerror==0?edata:W'(0)) || (eerror==0&&!data_known))
     $fatal(1,"interpolation mismatch id%0d error%0d expected%0d value%h expected%h known%0d",oid,err,eerror,result,edata,data_known);
    if(err==3) begin
     timeout_latency=cycle-accepted_cycle;
     if(timeout_latency!=4097) $fatal(1,"timeout must follow4096 CHECK rounds, latency%0d",timeout_latency);
    end
    if(expected_tie!=0) begin
     if(err!=0 || result[O-1:0]!==O'(expected_tie==1?0:-1) ||
        result[O+:O]!==O'(expected_tie==1?1:2))
      $fatal(1,"explicit positive/negative half rounding mismatch");
     tie_component_checks=tie_component_checks+2;
    end
    received=received+1;pending=0;seen=1;
    if(err==0 && ot[47:0]==48'hffffffffffff) wrap_neighbor_checks=wrap_neighbor_checks+1;
    case(err) 0:good=good+1;1:e1=e1+1;2:e2=e2+1;3:e3=e3+1;default:$fatal(1,"error encoding");endcase
   end
   held=ov&&!ready;
   if(held) begin
    heldg=og;heldid=oid;heldt=ot;heldmu=omu;helderr=err;helddata=result;out_stalls=out_stalls+1;
   end
   if(ov&&ready) seen=0;
   reqheld=rv&&!rr;
   if(reqheld) begin reqheldg=rg;reqheldid=rid;reqheldt=rt;reqheldmu=mu;req_stalls=req_stalls+1;end
   if(rv&&rr) begin
    if(pending) $fatal(1,"accepted second active request");
    pending=1;eg=rg;eid=rid;et=rt;emu=mu;edata=0;data_known=0;
    auto_expect=expect_override<0;eerror=auto_expect?0:expect_override;
    if(auto_expect) begin
     distance=mlatest[47:0]-et[47:0];
     if(eg!=mg || (qcount!=0&&et[63:48]!=mlatest[63:48])) eerror=1;
     else if(qcount!=0&&!distance[47]&&distance>=qcount) eerror=2;
    end
    if(eerror==0) lookup();
    issued=issued+1;
    accepted_cycle=cycle;
    expected_tie=tie_case;
    if(service) begin
     if(service_count>1) begin
      if(!cadence || last_cadence<0 || cycle-last_cadence>13)
       $fatal(1,"request service interval outside active4MSPS stream");
      concurrent_intervals=concurrent_intervals+1;
      if(cycle-lastservice>38) $fatal(1,"service exceeded38 clocks");
      if(cycle-lastservice>max_service) max_service=cycle-lastservice;
      if(cycle-lastservice<min_service) min_service=cycle-lastservice;
      service_intervals=service_intervals+1;
     end
     service_count=service_count+1;lastservice=cycle;
    end
   end
   if(iv&&ir) begin
    nexttag=inc_tag(mlatest,1);newwindow=qcount==0||istart||it!=nexttag;
    if(newwindow) begin qcount=0;qhead=0;mg=mg+1;mf=mf+1;windows=windows+1;end
    else if(mlatest[47:0]==48'hffffffffffff) wraps=wraps+1;
    qtag[qhead]=it;qdata[qhead]=idata;qhead=(qhead+1)%512;
    if(qcount<512) qcount=qcount+1;
    mlatest=it;inputs=inputs+1;
    if(held) writes_blocked_output=writes_blocked_output+1;
    if(cycle-lastwrite==1) writes_ii1=writes_ii1+1;
    lastwrite=cycle;
    if(cadence) begin
     if(last_cadence>=0 && cycle-last_cadence!=12 && cycle-last_cadence!=13)
      $fatal(1,"4MSPS cadence not12/13");
     last_cadence=cycle;cadenced=cadenced+1;
    end
    if(pending&&auto_expect&&eg!=mg) eerror=1;
   end
   if(pending && eerror==0 && !data_known) lookup();
  end
  #1;
  if(hgen!==mg || latest!==mlatest || count!==10'(qcount) || fences!==mf)
   $fatal(1,"history counters gen%0d/%0d count%0d/%0d fence%0d/%0d latest%h/%h",hgen,mg,count,qcount,fences,mf,latest,mlatest);
  if((rst||cancel)&& (ir||rr||ov)) $fatal(1,"reset/cancel admission/output");
 end
 // All drivers change only at falling edges. Input task has no idle bubble.
 task put(input [63:0] tag,input [79:0] value,input bit start);
  begin it=tag;idata=value;istart=start;iv=1;@(negedge clk);iv=0;istart=0;end
 endtask
 task request(input [63:0] tag,input [15:0] u,input integer error_override,input [31:0] generation);
  begin
   rt=tag;mu=u;rg=generation;rid=rid+1;expect_override=error_override;rv=1;
   @(posedge clk);while(!rr) @(posedge clk);
   @(negedge clk);rv=0;
  end
 endtask
 task drain;
  begin while(pending||ov||rv) @(negedge clk);end
 endtask
 task seed(input [63:0] tag,input integer length);
  integer k;begin for(k=0;k<length;k=k+1) put(inc_tag(tag,k),sample(k),k==0);end
 endtask
 task clear(input bit reset_it,input integer length);
  begin
   if(reset_it) rst=1;else cancel=1;
   repeat(length) @(negedge clk);
   rst=0;cancel=0;repeat(2) @(negedge clk);
  end
 endtask
 task phase_abort(input integer phase,input bit reset_it);
  begin
   seed(64'h0040000000000000,200);
   request(64'h0040000000000010,16'h8123,-1,mg);
   while(dut.state!=phase) @(negedge clk);
   clear(reset_it,reset_it?1:2);repeat(30) @(negedge clk);
   if(ov||pending||qcount) $fatal(1,"phase cancellation failed");
  end
 endtask
 integer j,k,before_count,before_good;reg [63:0] saved_tag,base;
 initial begin
  repeat(3) @(negedge clk);rst=0;
  // Empty requests wait; mu=0 still requires a neighbor.
  request(0,0,3,mg);drain();
  put(64'h0001000000000000,80'h7ffff800000000000000,1);
  request(64'h0001000000000000,0,3,mg);drain();
  put(64'h0001000000000001,80'h800007ffff00001fffff,0);
  request(64'h0001000000000000,0,-1,mg);drain();
  request(64'h0001000000000000,65535,-1,mg);drain();
  // Lanes0/1 are0->-1 and0->+1: exact negative/positive halves at output precision.
  tie_case=1;request(64'h0001000000000000,16'(32768>>F),-1,mg);drain();tie_case=0;
  if(F>=1) begin
   tie_case=2;request(64'h0001000000000000,16'(3*(32768>>F)),-1,mg);drain();tie_case=0;
  end
  for(j=0;j<256;j=j+1) begin
   put(inc_tag(mlatest,1),sample(j*13+7),0);
   request(inc_tag(mlatest,-1),16'(j*251+17),-1,mg);drain();
  end
  // Low48 wrap does not change epoch or generation; delayed135-sample read.
  seed(64'h0002ffffffffff60,400);
  request(64'h0002ffffffffffff,0,-1,mg);drain();
  request(64'h0002ffffffffffff,65535,-1,mg);drain();
  request(inc_tag(mlatest,-135),16'h8000,-1,mg);drain();
  // A future request succeeds when its two endpoints arrive normally.
  saved_tag=inc_tag(mlatest,4);request(saved_tag,32123,-1,mg);
  for(j=0;j<5;j=j+1) begin repeat(7) @(negedge clk);put(inc_tag(mlatest,1),sample(819+j),0);end
  drain();request(inc_tag(mlatest,100),0,3,mg);drain();
  // Endpoint arriving on penultimate CHECK is usable on final CHECK. An
  // arrival on the final CHECK (or one later) cannot replace old metadata.
  request(mlatest,0,-1,mg);
  repeat(4094) @(negedge clk);put(inc_tag(mlatest,1),sample(911),0);drain();
  request(mlatest,0,3,mg);
  repeat(4095) @(negedge clk);put(inc_tag(mlatest,1),sample(912),0);drain();
  request(mlatest,0,3,mg);
  repeat(4096) @(negedge clk);put(inc_tag(mlatest,1),sample(913),0);drain();
  // Stale generation and wrong epoch.
  request(inc_tag(mlatest,-4),1,-1,mg-1);drain();
  request(mlatest^(64'h1<<48),1,-1,mg);drain();
  // Gap, duplicate, epoch and explicit restart each fence active work.
  for(j=0;j<4;j=j+1) begin
   seed(64'h0003000000000000+j*1000,200);
   request(inc_tag(mlatest,-135),31234,1,mg);
   while(dut.state!=5) @(negedge clk);
   case(j)
    0:put(inc_tag(mlatest,5),sample(j),0);
    1:put(mlatest,sample(j),0);
    2:put(mlatest^(64'h1<<48),sample(j),0);
    3:put(inc_tag(mlatest,1),sample(j),1);
   endcase
   drain();
  end
  // A window fence on the result-production edge still invalidates work.
  seed(64'h000a000000000000,200);
  request(inc_tag(mlatest,-135),23456,1,mg);
  while(dut.state!=7) @(negedge clk);
  put(inc_tag(mlatest,1),sample(83),1);drain();
  // Already held output remains stable across fence, while writes stay II1.
  seed(64'h0004000000000000,300);ready=0;
  request(inc_tag(mlatest,-135),17231,-1,mg);
  while(!ov) @(negedge clk);
  put(64'h0005000000000000,sample(5),1);
  for(j=0;j<650;j=j+1) put(inc_tag(mlatest,1),sample(j),0);
  ready=1;drain();
  // Held request survives long output blockage; by admission its target expired.
  saved_tag=inc_tag(mlatest,-135);ready=0;
  request(saved_tag,731,-1,mg);while(!ov) @(negedge clk);
  fork
   begin request(saved_tag,731,-1,mg);drain();end
   begin
    for(k=0;k<600;k=k+1) put(inc_tag(mlatest,1),sample(k+1000),0);
    ready=1;
   end
  join
  // Oldest retained is valid, one older is expired.
  request(inc_tag(mlatest,-511),0,-1,mg);drain();
  request(inc_tag(mlatest,-512),0,-1,mg);drain();
  // CHECK x0 read collides with the same-address next sequential overwrite.
  seed(64'h0006000000000000,512);
  request(64'h0006000000000000,31999,2,mg);
  put(64'h0006000000000200,sample(512),0);drain();
  // x0 was snapshotted on CHECK: overwriting it during the x1 read is safe.
  seed(64'h0007000000000000,512);
  request(64'h0007000000000000,31999,-1,mg);
  while(dut.state!=2) @(negedge clk);
  put(64'h0007000000000200,sample(512),0);drain();
  // Both endpoints snapshotted: subsequent ordinary overwrite cannot revoke.
  seed(64'h0008000000000000,512);
  request(64'h0008000000000000,31999,-1,mg);
  while(dut.state!=4) @(negedge clk);
  for(j=0;j<30;j=j+1) put(inc_tag(mlatest,1),sample(512+j),0);
  drain();
  // A forced second-port same-address write is necessarily a gap and fences.
  seed(64'h0009000000000000,512);
  request(64'h0009000000000000,31999,1,mg);
  while(dut.state!=2) @(negedge clk);
  put(64'h0009000000000201,sample(513),0);drain();
  // Reset/cancel in each read, multiplication, and held response phase.
  phase_abort(2,0);phase_abort(3,0);phase_abort(5,0);
  phase_abort(2,1);phase_abort(3,1);phase_abort(5,1);
  for(j=0;j<2;j=j+1) begin
   seed(64'h0041000000000000,200);ready=0;
   request(inc_tag(mlatest,-135),16'h8123,-1,mg);while(!ov) @(negedge clk);
   repeat(3) @(negedge clk);clear(j==1,2);ready=1;
   repeat(30) @(negedge clk);if(ov) $fatal(1,"held cancellation failed");
  end
  // Simultaneous sustained4MSPS writes and saturated requests, after fill.
  seed(64'h0050000000000000,512);service=1;cadence=1;last_cadence=-1;
  fork
   begin
    for(k=0;k<650;k=k+1) begin
     repeat(11+k%2) @(negedge clk);put(inc_tag(mlatest,1),sample(4000+k),0);
    end
    cadence=0;
   end
   begin
    for(j=0;j<450;j=j+1) request(inc_tag(mlatest,-135),16'(j*491+31),-1,mg);
    drain();service=0;
   end
  join
  @ACTUAL@
  drain();repeat(5) @(negedge clk);
  if(issued!=received+aborted || good<700 || e1<8 || e2<3 || e3!=5 ||
     service_intervals<400 || concurrent_intervals!=service_intervals || max_service>38 || writes_ii1<3000 ||
     writes_blocked_output<1000 || req_stalls<500 || wraps<1 || cadenced!=650 ||
     reset_events<4 || cancel_cycles<8 || tie_component_checks!=(F>=1?4:2) || wrap_neighbor_checks<2)
   $fatal(1,"coverage issued%0d received%0d aborted%0d good%0d errors%0d/%0d/%0d service%0d ii%0d writesII1%0d blocked%0d reset%0d cancel%0d",issued,received,aborted,good,e1,e2,e3,service_intervals,max_service,writes_ii1,writes_blocked_output,reset_events,cancel_cycles);
  $display("G0_INTERPOLATION_PASS inputs=%0d issued=%0d outputs=%0d aborted=%0d good=%0d err1=%0d err2=%0d err3=%0d service_intervals=%0d concurrent_intervals=%0d min_ii=%0d max_ii=%0d writes_ii1=%0d writes_blocked_output=%0d out_stalls=%0d req_stalls=%0d windows=%0d wraps=%0d cadenced=%0d reset_events=%0d cancel_cycles=%0d timeout_latency=%0d tie_component_checks=%0d wrap_neighbor_checks=%0d",inputs,issued,received,aborted,good,e1,e2,e3,service_intervals,concurrent_intervals,min_service,max_service,writes_ii1,writes_blocked_output,out_stalls,req_stalls,windows,wraps,cadenced,reset_events,cancel_cycles,timeout_latency,tie_component_checks,wrap_neighbor_checks);
  $finish;
 end
 initial begin #100000000;$fatal(1,"timeout");end
endmodule
'''


def fixture_statements(paths, out, fraction):
    statements=[];metadata=[]
    for p in paths:
        source_metadata=json.loads(p.with_suffix('.json').read_text())
        if source_metadata.get('output_fraction_bits',0)!=fraction:
            raise ValueError(f'fixture output_fraction_bits does not match requested {fraction}: {p}')
        d=np.load(p);tags=d['rrc_center_tags'];values=d['rrc_lanes']
        if values.ndim!=2 or values.shape[1]!=4 or np.any(values<-(1<<19)) or np.any(values>=(1<<19)):
            raise ValueError('fixture input must have four signed20 lanes')
        requests=d['request_center'];us=d['request_u16'];expected=d['expected']
        endpoints=d['rrc_endpoint_tags'];issues=d['issue_after_endpoint']
        cursor=0
        for n,(target,u,want,issue) in enumerate(zip(requests,us,expected,issues)):
            while cursor<len(tags) and int(endpoints[cursor])<=int(issue):
                statements.append(f"put(64'h{int(tags[cursor]):016x},80'h{pack(values[cursor]):020x},{int(cursor==0)});")
                cursor+=1
            i=np.flatnonzero(tags==target)
            if len(i)!=1 or int(i[0])+1>=len(values):raise ValueError('fixture endpoints unavailable')
            x0,x1=values[int(i[0]):int(i[0])+2].astype(np.int64)
            oracle=(x0<<fraction)+((x1-x0)*int(u)+(1<<(15-fraction)))//(1<<(16-fraction))
            if not np.array_equal(oracle,want):raise ValueError('fixture independent integer oracle mismatch')
            statements.append(f"request(64'h{int(target):016x},16'd{int(u)},-1,mg);drain();")
        metadata.append({'path':str(p.resolve()),'requests':len(requests),'inputs':cursor,
                         'coordinate':'RRC center tags; source endpoint-20','delayed_request_samples':135,
                         'output_fraction_bits':fraction,'source_metadata':source_metadata})
        shutil.copyfile(p,out/p.name)
        if p.with_suffix('.json').exists():shutil.copyfile(p.with_suffix('.json'),out/p.with_suffix('.json').name)
    return '\n  '.join(statements),metadata


def main(a):
    project=Path(__file__).resolve().parents[1];out=a.output.resolve();out.mkdir(parents=True,exist_ok=False)
    actual,fixtures=fixture_statements(a.fixture,out,a.output_fraction_bits)
    tb=TB.replace('@ACTUAL@',actual).replace('@FAULT@',"W'(0)").replace('@FRACTION@',str(a.output_fraction_bits))
    (out/'tb.sv').write_text(tb)
    source=out/'interpolation_cost_probe.sv';shutil.copyfile(project/'sim/g0/interpolation_cost_probe.sv',source)
    runs=[];bp=a.vivado_bin.resolve()
    def run(name,args):
        runs.append(args);(out/'commands.json').write_text(json.dumps(runs,indent=2)+'\n')
        with (out/(name+'.log')).open('w') as log:
            subprocess.run(args,cwd=out,stdout=log,stderr=subprocess.STDOUT,check=True)
    run('compile',[str(bp/'xvlog'),'--sv',str(source),str(out/'tb.sv')])
    run('elaborate',[str(bp/'xelab'),'tb','-s','interpolation_tb','-mt','off'])
    run('simulate',[str(bp/'xsim'),'interpolation_tb','-runall'])
    log=(out/'simulate.log').read_text()
    if 'G0_INTERPOLATION_PASS' not in log or 'Fatal:' in log:raise ValueError('interpolation simulation failed')
    measured={k:int(v) for k,v in re.findall(r'(\w+)=(\d+)',log.split('G0_INTERPOLATION_PASS')[-1])}
    fault=tb.replace('module tb;','module fault_tb;').replace("raw_result ^ W'(0)","raw_result ^ (err==0 ? W'(1) : W'(0))")
    (out/'fault_tb.sv').write_text(fault)
    run('fault-compile',[str(bp/'xvlog'),'--sv',str(out/'fault_tb.sv')])
    run('fault-elaborate',[str(bp/'xelab'),'fault_tb','-s','interpolation_fault_tb','-mt','off'])
    run('fault-simulate',[str(bp/'xsim'),'interpolation_fault_tb','-runall'])
    faultlog=(out/'fault-simulate.log').read_text()
    if 'Fatal: interpolation mismatch' not in faultlog or 'G0_INTERPOLATION_PASS' in faultlog:raise ValueError('negative control not rejected')
    receipt={'scope':'G0 standalone bounded RRC history/configured interpolation; no timing estimator or complete PHY',
             'clock_hz':50000000,'reference':'insertion-ordered full-tag queue; direct signed64 integer arithmetic; ties toward positive infinity',
             'input_signed_bits':20,'input_scale':2,'output_fraction_bits':a.output_fraction_bits,
             'output_signed_bits':20+a.output_fraction_bits,'output_scale':2<<a.output_fraction_bits,
             'measured':measured,'actual_RRC_fixtures':fixtures,'single_bit_output_fault_rejected':True,
             'read1_same_address_collision':'Unreachable with continuous one-write-per-clock valid history: CHECK x0 collision or gap fence precedes it; exercised gap fence at second read and safe x0 overwrite.',
             'g0_passed':False,'routed_OOC':None}
    (out/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
    if not a.sim_only:
        (out/'ooc.tcl').write_text(f'''set_param general.maxThreads 4
read_verilog -sv {{{source}}}
synth_design -top interpolation_cost_probe -part xc7z020clg484-1 -mode out_of_context -generic OUT_FRACTION_BITS={a.output_fraction_bits}
create_clock -name processing -period 20.000 [get_ports clk]
set_input_delay 2.0 -clock processing [get_ports -filter {{DIRECTION == IN && NAME != clk}}]
set_output_delay 2.0 -clock processing [get_ports -filter {{DIRECTION == OUT}}]
opt_design
place_design
route_design
report_utilization -file utilization.rpt
report_utilization -hierarchical -file hierarchy.rpt
report_timing_summary -file timing.rpt -report_unconstrained
report_drc -file drc.rpt
report_timing -max_paths 10 -file paths.rpt
write_checkpoint -force interpolation_cost_probe.dcp
''')
        run('ooc',[str(bp/'vivado'),'-mode','batch','-source',str(out/'ooc.tcl'),'-nolog','-nojournal'])
        receipt['routed_OOC']=routed_measurements(out)
        (out/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
    print(json.dumps(receipt))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--vivado-bin',required=True,type=Path);p.add_argument('--output',required=True,type=Path)
    p.add_argument('--sim-only',action='store_true');p.add_argument('--fixture',action='append',default=[],type=Path)
    p.add_argument('--output-fraction-bits',type=int,choices=(0,1,2),default=1)
    main(p.parse_args())
