#!/usr/bin/env python3
"""Request-only cancellation checks using existing independent queue/ADC oracles.

No OOC is run here. Normal numerical/data-path tests remain in the reused TBs;
additional cases cancel only timing/interpolation work while history continues.
"""
import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess
from interpolation_cost_probe import TB as INTERP_TB
from frontend_chain_cost_probe import TB as FRONT_TB, make_vectors


def once(text, old, new):
    if text.count(old)!=1:raise ValueError('template anchor is not unique: '+old[:80])
    return text.replace(old,new)


def interpolation_tb():
    tb=INTERP_TB.replace('@FRACTION@','1').replace('@FAULT@',"W'(0)")
    tb=once(tb,' reg clk=0;', ''' reg request_cancel=0;wire request_idle,history_restart;
 integer request_cancel_cycles=0,request_cancel_writes=0,cancel_restarts=0,idle_checks=0;
 integer phase_cancel_count=0;reg expected_restart;
 reg clk=0;''')
    tb=once(tb,".request_cancel(1'b0),.request_idle(),.history_restart(),",'.request_cancel(request_cancel),.request_idle(request_idle),.history_restart(history_restart),')
    tb=once(tb,'  cycle=cycle+1;', '''  cycle=cycle+1;
  expected_restart=!rst&&!cancel&&iv&&(qcount==0||istart||it!=inc_tag(mlatest,1));
  if(history_restart!==expected_restart) $fatal(1,"standalone history_restart is not exact same-edge event");''')
    tb=once(tb,'   if(held &&', '''   if(request_cancel) begin
    if(pending) aborted=aborted+1;
    pending=0;seen=0;held=0;reqheld=0;request_cancel_cycles=request_cancel_cycles+1;
   end else begin
   if(held &&''')
    tb=once(tb,'   if(iv&&ir) begin', '''   end
   if(iv&&ir) begin
    if(request_cancel) request_cancel_writes=request_cancel_writes+1;
    if(request_cancel&&history_restart) cancel_restarts=cancel_restarts+1;''')
    tb=once(tb,'  #1;\n  if(hgen', '''  #1;
  idle_checks=idle_checks+1;
  if(request_idle!==!(rst||cancel||request_cancel||pending||seen)) $fatal(1,"standalone request_idle mismatch");
  if(request_cancel && (rr||ov||request_idle)) $fatal(1,"request cancellation did not suppress handshake");
  if(hgen''')
    extra=r'''
  // Every active interpolation state, retaining the independent input queue.
  for(j=1;j<=7;j=j+1) begin
   seed(64'h0080000000000000+j*1000,200);
   saved_tag=inc_tag(mlatest,-135);
   request(saved_tag,16'h8123,-1,mg);
   while(dut.state!=j) @(negedge clk);
   request_cancel=1;
   for(k=0;k<3;k=k+1) put(inc_tag(mlatest,1),sample(8000+j*10+k),0);
   request_cancel=0;@(negedge clk);
   if(!request_idle) $fatal(1,"cancel failed to empty state%0d",j);
   phase_cancel_count=phase_cancel_count+1;
   request(saved_tag,16'h8123,-1,mg);drain();
  end
  // Idle cancellation has no history effect, even across several clocks.
  request_cancel=1;repeat(3) @(negedge clk);request_cancel=0;@(negedge clk);
  if(!request_idle) $fatal(1,"idle cancellation");
  // A held response must not transfer when ready and cancel rise together.
  ready=0;request(inc_tag(mlatest,-135),1234,-1,mg);
  while(!ov) @(negedge clk);repeat(3) @(negedge clk);
  request_cancel=1;ready=1;
  #1;if(ov||rr) $fatal(1,"held response survived request cancel");
  for(k=0;k<3;k=k+1) put(inc_tag(mlatest,1),sample(9000+k),0);
  request_cancel=0;@(negedge clk);
  request(inc_tag(mlatest,-135),1234,-1,mg);drain();
  // New history is still accepted on the cancellation edge, with one fence.
  request(inc_tag(mlatest,1000),0,-1,mg);
  request_cancel=1;
  put(64'h0090000000000000,sample(901),1);
  put(64'h0090000000000001,sample(902),0);
  request_cancel=0;@(negedge clk);
  if(!request_idle || qcount!=2) $fatal(1,"cancelled restart history not retained");
  request(64'h0090000000000000,16'hffff,-1,mg);drain();
  if(phase_cancel_count!=7 || request_cancel_writes<26 || cancel_restarts!=1 || request_cancel_cycles<29)
   $fatal(1,"request-only standalone coverage");
  $display("G0_INTERPOLATION_REQUEST_CANCEL_PASS phases=%0d cancel_cycles=%0d writes_during_cancel=%0d restart_during_cancel=%0d idle_checks=%0d",phase_cancel_count,request_cancel_cycles,request_cancel_writes,cancel_restarts,idle_checks);
'''
    return once(tb,'  @ACTUAL@',extra)


