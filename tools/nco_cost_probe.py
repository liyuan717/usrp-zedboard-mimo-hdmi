#!/usr/bin/env python3
"""Independent full-circle oracle and routed G0 common-phase NCO cost probe.

Only diagnostic configured phase/step is tested, not CFO estimation. The integer
oracle computes full-circle sin/cos directly rather than using RTL quarterwave
address reflection. Float bounds refer to the exact quantized 32-bit step;
requested-frequency rounding is separately reported, not hidden in that bound.
"""
import argparse
import json
import hashlib
from pathlib import Path
import re
import shutil
import subprocess
import zlib
import numpy as np
from fec_inverse_cost_probe import routed_measurements


def pack(values,width):return sum((int(x)&((1<<width)-1))<<(i*width) for i,x in enumerate(values))


def main(a):
    project=Path(__file__).resolve().parents[1];out=a.output.resolve();out.mkdir(parents=True,exist_ok=False)
    sine=np.rint(65536*np.sin(np.arange(1024)*2*np.pi/4096)).astype(np.int64)
    (out/'sine.hex').write_text(''.join(f'{int(x):05x}\n' for x in sine))
    angle=2*np.pi*np.arange(4096)/4096
    full_c=np.rint(65536*np.cos(angle)).astype(np.int64);full_s=np.rint(65536*np.sin(angle)).astype(np.int64)
    assert int(np.max(abs(full_c)+abs(full_s)))==92682
    rng=np.random.default_rng(32409616);mask=(1<<48)-1
    commands=[];expected=[];conjugate=False;sense_outputs=[0,0];sense_bins=[set(),set()];phase=step=tag=0;first=True;armed=False;inputs=0;fences=0;config_count=0
    max_error=0.;max_bound=0.;covered=set();adc_receipts=[];frequency_cases=[];positive_ties=negative_ties=0
    def cfg(first_tag,initial,increment,mode=0,hold=0,sense=0):
        nonlocal tag,phase,step,first,armed,config_count,conjugate
        tag=first_tag;phase=initial&0xffffffff;step=increment&0xffffffff;first=True;armed=True;config_count+=1
        conjugate=bool(sense and a.enable_conjugate)
        commands.append((0,mode,hold,tag,phase,step,int(conjugate)))
    def emit(values,actual_tag=None,mode=0,period=0,reset=False):
        nonlocal tag,phase,first,armed,inputs,fences,max_error,max_bound,positive_ties,negative_ties
        actual_tag=tag if actual_tag is None else actual_tag
        values=np.array(values,dtype=np.int64)
        if np.any(values< -32768) or np.any(values>32767):raise ValueError('input width')
        commands.append((3 if reset else 1,mode,period,actual_tag,0,0,pack(values,16)));inputs+=1
        if reset:armed=False;fences=0;return
        if actual_tag!=tag:
            assert armed;armed=False;fences+=1;return
        assert armed
        bin_index=((phase+0x80000)&0xffffffff)>>20;covered.add(bin_index)
        sense_bins[int(conjugate)].add(bin_index);sense_outputs[int(conjugate)]+=1
        c=int(full_c[bin_index]);s=int(full_s[bin_index]);acc=[]
        for rx in range(2):
            re,im=map(int,values[rx*2:rx*2+2]);im=-im if conjugate else im
            acc.extend((re*c-im*s,re*s+im*c))
        acc=np.array(acc,dtype=np.int64);result=(acc+16384)//32768
        positive_ties+=int(np.sum((acc>0)&(acc%32768==16384)))
        negative_ties+=int(np.sum((acc<0)&(acc%32768==16384)))
        ideal=(values[::2]+(-1j if conjugate else 1j)*values[1::2])*2*np.exp(2j*np.pi*phase/2**32)
        floating=np.column_stack((ideal.real,ideal.imag)).ravel()
        errors=abs(result-floating)
        for rx in range(2):
            re,im=map(int,values[rx*2:rx*2+2])
            bound=4*np.hypot(re,im)*np.sin(np.pi/8192)+(abs(re)+abs(im))/65536+.5
            if np.max(errors[rx*2:rx*2+2])>bound+1e-7:raise ValueError('local float bound exceeded')
            max_bound=max(max_bound,float(bound))
        max_error=max(max_error,float(np.max(errors)))
        expected.append((actual_tag,int(first),pack(result,18)))
        phase=(phase+step)&0xffffffff;tag=(tag&~mask)|((tag+1)&mask);first=False
    # Reset after an accepted sample, before its multiplier stage. No output
    # oracle is queued for the revoked transaction.
    for sense in range(2 if a.enable_conjugate else 1):
        for stage in range(4):
            cfg((50+4*sense+stage)<<48,0x23456789,0x1020304,sense=sense)
            emit([32767,-32768,-32768,-32768],reset=True,mode=stage)
    # Enumerate all4096 phase bins both sides of and exactly at rounding ties.
    for sense in range(2 if a.enable_conjugate else 1):
        for epoch,offset in enumerate((0,0x7ffff,0x80000,0x80001),start=2):
            cfg((epoch+30*sense)<<48,offset,1<<20,hold=8 if epoch>2 else 0,sense=sense)
            for i in range(4096):
                v=[-32768,32767,32767,-32768] if i%4==0 else rng.integers(-32768,32768,4)
                emit(v)
    # Signed increments, fractional phase bits and opposite rotation signs.
    for i,freq in enumerate((-350000.,-236000.123,0.,236000.123,350000.)):
        increment=round(-freq*2**32/4000000)
        actual=-increment*4000000/2**32
        frequency_cases.append({'requested_CFO_hz':freq,'correction_step_signed':increment,
            'represented_CFO_hz':actual,'frequency_error_hz':actual-freq,
            'phase_error_bound_after_60s_rad':2*np.pi*abs(actual-freq)*60})
        cfg((7+i)<<48,int(rng.integers(0,2**32)),increment,mode=1,hold=8,sense=i%2)
        for n in range(800):emit(rng.integers(-32768,32768,4),mode=1)
    # Gap, duplicate and changed epoch disarm immediately; unarmed input stays
    # backpressured until an explicit new exact-boundary configuration.
    for i,kind in enumerate(('gap','duplicate','epoch')):
        bad=tag+7 if kind=='gap' else (tag-1 if kind=='duplicate' else tag+(1<<48))
        emit([1,-2,3,-4],actual_tag=bad,mode=1)
        commands.append((2,1,0,bad+1,0,0,pack([5,6,7,8],16)))
        # The five unarmed attempts may already have drained the old output;
        # this rearm must not falsely require an outstanding transaction.
        cfg((14+i)<<48,0x12345678+i,0xf0000000,mode=1,sense=i%2)
        for n in range(80):emit(rng.integers(-32768,32768,4),mode=1)
    cfg((18<<48)|(mask-299),0xffffffff,0x80000001,hold=8,sense=1)
    for n in range(700):emit(rng.integers(-32768,32768,4))
    for i,path in enumerate(a.capture):
        raw=np.memmap(path,dtype=np.dtype([('tag','<u8'),('iq','<i2',(4,))]),mode='r')
        crc=f'{zlib.crc32(raw):08x}';wanted=json.loads((path.parent/'receipt.json').read_text())['capture']['crc32']
        if crc!=wanted:raise ValueError('capture CRC mismatch')
        segment=raw[154000:156048]
        cfg(int(segment[0]['tag']),0x98765432,round(236000*2**32/4000000),hold=8,sense=i%2)
        before=len(expected)
        for n,r in enumerate(segment):emit(r['iq'],actual_tag=int(r['tag']),period=12+n%2)
        adc_receipts.append({'capture':str(path),'crc32':crc,'inputs':len(segment),'outputs':len(expected)-before,
                            'test_CFO_hz':-236000,'CFO_estimation_tested':False})
    if len(covered)!=4096:raise ValueError('phase coverage')
    (out/'commands.txt').write_text(''.join(f'{k} {m} {per} {t:016x} {ph:08x} {st:08x} {d:016x}\n' for k,m,per,t,ph,st,d in commands))
    (out/'expected.txt').write_text(''.join(f'{t:016x} {start} {d:018x}\n' for t,start,d in expected))
    tb=r'''`timescale 1ns/1ps
module tb;
 reg clk=0;always #10 clk=~clk;
 reg rst=1,cv=0,iv=0,ready=1,force_hold=0,cconj=0;
 reg [63:0] ct=0,it=0,data=0;reg [31:0] cp=0,cs=0;
 wire cr,ir,ov,os,armed;wire [63:0] ot;wire [71:0] result;wire [31:0] fences;
 nco_cost_probe #(.ENABLE_CONJUGATE(@CONJ@)) dut(clk,rst,cv,cr,ct,cp,cs,iv,ir,it,data,ov,ready,ot,os,result,armed,fences,cconj,,);
 integer fd,ef,n,kind,mode=0,period,cycle=0,sent=0,received=0,configs=0;
 integer last_accept=-100,actual_period,previous_cadenced=0,saturated_previous=0;
 integer saturated_intervals=0,min_ii=100,max_ii=0,cadenced=0,in_stalls=0,out_stalls=0,cfg_blocked=0,unarmed_cycles=0;
 reg [63:0] ft,fdata,et,held_tag,held_itag;reg [31:0] fp,fs;
 reg [71:0] ev,held_result;reg [63:0] held_input;integer estart;
 reg ih=0,oh=0,held_start;
 always @(negedge clk) ready=!force_hold && (mode!=1 || !((cycle%97<19)||(cycle%113<11)));
 always @(posedge clk) begin
  cycle=cycle+1;
  if(rst) begin ih=0;oh=0;last_accept=-100;previous_cadenced=0;saturated_previous=0;end
  else begin
   if(oh && (!ov || result!==held_result || ot!==held_tag || os!==held_start)) $fatal(1,"output stability");
   if(ih && (!iv || it!==held_itag || data!==held_input)) $fatal(1,"input stability");
   oh=ov&&!ready;ih=iv&&!ir && kind!=2;
   if(oh) begin held_result=result;held_tag=ot;held_start=os;out_stalls=out_stalls+1;end
   if(ih) begin held_itag=it;held_input=data;in_stalls=in_stalls+1;end
   if(cv) begin
    if(ir) $fatal(1,"config failed to stop admission");
    if(!cr) cfg_blocked=cfg_blocked+1;
   end
   if(cv && cr) begin configs=configs+1;saturated_previous=0;previous_cadenced=0;end
   if(iv && ir) begin
    actual_period=cycle-last_accept;
    if(saturated_previous && mode==0 && period==0 && kind==1) begin
     saturated_intervals=saturated_intervals+1;
     if(actual_period<min_ii) min_ii=actual_period;
     if(actual_period>max_ii) max_ii=actual_period;
     if(actual_period!=3) $fatal(1,"saturated II mismatch%0d",actual_period);
    end
    if(period>0 && previous_cadenced && actual_period!=period) $fatal(1,"4MSPS missed");
    if(period>0) cadenced=cadenced+1;
    saturated_previous=mode==0 && period==0 && kind==1;previous_cadenced=period>0;
    last_accept=cycle;sent=sent+1;
   end
   if(ov && ready) begin
    n=$fscanf(ef,"%h %d %h\n",et,estart,ev);
    if(n!=3 || ot!==et || os!==estart[0] || result!==ev) $fatal(1,"NCO mismatch output%0d tag%h expected%h got%h expected%h",received,ot,et,result,ev);
    received=received+1;
   end
  end
 end
 initial begin
  fd=$fopen("commands.txt","r");ef=$fopen("expected.txt","r");
  repeat(3) @(negedge clk);rst=0;
  while(!$feof(fd)) begin
   n=$fscanf(fd,"%d %d %d %h %h %h %h\n",kind,mode,period,ft,fp,fs,fdata);
   if(n!=7) $fatal(1,"parse");
   if(kind==0) begin
    ct=ft;cp=fp;cs=fs;cconj=fdata[0];cv=1;
    if(period>0) begin
     #1;force_hold=1;ready=0;
     repeat(period) begin @(posedge clk);if(cr) $fatal(1,"config accepted before draining");@(negedge clk);end
     force_hold=0;#1;ready=1;
    end
    @(posedge clk);while(!cr) @(posedge clk);
    @(negedge clk);cv=0;ct=~ft;cp=~fp;cs=~fs;cconj=~fdata[0];
   end else if(kind==2) begin
    it=ft;data=fdata;iv=1;
    repeat(5) begin @(posedge clk);if(ir || armed) $fatal(1,"unarmed input admitted");unarmed_cycles=unarmed_cycles+1;@(negedge clk);end
    iv=0;
   end else begin
    if(period>0) repeat(period-1) @(negedge clk);
    it=ft;data=fdata;iv=1;#1;
    if(period>0 && !ir) $fatal(1,"not ready for4MSPS");
    @(posedge clk);while(!ir) @(posedge clk);
    @(negedge clk);iv=0;
    if(kind==3) begin
     force_hold=1;ready=0;repeat(mode) @(negedge clk);
     rst=1;repeat(2) @(negedge clk);rst=0;force_hold=0;
    end
   end
  end
  mode=0;repeat(40) @(negedge clk);
  if(sent!=@INPUTS@ || received!=@OUTPUTS@ || configs!=@CONFIGS@ || fences!=@FENCES@ ||
     min_ii!=3 || max_ii!=3 || saturated_intervals<12000 || cadenced!=@CADENCED@ ||
     in_stalls==0 || out_stalls==0 || cfg_blocked<50 || unarmed_cycles!=15) $fatal(1,"coverage");
  if(!$feof(ef)) $fatal(1,"unused oracle");
  $display("G0_NCO_PASS inputs=%0d outputs=%0d configs=%0d fences=%0d saturated_intervals=%0d min_ii=%0d max_ii=%0d cadenced=%0d in_stalls=%0d out_stalls=%0d cfg_blocked=%0d unarmed_cycles=%0d",
     sent,received,configs,fences,saturated_intervals,min_ii,max_ii,cadenced,in_stalls,out_stalls,cfg_blocked,unarmed_cycles);
  $finish;
 end
 initial begin #100000000;$fatal(1,"timeout");end
endmodule
'''
    for key,value in [('CONJ',int(a.enable_conjugate)),('INPUTS',inputs),('OUTPUTS',len(expected)),('CONFIGS',config_count),('FENCES',fences),('CADENCED',sum(r['inputs'] for r in adc_receipts))]:tb=tb.replace('@'+key+'@',str(value))
    (out/'tb.sv').write_text(tb);runs=[]
    def run(name,argv):
        runs.append(argv);(out/'commands.json').write_text(json.dumps(runs,indent=2)+'\n')
        with (out/(name+'.log')).open('w') as log:subprocess.run(argv,cwd=out,stdout=log,stderr=subprocess.STDOUT,check=True)
    bp=a.vivado_bin.resolve();rtl=out/'nco_cost_probe.sv';shutil.copyfile(project/'sim/g0/nco_cost_probe.sv',rtl)
    run('compile',[str(bp/'xvlog'),'--sv',str(rtl),str(out/'tb.sv')])
    run('elaborate',[str(bp/'xelab'),'tb','-s','nco_tb','-mt','off'])
    run('simulate',[str(bp/'xsim'),'nco_tb','-runall'])
    sim=(out/'simulate.log').read_text()
    if 'G0_NCO_PASS' not in sim or 'Fatal:' in sim:raise ValueError('NCO simulation failed')
    measured={k:int(v) for k,v in re.findall(r'(inputs|outputs|configs|fences|saturated_intervals|min_ii|max_ii|cadenced|in_stalls|out_stalls|cfg_blocked|unarmed_cycles)=(\d+)',sim)}
    fault=tb.replace('module tb;','module fault_tb;').replace('wire [71:0] result;',"wire [71:0] raw_result;wire [71:0] result=raw_result^72'd1;").replace('ot,os,result,armed','ot,os,raw_result,armed')
    (out/'fault_tb.sv').write_text(fault)
    run('fault-compile',[str(bp/'xvlog'),'--sv',str(out/'fault_tb.sv')])
    run('fault-elaborate',[str(bp/'xelab'),'fault_tb','-s','nco_fault_tb','-mt','off'])
    run('fault-simulate',[str(bp/'xsim'),'nco_fault_tb','-runall'])
    fl=(out/'fault-simulate.log').read_text()
    if 'Fatal: NCO mismatch' not in fl or 'G0_NCO_PASS' in fl:raise ValueError('output corruption not rejected')
    if a.enable_conjugate:
        wrong=tb.replace('module tb;', 'module wrong_sense_tb;').replace('.ENABLE_CONJUGATE(1)', '.ENABLE_CONJUGATE(0)')
        (out/'wrong_sense_tb.sv').write_text(wrong)
        run('sense-compile',[str(bp/'xvlog'),'--sv',str(out/'wrong_sense_tb.sv')])
        run('sense-elaborate',[str(bp/'xelab'),'wrong_sense_tb','-s','wrong_sense_tb','-mt','off'])
        run('sense-simulate',[str(bp/'xsim'),'wrong_sense_tb','-runall'])
        log=(out/'sense-simulate.log').read_text()
        if 'Fatal: NCO mismatch' not in log or 'G0_NCO_PASS' in log:raise ValueError('wrong conjugation not rejected')
    (out/'ooc.tcl').write_text(f'''set_param general.maxThreads 4
read_verilog -sv {{{rtl}}}
synth_design -top nco_cost_probe -part xc7z020clg484-1 -mode out_of_context -generic ENABLE_CONJUGATE={int(a.enable_conjugate)}
create_clock -name processing -period 20.000 [get_ports clk]
set_input_delay 2.0 -clock processing [get_ports -filter {{DIRECTION == IN && NAME != clk}}]
set_output_delay 2.0 -clock processing [get_ports -filter {{DIRECTION == OUT}}]
opt_design
place_design
route_design
report_utilization -file utilization.rpt
report_timing_summary -file timing.rpt -report_unconstrained
report_drc -file drc.rpt
report_timing -max_paths 10 -file paths.rpt
write_checkpoint -force nco_cost_probe.dcp
''')
    run('ooc',[str(bp/'vivado'),'-mode','batch','-source',str(out/'ooc.tcl'),'-nolog','-nojournal'])
    receipt={'scope':'G0 configured common-phase dual-RX NCO, not CFO estimation or full PHY',
        'enable_conjugate':a.enable_conjugate,'sense_outputs':sense_outputs,'sense_phase_bins':[len(x) for x in sense_bins],
        'reset_after_accept_offsets_cycles':[0,1,2,3],'config_source_mutated_after_accept':True,'wrong_sense_negative_control':a.enable_conjugate,
        'source_sha256':{str(p.relative_to(project)):hashlib.sha256(p.read_bytes()).hexdigest() for p in [project/'sim/g0/nco_cost_probe.sv',Path(__file__).resolve()]},
        'clock_hz':50000000,'sample_rate_hz':4000000,'input_bits':16,'output_bits':18,'output_scale':2,
        'phase_bits':32,'phase_bins':4096,'ROM_quarter_entries':1024,'ROM_endpoint_explicit':65536,
        'coefficient_Q':16,'integer_oracle':'direct full-circle rounded sin/cos, independent of RTL quarterwave reflection',
        'phase_bins_checked':len(covered),'rounding_boundary_offsets_checked':[0,0x7ffff,0x80000,0x80001],
        'max_abs_coefficient_pair_sum':92682,'absolute_accumulator_bound':3037003776,'absolute_rounded_output_bound':92682,
        'maximum_observed_float_error_output_LSB':max_error,'full_range_float_error_bound_output_LSB':72.58612526811142,
        'float_reference_uses_exact_quantized_phase_step':True,'frequency_quantization':frequency_cases,
        'positive_exact_output_half_cases':positive_ties,'negative_exact_output_half_cases':negative_ties,
        'measured':measured,'integer_component_comparisons':4*len(expected),'ADC_replays':adc_receipts,
        'single_bit_output_corruption_rejected':True,'routed_OOC':routed_measurements(out),'g0_passed':False}
    (out/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n');print(json.dumps(receipt))


if __name__=='__main__':
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--vivado-bin',type=Path,required=True);ap.add_argument('--output',type=Path,required=True)
    ap.add_argument('--enable-conjugate',action='store_true')
    ap.add_argument('--capture',type=Path,action='append',default=[])
    main(ap.parse_args())
