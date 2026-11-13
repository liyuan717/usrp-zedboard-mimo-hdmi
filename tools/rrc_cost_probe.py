#!/usr/bin/env python3
"""G0 fixed-point four-real-lane RRC data/service/routed cost measurement."""
import argparse
from collections import deque
import json
from pathlib import Path
import re
import shutil
import subprocess
import zlib
import numpy as np
from g0_sounder import rrc
from fec_inverse_cost_probe import routed_measurements
from workspace_storage_guard import check,GIB


def pack(v,w):return sum((int(x)&((1<<w)-1))<<(i*w) for i,x in enumerate(v))


def _main(a):
    p=Path(__file__).resolve().parents[1];out=a.output.resolve();out.mkdir(parents=True,exist_ok=False)
    h=rrc();q=np.rint(h*131072).astype(np.int64)
    if len(h)!=41 or not np.array_equal(q,q[::-1]):raise ValueError('symmetric41 taps required')
    l1=int(abs(q).sum());acc_bound=131072*l1;output_bound=(acc_bound+65536)//131072
    if acc_bound>=1<<39 or output_bound>=1<<19:raise ValueError('fixed-point range proof failed')
    tap_error=float(abs(q/131072-h).sum());relative_l2=float(np.linalg.norm(q/131072-h)/np.linalg.norm(h))
    if relative_l2>1e-4:raise ValueError('coefficient quantization budget exceeded')
    (out/'rrc.hex').write_text(''.join(f'{int(x)&0x3ffff:05x}\n' for x in q[:21]))
    rng=np.random.default_rng(413504);hist=deque(maxlen=41);previous=None;previous_context=None
    inputs=outputs=fences=starts=window_outputs=0;positive_half_ties=negative_half_ties=0;max_error=0.;errsq=signalsq=0.;raw=[]
    sf=(out/'input.txt').open('w');ef=(out/'expected.txt').open('w')
    def emit(tag,v,start=False,mode=0,cadence=0,context=0):
        nonlocal previous,previous_context,inputs,outputs,fences,starts,window_outputs,max_error,errsq,signalsq,positive_half_ties,negative_half_ties
        values=np.array(v,dtype=np.int64)
        if np.any(values< -131072) or np.any(values>131071):raise ValueError('input width')
        gap=previous is None or start or context!=previous_context or tag>>48!=previous>>48 or (tag&((1<<48)-1))!=((previous+1)&((1<<48)-1))
        if gap:
            if previous is not None:fences+=1
            hist.clear();window_outputs=0
        previous=tag;previous_context=context;hist.append(values);inputs+=1
        sf.write(f'0 {int(start)} {mode} {cadence} {tag:016x} {pack(values,18):018x} {context:08x}\n')
        if len(hist)<41:return
        window=np.array(hist)[::-1];acc=q@window;integer=(acc+65536)//131072;floating=h@window
        positive_half_ties+=int(np.sum((acc>0)&(acc%131072==65536)))
        negative_half_ties+=int(np.sum((acc<0)&(acc%131072==65536)))
        err=integer-floating
        if np.max(abs(err))>float(np.max(abs(window)))*tap_error+.500001:raise ValueError('quantization bound')
        max_error=max(max_error,float(np.max(abs(err))));errsq+=float(err@err);signalsq+=float(floating@floating)
        outtag=(tag&~((1<<48)-1))|((tag-20)&((1<<48)-1))
        output_start=window_outputs==0
        ef.write(f'{outtag:016x} {pack(integer,20):020x} {int(output_start)} {context:08x}\n')
        outputs+=1;starts+=int(output_start);window_outputs+=1
    for n in range(30):emit((1<<48)+n,rng.integers(-131072,131072,4))
    sf.write('2 0 0 0 0 0 0\n');previous=None;previous_context=None;hist.clear();fences=0;window_outputs=0
    for n in range(350):emit((2<<48)+n,[131071,-131072,131071,-131072])
    for n in range(350):
        v=[131071,-131072,1,-1] if n==70 else ([65536,-65536,65535,-65537] if n==180 else [0]*4)
        emit((2<<48)+350+n,v)
    # Force an L1-bound neighborhood, then broad full-range random data.
    for n in range(82):emit((2<<48)+700+n,[int(np.sign(q[n%41]))*131071]*4)
    for n in range(2200):emit((2<<48)+782+n,rng.integers(-131072,131072,4))
    for n in range(2200):emit((3<<48)+n,rng.integers(-131072,131072,4),mode=1)
    low=4000;epoch=4
    for n in range(600):
        if n==150:low+=7
        if n==300:low-=1
        if n==450:epoch=5
        emit((epoch<<48)+low,rng.integers(-131072,131072,4),start=n==525,mode=1);low+=1
    for path in a.capture:
        if path.stat().st_size%16:raise ValueError('truncated ADC capture')
        adc=np.memmap(path,dtype=np.dtype([('tag','<u8'),('iq','<i2',(4,))]),mode='r')
        crc=f'{zlib.crc32(adc):08x}';expected=json.loads((path.parent/'receipt.json').read_text())['capture']['crc32']
        if crc!=expected:raise ValueError('capture CRC mismatch')
        segment=adc[154000:156048];before=outputs;es=errsq;ss=signalsq
        for n,r in enumerate(segment):emit(int(r['tag']),r['iq'],start=n==0,cadence=12+n%2)
        raw.append({'capture':str(path),'crc32':crc,'inputs':len(segment),'outputs':outputs-before,
                    'RRC_only_rms_error_input_LSB':((errsq-es)/(4*(outputs-before)))**.5,
                    'RRC_only_relative_rms_error':((errsq-es)/(signalsq-ss))**.5})
    for n in range(600):emit((9<<48)|((((1<<48)-301)+n)&((1<<48)-1)),rng.integers(-131072,131072,4))
    # The new window overlaps40 input samples: its first center is exactly
    # the previous window's final center+1, so tag gaps cannot replace start.
    base=(10<<48)+10000
    for n in range(80):emit(base+n,rng.integers(-131072,131072,4),context=11)
    emit(base+40,rng.integers(-131072,131072,4),start=True,mode=2,context=12)
    # New in_start is accepted as old work finishes; hold that older output.
    sf.write('5 0 2 24 0 0 0\n')
    for n in range(1,41):emit(base+40+n,rng.integers(-131072,131072,4),mode=2 if n==40 else 0,context=12)
    # Hold the new window's first valid output, including its asserted marker.
    sf.write('5 0 2 24 0 0 0\n')
    for n in range(41,61):emit(base+40+n,rng.integers(-131072,131072,4),context=12)
    # Context alone invalidates history, even with contiguous tags/start low.
    for n in range(61):emit(base+101+n,rng.integers(-131072,131072,4),context=13)
    sf.close();ef.close()
    if positive_half_ties==0 or negative_half_ties==0:raise ValueError('rounding tie coverage missing')
    tb=r'''`timescale 1ns/1ps
module tb;
 reg clk=0;always #10 clk=~clk;
 reg rst=1,iv=0,istart=0,ready=1;reg [63:0] tag=0;reg [71:0] data=0;
 reg [31:0] ictx=0;
 wire ir,ov,ostart;wire [79:0] result;wire [63:0] otag;wire [31:0] fences,ocontext;
 rrc_cost_probe dut(clk,rst,iv,ir,istart,tag,data,ov,ready,otag,result,fences,ostart,ictx,ocontext);
 integer fd,ef,n,kind,mode=0,period,start_flag,cycles=0,sent=0,received=0;
 integer last_accept=-100,actual_period,previous_cadenced=0,saturated_previous=0;
 integer saturated_intervals=0,min_ii=100,max_ii=0,cadenced=0,in_stalls=0,out_stalls=0;
 integer expected_start,starts=0,contiguous_starts=0,start_stalls=0,old_output_restart_stalls=0,context_only_fences=0;
 reg [31:0] fc,ec,held_context,held_ocontext,previous_context;
 reg [63:0] ft,et,held_tag,held_itag;reg [71:0] fv,held_input;reg [79:0] ev,held_result;
 reg input_held=0,output_held=0,held_start,held_ostart,restart_pending=0,have_last_output=0,have_input=0;
 reg [63:0] last_output,previous_input;
 always @(negedge clk) ready=mode==2?0:(mode!=1 || !((cycles%97<19)||(cycles%113<11)));
 always @(posedge clk) begin
  cycles=cycles+1;
  if(rst) begin input_held=0;output_held=0;last_accept=-100;previous_cadenced=0;saturated_previous=0;restart_pending=0;have_last_output=0;have_input=0;end
  else begin
   if(input_held && (!iv || tag!==held_itag || data!==held_input || istart!==held_start || ictx!==held_context)) $fatal(1,"input unstable");
   if(output_held && (!ov || result!==held_result || otag!==held_tag || ostart!==held_ostart || ocontext!==held_ocontext)) $fatal(1,"output unstable");
   input_held=iv&&!ir;output_held=ov&&!ready;
   if(input_held) begin held_itag=tag;held_input=data;held_start=istart;held_context=ictx;in_stalls=in_stalls+1;end
   if(output_held) begin
    held_result=result;held_tag=otag;held_ostart=ostart;held_ocontext=ocontext;out_stalls=out_stalls+1;
    if(ostart) start_stalls=start_stalls+1;
    else if(restart_pending) old_output_restart_stalls=old_output_restart_stalls+1;
   end
   if(iv && ir) begin
    if(istart) restart_pending=1;
    if(have_input && ictx!=previous_context && !istart && tag=={previous_input[63:48],previous_input[47:0]+48'd1}) context_only_fences=context_only_fences+1;
    previous_input=tag;previous_context=ictx;have_input=1;
    actual_period=cycles-last_accept;
    if(saturated_previous && mode==0 && period==0) begin
     saturated_intervals=saturated_intervals+1;
     if(actual_period<min_ii) min_ii=actual_period;
     if(actual_period>max_ii) max_ii=actual_period;
     if(actual_period!=8) $fatal(1,"saturated II mismatch %0d",actual_period);
    end
    if(period>0 && previous_cadenced && actual_period!=period) $fatal(1,"4MSPS cadence missed");
    if(period>0) cadenced=cadenced+1;
    saturated_previous=mode==0 && period==0;previous_cadenced=period>0;
    last_accept=cycles;sent=sent+1;
   end
   if(ov && ready) begin
    n=$fscanf(ef,"%h %h %d %h\n",et,ev,expected_start,ec);
    if(n!=4 || otag!==et || result!==ev || ostart!==1'(expected_start) || ocontext!==ec) $fatal(1,"RRC mismatch output%0d tag%h expected%h got%h expected%h start%0d expected%0d context%0d expected%0d",received,otag,et,result,ev,ostart,expected_start,ocontext,ec);
    if(ostart) begin
     starts=starts+1;restart_pending=0;
     if(have_last_output && otag=={last_output[63:48],last_output[47:0]+48'd1}) contiguous_starts=contiguous_starts+1;
    end
    have_last_output=1;last_output=otag;
    received=received+1;
   end
  end
  #1;if(rst && (ov || ostart || ocontext!=0)) $fatal(1,"reset did not clear output metadata");
 end
 initial begin
  fd=$fopen("input.txt","r");ef=$fopen("expected.txt","r");
  repeat(3) @(negedge clk);rst=0;
  while(!$feof(fd)) begin
   n=$fscanf(fd,"%d %d %d %d %h %h %h\n",kind,start_flag,mode,period,ft,fv,fc);
   if(n!=7) $fatal(1,"parse");
   if(kind==2) begin iv=0;rst=1;repeat(2) @(negedge clk);rst=0;end
   else if(kind==5) begin
    iv=0;repeat(period) @(negedge clk);mode=0;
    saturated_previous=0;previous_cadenced=0;repeat(2) @(negedge clk);
   end
   else begin
    if(period>0) repeat(period-1) @(negedge clk);
    tag=ft;data=fv;istart=start_flag;ictx=fc;iv=1;
    #1;if(period>0 && !ir) $fatal(1,"not ready for4MSPS");
    @(posedge clk);while(!ir) @(posedge clk);
    @(negedge clk);iv=0;
   end
  end
  mode=0;repeat(60) @(negedge clk);
  if(sent!=@INPUTS@ || received!=@OUTPUTS@ || fences!=@FENCES@ || saturated_intervals<2000 ||
     max_ii!=8 || min_ii!=8 || cadenced!=@CADENCED@ || in_stalls==0 || out_stalls==0 ||
     starts!=@STARTS@ || contiguous_starts<1 || start_stalls==0 || old_output_restart_stalls==0 || context_only_fences<1) $fatal(1,"coverage");
  if(!$feof(ef)) $fatal(1,"unused expected outputs");
  $display("G0_RRC_PASS inputs=%0d outputs=%0d saturated_intervals=%0d min_ii=%0d max_ii=%0d cadenced=%0d in_stalls=%0d out_stalls=%0d fences=%0d starts=%0d contiguous_starts=%0d start_stalls=%0d old_output_restart_stalls=%0d context_only_fences=%0d",
             sent,received,saturated_intervals,min_ii,max_ii,cadenced,in_stalls,out_stalls,fences,starts,contiguous_starts,start_stalls,old_output_restart_stalls,context_only_fences);
  $finish;
 end
 initial begin #100000000;$fatal(1,"timeout");end
endmodule
'''
    for key,value in [('INPUTS',inputs),('OUTPUTS',outputs),('FENCES',fences),('STARTS',starts),('CADENCED',sum(r['inputs'] for r in raw))]:tb=tb.replace('@'+key+'@',str(value))
    (out/'tb.sv').write_text(tb);commands=[]
    def run(name,argv):
        commands.append(argv);(out/'commands.json').write_text(json.dumps(commands,indent=2)+'\n')
        with (out/(name+'.log')).open('w') as log:subprocess.run(argv,cwd=out,stdout=log,stderr=subprocess.STDOUT,check=True)
    bp=a.vivado_bin.resolve();rtl=out/'rrc_cost_probe.sv';shutil.copyfile(p/'sim/g0/rrc_cost_probe.sv',rtl)
    xpm=bp.parent/'data/ip/xpm'
    run('compile',[str(bp/'xvlog'),'--sv',str(xpm/'xpm_cdc/hdl/xpm_cdc.sv'),str(xpm/'xpm_memory/hdl/xpm_memory.sv'),str(rtl),str(out/'tb.sv')])
    run('elaborate',[str(bp/'xelab'),'tb','-s','rrc_tb','-mt','off'])
    run('simulate',[str(bp/'xsim'),'rrc_tb','-runall'])
    sim=(out/'simulate.log').read_text()
    if 'G0_RRC_PASS' not in sim or 'Fatal:' in sim:raise ValueError('RRC comparison failed')
    measured={k:int(v) for k,v in re.findall(r'(\w+)=(\d+)',sim.split('G0_RRC_PASS')[-1])}
    fault=tb.replace('module tb;','module fault_tb;').replace('wire [79:0] result;',"wire [79:0] raw_result;wire [79:0] result=raw_result^80'd1;").replace('otag,result,fences,ostart,ictx,ocontext);','otag,raw_result,fences,ostart,ictx,ocontext);')
    (out/'fault_tb.sv').write_text(fault)
    run('fault-compile',[str(bp/'xvlog'),'--sv',str(out/'fault_tb.sv')])
    run('fault-elaborate',[str(bp/'xelab'),'fault_tb','-s','rrc_fault_tb','-mt','off'])
    run('fault-simulate',[str(bp/'xsim'),'rrc_fault_tb','-runall'])
    fl=(out/'fault-simulate.log').read_text()
    if 'Fatal: RRC mismatch' not in fl or 'G0_RRC_PASS' in fl:raise ValueError('fault not rejected')
    marker_fault=tb.replace('module tb;','module marker_fault_tb;').replace('wire ir,ov,ostart;',"wire ir,ov,raw_start;wire ostart=ov?~raw_start:raw_start;").replace('result,fences,ostart,ictx,ocontext);','result,fences,raw_start,ictx,ocontext);')
    (out/'marker_fault_tb.sv').write_text(marker_fault)
    run('marker-fault-compile',[str(bp/'xvlog'),'--sv',str(out/'marker_fault_tb.sv')])
    run('marker-fault-elaborate',[str(bp/'xelab'),'marker_fault_tb','-s','rrc_marker_fault_tb','-mt','off'])
    run('marker-fault-simulate',[str(bp/'xsim'),'rrc_marker_fault_tb','-runall'])
    ml=(out/'marker-fault-simulate.log').read_text()
    if 'Fatal: RRC mismatch' not in ml or 'G0_RRC_PASS' in ml:raise ValueError('marker fault not rejected')
    if not a.sim_only:
        (out/'ooc.tcl').write_text(f'''set_param general.maxThreads 2
create_project -in_memory -part xc7z020clg484-1
set_property XPM_LIBRARIES {{XPM_MEMORY}} [current_project]
read_verilog -sv {{{rtl}}}
synth_design -top rrc_cost_probe -part xc7z020clg484-1 -mode out_of_context
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
write_checkpoint -force rrc_cost_probe.dcp
''')
        run('ooc',[str(bp/'vivado'),'-mode','batch','-source',str(out/'ooc.tcl'),'-nolog','-nojournal'])
    receipt={'scope':'G0 four-real-lane fixed-point RRC only; no NCO/interpolation/acquisition or full PHY',
        'clock_hz':50000000,'input_signed_bits':18,'coefficient_signed_bits':18,'coefficient_fractional_bits':17,
        'accumulator_signed_bits':40,'output_signed_bits':20,'taps':q.tolist(),'rounding':'nearest, exact halves toward positive infinity',
        'absolute_accumulator_bound':acc_bound,'absolute_rounded_output_bound':output_bound,
        'tap_quantization_L1_error':tap_error,'tap_relative_L2_error':relative_l2,
        'maximum_test_output_error_input_LSB':max_error,'worst_input_range_output_error_bound_LSB':131072*tap_error+.5,
        'integer_oracle':'direct41-tap convolution, not folded symmetric architecture',
        'positive_exact_half_rounding_cases':positive_half_ties,'negative_exact_half_rounding_cases':negative_half_ties,
        'measured':measured,'integer_component_comparisons':4*outputs,'ADC_replays':raw,
        'service_samples_per_second':50000000/measured['max_ii'],'single_bit_fault_rejected':True,
        'out_start_oracle':'first direct41-tap output after reset or a new input window, independent of output tag continuity',
        'out_start_comparisons':outputs,'out_start_fault_rejected':True,
        'out_context_comparisons':outputs,'context_change_invalidates_history':True,
        'routed_OOC':None if a.sim_only else routed_measurements(out),'g0_passed':False}
    (out/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n');print(json.dumps(receipt))


def main(a):
    c=check('before-RRC-history-probe');assert c['disk']['available_bytes']>=5*GIB
    try:_main(a)
    finally:
        if a.output.exists():
            (a.output/'disk-before.json').write_text(json.dumps(c,indent=2)+'\n')
            (a.output/'disk-after.json').write_text(json.dumps(check('after-RRC-history-probe'),indent=2)+'\n')


if __name__=='__main__':
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--vivado-bin',type=Path,required=True);ap.add_argument('--output',type=Path,required=True)
    ap.add_argument('--capture',type=Path,action='append',default=[])
    ap.add_argument('--sim-only',action='store_true')
    main(ap.parse_args())