def frontend_tb(count, metadata, initial):
    tb=FRONT_TB.replace('@LAST@',str(count-1)).replace('@SEG_LAST@',str(len(metadata)-1))
    tb=tb.replace('@ABORT_SEG@',str(len(metadata)-1)).replace('@SEG_INIT@',initial)
    tb=tb.replace('@ACTUAL_INTERVALS@','0').replace('@FAULT@',"84'd0")
    tb=once(tb,' reg clk=0;', ''' reg request_cancel=0;wire request_idle,history_restart;
 integer request_cancel_cycles=0,request_cancelled_work=0,preserved_writes=0;
 integer cancel_restart_events=0,same_cfg_cancel=0,idle_checks=0;
 reg clk=0;''')
    tb=once(tb,".request_cancel(1'b0),.request_idle(),.history_restart(),",'.request_cancel(request_cancel),.request_idle(request_idle),.history_restart(history_restart),')
    tb=once(tb,'   if(held&&', '''   if(!dut.filtered_ready) $fatal(1,"request cancellation backpressured RRC history writes");
   if(request_cancel) begin
    if(pending||seen||desc_active) request_cancelled_work=request_cancelled_work+1;
    pending=0;seen=0;held=0;reqheld=0;desc_active=0;
    request_cancel_cycles=request_cancel_cycles+1;
   end else begin
   if(held&&''')
    tb=once(tb,'   if(iv&&ir) begin', '''   end
   if(iv&&ir) begin''')
    tb=once(tb,'   if(dut.filtered_valid&&dut.filtered_ready) begin', '''   if(!(dut.filtered_valid&&dut.filtered_ready) && history_restart)
    $fatal(1,"history_restart without accepted filtered sample");
   if(dut.filtered_valid&&dut.filtered_ready) begin
    if(request_cancel) preserved_writes=preserved_writes+1;''')
    tb=once(tb,'    if(newwindow) begin', '''    if(history_restart!==newwindow) $fatal(1,"chain history_restart differs from independent queue");
    if(history_restart&&request_cancel) cancel_restart_events=cancel_restart_events+1;
    if(newwindow) begin''')
    tb=once(tb,'   if(cv&&cr) begin', '''   if(cv&&cr) begin
    if(request_cancel) same_cfg_cancel=same_cfg_cancel+1;''')
    tb=once(tb,'  #1;\n  if(active_ctx', '''  #1;
  idle_checks=idle_checks+1;
  if(request_idle!==!(rst||cancel||request_cancel||(cv&&cr)||desc_active||pending||seen))
   $fatal(1,"chain request_idle mismatch desc%0d pending%0d seen%0d",desc_active,pending,seen);
  if(request_cancel && (ov||tr||dut.req_valid||dut.req_ready||request_idle))
   $fatal(1,"cancelled request/response handshake survived");
  if(active_ctx''')
    extra=r'''
  // Cancel both the future interpolation and generator's blocked next token,
  // while40 further ADC/RRC samples continue into the same history window.
  saved_latest=latest;saved_context=active_ctx;
  descriptor(plus_tag(latest,10000),0,35'h400000000,100,32'h8000,active_ctx);
  while(!pending||!dut.req_valid||dut.req_ready) @(negedge clk);
  request_cancel=1;adc_group=0;
  feed(@ABORT_SEG@,120,160,8);settled();
  request_cancel=0;@(negedge clk);
  if(!request_idle||!armed||!wr||active_ctx!=saved_context) $fatal(1,"request cancel changed producer ownership");
  descriptor(plus_tag(saved_latest,-20),32'h81230000,35'h400000000,1,32'h8001,active_ctx);drain();
  // Output withdrawal with ready high, including remaining generator work.
  ready=0;descriptor(plus_tag(latest,-20),0,35'h400000000,8,32'h8100,active_ctx);
  while(!ov) @(negedge clk);repeat(3) @(negedge clk);
  request_cancel=1;ready=1;#1;if(ov) $fatal(1,"cancelled held output still valid");
  repeat(2) @(negedge clk);request_cancel=0;@(negedge clk);
  if(!request_idle) $fatal(1,"held output kept request path busy");
  descriptor(plus_tag(latest,-20),0,35'h400000000,1,32'h8101,active_ctx);drain();
  // A newly accepted NCO configuration survives same-edge request_cancel.
  descriptor(plus_tag(latest,10000),0,35'h400000000,100,32'h8200,active_ctx);
  while(!pending) @(negedge clk);
  request_cancel=1;configure(0);
  if(!armed) $fatal(1,"request cancel reset a new NCO configuration");
  request_cancel=0;@(negedge clk);
  if(!request_idle||wr) $fatal(1,"empty requests confused with unestablished history");
  request_cancel=1;feed(0,0,45,8);settled();
  if(!wr||hcount!=5) $fatal(1,"new window was suppressed by request cancellation");
  request_cancel=0;@(negedge clk);
  descriptor(plus_tag(latest,-2),32'hffff8000,35'h400000000,1,32'h8201,active_ctx);drain();
  // Generator done is insufficient for idle while its last accepted request waits.
  descriptor(plus_tag(latest,10000),0,35'h400000000,1,32'h8300,active_ctx);
  while(!pending) @(negedge clk);
  repeat(3) @(negedge clk);if(request_idle) $fatal(1,"idle ignored accepted future interpolation");
  request_cancel=1;repeat(2) @(negedge clk);request_cancel=0;@(negedge clk);
  if(!request_idle) $fatal(1,"future request cancellation was not bounded");
  descriptor(plus_tag(latest,-2),0,35'h400000000,1,32'h8301,active_ctx);drain();
  if(request_cancelled_work<4||preserved_writes<45||cancel_restart_events!=1||same_cfg_cancel!=1)
   $fatal(1,"request-only chain coverage");
  $display("G0_FRONTEND_REQUEST_CANCEL_PASS cancel_cycles=%0d cancelled_work=%0d writes_during_cancel=%0d restart_during_cancel=%0d same_NCO_cfg_cancel=%0d idle_checks=%0d",request_cancel_cycles,request_cancelled_work,preserved_writes,cancel_restart_events,same_cfg_cancel,idle_checks);
'''.replace('@ABORT_SEG@',str(len(metadata)-1))
    return once(tb,'  repeat(20) @(negedge clk);\n  if(fp_head',extra+'  repeat(20) @(negedge clk);\n  if(fp_head')


