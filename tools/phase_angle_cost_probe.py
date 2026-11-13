#!/usr/bin/env python3
"""Iterative atan2 CORDIC: exact integer oracle, independent error bound, RTL/OOC.

Angles are turns modulo2**32; 0x80000000 represents -pi. Each coordinate is
signed61 with32 fractional bits. The bound applies to every nonzero signed27
integer input, including magnitude1, and includes all internal shift errors.
"""
import argparse
from decimal import Decimal, localcontext, ROUND_FLOOR
import json
import math
import operator
from pathlib import Path
import random
import re
import shutil
import subprocess
from fec_inverse_cost_probe import routed_measurements

INPUT_BITS=27
INTERNAL_BITS=61
FRACTION_BITS=32
ITERATIONS=32
PHASE_MASK=(1<<32)-1
PHASE_ERROR_BOUND_LSB=32
ATAN_TURNS=(536870912,316933406,167458907,85004756,42667331,21354465,
            10679838,5340245,2670163,1335087,667544,333772,166886,83443,
            41722,20861,10430,5215,2608,1304,652,326,163,81,41,20,10,5,3,1,1,0)


def cordic_phase(x,y):
    """Return(unsigned phase32,error1), using only exact integer operations."""
    x=operator.index(x);y=operator.index(y)
    if not -(1<<26)<=x<(1<<26) or not -(1<<26)<=y<(1<<26):
        raise ValueError('CORDIC inputs must fit signed27')
    zero=int(x==0 and y==0)
    vx=x<<FRACTION_BITS;vy=y<<FRACTION_BITS;phase=0
    if x<0:vx=-vx;vy=-vy;phase=1<<31
    for i,angle in enumerate(ATAN_TURNS):
        if vy>0:vx,vy,phase=vx+(vy>>i),vy-(vx>>i),(phase+angle)&PHASE_MASK
        elif vy<0:vx,vy,phase=vx-(vy>>i),vy+(vx>>i),(phase-angle)&PHASE_MASK
        if not -(1<<60)<=vx<(1<<60) or not -(1<<60)<=vy<(1<<60):
            raise AssertionError('internal coordinate overflow')
    return (0 if zero else phase),zero


def verify_table_and_bound():
    # Independent90-digit atan series plus Machin pi checks the fixed constants;
    # binary floating point is not used to generate the bit-exact oracle table.
    with localcontext() as ctx:
        ctx.prec=90
        def atan(x):
            term=x;total=x;n=1
            while abs(term)>Decimal('1e-85'):
                term=-term*x*x;total+=term/Decimal(2*n+1);n+=1
            return total
        pi=16*atan(Decimal(1)/5)-4*atan(Decimal(1)/239)
        for i,want in enumerate(ATAN_TURNS):
            exact=Decimal(1<<29) if i==0 else atan(Decimal(2)**(-i))*Decimal(1<<32)/(2*pi)
            got=int((exact+Decimal('.5')).to_integral_value(rounding=ROUND_FLOOR))
            if got!=want:raise AssertionError('atan table rounding mismatch')
    # Let A=2**F, the smallest nonzero vector length. Each iteration's pair of
    # arithmetic-floor shifts injects a vector error of norm<sqrt2. Since each
    # ideal rotation has gain>=1, every computed norm is>=A-32sqrt2. Per-step
    # angular perturbation is<=asin(sqrt2/(A-32sqrt2)); their sum is E below.
    # After quadrant preprocessing |theta0|<=pi/2. Greedy vectoring and
    # atan(2x)<=2atan(x) give |theta_final|<=atan(2**-31)+E, also when y==0
    # stops early. Relating that residual to initial angle adds another E.
    # The32 nearest-integer table entries add at most16 phase LSB total.
    e=32*math.asin(math.sqrt(2)/((1<<FRACTION_BITS)-32*math.sqrt(2)))
    bound=(math.atan(2.0**-31)+2*e)*(1<<32)/(2*math.pi)+16
    if not bound<PHASE_ERROR_BOUND_LSB:raise AssertionError('analytical error bound exceeds contract')
    return {'analytic_phase_error_bound_LSB':bound,'declared_bound_LSB':PHASE_ERROR_BOUND_LSB,
            'bound_equation':'(atan(2^-31)+2*32*asin(sqrt(2)/(2^32-32*sqrt(2))))*2^32/(2*pi)+16',
            'minimum_nonzero_input_norm':1,'shift_error_included':True,
            'table_rounding':'nearest integer, ties toward positive infinity; independently verified at90decimal digits',
            'pi_mapping':'0x80000000 is signed -pi; phase differences measured modulo2^32',
            'y_zero_rule':'hold coordinates/phase for remaining iterations; fixed latency retained',
            'coordinate_overflow_bound':'1.647*(sqrt(2)*2^58+32*sqrt(2)) < 2^60 signed61 limit'}


