#!/usr/bin/env python3
"""Independent big-integer oracle/TB and routed cost for configured timing requests."""
import argparse
import json
from pathlib import Path
import random
import re
import shutil
import subprocess
from fec_inverse_cost_probe import routed_measurements

Q32=1<<32
MASK80=(1<<80)-1
MIN_STEP=(4*Q32*4999+2500)//5000
MAX_STEP=(4*Q32*5001+2500)//5000


def descriptors(reports):
    rng=random.Random(762325);rows=[];actual=[]
    def add(position,step=4*Q32,count=23,mode=0,op=0,after=0,epoch=0x1234,identifier=0):
        generation=rng.randrange(1<<32)
        error=1 if count==0 or count>20000 else (2 if not MIN_STEP<=step<=MAX_STEP else 0)
        outputs=0 if error else (after if op else count)
        rows.append((op,mode,after,generation,epoch,position&MASK80,step,count,identifier,error,outputs))
    for fraction in (0,0x7fff,0x8000,0xffff,0xffff7fff,0xffff8000,0xffffffff):
        add((123<<32)|fraction,identifier=0xfffffff0)
        add((((1<<48)-1)<<32)|fraction,step=MIN_STEP,mode=1,identifier=0xfffffffe)
    add(MASK80-100,MAX_STEP,20000)
    add((15<<32)|0x8000,MIN_STEP,20000)
    for j in range(40):
        add(rng.randrange(1<<80),rng.randint(MIN_STEP,MAX_STEP),rng.randrange(1,130),mode=j%2,
            epoch=rng.randrange(1<<16),identifier=rng.randrange(1<<32))
    # Last valid counts and illegal values, including count-priority collision.
    add(0,count=1);add(0,count=0);add(0,count=20001);add(0,count=65535)
    add(0,MIN_STEP-1);add(0,MAX_STEP+1);add(0,0);add(0,(1<<35)-1)
    add(0,0,count=0);add(0,MAX_STEP+1,count=20001)
    # Cancel/reset before first request and after partial service; then reconfigure.
    for op in (1,2):
        for emitted in (0,1,19):
            add(MASK80-32768,MAX_STEP,count=99,op=op,after=emitted)
            add((op<<64)|0xffff8000,MIN_STEP,count=29,mode=1,epoch=0xabcd)
    for i,path in enumerate(reports):
        source=json.loads(path.read_text());calls=source['sampling_candidate_calls']
        c=calls[0];first=int(c['first_position_q32']);step=int(c['step_q32']);count=int(c['symbol_count'])
        if count!=20000 or not MIN_STEP<=step<=MAX_STEP:raise ValueError('actual configuration outside contract')
        # Software report coordinates are capture-relative RRC endpoints.
        # Subtract20 integer samples once; assign an unrelated explicit epoch.
        center=(first-20*Q32)&MASK80;epoch=0x9000+i
        add(center,step,count,mode=i%2,epoch=epoch,identifier=0xfffffff0)
        actual.append({'report':str(path.resolve()),'source_first_endpoint_position_q32':first,
                       'first_center_position_q32':center,'step_q32':step,'count':count,'epoch':epoch,
                       'coordinate_conversion':'capture-relative endpoint minus20 samples; epoch separately assigned'})
    return rows,actual


