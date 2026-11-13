#!/usr/bin/env python3
"""Exact public differential-correlation G0 RTL and routed cost probe.

The oracle performs direct127-tap integer convolution; it does NOT use the
RTL's period16/comb factorization. This is not full CFO acquisition or a
production receiver: normalization, angle estimation and NCO stay pending.
"""
import argparse
from workspace_storage_guard import check,GIB
from collections import deque
import json
import re
import shutil
from pathlib import Path
import subprocess
import zlib
import numpy as np
from g0_sounder import symbols

W=29
CW=W+16
LOW_MASK=(1<<48)-1


def pack(values,width):return sum((int(x)&((1<<width)-1))<<(i*width) for i,x in enumerate(values))


def _main(a):
    global W,CW
    W=a.bits;CW=W+16
    project=Path(__file__).resolve().parents[1];out=a.output.resolve();out.mkdir(parents=True,exist_ok=False)
    s,_=symbols();pre=s[256:384,0];d=pre[:-1].conj()*pre[1:]
    dr=np.rint(d.real).astype(np.int64);di=np.rint(d.imag).astype(np.int64)
    if not np.array_equal(dr[16:],dr[:-16]) or not np.array_equal(di[16:],di[:-16]):raise ValueError('public sequence is not16-periodic')
    if (int(dr.sum()),int(di.sum()))!=(16,17):raise ValueError('public differential mean changed')
    wr=127*dr[::-1]-dr.sum();wi=127*di[::-1]-di.sum()
    rng=np.random.default_rng(719310);history=deque(maxlen=505)
    previous=None;fences=0;inputs=outputs=0;max_result=0;rf=[];events=[]
    source=(out/'input.txt').open('w');golden=(out/'expected.txt').open('w')
    def reset():
        nonlocal previous,fences
        history.clear();previous=None;fences=0
        source.write('2 0 0 0 0 0\n')
    def emit(tag,values,start=False,mode=0,cadence=0):
        nonlocal previous,fences,inputs,outputs,max_result
        if len(values)!=4 or np.any(np.asarray(values)<-(1<<(W-1))) or np.any(np.asarray(values)>=(1<<(W-1))):
            raise ValueError('input outside declared signed width')
        gap=(previous is None or start or tag>>48!=previous>>48 or (tag&LOW_MASK)!=((previous+1)&LOW_MASK))
        if gap:
            if previous is not None:fences+=1
            history.clear()
        previous=tag;history.append(np.asarray(values,dtype=np.int64))
        source.write(f'0 {int(start)} {mode} {cadence} {tag:016x} {pack(values,W):029x}\n');inputs+=1
        if len(history)<505:return
        window=np.array(history)[::-4]
        result=[]
        for sense in (1,-1):
            weights_i=wi*(-sense)
            for rx in range(2):
                re=window[:,rx*2];im=window[:,rx*2+1]
                result.extend([int(np.dot(wr,re)-np.dot(weights_i,im)),int(np.dot(weights_i,re)+np.dot(wr,im))])
        peak=max(abs(x) for x in result);max_result=max(max_result,peak)
        if peak>=(1<<(CW-1)):raise ValueError('oracle result overflows output width')
        start_tag=(tag&~LOW_MASK)|(((tag&LOW_MASK)-508)&LOW_MASK)
        golden.write(f'{start_tag:016x} {pack(result,CW):090x}\n');outputs+=1
    # Reset inside an unfinished warm-up transaction: no output was eligible.
    for i in range(250):emit((1<<48)+i+4,rng.integers(-(1<<(W-1)),1<<(W-1),4))
    reset()
    for i in range(1800):
        if i<600:v=[(1<<(W-1))-1,-(1<<(W-1)),(1<<(W-1))-1,-(1<<(W-1))]
        elif i<700:v=[0,0,0,0] if i!=601 else [1,-1,2,-2]
        else:v=rng.integers(-(1<<(W-1)),1<<(W-1),4)
        emit((2<<48)+i+4,v,start=i==0)
    low=5004;epoch=3
    for i in range(2500):
        if i==1000:low+=19;events.append('sample gap after warmup')
        if i==1700:epoch=4;low=7004;events.append('epoch change after warmup')
        emit((epoch<<48)+low,rng.integers(-(1<<(W-1)),1<<(W-1),4),start=i==0,mode=1)
        low+=1
    for capture in a.capture:
        dtype=np.dtype([('tag','<u8'),('iq','<i2',(4,))]);raw=np.memmap(capture,dtype=dtype,mode='r')
        crc=f'{zlib.crc32(raw):08x}'
        expected=json.loads((capture.parent/'receipt.json').read_text())['capture']['crc32']
        if crc!=expected:raise ValueError('actual capture CRC differs')
        start=154000;n=2048
        iq=np.array(raw['iq'][start:start+n+4],dtype=np.int64)
        tags=np.array(raw['tag'][start:start+n+4],dtype=np.uint64)
        if np.any(np.diff(tags)!=1):raise ValueError('ADC gap inside fixture')
        count_before=outputs
        for i in range(4,n+4):
            values=[]
            for rx in range(2):
                ar,ai=iq[i-4,rx*2:rx*2+2];br,bi=iq[i,rx*2:rx*2+2]
                values.extend([int(ar*br+ai*bi),int(ar*bi-ai*br)])
            emit(int(tags[i]),values,start=i==4,cadence=12+(i%2))
        rf.append({'capture':str(capture),'crc32':crc,'ADC_endpoint_records':n,'correlations_compared':outputs-count_before})
    for i in range(1400):
        emit((9<<48)|((LOW_MASK-999+i)&LOW_MASK),rng.integers(-(1<<(W-1)),1<<(W-1),4),start=i==0)
    source.close();golden.close()
    (out/'tb.sv').write_text(f'''`timescale 1ns/1ps
module tb;
 localparam W={W}, CW={CW};
 reg clk=0;always #10 clk=~clk;
 reg rst=1,iv=0,istart=0,ready=1;
 reg [4*W-1:0] z=0;reg [63:0] tag=0;
 wire ir,ov;wire [8*CW-1:0] result;wire [63:0] otag;wire [31:0] fence_count;
 differential_corr_cost_probe #(.W(W)) dut(clk,rst,iv,ir,z,tag,istart,ov,ready,result,otag,fence_count);
 integer fd,expected_fd,n,kind,start_flag,mode=0,period,cycles=0,sent=0,received=0;
 integer min_ii=100,last_accept=-100,input_stalls=0,output_stalls=0,cadenced=0;
 integer requested_period=0,actual_period=0,previous_was_cadenced=0;
 integer saturated_previous=0,saturated_count=0,saturated_max_ii=0;
 reg [63:0] file_tag,expected_tag,held_tag,held_input_tag;
 reg [4*W-1:0] file_z,held_z;
 reg [8*CW-1:0] expected_data,held_data;
 reg last_stalled=0,in_stalled=0,held_start;
 always @(negedge clk) begin
  if(mode==1) ready=!((cycles%97<19)||(cycles%113<11));else ready=1;
 end
 always @(posedge clk) begin
  cycles=cycles+1;
  if(rst) begin last_stalled=0;in_stalled=0;last_accept=-100;previous_was_cadenced=0;saturated_previous=0;end
  else begin
   if(last_stalled && (!ov || result!==held_data || otag!==held_tag)) $fatal(1,"output changed under stall");
   if(in_stalled && (!iv || z!==held_z || tag!==held_input_tag || istart!==held_start)) $fatal(1,"source changed under stall");
   last_stalled=ov&&!ready;
   if(last_stalled) begin held_data=result;held_tag=otag;output_stalls=output_stalls+1;end
   in_stalled=iv&&!ir;
   if(in_stalled) begin held_z=z;held_input_tag=tag;held_start=istart;input_stalls=input_stalls+1;end
   if(iv && ir) begin
    actual_period=cycles-last_accept;
    if(actual_period<min_ii) min_ii=actual_period;
    if(saturated_previous && mode==0 && requested_period==0) begin
     saturated_count=saturated_count+1;
     if(actual_period>saturated_max_ii) saturated_max_ii=actual_period;
     if(actual_period>9) $fatal(1,"saturated service slower than1.3x4MSPS:II%0d",actual_period);
    end
    saturated_previous=(mode==0 && requested_period==0);
    if(requested_period>0 && previous_was_cadenced && actual_period!=requested_period) $fatal(1,"4MSPS cadence missed got%0d expected%0d",actual_period,requested_period);
    previous_was_cadenced=requested_period>0;last_accept=cycles;sent=sent+1;
    if(requested_period>0) cadenced=cadenced+1;
   end
   if(ov && ready) begin
    n=$fscanf(expected_fd,"%h %h\\n",expected_tag,expected_data);
    if(n!=2 || otag!==expected_tag || result!==expected_data)
     $fatal(1,"correlation mismatch output%0d tag%h expected%h got%h expected%h",received,otag,expected_tag,result,expected_data);
    received=received+1;
   end
  end
 end
 initial begin
  expected_fd=$fopen("expected.txt","r");fd=$fopen("input.txt","r");
  repeat(3) @(negedge clk);rst=0;
  while(!$feof(fd)) begin
   n=$fscanf(fd,"%d %d %d %d %h %h\\n",kind,start_flag,mode,period,file_tag,file_z);
   if(n!=6) $fatal(1,"source parse");
   if(kind==2) begin
    iv=0;rst=1;repeat(2) @(negedge clk);rst=0;
   end else begin
    if(period>0) repeat(period-1) @(negedge clk);
    requested_period=period;z=file_z;tag=file_tag;istart=start_flag;iv=1;
    #1;
    if(period>0 && !ir) $fatal(1,"not ready for4MSPS source");
    // Observe actual sampling edges. Ready may change at the drive edge
    // with the output-stall generator; a negedge-only loop can duplicate
    // a request whose handshake already happened on the intervening edge.
    @(posedge clk);while(!ir) @(posedge clk);
    @(negedge clk);iv=0;
   end
  end
  mode=0;
  repeat(60) @(negedge clk);
  if(sent!={inputs} || received!={outputs} || min_ii>9 || saturated_count<1000 || saturated_max_ii>9 || input_stalls==0 || output_stalls==0 || fence_count!={fences})
   $fatal(1,"coverage mismatch sent%0d received%0d minII%0d istall%0d ostall%0d fences%0d",sent,received,min_ii,input_stalls,output_stalls,fence_count);
  if(!$feof(expected_fd)) $fatal(1,"expected outputs remain");
  $display("G0_DIFF_CORR_PASS inputs=%0d outputs=%0d min_ii=%0d saturated_intervals=%0d saturated_max_ii=%0d cadenced=%0d in_stalls=%0d out_stalls=%0d fences=%0d",sent,received,min_ii,saturated_count,saturated_max_ii,cadenced,input_stalls,output_stalls,fence_count);
  $finish;
 end
 initial begin #100000000;$fatal(1,"timeout");end
endmodule
''')
    commands=[]
    def run(name,args):
        commands.append(args);(out/'commands.json').write_text(json.dumps(commands,indent=2)+'\n')
        with (out/(name+'.log')).open('w') as log:subprocess.run(args,cwd=out,stdout=log,stderr=subprocess.STDOUT,check=True)
    binpath=a.vivado_bin.resolve();rtl=out/'differential_corr_cost_probe.sv'
    shutil.copyfile(project/'sim/g0/differential_corr_cost_probe.sv',rtl)
    run('compile',[str(binpath/'xvlog'),'--sv',str(rtl),str(out/'tb.sv')])
    run('elaborate',[str(binpath/'xelab'),'tb','-s','diff_corr_tb','-mt','off'])
    run('simulate',[str(binpath/'xsim'),'diff_corr_tb','-runall'])
    sim=(out/'simulate.log').read_text()
    if 'G0_DIFF_CORR_PASS' not in sim:raise ValueError('simulation not passed')
    measured={key:int(value) for key,value in re.findall(r'(inputs|outputs|min_ii|saturated_intervals|saturated_max_ii|cadenced|in_stalls|out_stalls|fences)=(\d+)',sim)}
    # Negative control corrupts the observed result, never the oracle. It
    # must fail at the first eligible output rather than merely count data.
    faulty=(out/'tb.sv').read_text().replace('module tb;','module fault_tb;')
    faulty=faulty.replace('wire [8*CW-1:0] result;',
        "wire [8*CW-1:0] raw_result;wire [8*CW-1:0] result=raw_result ^ {{(8*CW-1){1'b0}},1'b1};")
    faulty=faulty.replace('ov,ready,result,otag,fence_count);','ov,ready,raw_result,otag,fence_count);')
    (out/'fault_tb.sv').write_text(faulty)
    run('fault-compile',[str(binpath/'xvlog'),'--sv',str(out/'fault_tb.sv')])
    run('fault-elaborate',[str(binpath/'xelab'),'fault_tb','-s','diff_corr_fault_tb','-mt','off'])
    run('fault-simulate',[str(binpath/'xsim'),'diff_corr_fault_tb','-runall'])
    fault_log=(out/'fault-simulate.log').read_text()
    if 'Fatal: correlation mismatch' not in fault_log or 'G0_DIFF_CORR_PASS' in fault_log:
        raise ValueError('single-bit corruption was not rejected by data oracle')
    (out/'ooc.tcl').write_text(f'''set_param general.maxThreads 4
read_verilog -sv {{{rtl}}}
synth_design -top differential_corr_cost_probe -part xc7z020clg484-1 -mode out_of_context -generic W={W}
create_clock -name processing -period 20.000 [get_ports clk]
set_input_delay 2.0 -clock processing [get_ports -filter {{DIRECTION == IN && NAME != clk}}]
set_output_delay 2.0 -clock processing [get_ports -filter {{DIRECTION == OUT}}]
opt_design
place_design
route_design
report_utilization -file utilization.rpt
report_timing_summary -file timing.rpt -report_unconstrained
report_drc -file drc.rpt
set memory_file [open bram-properties.txt w]
foreach c [get_cells -hier -filter {{REF_NAME =~ RAMB*}}] {{
 puts $memory_file "$c [get_property REF_NAME $c] [get_property READ_WIDTH_A $c] [get_property READ_WIDTH_B $c] [get_property WRITE_WIDTH_A $c] [get_property WRITE_WIDTH_B $c] [get_property WRITE_MODE_A $c] [get_property WRITE_MODE_B $c]"
}}
close $memory_file
write_checkpoint -force differential_corr_cost_probe.dcp
''')
    run('ooc',[str(binpath/'vivado'),'-mode','batch','-source',str(out/'ooc.tcl'),'-nolog','-nojournal'])
    receipt={'scope':'G0 isolated exact integer public differential correlator cost; not full acquisition/PHY/G0',
        'input_word_bits':W,'output_component_bits':CW,'input_records':inputs,'correlations_compared':outputs,
        'oracle':'direct127-tap weighted integer convolution, independently of RTL factorization',
        'both_IQ_senses_and_two_RX_checked':True,'maximum_absolute_oracle_component':max_result,
        'reset_during_warmup_sample_gap_epoch_wrap_and_backpressure':True,
        'expected_fence_count':fences,'minimum_II_requirement_cycles':9,'clock_hz':50000000,
        'simulation_measured':measured,
        'saturated_service_samples_per_second':50000000/measured['saturated_max_ii'],
        'single_bit_output_corruption_rejected':True,
        'RF_ADC_replays':rf,'full_CFO_normalization_NCO_implemented':False,'g0_passed':False}
    (out/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n');print(json.dumps(receipt))


def main(a):
    before=check('before-production-width-correlation-probe');assert before['disk']['available_bytes']>=5*GIB
    try:
        _main(a)
    finally:
        if a.output.exists():
            (a.output/'disk-before.json').write_text(json.dumps(before,indent=2)+'\n')
            (a.output/'disk-after.json').write_text(json.dumps(check('after-production-width-correlation-probe'),indent=2)+'\n')


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--vivado-bin',type=Path,required=True)
    p.add_argument('--bits',type=int,choices=(29,33),default=29);p.add_argument('--capture',type=Path,action='append',default=[]);p.add_argument('--output',type=Path,required=True)
    main(p.parse_args())