def make_vectors():
    rng=random.Random(867234);rows=[]
    def add(x,y,mode=0,kind=0,delay=0):
        phase,error=cordic_phase(x,y);identity=(0xfffffff0+len(rows)*13)&PHASE_MASK
        rows.append((kind,mode,delay,identity,x,y,phase,error))
    for x in range(-16,17):
        for y in range(-16,17):add(x,y)
    rails=(-(1<<26),-(1<<26)+1,-32768,-2,-1,0,1,2,32767,(1<<26)-2,(1<<26)-1)
    for x in rails:
        for y in rails:add(x,y,mode=1)
    for i in range(4096):
        add(rng.randrange(-(1<<26),1<<26),rng.randrange(-(1<<26),1<<26),mode=int(i%5==0))
    for scale in (1,3,127,32767,(1<<26)-1):
        for x,y in ((scale,0),(-scale,0),(0,scale),(0,-scale)):
            add(x,y,mode=2,kind=2,delay=17)
    for kind in (3,4):
        for stage in (1,16,32):
            add(-(1<<26),1,kind=kind,delay=stage)
            add(1,-(1<<26))
    for kind in (5,6):
        add(-(1<<26),-(1<<26),mode=2,kind=kind,delay=17)
        add(0,0)
    return rows


TB=r'''`timescale 1ns/1ps
module tb;
 reg clk=0;always #10 clk=~clk;
 reg rst=1,cancel=0,iv=0,hold=0;
 wire ir,ov,ready,error;reg [31:0] iid=0;reg signed [26:0] ix=0,iy=0;
 wire [31:0] oid,raw_phase;wire [31:0] phase=raw_phase ^ @FAULT@;
 integer consumer_mode=0,mode=0,cycle=0;
 assign ready=!hold && (consumer_mode!=1 || !((cycle%97<19)||(cycle%113<11)));
 phase_angle_cost_probe dut(.clk(clk),.rst(rst),.cancel(cancel),.in_valid(iv),.in_ready(ir),
 .in_id(iid),.in_x(ix),.in_y(iy),.out_valid(ov),.out_ready(ready),
 .out_id(oid),.out_phase(raw_phase),.out_error(error));
 integer fd,n,kind,delay,gerror,accepted=0,produced=0,consumed=0,abort_active=0,abort_held=0;
 integer input_stalls=0,output_stalls=0,service_intervals=0,min_ii=999,max_ii=0;
 integer accepted_cycle=0,last_accept=0,previous_mode=-1,previous_kind=-1;
 integer cancel_cycles=0,reset_cycles=0,zero_outputs=0,min_latency=999,max_latency=0;
 reg [31:0] gphase,expected_phase,expected_id,held_phase,held_id,held_iid;
 reg expected_error,held_error,pending=0,seen=0,held=0,input_held=0;
 reg signed [26:0] held_ix,held_iy;
 always @(posedge clk) begin
  cycle=cycle+1;
  if(rst||cancel) begin
   if(pending) abort_active=abort_active+1;
   if(ov&&seen) abort_held=abort_held+1;
   pending=0;seen=0;held=0;input_held=0;previous_kind=-1;previous_mode=-1;
   if(cancel) cancel_cycles=cancel_cycles+1;
   if(rst) reset_cycles=reset_cycles+1;
  end else begin
   if(held && (!ov || {oid,phase,error}!=={held_id,held_phase,held_error})) $fatal(1,"held output changed");
   if(input_held && (!iv || {iid,ix,iy}!=={held_iid,held_ix,held_iy})) $fatal(1,"held input changed");
   if(pending&&!ov&&ir) $fatal(1,"accepted capacity while active");
   if(ov&&!ready&&ir) $fatal(1,"capacity while held output");
   if(ov&&!seen) begin
    if(!pending || {oid,phase,error}!=={expected_id,expected_phase,expected_error})
     $fatal(1,"phase mismatch id%h/%h phase%h/%h error%0d/%0d",oid,expected_id,phase,expected_phase,error,expected_error);
    if(cycle-accepted_cycle!=33) $fatal(1,"CORDIC result latency not33 observed clocks");
    if(cycle-accepted_cycle<min_latency) min_latency=cycle-accepted_cycle;
    if(cycle-accepted_cycle>max_latency) max_latency=cycle-accepted_cycle;
    if(error) zero_outputs=zero_outputs+1;
    pending=0;seen=1;produced=produced+1;
   end
   held=ov&&!ready;
   if(held) begin held_id=oid;held_phase=phase;held_error=error;output_stalls=output_stalls+1;end
   if(ov&&ready) begin seen=0;consumed=consumed+1;end
   input_held=iv&&!ir;
   if(input_held) begin held_iid=iid;held_ix=ix;held_iy=iy;input_stalls=input_stalls+1;end
   if(iv&&ir) begin
    if(pending) $fatal(1,"two active inputs");
    pending=1;expected_id=iid;expected_phase=gphase;expected_error=1'(gerror);accepted_cycle=cycle;
    if(kind==0&&mode==0&&previous_kind==0&&previous_mode==0) begin
     if(cycle-last_accept!=33) $fatal(1,"saturated interval not33");
     if(cycle-last_accept<min_ii) min_ii=cycle-last_accept;
     if(cycle-last_accept>max_ii) max_ii=cycle-last_accept;
     service_intervals=service_intervals+1;
    end
    previous_mode=mode;previous_kind=kind;last_accept=cycle;accepted=accepted+1;
   end
  end
  #1;if((rst||cancel)&&(ov||ir)) $fatal(1,"reset/cancel did not retract");
 end
 initial begin
  fd=$fopen("vectors.txt","r");repeat(3) @(negedge clk);rst=0;
  while(!$feof(fd)) begin
   n=$fscanf(fd,"%d %d %d %h %h %h %h %d\n",kind,mode,delay,iid,ix,iy,gphase,gerror);
   if(n!=8) $fatal(1,"vector parse");
   iv=1;@(posedge clk);while(!ir) @(posedge clk);
   @(negedge clk);iv=0;consumer_mode=mode;
   if(kind==2 || kind==5 || kind==6) begin
    hold=1;while(!ov) @(negedge clk);
    repeat(delay) @(negedge clk);
    if(kind==5) cancel=1;else if(kind==6) rst=1;
    if(kind!=2) begin repeat(2) @(negedge clk);cancel=0;rst=0;end
    hold=0;
   end else if(kind==3 || kind==4) begin
    repeat(delay-1) @(negedge clk);
    if(kind==3) cancel=1;else rst=1;
    repeat(2) @(negedge clk);cancel=0;rst=0;
   end
  end
  consumer_mode=0;hold=0;
  while(pending||ov) @(negedge clk);
  repeat(4) @(negedge clk);
  if(accepted!=@INPUTS@ || accepted!=produced+abort_active || produced!=consumed+abort_held ||
     abort_active!=6 || abort_held!=2 || zero_outputs<4 || service_intervals<3000 ||
     input_stalls==0 || output_stalls<300 || min_ii!=33 || max_ii!=33 ||
     cancel_cycles!=8 || min_latency!=33 || max_latency!=33)
   $fatal(1,"coverage input%0d produced%0d consumed%0d abort%0d/%0d zero%0d interval%0d",accepted,produced,consumed,abort_active,abort_held,zero_outputs,service_intervals);
  $display("G0_PHASE_ANGLE_PASS inputs=%0d outputs_produced=%0d outputs_consumed=%0d active_cancelled=%0d held_cancelled=%0d zero_outputs=%0d service_intervals=%0d min_ii=%0d max_ii=%0d input_stalls=%0d output_stalls=%0d cancel_cycles=%0d reset_cycles=%0d min_observed_latency=%0d max_observed_latency=%0d",accepted,produced,consumed,abort_active,abort_held,zero_outputs,service_intervals,min_ii,max_ii,input_stalls,output_stalls,cancel_cycles,reset_cycles,min_latency,max_latency);
  $finish;
 end
 initial begin #100000000;$fatal(1,"timeout");end
endmodule
'''