TB=r'''`timescale 1ns/1ps
module tb;
 reg clk=0;always #10 clk=~clk;
 reg rst=1,cancel=0,cv=0,hold=0;wire cr,rv,rr,done;wire [1:0] error;
 reg [31:0] cg=0,ci=0;reg [15:0] ce=0,cc=0;reg [79:0] cp=0;reg [34:0] cs=0;
 wire [31:0] rg,ri;wire [63:0] raw_tag;wire [15:0] raw_mu,emitted;
 wire [63:0] tag=raw_tag ^ @TAG_FAULT@;
 wire [15:0] mu=raw_mu ^ @MU_FAULT@;
 integer mode=0,live_mode=0,cycle=0;
 assign rr=!hold && (live_mode==0 || ((cycle%31)>=7 && (cycle%53)>=11));
 timing_request_cost_probe dut(.clk(clk),.rst(rst),.cancel(cancel),
 .cfg_valid(cv),.cfg_ready(cr),.cfg_generation(cg),.cfg_epoch(ce),.cfg_first_position(cp),
 .cfg_step(cs),.cfg_count(cc),.cfg_first_id(ci),.req_valid(rv),.req_ready(rr),
 .req_generation(rg),.req_id(ri),.req_tag(raw_tag),.req_mu(raw_mu),
 .done(done),.cfg_error(error),.emitted_count(emitted));
 integer model_remaining=0,model_emitted=0,model_error=0,model_done=0;
 integer fd,ef,n,op,after_count,expected_error,unused_outputs,outputs=0,configs=0;
 integer good_configs=0,error1=0,error2=0,done_count=0,cancel_cycles=0,reset_cycles=0;
 integer request_stalls=0,config_stalls=0,service_intervals=0,min_ii=999,max_ii=0;
 integer group_count=0,last_request=-1,aborted_descriptors=0;
 reg held_req=0,held_cfg=0;
 reg [31:0] hg,hi,eg,ei,hcg,hci;reg [63:0] ht,et;reg [15:0] hm,em,hce,hcc;
 reg [79:0] hcp;reg [34:0] hcs;
 always @(posedge clk) begin
  cycle=cycle+1;model_done=0;model_error=0;
  if(rst||cancel) begin
   if(model_remaining>0) aborted_descriptors=aborted_descriptors+1;
   model_remaining=0;model_emitted=0;held_req=0;held_cfg=0;group_count=0;
   if(cancel) cancel_cycles=cancel_cycles+1;
   if(rst) reset_cycles=reset_cycles+1;
  end else begin
   if(cr!==(model_remaining==0)) $fatal(1,"configuration admission mismatch");
   if(held_req && (!rv || {rg,ri,tag,mu}!=={hg,hi,ht,hm})) $fatal(1,"held request changed");
   if(held_cfg && (!cv || {cg,ce,cp,cs,cc,ci}!=={hcg,hce,hcp,hcs,hcc,hci}))
    $fatal(1,"held configuration changed");
   held_req=rv&&!rr;held_cfg=cv&&!cr;
   if(held_req) begin hg=rg;hi=ri;ht=tag;hm=mu;request_stalls=request_stalls+1;end
   if(held_cfg) begin hcg=cg;hce=ce;hcp=cp;hcs=cs;hcc=cc;hci=ci;config_stalls=config_stalls+1;end
   if(rv&&rr) begin
    if(model_remaining<=0) $fatal(1,"unsolicited request");
    n=$fscanf(ef,"%h %h %h %h\n",eg,ei,et,em);
    if(n!=4 || {rg,ri,tag,mu}!=={eg,ei,et,em})
     $fatal(1,"timing request mismatch ordinal%0d got%h/%h/%h/%h expected%h/%h/%h/%h",outputs,rg,ri,tag,mu,eg,ei,et,em);
    outputs=outputs+1;model_emitted=model_emitted+1;model_remaining=model_remaining-1;
    if(model_remaining==0) begin model_done=1;done_count=done_count+1;end
    if(live_mode==0 && group_count>0) begin
     if(cycle-last_request!=1) $fatal(1,"saturated service notII1");
     if(cycle-last_request<min_ii) min_ii=cycle-last_request;
     if(cycle-last_request>max_ii) max_ii=cycle-last_request;
     service_intervals=service_intervals+1;
    end
    group_count=group_count+1;last_request=cycle;
   end
   if(cv&&cr) begin
    configs=configs+1;model_emitted=0;model_error=expected_error;live_mode=mode;group_count=0;
    if(expected_error==0) begin model_remaining=cc;good_configs=good_configs+1;end
    else begin
     model_remaining=0;
     if(expected_error==1) error1=error1+1;else if(expected_error==2) error2=error2+1;
    end
   end
  end
  #1;
  if(done!==1'(model_done) || error!==2'(model_error) || emitted!==16'(model_emitted))
   $fatal(1,"status mismatch done%0d/%0d err%0d/%0d emitted%0d/%0d",done,model_done,error,model_error,emitted,model_emitted);
  if(rv!==(!rst&&!cancel&&model_remaining>0) || cr!==(!rst&&!cancel&&model_remaining==0))
   $fatal(1,"valid/ready state mismatch");
 end
 initial begin
  fd=$fopen("descriptors.txt","r");ef=$fopen("expected.txt","r");
  repeat(3) @(negedge clk);rst=0;
  while(!$feof(fd)) begin
   n=$fscanf(fd,"%d %d %d %h %h %h %h %h %h %d %d\n",op,mode,after_count,cg,ce,cp,cs,cc,ci,expected_error,unused_outputs);
   if(n!=11) $fatal(1,"descriptor parse");
   cv=1;@(posedge clk);while(!cr) @(posedge clk);
   @(negedge clk);cv=0;
   if(op!=0) begin
    while(emitted<after_count) @(negedge clk);
    hold=1;repeat(13) @(negedge clk);
    if(!rv || done) $fatal(1,"abort must have a held request");
    if(op==1) cancel=1;else rst=1;
    repeat(2) @(negedge clk);cancel=0;rst=0;hold=0;
    repeat(5) @(negedge clk);
    if(rv || done || error || emitted) $fatal(1,"abort did not clear");
   end
  end
  while(model_remaining>0) @(negedge clk);
  repeat(5) @(negedge clk);
  if(outputs!=@OUTPUTS@ || configs!=@CONFIGS@ || done_count!=@DONE@ || error1!=@ERROR1@ || error2!=@ERROR2@ ||
     aborted_descriptors!=6 || service_intervals<30000 || min_ii!=1 || max_ii!=1 ||
     request_stalls<100 || config_stalls<100 || cancel_cycles!=6)
   $fatal(1,"coverage outputs%0d configs%0d done%0d err%0d/%0d aborted%0d ii%0d/%0d",outputs,configs,done_count,error1,error2,aborted_descriptors,min_ii,max_ii);
  if(!$feof(ef)) $fatal(1,"unconsumed expected requests");
  $display("G0_TIMING_REQUEST_PASS requests=%0d configs=%0d good_configs=%0d done=%0d err1=%0d err2=%0d aborted=%0d request_stalls=%0d config_stalls=%0d service_intervals=%0d min_ii=%0d max_ii=%0d cancel_cycles=%0d reset_cycles=%0d",outputs,configs,good_configs,done_count,error1,error2,aborted_descriptors,request_stalls,config_stalls,service_intervals,min_ii,max_ii,cancel_cycles,reset_cycles);
  $finish;
 end
 initial begin #100000000;$fatal(1,"timeout");end
endmodule
'''


