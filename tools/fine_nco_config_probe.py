#!/usr/bin/env python3
"""Independent Fraction oracle, RTL controls and OOC for final NCO arithmetic.

This tests configuration calculation only, not sample-time NCO dispatch. The
admitted startup num/D is supplied as input; this engine does not repeat CFO or
quality admission. D=0 is an explicit identity-preserving error response.
"""
import argparse
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import random
import re
import shutil
import subprocess
import sys
import traceback
from fec_inverse_cost_probe import routed_measurements
from g0_fixed_startup_model import final_nco_configuration

MASK32=(1<<32)-1
MASK48=(1<<48)-1


def nearest(value):
    floor=value.numerator//value.denominator
    return floor+int(value-floor>=Fraction(1,2))


def golden(identifier,generation,numerator,divisor,coarse,tag,origin):
    if divisor==0:return (identifier,generation,tag,0,0,1)
    signed=coarse if coarse<1<<31 else coarse-(1<<32)
    correction=Fraction(signed)-Fraction(numerator,4*divisor)
    offset=((tag&MASK48)-origin)&MASK48
    step=nearest(correction)&MASK32;phase=nearest(correction*offset)&MASK32
    return (identifier,generation,tag,step,phase,0)


def pack(fields,widths):
    word=0
    for value,width in zip(fields,widths):word=(word<<width)|(int(value)&((1<<width)-1))
    return word


