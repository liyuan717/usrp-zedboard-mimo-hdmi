#!/usr/bin/env python3
"""G0 actual connected NCO/RRC RTL versus independent integer convolution."""
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


def pack(v,w):return sum((int(x)&((1<<w)-1))<<(i*w) for i,x in enumerate(v))


def main(a):
    project=Path(__file__).resolve().parents[1];out=a.output.resolve();out.mkdir(parents=True,exist_ok=False)
    q=np.rint(rrc()*131072).astype(np.int64)
    (out/'rrc.hex').write_text(''.join(f'{int(x)&0x3ffff:05x}\n' for x in q[:21]))
    sine=np.rint(65536*np.sin(2*np.pi*np.arange(1024)/4096)).astype(np.int64)
    (out/'sine.hex').write_text(''.join(f'{int(x):05x}\n' for x in sine))
    c=np.rint(65536*np.cos(2*np.pi*np.arange(4096)/4096)).astype(np.int64)
    s=np.rint(65536*np.sin(2*np.pi*np.arange(4096)/4096)).astype(np.int64)
    rng=np.random.default_rng(321841);mask=(1<<48)-1
    history=deque(maxlen=41);phase=step=tag=0;armed=False;inputs=outputs=0;configs=0
    fences=rrc_fences=0;rrc_have=False;first=False;adc=[];commands=[];expected=[]
    context=starts=window_outputs=0
    def cfg(t,p,d,mode=0,hold=0):
        nonlocal phase,step,tag,armed,first,configs,context,window_outputs
        tag=t;phase=p&0xffffffff;step=d&0xffffffff;armed=True;first=True;configs+=1
        context=(context+1)&0xffffffff;window_outputs=0
        history.clear();commands.append((0,mode,hold,t,phase,step,0))
    def emit(v,mode=0,period=0,actual=None,reset=False):
        nonlocal phase,tag,armed,first,inputs,outputs,fences,rrc_fences,rrc_have,context,starts,window_outputs
        actual=tag if actual is None else actual;v=np.array(v,dtype=np.int64)
        if np.any(v< -32768) or np.any(v>32767):raise ValueError('input overflow')
        commands.append((3 if reset else 1,mode,period,actual,0,0,pack(v,16)));inputs+=1
        if reset:
            history.clear();armed=False;fences=rrc_fences=0;rrc_have=False;context=window_outputs=0;return
        if actual!=tag:
            if not armed:raise ValueError('mismatch fixture while unarmed')
            armed=False;fences+=1;return
        if not armed:raise ValueError('fixture input unarmed')
        idx=((phase+0x80000)&0xffffffff)>>20
        mixed=[]
        for rx in range(2):
            re,im=map(int,v[rx*2:rx*2+2]);co,si=int(c[idx]),int(s[idx])
            mixed.extend(((re*co-im*si+16384)//32768,(re*si+im*co+16384)//32768))
        if first:
            if rrc_have:rrc_fences+=1
            history.clear();window_outputs=0;first=False
        rrc_have=True;history.append(np.array(mixed,dtype=np.int64))
        if len(history)==41:
            # Full direct convolution, not the RTL's symmetric 3-MAC folding.
            result=(q@np.array(history)[::-1]+65536)//131072
            if np.any(result<-(1<<19)) or np.any(result>=(1<<19)):raise ValueError('RRC output overflow')
            output_start=window_outputs==0
            expected.append(((tag&~mask)|((tag-20)&mask),pack(result,20),int(output_start),context))
            outputs+=1;starts+=int(output_start);window_outputs+=1
        phase=(phase+step)&0xffffffff;tag=(tag&~mask)|((tag+1)&mask)
    cfg(1<<48,0x12345678,0x23456789);emit([32767,-32768,1,-1],reset=True)
    for i,freq in enumerate((-350000,-236000.123,236000.123,350000)):
        # Same-epoch contiguous config transitions deliberately require out_start
        # to reset RRC even though tag continuity alone would not detect them.
        t=(2<<48) if i==0 else tag
        cfg(t,int(rng.integers(0,1<<32)),round(-freq*(1<<32)/4000000),hold=23 if i else 0)
        for j in range(1400):
            values=[32767,-32768,32767,-32768] if j%7==0 else rng.integers(-32768,32768,4)
            emit(values)
    for j in range(1600):emit(rng.integers(-32768,32768,4),mode=1)
    for kind in ('gap','duplicate','epoch'):
        bad=tag+7 if kind=='gap' else (tag-1 if kind=='duplicate' else tag+(1<<48))
        emit([10,20,-30,-40],actual=bad)
        commands.append((4,0,7,bad+1,0,0,0))
        cfg((4<<48)+fences*3000,0x456789ab,round(-12345*(1<<32)/4000000))
        for j in range(500):emit(rng.integers(-32768,32768,4))
    cfg((5<<48)|(mask-300),0xffff0000,0xfedcba98)
    for j in range(700):emit(rng.integers(-32768,32768,4))
    # Reconfigure with40 overlapping input tags. The first new output center
    # remains contiguous with the last old center but start/context must change.
    base=(6<<48)+10000
    cfg(base,0x31234567,0x013579ab)
    for j in range(80):emit(rng.integers(-32768,32768,4))
    cfg(base+40,0x81234567,0xf13579ab,hold=23)
    for j in range(41):emit(rng.integers(-32768,32768,4),mode=2 if j==40 else 0)
    commands.append((5,2,32,0,0,0,0))
    for j in range(20):emit(rng.integers(-32768,32768,4))
    for path in a.capture:
        if path.stat().st_size%16:raise ValueError('truncated ADC capture')
        raw=np.memmap(path,dtype=np.dtype([('tag','<u8'),('iq','<i2',(4,))]),mode='r')
        crc=f'{zlib.crc32(raw):08x}'
        if crc!=json.loads((path.parent/'receipt.json').read_text())['capture']['crc32']:raise ValueError('ADC CRC differs')
        segment=raw[154000:156048];before=outputs
        cfg(int(segment[0]['tag']),0x81234567,round(236000*(1<<32)/4000000))
        for j,row in enumerate(segment):emit(row['iq'],actual=int(row['tag']),period=12+j%2)
        adc.append({'capture':str(path),'crc32':crc,'inputs':len(segment),'outputs':outputs-before,
                    'CFO_for_cost_probe_only_hz':-236000})
    (out/'input.txt').write_text(''.join(f'{k} {m} {n} {t:016x} {p:08x} {d:08x} {v:016x}\n' for k,m,n,t,p,d,v in commands))
    (out/'expected.txt').write_text(''.join(f'{t:016x} {v:020x} {s} {c:08x}\n' for t,v,s,c in expected))
    tb=r'''`timescale 1ns/1ps
module tb;
 reg clk=0;always #10 clk=~clk;
 reg rst=1,cv=0,iv=0,ready=1;reg [63:0] ct=0,it=0,idata=0;
 reg [31:0] cp=0,cs=0;wire cr,ir,ov,armed,ostart;wire [63:0] ot;wire [79:0] result;
 wire [31:0] nf,rf,ocontext;
 nco_rrc_cost_probe dut(clk,rst,cv,cr,ct,cp,cs,iv,ir,it,idata,ov,ready,ot,result,armed,nf,rf,ostart,ocontext);
 integer fd,ef,n,kind,mode=0,period,cycle=0,sent=0,received=0,cfgs=0;
 integer last_input=-100,group_count=0,last_cadenced=0,previous_mode=0,interval;
 integer sustained=0,min_ii=100,max_ii=0,cadenced=0,in_stalls=0,out_stalls=0,cfg_stalls=0,unarmed=0;
 integer expected_start,starts=0,contiguous_starts=0,start_stalls=0,old_context_outputs=0;
 reg [63:0] ft,fv,et,held_it,held_input,held_ot;reg [31:0] fp,fs;reg [79:0] ev,held_result;
 reg held_in=0,held_out=0,held_ostart,have_output=0;
 reg [31:0] ec,held_context,latest_context=0,previous_output_context;
 reg [63:0] previous_output_tag;
 always @(negedge clk) ready=mode==2?0:(mode==1?!((cycle%97<19)||(cycle%113<11)):1);
 always @(posedge clk) begin
  cycle=cycle+1;
  if(rst) begin held_in=0;held_out=0;group_count=0;last_cadenced=0;last_input=-100;latest_context=0;have_output=0;end
  else begin
   if(held_in && (!iv || it!==held_it || idata!==held_input)) $fatal(1,"input stall mutation");
   if(held_out && (!ov || ot!==held_ot || result!==held_result || ostart!==held_ostart || ocontext!==held_context)) $fatal(1,"output stall mutation");
   held_in=iv&&!ir;held_out=ov&&!ready;
   if(held_in) begin held_it=it;held_input=idata;in_stalls=in_stalls+1;end
   if(held_out) begin held_ot=ot;held_result=result;held_ostart=ostart;held_context=ocontext;out_stalls=out_stalls+1;if(ostart) start_stalls=start_stalls+1;end
   if(cv && !cr) cfg_stalls=cfg_stalls+1;
   if(cv && cr) begin cfgs=cfgs+1;group_count=0;last_cadenced=0;latest_context=latest_context+1;end
   if(iv && ir) begin
    interval=cycle-last_input;
    if(group_count>16 && period==0 && mode==0 && previous_mode==0) begin
     sustained=sustained+1;
     if(interval<min_ii) min_ii=interval;
     if(interval>max_ii) max_ii=interval;
     if(interval>9) $fatal(1,"combined service slower than1.3x4M II%0d",interval);
    end
    if(period>0 && last_cadenced && interval!=period) $fatal(1,"cadence missed %0d !=%0d",interval,period);
    if(period>0) cadenced=cadenced+1;
    last_cadenced=period>0;last_input=cycle;group_count=group_count+1;previous_mode=mode;sent=sent+1;
   end
   if(ov && ready) begin
    n=$fscanf(ef,"%h %h %d %h\n",et,ev,expected_start,ec);
    if(n!=4 || ot!==et || result!==ev || ostart!==1'(expected_start) || ocontext!==ec) $fatal(1,"connected mismatch out%0d tag%h expected%h got%h expected%h start%0d expected%0d context%0d expected%0d",received,ot,et,result,ev,ostart,expected_start,ocontext,ec);
    if(ostart) begin
     starts=starts+1;
     if(have_output && ot=={previous_output_tag[63:48],previous_output_tag[47:0]+48'd1}) begin
      if(ocontext==previous_output_context) $fatal(1,"overlap restart context did not change");
      contiguous_starts=contiguous_starts+1;
     end
    end
    if(ocontext!=latest_context) old_context_outputs=old_context_outputs+1;
    previous_output_tag=ot;previous_output_context=ocontext;have_output=1;
    received=received+1;
   end
  end
  #1;if(rst && (ov || ostart || ocontext!=0)) $fatal(1,"reset did not clear output metadata");
 end
 initial begin
  fd=$fopen("input.txt","r");ef=$fopen("expected.txt","r");
  repeat(3) @(negedge clk);rst=0;
  while(!$feof(fd)) begin
   n=$fscanf(fd,"%d %d %d %h %h %h %h\n",kind,mode,period,ft,fp,fs,fv);
   if(n!=7) $fatal(1,"parse");
   if(kind==0) begin
    ct=ft;cp=fp;cs=fs;
    if(period>0) begin
     // Hold downstream first; assert config after its fixed hold interval.
     // Older filtered outputs stay in place until the consumer resumes.
     mode=2;repeat(period) @(negedge clk);mode=0;
    end
    cv=1;@(posedge clk);while(!cr) @(posedge clk);
    @(negedge clk);cv=0;
   end else if(kind==5) begin
    iv=0;repeat(period) @(negedge clk);mode=0;group_count=0;last_cadenced=0;repeat(2) @(negedge clk);
   end else if(kind==4) begin
    // After the mismatched sample was actually consumed, admission must
    // remain disabled until a new configuration; no further input is sent.
    repeat(period) begin
     #1;if(ir || armed) $fatal(1,"unarmed accepted input");
     @(negedge clk);unarmed=unarmed+1;
    end
   end else begin
    if(period>0) repeat(period-1) @(negedge clk);
    it=ft;idata=fv;iv=1;
    #1;if(period>0 && !ir) $fatal(1,"not ready4MSPS");
    @(posedge clk);while(!ir) @(posedge clk);
    @(negedge clk);iv=0;
    if(kind==3) begin rst=1;repeat(2) @(negedge clk);rst=0;end
   end
  end
  mode=0;repeat(100) @(negedge clk);
  if(sent!=@INPUTS@ || received!=@OUTPUTS@ || cfgs!=@CONFIGS@ || nf!=@FENCES@ || rf!=@RRCFENCES@ ||
     sustained<3000 || max_ii>9 || cadenced!=@CADENCED@ || cfg_stalls==0 || out_stalls==0 || unarmed!=21 ||
     starts!=@STARTS@ || contiguous_starts<1 || start_stalls==0 || old_context_outputs==0)
    $fatal(1,"coverage sent%0d received%0d cfg%0d nf%0d rf%0d ii%0d cadence%0d",sent,received,cfgs,nf,rf,max_ii,cadenced);
  if(!$feof(ef)) $fatal(1,"unconsumed oracle");
  $display("G0_NCO_RRC_PASS inputs=%0d outputs=%0d configs=%0d sustained_intervals=%0d min_ii=%0d max_ii=%0d cadenced=%0d in_stalls=%0d out_stalls=%0d cfg_stalls=%0d unarmed=%0d nco_fences=%0d rrc_fences=%0d starts=%0d contiguous_starts=%0d start_stalls=%0d old_context_outputs=%0d",
   sent,received,cfgs,sustained,min_ii,max_ii,cadenced,in_stalls,out_stalls,cfg_stalls,unarmed,nf,rf,starts,contiguous_starts,start_stalls,old_context_outputs);
  $finish;
 end
 initial begin #100000000;$fatal(1,"timeout");end
endmodule
'''
    for key,v in [('INPUTS',inputs),('OUTPUTS',outputs),('CONFIGS',configs),('FENCES',fences),('RRCFENCES',rrc_fences),('STARTS',starts),('CADENCED',sum(r['inputs'] for r in adc))]:tb=tb.replace('@'+key+'@',str(v))
    (out/'tb.sv').write_text(tb);runs=[]
    def run(name,args):
        runs.append(args);(out/'commands.json').write_text(json.dumps(runs,indent=2)+'\n')
        with (out/(name+'.log')).open('w') as log:subprocess.run(args,cwd=out,stdout=log,stderr=subprocess.STDOUT,check=True)
    bp=a.vivado_bin.resolve();sources=[]
    for name in ('nco_cost_probe.sv','rrc_cost_probe.sv','nco_rrc_cost_probe.sv'):
        dest=out/name;shutil.copyfile(project/'sim/g0'/name,dest);sources.append(str(dest))
    run('compile',[str(bp/'xvlog'),'--sv',*sources,str(out/'tb.sv')])
    run('elaborate',[str(bp/'xelab'),'tb','-s','nco_rrc_tb','-mt','off'])
    run('simulate',[str(bp/'xsim'),'nco_rrc_tb','-runall'])
    sim=(out/'simulate.log').read_text()
    if 'G0_NCO_RRC_PASS' not in sim or 'Fatal:' in sim:raise ValueError('connected RTL data/service failed')
    measured={k:int(v) for k,v in re.findall(r'(\w+)=(\d+)',sim.split('G0_NCO_RRC_PASS')[-1])}
    fault=tb.replace('module tb;','module fault_tb;').replace('wire [79:0] result;',"wire [79:0] raw_result;wire [79:0] result=raw_result^80'd1;").replace('ot,result,armed,nf,rf,ostart,ocontext);','ot,raw_result,armed,nf,rf,ostart,ocontext);')
    (out/'fault_tb.sv').write_text(fault)
    run('fault-compile',[str(bp/'xvlog'),'--sv',str(out/'fault_tb.sv')])
    run('fault-elaborate',[str(bp/'xelab'),'fault_tb','-s','nco_rrc_fault_tb','-mt','off'])
    run('fault-simulate',[str(bp/'xsim'),'nco_rrc_fault_tb','-runall'])
    fl=(out/'fault-simulate.log').read_text()
    if 'Fatal: connected mismatch' not in fl or 'G0_NCO_RRC_PASS' in fl:raise ValueError('fault not rejected')
    for name,decl,mutated,old_port,new_port in [
        ('marker','wire cr,ir,ov,armed,ostart;','wire cr,ir,ov,armed,raw_start;wire ostart=ov?~raw_start:raw_start;',
         'rf,ostart,ocontext);','rf,raw_start,ocontext);'),
        ('context','wire [31:0] nf,rf,ocontext;',"wire [31:0] nf,rf,raw_context;wire [31:0] ocontext=raw_context^(ov?32'd1:32'd0);",
         'rf,ostart,ocontext);','rf,ostart,raw_context);')]:
        faulty=tb.replace('module tb;',f'module {name}_fault_tb;').replace(decl,mutated).replace(old_port,new_port)
        path=out/f'{name}_fault_tb.sv';path.write_text(faulty)
        run(name+'-fault-compile',[str(bp/'xvlog'),'--sv',str(path)])
        run(name+'-fault-elaborate',[str(bp/'xelab'),name+'_fault_tb','-s','nco_rrc_'+name+'_fault_tb','-mt','off'])
        run(name+'-fault-simulate',[str(bp/'xsim'),'nco_rrc_'+name+'_fault_tb','-runall'])
        faultlog=(out/(name+'-fault-simulate.log')).read_text()
        if 'Fatal: connected mismatch' not in faultlog or 'G0_NCO_RRC_PASS' in faultlog:raise ValueError(name+' fault not rejected')
    receipt={'scope':'G0 connected NCO/RRC only, no notch/CFO estimator/interpolator or complete PHY',
        'clock_hz':50000000,'input_signed_bits':16,'output_signed_bits':20,'output_input_scale':2,
        'output_tag_coordinate':'RRC window center = original endpoint-20, epoch retained',
        'oracle':'full-circle sincos quantization and direct41-tap integer convolution, not quarterwave/symmetric folding',
        'measured':measured,'integer_component_comparisons':4*outputs,'ADC_replays':adc,
        'single_bit_output_fault_rejected':True,'out_start_fault_rejected':True,'out_context_fault_rejected':True,
        'out_start_and_context_comparisons':outputs,
        'context_semantics':'increment on each drained NCO configuration; latched per RRC work item, including old outputs after reconfiguration',
        'g0_passed':False}
    if not a.sim_only:
        (out/'ooc.tcl').write_text('set_param general.maxThreads 4\n'+''.join(f'read_verilog -sv {{{s}}}\n' for s in sources)+'''
synth_design -top nco_rrc_cost_probe -part xc7z020clg484-1 -mode out_of_context
create_clock -name processing -period 20.000 [get_ports clk]
set_input_delay 2.0 -clock processing [get_ports -filter {DIRECTION == IN && NAME != clk}]
set_output_delay 2.0 -clock processing [get_ports -filter {DIRECTION == OUT}]
opt_design
place_design
route_design
report_utilization -file utilization.rpt
report_utilization -hierarchical -file hierarchy.rpt
report_timing_summary -file timing.rpt -report_unconstrained
report_drc -file drc.rpt
report_timing -max_paths 10 -file paths.rpt
write_checkpoint -force nco_rrc_cost_probe.dcp
''')
        run('ooc',[str(bp/'vivado'),'-mode','batch','-source',str(out/'ooc.tcl'),'-nolog','-nojournal'])
        receipt['routed_OOC']=routed_measurements(out)
    else:receipt['routed_OOC']=None
    (out/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n');print(json.dumps(receipt))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--vivado-bin',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True);p.add_argument('--sim-only',action='store_true')
    p.add_argument('--capture',type=Path,action='append',default=[]);main(p.parse_args())