def main(a):
    out=a.output.resolve();out.mkdir(parents=True,exist_ok=False)
    project=Path(__file__).resolve().parents[1];bp=a.vivado_bin.resolve()
    count,metadata,initial=make_vectors(out,[])
    sources=[]
    for name in ('nco_cost_probe.sv','rrc_cost_probe.sv','timing_request_cost_probe.sv','interpolation_cost_probe.sv','frontend_chain_cost_probe.sv'):
        dest=out/name;shutil.copyfile(project/'sim/g0'/name,dest);sources.append(str(dest))
    runs=[];receipt={'scope':'request-only cancellation, empty-path status, same-edge history restart; configured arithmetic only',
                      'clock_hz':50000000,'g0_passed':False,'routed_OOC':None,'checks':{}}
    def run(name,args):
        runs.append(args);(out/'commands.json').write_text(json.dumps(runs,indent=2)+'\n')
        with (out/(name+'.log')).open('w') as log:subprocess.run(args,cwd=out,stdout=log,stderr=subprocess.STDOUT,check=True)
    run('compile-sources',[str(bp/'xvlog'),'--sv',*sources])
    for name,tb,marker,base_marker,expression in (
        ('interpolation',interpolation_tb(),'G0_INTERPOLATION_REQUEST_CANCEL_PASS','G0_INTERPOLATION_PASS',("raw_result ^ W'(0)","raw_result ^ W'(1)")),
        ('frontend',frontend_tb(count,metadata,initial),'G0_FRONTEND_REQUEST_CANCEL_PASS','G0_FRONTEND_CHAIN_PASS',("raw_result ^ 84'd0","raw_result ^ 84'd1"))):
        module=name+'_cancel_tb';tb=once(tb,'module tb;',f'module {module};')
        (out/(module+'.sv')).write_text(tb)
        run(name+'-compile',[str(bp/'xvlog'),'--sv',str(out/(module+'.sv'))])
        run(name+'-elaborate',[str(bp/'xelab'),module,'-s',module,'-mt','off'])
        run(name+'-simulate',[str(bp/'xsim'),module,'-runall'])
        log=(out/(name+'-simulate.log')).read_text()
        if marker not in log or base_marker not in log or 'Fatal:' in log:raise ValueError(name+' cancel simulation failed')
        measured={k:int(v) for k,v in re.findall(r'(\w+)=(\d+)',log.split(marker)[-1].split('\n')[0])}
        receipt['checks'][name]=measured
        faultmodule=name+'_cancel_fault_tb';fault=once(tb,f'module {module};',f'module {faultmodule};')
        fault=once(fault,*expression);(out/(faultmodule+'.sv')).write_text(fault)
        run(name+'-fault-compile',[str(bp/'xvlog'),'--sv',str(out/(faultmodule+'.sv'))])
        run(name+'-fault-elaborate',[str(bp/'xelab'),faultmodule,'-s',faultmodule,'-mt','off'])
        run(name+'-fault-simulate',[str(bp/'xsim'),faultmodule,'-runall'])
        fl=(out/(name+'-fault-simulate.log')).read_text()
        rejection='Fatal: interpolation mismatch' if name=='interpolation' else 'Fatal: chain response mismatch'
        if rejection not in fl or marker in fl or base_marker in fl:raise ValueError(name+' negative control escaped')
    receipt['single_bit_controls_rejected']=True
    (out/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n');print(json.dumps(receipt))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--vivado-bin',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True);main(p.parse_args())