def main(a):
    out=a.output.resolve();out.mkdir(parents=True,exist_ok=False)
    project=Path(__file__).resolve().parents[1]
    bound=verify_table_and_bound();rows=make_vectors();max_error=0.;worst=None
    for kind,mode,delay,identity,x,y,phase,error in rows:
        if not error:
            exact=math.atan2(y,x)*(1<<32)/(2*math.pi)
            difference=abs((phase-exact+(1<<31))%(1<<32)-(1<<31))
            if difference>max_error:max_error=difference;worst=[x,y,phase]
            if difference>PHASE_ERROR_BOUND_LSB:raise AssertionError('independent atan2 error exceeds analytical bound')
    (out/'vectors.txt').write_text(''.join(f'{k} {m} {d} {i:08x} {x&0x7ffffff:07x} {y&0x7ffffff:07x} {p:08x} {e}\n' for k,m,d,i,x,y,p,e in rows))
    (out/'atan_table.json').write_text(json.dumps(ATAN_TURNS)+'\n')
    tb=TB.replace('@INPUTS@',str(len(rows))).replace('@FAULT@',"32'd0")
    (out/'tb.sv').write_text(tb)
    source=out/'phase_angle_cost_probe.sv';shutil.copyfile(project/'sim/g0'/source.name,source)
    bp=a.vivado_bin.resolve();runs=[]
    def run(name,args):
        runs.append(args);(out/'commands.json').write_text(json.dumps(runs,indent=2)+'\n')
        with (out/(name+'.log')).open('w') as log:subprocess.run(args,cwd=out,stdout=log,stderr=subprocess.STDOUT,check=True)
    run('compile',[str(bp/'xvlog'),'--sv',str(source),str(out/'tb.sv')])
    run('elaborate',[str(bp/'xelab'),'tb','-s','phase_angle_tb','-mt','off'])
    run('simulate',[str(bp/'xsim'),'phase_angle_tb','-runall'])
    sim=(out/'simulate.log').read_text()
    if 'G0_PHASE_ANGLE_PASS' not in sim or 'Fatal:' in sim:raise ValueError('CORDIC simulation failed')
    measured={k:int(v) for k,v in re.findall(r'(\w+)=(\d+)',sim.split('G0_PHASE_ANGLE_PASS')[-1])}
    fault=tb.replace('module tb;','module fault_tb;').replace("raw_phase ^ 32'd0","raw_phase ^ (ov?32'd1:32'd0)")
    (out/'fault_tb.sv').write_text(fault)
    run('fault-compile',[str(bp/'xvlog'),'--sv',str(out/'fault_tb.sv')])
    run('fault-elaborate',[str(bp/'xelab'),'fault_tb','-s','phase_angle_fault_tb','-mt','off'])
    run('fault-simulate',[str(bp/'xsim'),'phase_angle_fault_tb','-runall'])
    fl=(out/'fault-simulate.log').read_text()
    if 'Fatal: phase mismatch' not in fl or 'G0_PHASE_ANGLE_PASS' in fl:raise ValueError('bitflip not rejected')
    receipt={'scope':'G0 reusable iterative atan2 arithmetic/service only; no training/CFO estimator or complete PHY',
             'clock_hz':50000000,'input_signed_bits':INPUT_BITS,'internal_signed_bits':INTERNAL_BITS,
             'internal_fraction_bits':FRACTION_BITS,'iterations':ITERATIONS,'phase_turn_bits':32,
             'integer_oracle':'exact integer vectoring recurrence with independently Decimal-verified constants',
             'analytical_bound':bound,'maximum_observed_math_atan2_error_phase_LSB':max_error,
             'worst_math_atan2_case_x_y_phase':worst,'gold_vectors':len(rows),
             'measured':measured,'service_angles_per_second':50000000/measured['max_ii'],
             'latency_note':'32 iteration edges after acceptance; first pre-edge monitor/handshake sees result at33 clocks',
             'single_bit_phase_fault_rejected':True,'routed_OOC':None,'g0_passed':False}
    (out/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
    if not a.sim_only:
        (out/'ooc.tcl').write_text(f'''set_param general.maxThreads 4
read_verilog -sv {{{source}}}
synth_design -top phase_angle_cost_probe -part xc7z020clg484-1 -mode out_of_context
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
write_checkpoint -force phase_angle_cost_probe.dcp
''')
        run('ooc',[str(bp/'vivado'),'-mode','batch','-source',str(out/'ooc.tcl'),'-nolog','-nojournal'])
        receipt['routed_OOC']=routed_measurements(out)
        (out/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
    print(json.dumps(receipt))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--vivado-bin',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True);p.add_argument('--sim-only',action='store_true')
    main(p.parse_args())