def vectors(fixture_directory):
    rng=random.Random(890166);rows=[];actual=[];model_checks=0
    def add(num,d,coarse,known,origin,epoch=0,op=0,mode=0,stage=0,iteration=0,kind='arithmetic'):
        nonlocal model_checks
        identifier=(0xfffffff0+len(rows)*13)&MASK32;generation=rng.randrange(1<<32)
        tag=(epoch<<48)|known;output=golden(identifier,generation,num,d,coarse,tag,origin)
        if d:
            model=final_nco_configuration(coarse,num,d,known,origin)
            assert (model['step_u32'],model['phase_u32'])==output[3:5]
            model_checks+=1
        rows.append({'op':op,'mode':mode,'stage':stage,'iteration':iteration,'kind':kind,
            'input':[identifier,generation,num,d,coarse,tag,origin],'expected':list(output)})
    # The earlier software audit's exact2940 set, now presented to real RTL.
    for coarse in [0,1,0x7fffffff,0x80000000,0xffffffff]:
        for b in range(8,29):
            d=8*b*(b*b-1)//3
            for num in [0,1,-1,d//2,-d//2,(1<<47)-1,-(1<<47)]:
                for known,origin in [(0,0),(160636,0),(0,MASK48),(MASK48,0)]:
                    add(num,d,coarse,known,origin,epoch=rng.randrange(1<<16))
    assert len(rows)==2940
    for d in (1,2,3,4,65534,65535):
        for coarse in (0,1,0x7fffffff,0x80000000,0xfffffffe,0xffffffff):
            for num in (-(1<<47),-(1<<47)+1,-6*d,-2*d,-2*d+1,-1,0,1,2*d-1,2*d,6*d,(1<<47)-1):
                for known,origin in ((0,0),(1,0),(0,1),(MASK48,0)):
                    add(num,d,coarse,known,origin,epoch=0xffff,mode=1,kind='fullrange_and_ties')
    for i in range(1024):
        add(rng.randrange(-(1<<47),1<<47),rng.randrange(1,1<<16),rng.randrange(1<<32),
            rng.randrange(1<<48),rng.randrange(1<<48),epoch=rng.randrange(1<<16),mode=i%3,kind='random')
    # Epoch must be copied, never included in the phase multiplication.
    for epoch in (0,1,0x8000,0xffff):
        add(-1234567890123,65535,0x80000000,19,MASK48-31,epoch=epoch,mode=1,kind='epoch_identity')
    for i in range(12):add((1<<47)-1 if i%2 else -(1<<47),0,rng.randrange(1<<32),MASK48-i,i,epoch=0xabcd,mode=i%3,kind='zero_divisor')
    # Every active state: actual first/middle/final iteration cancellation/reset.
    for op in (1,2):
        for stage,iterations in ((1,(0,47,95)),(2,(0,23,47)),(3,(0,47,95))):
            for iteration in iterations:
                add(-(1<<47),1,0x80000000,MASK48,0,epoch=0xbeef,op=op,stage=stage,iteration=iteration,kind='active_revoke')
                add(983040,65535,0x7fffffff,3,MASK48,epoch=0x1122,kind='recovery')
    for op in (3,4):
        for d in (0,1):
            add(-(1<<47),d,0xffffffff,MASK48,0,epoch=0xffff,op=op,mode=2,kind='held_revoke')
            add(-111,41600,253645233,157841,0,epoch=19,kind='recovery')
    for index in range(1,4):
        path=fixture_directory/f'packet-air-g12-long-r{index}-startup-0.json'
        fixture=json.loads(path.read_text());e=fixture['expected'];c=e['coarse_nco_config'];f=e['final_nco_config']
        expected=golden(0,0,e['num_s48'],e['D_u16'],c['step_u32'],e['commit_endpoint'],f['phase_origin_index48'])
        assert expected[3:5]==(f['step_u32'],f['phase_u32'])
        add(e['num_s48'],e['D_u16'],c['step_u32'],e['commit_endpoint'],f['phase_origin_index48'],epoch=0x9000+index,mode=2,kind='actual_startup')
        actual.append({'fixture':str(path),'num_s48':e['num_s48'],'D_u16':e['D_u16'],
            'coarse_step_u32':c['step_u32'],'known_index48':e['commit_endpoint'],
            'origin48':f['phase_origin_index48'],'expected_step_u32':f['step_u32'],'expected_phase_u32':f['phase_u32']})
    return rows,{'independent_Fraction_vectors':len(rows),'model_crosschecks':model_checks,'actual':actual,
        'original_2940_presented_to_RTL':True,'zero_divisor_contract':'error1, zero step/phase, identity unchanged'}


TB=r'''`timescale 1ns/1ps
module tb;
 reg clk=0;always #10 clk=~clk;
 reg rst=1,cancel=0,iv=0,hold=0;wire ir,ov,ready;
 reg [271:0] source=0;wire [31:0] iid,igen,coarse;wire signed [47:0] numerator;
 wire [15:0] divisor;wire [63:0] known;wire [47:0] origin;
 assign {iid,igen,numerator,divisor,coarse,known,origin}=source;
 wire [31:0] oid,ogen,raw_step,raw_phase;wire [63:0] tag;wire [1:0] error;
 wire [31:0] step=raw_step ^ @STEPFAULT@,phase=raw_phase ^ @PHASEFAULT@;
 wire [193:0] observed={oid,ogen,tag,step,phase,error};
 integer cycle=0,consumer_mode=0;
 assign ready=!hold && (consumer_mode!=1 || ((cycle%37)>=17 && (cycle%53)>=11));
 fine_nco_config_cost_probe dut(.clk(clk),.rst(rst),.cancel(cancel),.in_valid(iv),.in_ready(ir),
 .in_id(iid),.in_generation(igen),.in_numerator(numerator),.in_divisor(divisor),.in_coarse_step(coarse),
 .in_known_tag(known),.in_phase_origin_index(origin),.out_valid(ov),.out_ready(ready),
 .out_id(oid),.out_generation(ogen),.out_known_tag(tag),.out_step(raw_step),.out_phase(raw_phase),.out_error(error));
 integer fd,parsed,op,mode,target_state,target_iteration,accepted=0,produced=0,consumed=0;
 integer active_revoked=0,held_revoked=0,input_stalls=0,output_stalls=0,source_mutations=0;
 integer accepted_at=0,last_accept=0,prev_op=-1,prev_mode=-1,prev_divisor=0;
 integer service_intervals=0,min_ii=999999,max_ii=0,min_latency=999999,max_latency=0,zero_outputs=0;
 integer cancel_mask=0,reset_mask=0,control_bit=0,cancel_cycles=0,reset_cycles=0,latency;
 reg [193:0] gold,expected,held_output;reg [271:0] held_input;
 reg pending=0,seen=0,held=0,input_held=0;integer expected_error=0;
 always @(posedge clk) begin
  cycle=cycle+1;
  if(rst||cancel) begin
   if(pending) active_revoked=active_revoked+1;
   if(ov&&seen) held_revoked=held_revoked+1;
   pending=0;seen=0;held=0;input_held=0;prev_op=-1;prev_mode=-1;prev_divisor=0;
   if(rst) reset_cycles=reset_cycles+1;
   if(cancel) cancel_cycles=cancel_cycles+1;
  end else begin
   if(ir!==(!pending&&!ov)) $fatal(1,"capacity mismatch pending%0d ov%0d ir%0d",pending,ov,ir);
   if(held && (!ov || observed!==held_output)) $fatal(1,"held result changed");
   if(input_held && (!iv || source!==held_input)) $fatal(1,"held request changed");
   if(ov&&!seen) begin
    if(!pending || observed!==expected) $fatal(1,"configuration mismatch got%h expected%h",observed,expected);
    latency=cycle-accepted_at;
    if(latency!=(expected_error?1:241)) $fatal(1,"unexpected exact engine latency %0d",latency);
    if(expected_error) zero_outputs=zero_outputs+1;
    else begin if(latency<min_latency) min_latency=latency;if(latency>max_latency) max_latency=latency;end
    pending=0;seen=1;produced=produced+1;
   end
   held=ov&&!ready;if(held) begin held_output=observed;output_stalls=output_stalls+1;end
   if(ov&&ready) begin consumed=consumed+1;seen=0;end
   input_held=iv&&!ir;if(input_held) begin held_input=source;input_stalls=input_stalls+1;end
   if(iv&&ir) begin
    if(pending||ov) $fatal(1,"multiple active jobs");
    pending=1;expected=gold;expected_error=gold[1:0];accepted_at=cycle;accepted=accepted+1;
    if(op==0&&mode==0&&divisor!=0&&prev_op==0&&prev_mode==0&&prev_divisor!=0) begin
     latency=cycle-last_accept;if(latency!=242) $fatal(1,"saturated II not242 observed%0d",latency);
     service_intervals=service_intervals+1;if(latency<min_ii) min_ii=latency;if(latency>max_ii) max_ii=latency;
    end
    last_accept=cycle;prev_op=op;prev_mode=mode;prev_divisor=divisor;
   end
  end
  #1;if((rst||cancel)&&(ov||ir)) $fatal(1,"cancel/reset did not fence ports");
 end
 initial begin
  fd=$fopen("vectors.txt","r");repeat(3) @(negedge clk);rst=0;
  while(!$feof(fd)) begin
   parsed=$fscanf(fd,"%d %d %d %d %h %h\n",op,mode,target_state,target_iteration,source,gold);
   if(parsed!=6) $fatal(1,"vector parse");
   iv=1;@(posedge clk);while(!ir) @(posedge clk);
   @(negedge clk);iv=0;source=~source;source_mutations=source_mutations+1;consumer_mode=mode;
   if(op==1||op==2) begin
    while(dut.state!=target_state || dut.iteration!=target_iteration) @(negedge clk);
    control_bit=(target_state-1)*3+(target_iteration==0?0:target_iteration==(target_state==2?23:47)?1:2);
    if(op==1) begin cancel_mask=cancel_mask|(1<<control_bit);cancel=1;end
    else begin reset_mask=reset_mask|(1<<control_bit);rst=1;end
    repeat(2) @(negedge clk);cancel=0;rst=0;
   end else if(mode==2||op==3||op==4) begin
    hold=1;while(!ov) @(negedge clk);repeat(17) @(negedge clk);
    if(op==3) cancel=1;else if(op==4) rst=1;
    if(op!=0) begin repeat(2) @(negedge clk);cancel=0;rst=0;end
    hold=0;
   end
  end
  consumer_mode=0;hold=0;while(pending||ov) @(negedge clk);repeat(4) @(negedge clk);
  if(accepted!=@INPUTS@ || accepted!=produced+active_revoked || produced!=consumed+held_revoked ||
     active_revoked!=18 || held_revoked!=4 || cancel_mask!=511 || reset_mask!=511 ||
     zero_outputs!=14 || service_intervals<2900 || input_stalls<1000 || output_stalls<300 ||
     min_latency!=241 || max_latency!=241 || min_ii!=242 || max_ii!=242 || source_mutations!=accepted)
   $fatal(1,"coverage input%0d produced%0d consumed%0d active%0d held%0d zero%0d masks%0d/%0d ii%0d",accepted,produced,consumed,active_revoked,held_revoked,zero_outputs,cancel_mask,reset_mask,service_intervals);
  $display("G0_FINE_NCO_CONFIG_PASS inputs=%0d produced=%0d consumed=%0d active_revoked=%0d held_revoked=%0d zero_outputs=%0d input_stalls=%0d output_stalls=%0d source_mutations=%0d cancel_mask=%0d reset_mask=%0d service_intervals=%0d min_ii=%0d max_ii=%0d min_observed_latency=%0d max_observed_latency=%0d cancel_cycles=%0d reset_cycles=%0d",accepted,produced,consumed,active_revoked,held_revoked,zero_outputs,input_stalls,output_stalls,source_mutations,cancel_mask,reset_mask,service_intervals,min_ii,max_ii,min_latency,max_latency,cancel_cycles,reset_cycles);
  $finish;
 end
 initial begin #1000000000;$fatal(1,"timeout");end
endmodule
'''


def main(a):
    out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);project=Path(__file__).resolve().parents[1]
    receipt={'scope':'exact rational final NCO parameter engine only; no actual NCO dispatch, estimator admission or full G0',
        'status':'started','command':sys.argv,'clock_hz':50000000,'g0_passed':False,'hardware_touched':False,'new_RF_transmissions':False}
    def save(): (out/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
    save();runs=[]
    def run(name,args):
        runs.append(args);(out/'commands.json').write_text(json.dumps(runs,indent=2)+'\n')
        with (out/(name+'.log')).open('w') as log:subprocess.run(args,cwd=out,stdout=log,stderr=subprocess.STDOUT,check=True)
    try:
        rows,arithmetic=vectors(a.fixtures.resolve());receipt['arithmetic']=arithmetic
        source=out/'fine_nco_config_cost_probe.sv';shutil.copyfile(project/'sim/g0'/source.name,source)
        snapshot={}
        for file in [source,Path(__file__),project/'tools/g0_fixed_startup_model.py']:
            data=file.read_bytes();snapshot[file.name]={'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()}
        receipt['source_snapshot']=snapshot;save()
        (out/'vectors.txt').write_text(''.join(f"{v['op']} {v['mode']} {v['stage']} {v['iteration']} {pack(v['input'],[32,32,48,16,32,64,48]):068x} {pack(v['expected'],[32,32,64,32,32,2]):049x}\n" for v in rows))
        tb=TB.replace('@INPUTS@',str(len(rows))).replace('@STEPFAULT@',"32'd0").replace('@PHASEFAULT@',"32'd0")
        (out/'tb.sv').write_text(tb);bp=a.vivado_bin.resolve()
        run('compile',[str(bp/'xvlog'),'--sv',str(source),str(out/'tb.sv')])
        run('elaborate',[str(bp/'xelab'),'tb','-s','fine_nco_tb','-mt','off'])
        run('simulate',[str(bp/'xsim'),'fine_nco_tb','-runall'])
        log=(out/'simulate.log').read_text()
        if 'G0_FINE_NCO_CONFIG_PASS' not in log or 'Fatal:' in log:raise ValueError('fine NCO functional/control simulation failed')
        receipt['measured']={k:int(v) for k,v in re.findall(r'(\w+)=(\d+)',log.split('G0_FINE_NCO_CONFIG_PASS')[-1])}
        receipt['negative_controls']={}
        for name,original in [('step',"raw_step ^ 32'd0"),('phase',"raw_phase ^ 32'd0")]:
            fault=tb.replace('module tb;',f'module fault_{name};').replace(original,original.replace("32'd0","(ov?32'd1:32'd0)"))
            path=out/f'fault_{name}.sv';path.write_text(fault)
            run(f'fault-{name}-compile',[str(bp/'xvlog'),'--sv',str(path)])
            run(f'fault-{name}-elaborate',[str(bp/'xelab'),f'fault_{name}','-s',f'fault_{name}_tb','-mt','off'])
            run(f'fault-{name}-simulate',[str(bp/'xsim'),f'fault_{name}_tb','-runall'])
            fl=(out/f'fault-{name}-simulate.log').read_text()
            if 'Fatal: configuration mismatch' not in fl or 'G0_FINE_NCO_CONFIG_PASS' in fl:raise ValueError(name+' negative was not rejected')
            receipt['negative_controls'][name+'_bitflip_rejected']=True
        receipt['status']='simulation_passed';receipt['routed_OOC']=None;save()
        if not a.sim_only:
            (out/'ooc.tcl').write_text(f'''set_param general.maxThreads 4
read_verilog -sv {{{source}}}
synth_design -top fine_nco_config_cost_probe -part xc7z020clg484-1 -mode out_of_context
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
write_checkpoint -force fine_nco_config_cost_probe.dcp
''')
            run('ooc',[str(bp/'vivado'),'-mode','batch','-source',str(out/'ooc.tcl'),'-nolog','-nojournal'])
            receipt['routed_OOC']=routed_measurements(out)
        receipt['status']='passed';receipt['latency_contract']='240 internal edges after acceptance; pre-edge monitor/handshake sees result241cycles later; capacity next cycle gives saturated II242'
        receipt['complete_clocked_chain_or_NCO_dispatch']=False;save();print(json.dumps(receipt))
    except Exception as error:
        receipt['status']='failed';receipt['exception']=repr(error);receipt['traceback']=traceback.format_exc();save();raise


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--vivado-bin',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--fixtures',type=Path,default=Path('build/g0-fixed-startup-r1'));p.add_argument('--sim-only',action='store_true')
    main(p.parse_args())
