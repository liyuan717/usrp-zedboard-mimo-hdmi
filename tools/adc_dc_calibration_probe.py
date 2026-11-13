#!/usr/bin/env python3
"""Exact raw four-lane startup-DC candidate: mathematical vectors and routed cost.
No RF emission, physical IO or notch/whole-receiver acceptance.
"""
import argparse,hashlib,json,shutil,struct,subprocess
from pathlib import Path
import numpy as np
from fec_inverse_cost_probe import routed_measurements
from workspace_storage_guard import check,GIB
SOURCE='adc_dc_calibration_cost_probe.sv'
TB='''`timescale 1ns/1ps
module tb;
 reg clk=0;always #10 clk=~clk;
 reg rst=1,en=0,iv=0,loss=0;reg [63:0] tag=0,data=0;
 wire ov,ol,cal;wire [63:0] ot,od,first;wire [31:0] faults,drops;
 adc_dc_calibration_cost_probe #(.N(@N@),.GUARD(@GUARD@),.GAP_TIMEOUT(@GAP@)) dut(
 clk,rst,en,iv,loss,tag,data,ov,ol,ot,od,cal,faults,drops,first);
 integer f,rc,enable_i,valid_i,loss_i,want,ef,ed,checked=0,rows=0;
 reg [63:0] expected;
 initial begin
  repeat(5)@(negedge clk);rst=0;
  f=$fopen("vectors.txt","r");
  while(!$feof(f))begin
   rc=$fscanf(f,"%d %d %d %h %h %d %h %d %d\\n",enable_i,valid_i,loss_i,tag,data,want,expected,ef,ed);
   if(rc!=9)$fatal(1,"vector parse");
   en=enable_i;iv=valid_i;loss=loss_i;@(posedge clk);#1;
   if(ov!==want[0] || (ov&&(od!==expected||ot!==tag)) || faults!==ef || drops!==ed)
    $fatal(1,"DC oracle row%0d valid%0d/%0d data%h/%h faults%0d/%0d drops%0d/%0d",rows,ov,want,od,expected,faults,ef,drops,ed);
   if(ov)checked=checked+1;rows=rows+1;@(negedge clk);
  end
  $fclose(f);$display("G0_ADC_DC_PASS rows=%0d exact_outputs=%0d faults=%0d range_drops=%0d",rows,checked,faults,drops);$finish;
 end
 initial begin #1000000000;$fatal(1,"DC timeout");end
endmodule
'''

def rne(n,d):
 q,r=divmod(n,d);return q+(2*r>d or (2*r==d and q&1))
def pack(values):return sum((int(v)&65535)<<(i*16) for i,v in enumerate(values))