def main(a):
    out=a.output.resolve();out.mkdir(parents=True,exist_ok=False)
    project=Path(__file__).resolve().parents[1];rows,actual=descriptors(a.report)
    expected=[]
    for op,mode,after,g,e,p,s,count,i,error,outputs in rows:
        for k in range(outputs):
            # Direct closed form with Python unbounded integers; not a register-step simulation.
            position=(p+k*s)&MASK80
            q16=(position+32768)//65536
            tag=(e<<48)|((q16//65536)&((1<<48)-1));mu=q16%65536
            expected.append((g,(i+k)&0xffffffff,tag,mu))
    (out/'descriptors.txt').write_text(''.join(f'{op} {mode} {after} {g:08x} {e:04x} {p:020x} {s:09x} {c:04x} {i:08x} {err} {outputs}\n' for op,mode,after,g,e,p,s,c,i,err,outputs in rows))
    (out/'expected.txt').write_text(''.join(f'{g:08x} {i:08x} {t:016x} {u:04x}\n' for g,i,t,u in expected))
    tb=TB.replace('@TAG_FAULT@',"64'd0").replace('@MU_FAULT@',"16'd0")
    summary={'OUTPUTS':len(expected),'CONFIGS':len(rows),'DONE':sum(r[0]==0 and r[9]==0 for r in rows),
             'ERROR1':sum(r[9]==1 for r in rows),'ERROR2':sum(r[9]==2 for r in rows)}
    for k,v in summary.items():tb=tb.replace('@'+k+'@',str(v))
    (out/'tb.sv').write_text(tb)
    source=out/'timing_request_cost_probe.sv';shutil.copyfile(project/'sim/g0'/source.name,source)
    bp=a.vivado_bin.resolve();runs=[]
    def run(name,args):
        runs.append(args);(out/'commands.json').write_text(json.dumps(runs,indent=2)+'\n')
        with (out/(name+'.log')).open('w') as log:subprocess.run(args,cwd=out,stdout=log,stderr=subprocess.STDOUT,check=True)
    run('compile',[str(bp/'xvlog'),'--sv',str(source),str(out/'tb.sv')])
    run('elaborate',[str(bp/'xelab'),'tb','-s','timing_request_tb','-mt','off'])
    run('simulate',[str(bp/'xsim'),'timing_request_tb','-runall'])
    sim=(out/'simulate.log').read_text()
    if 'G0_TIMING_REQUEST_PASS' not in sim or 'Fatal:' in sim:raise ValueError('timing request simulation failed')
    measured={k:int(v) for k,v in re.findall(r'(\w+)=(\d+)',sim.split('G0_TIMING_REQUEST_PASS')[-1])}
    faults={}
    for label,expr,replacement in [('mu',"raw_mu ^ 16'd0","raw_mu ^ 16'd1"),('tag',"raw_tag ^ 64'd0","raw_tag ^ 64'd1")]:
        name='fault_'+label;fault=tb.replace('module tb;',f'module {name};').replace(expr,replacement)
        (out/(name+'.sv')).write_text(fault)
        run(name+'-compile',[str(bp/'xvlog'),'--sv',str(out/(name+'.sv'))])
        run(name+'-elaborate',[str(bp/'xelab'),name,'-s',name,'-mt','off'])
        run(name+'-simulate',[str(bp/'xsim'),name,'-runall'])
        fl=(out/(name+'-simulate.log')).read_text()
        if 'Fatal: timing request mismatch' not in fl or 'G0_TIMING_REQUEST_PASS' in fl:raise ValueError('negative control escaped: '+label)
        faults[label]=True
    receipt={'scope':'G0 configured timing-request accumulator only; no timing estimator or complete PHY',
             'clock_hz':50000000,'input_coordinate':'unsigned80 Q32 RRC center; separate16-bit epoch',
             'step_admission_Q32':{'min':MIN_STEP,'max':MAX_STEP,'definition':'nearestQ32(4*(1 +/- 1/5000))'},
             'max_count':20000,'oracle':'Python big-integer closed-form p0+k*step, modulo80 then nearestQ16 ties+infinity',
             'measured':measured,'negative_controls_rejected':faults,'actual_report_replays':actual,
             'routed_OOC':None,'g0_passed':False}
    (out/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
    if not a.sim_only:
        (out/'ooc.tcl').write_text(f'''set_param general.maxThreads 4
read_verilog -sv {{{source}}}
synth_design -top timing_request_cost_probe -part xc7z020clg484-1 -mode out_of_context
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
write_checkpoint -force timing_request_cost_probe.dcp
''')
        run('ooc',[str(bp/'vivado'),'-mode','batch','-source',str(out/'ooc.tcl'),'-nolog','-nojournal'])
        receipt['routed_OOC']=routed_measurements(out)
        (out/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
    print(json.dumps(receipt))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--vivado-bin',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True);p.add_argument('--sim-only',action='store_true')
    p.add_argument('--report',type=Path,action='append',default=[]);main(p.parse_args())