def prepare(a,out):
 n,guard,gap=(16,160,512) if a.synthetic else (80000,2048,1000000)
 rows=0;outputs=0;faults=0;drops=0;exact_fixture=0
 with (out/'vectors.txt').open('w') as f:
  def emit(en,iv,loss,tag,lanes,want=0,expected=0):
   nonlocal rows,outputs
   f.write(f'{en} {iv} {loss} {tag:016x} {pack(lanes):016x} {want} {expected:016x} {faults} {drops}\n');rows+=1;outputs+=want
  if a.rails:
   head=[[-32768,32767,-32768,32767]]*(n//2)+[[-32767,32766,-32768,32767]]*(n//2)
   samples=head+[[-32767,32766,-32768,32767]]*(guard+32)
   sums=[sum(v[j] for v in head) for j in range(4)]
   assert max(abs(v) for v in sums)>2**31
   for i,lanes in enumerate(samples):
    expected=[rne(v*n-s,n) for v,s in zip(lanes,sums)]
    valid=i>=n+guard;overflow=valid and any(v < -32768 or v >32767 for v in expected)
    drops+=overflow;emit(1,1,0,0x1234000000000000+i,lanes,int(valid and not overflow),pack(expected))
   detail={'full80000_sample_signed_rails':True,'sum_integer':sums,'exceeds_signed32_accumulator':True,'input_spacing_cycles':1}
  elif not a.synthetic:
   raw=np.memmap(a.capture,mode='r',dtype=np.dtype([('tag','<u8'),('iq','<i2',(4,))]));assert len(raw)==2097152
   assert np.all(np.diff(raw['tag'])==1)
   sums=raw['iq'][:n].astype(np.int64).sum(axis=0)
   reference=out.parent/'g0-adc-dma-batch-air-r4/adc.hex'
   assert reference.exists();gold=iter(reference.read_text().splitlines())
   for i,row in enumerate(raw):
    expected=[rne(int(v)*n-int(s),n) for v,s in zip(row['iq'],sums)]
    valid=i>=n+guard;overflow=valid and any(v < -32768 or v >32767 for v in expected)
    drops+=overflow;want=int(valid and not overflow)
    word=pack(expected)
    if i>=83644:
     assert want and word==int(next(gold),16),(i,word);exact_fixture+=1
    emit(1,1,0,int(row['tag']),row['iq'],want,word)
   assert next(gold,None) is None
   detail={'raw_capture':str(a.capture),'sum_integer':list(map(int,sums)),'outputs_matching_existing52RTL_ADC_fixture':exact_fixture,'notch_implemented':False,'input_spacing_cycles':1}
  else:
   tag=0x8011fffffffffff0
   def advance():
    nonlocal tag
    tag=(tag&0xffff000000000000)|((tag+1)&((1<<48)-1))
   def phase(head,body):
    nonlocal drops
    sums=[sum(v[j] for v in head) for j in range(4)]
    for i,v in enumerate(head+body):
     wanted=i>=n+guard;answer=[rne(v[j]*n-sums[j],n) for j in range(4)]
     overflow=wanted and any(x < -32768 or x >32767 for x in answer)
     drops+=overflow;emit(1,1,0,tag,v,int(wanted and not overflow),pack(answer));advance()
     # Nominal4MSPS spacing; both12 and13-cycle intervals are covered.
     if i!=len(head)+len(body)-1:
      for _ in range(11+i%2):emit(1,0,0,tag,[0]*4)
   head=[[i%2,-(i%2),1,-1] for i in range(n)]
   phase(head,[[i-90,90-i,32767,-32768] if i%3 else [-32768,32767,-32768,32767] for i in range(200)])
   # Epoch/tag discontinuity while calibrated: current sample is discarded,
   # next sample begins a fresh mean without external rearm/global reset.
   tag^=1<<48;faults+=1;emit(1,1,0,tag,[7]*4);advance()
   phase([[-32768,32767,-3,3]]*n,[[0,0,i%9-4,4-i%9] for i in range(180)])
   for i in range(gap+2):
    if i==gap-1:faults+=1
    emit(1,0,0,tag,[0]*4)
   phase([[1,-1,0,0]]*n,[[i%7]*4 for i in range(180)])
   faults+=1;emit(1,0,1,tag,[0]*4)
   phase([[i%2]*4 for i in range(n)],[[i%13]*4 for i in range(180)])
   detail={'ties_even_positive_negative':True,'signed_rails_and_out_of_range_drop':True,'tag_low48_wrap_and_epoch_change':True,'idle_timeout_and_upstream_loss_recalibrate_without_reset':True,'input_spacing_cycles':'12/13'}
 return n,guard,gap,{'rows':rows,'expected_outputs':outputs,'expected_faults':faults,'expected_range_drops':drops,**detail}

def main(a):
 r=Path(__file__).resolve().parents[1];out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);bp=a.vivado_bin.resolve()
 c=check('before-PL-DC-calibration');(out/'disk-before.json').write_text(json.dumps(c,indent=2));assert c['disk']['available_bytes']>=5*GIB
 try:
  n,g,timeout,detail=prepare(a,out);shutil.copyfile(r/'sim/g0'/SOURCE,out/SOURCE)
  (out/'tb.sv').write_text(TB.replace('@N@',str(n)).replace('@GUARD@',str(g)).replace('@GAP@',str(timeout)))
  def run(label,args):
   with (out/(label+'.log')).open('w') as f:subprocess.run([str(x) for x in args],cwd=out,stdout=f,stderr=subprocess.STDOUT,check=True)
  run('compile',[bp/'xvlog','--sv',SOURCE,'tb.sv']);run('elaborate',[bp/'xelab','tb','-s','dc_tb','-mt','off']);run('simulate',[bp/'xsim','dc_tb','-runall'])
  log=(out/'simulate.log').read_text();assert 'G0_ADC_DC_PASS' in log and 'Fatal:' not in log,log[-2500:]
  receipt={'scope':__doc__,'vectors':detail,'simulation':next(l for l in log.splitlines() if l.startswith('G0_ADC_DC_PASS')),'source_sha256':hashlib.sha256((out/SOURCE).read_bytes()).hexdigest(),'routed_OOC':None,'whole_receiver_or_physical_G0_passed':False}
  if not a.sim_only:
   (out/'ooc.tcl').write_text('set_param general.maxThreads 2\nread_verilog -sv {'+str(out/SOURCE)+'}\nsynth_design -top adc_dc_calibration_cost_probe -part xc7z020clg484-1 -mode out_of_context\ncreate_clock -name clk -period 20 [get_ports clk]\nset_input_delay 2 -clock clk [get_ports -filter {DIRECTION == IN && NAME != clk}]\nset_output_delay 2 -clock clk [all_outputs]\nopt_design\nplace_design\nroute_design\nreport_utilization -file utilization.rpt\nreport_utilization -hierarchical -file hierarchy.rpt\nreport_timing_summary -file timing.rpt -report_unconstrained\nreport_drc -file drc.rpt\nwrite_checkpoint -force dc.dcp\n')
   run('ooc',[bp/'vivado','-mode','batch','-source','ooc.tcl','-nolog','-nojournal']);receipt['routed_OOC']=routed_measurements(out)
  (out/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n');print(json.dumps(receipt),flush=True)
 finally:(out/'disk-after.json').write_text(json.dumps(check('after-PL-DC-calibration'),indent=2)+'\n')

if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__)
 p.add_argument('--output',type=Path,required=True);p.add_argument('--vivado-bin',type=Path,required=True)
 p.add_argument('--capture',type=Path,default=Path(__file__).resolve().parents[1]/'build/g0-fec-session-air-r2/adc.raw')
 p.add_argument('--synthetic',action='store_true');p.add_argument('--rails',action='store_true');p.add_argument('--sim-only',action='store_true');main(p.parse_args())
