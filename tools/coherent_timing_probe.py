#!/usr/bin/env python3
"""Configured coherent timing from three captured, calibrated integer RRC streams.
Real192-symbol C/E and peak/parabola run in RTL. Search window is predicted from
the already validated preceding coarse peak, not from the future coherent peak.
SFO fitting, live upstream connection and normal-training dispatch are pending.
"""
import argparse,json,hashlib,math,shutil,subprocess,zlib
from fractions import Fraction
from pathlib import Path
import numpy as np
from scipy import signal
from g0_causal_acquisition import calibrated_input
from g0_fixed_differential_model import quantize_adc
from g0_packet_probe import PERIOD,public_prefix
from g0_sounder import rrc
from coherent_window_probe import metric,pack
from coherent_peak_probe import refine
from fec_inverse_cost_probe import routed_measurements
from workspace_storage_guard import check as disk_check,GIB

SOURCES=['coherent_window_cost_probe.sv','coherent_peak_cost_probe.sv','coherent_timing_cost_probe.sv']

def unpack(rows,bits):
 result=np.array([[(int(x,16)>>(bits*j))&((1<<bits)-1) for j in range(4)] for x in rows],dtype=np.int64)
 result[result>=(1<<(bits-1))]-=1<<bits;return result

def prepare(out,reference):
 old=json.loads((reference/'receipt.json').read_text());assert old['simulation'].startswith('G0_ACQUIRING_FRONTEND_PASS') and len(old['fixtures'])==6
 old_filtered=(reference/'filtered.hex').read_text().splitlines();old_adcs=(reference/'adc.hex').read_text().splitlines()
 pre=public_prefix()[256:448,0];signs=np.column_stack((np.where(pre.real>0,1,-1),np.where(pre.imag>0,1,-1)))
 (out/'coherent_signs.hex').write_text(''.join(f'{int(si<0)+2*int(sq<0):x}\n' for si,sq in signs))
 sine=np.array([round(65536*math.sin(2*math.pi*k/4096)) for k in range(4096)],dtype=np.int64)
 cosine=np.array([round(65536*math.cos(2*math.pi*k/4096)) for k in range(4096)],dtype=np.int64)
 taps=np.rint(rrc()*131072).astype(np.int64);allrows=[];gold=[];initial=[];reports=[];old_offset=0;cache={}
 for j,f in enumerate(old['fixtures']):
  rawpath=Path(f['capture']);origin,oldend=f['source_slice'];peak=f['coarse']['selected_start_index'];commit=peak+2960;end=peak+PERIOD*4+5000
  if rawpath not in cache:
   raw=np.memmap(rawpath,dtype=np.dtype([('tag','<u8'),('iq','<i2',(4,))]),mode='r');assert f'{zlib.crc32(raw):08x}'==f['capture_crc32']
   iq=np.asarray(raw['iq'][:end],dtype=np.float64);y,cal=calibrated_input(iq[:,0::2]+1j*iq[:,1::2],PERIOD);adc,_=quantize_adc(y);cache[rawpath]=(adc,cal)
  adc,cal=cache[rawpath];values=adc.astype(np.int64).copy()
  if f['sense']:values[:,1::2]*=-1
  prefix=unpack(old_adcs[old_offset:old_offset+oldend-origin],16);assert np.array_equal(values[origin:oldend],prefix)
  cfg=f['final_configuration'];phase=(np.arange(end-commit,dtype=np.uint64)*np.uint64(cfg['step_u32'])+np.uint64(cfg['phase_u32']))&np.uint64(0xffffffff)
  bins=(((phase+np.uint64(0x80000))&np.uint64(0xffffffff))>>np.uint64(20)).astype(np.int64);co,si=cosine[bins],sine[bins]
  mixed=np.empty((end-commit,4),dtype=np.int64)
  nco_input=values[commit:].copy()
  if f['sense']:nco_input[:,1::2]*=-1 # Actual cfg_conjugate before multiplication.
  for rx in (0,1):
   i=nco_input[:,rx*2];q=nco_input[:,rx*2+1]
   mixed[:,rx*2]=(i*co-q*si+16384)//32768;mixed[:,rx*2+1]=(i*si+q*co+16384)//32768
  filtered=np.column_stack([(np.convolve(mixed[:,l],taps,'full')[:end-commit]+65536)//131072 for l in range(4)])
  retained=filtered[40:];assert np.max(retained)<1<<19 and np.min(retained)>=-(1<<19)
  previous=unpack(old_filtered[old_offset+commit-origin+40:old_offset+oldend-origin],20)
  assert np.array_equal(retained[:len(previous)],previous),('extended corrected RRC prefix differs from previously RTL-validated data',j)
  source_first_center=commit+20;first=peak+PERIOD*4-32;relative=first-source_first_center
  metrics=[]
  for k in range(65):
   row=metric(retained,signs,relative+k);gold.append(row);metrics.append(((row>>116)&((1<<60)-1),row>>176))
  best,mu,error=refine(metrics);assert error==0,(j,best,error)
  ratios=[Fraction(n,e) if e else Fraction(0) for n,e in metrics[best-1:best+2]]
  exact=(ratios[0]-ratios[2])/(2*(ratios[0]-2*ratios[1]+ratios[2]));exact_q=(2*exact.numerator*65536+exact.denominator)//(2*exact.denominator)
  assert abs(exact_q-mu)<=1
  offset=len(allrows);allrows.extend(pack(row,20) for row in retained)
  initial.append(f"offsets[{j}]={offset};lengths[{j}]={len(retained)};windows[{j}]={relative};best_indices[{j}]={best};fractions[{j}]=18'h{mu&0x3ffff:05x};")
  reports.append({'capture':str(rawpath),'capture_crc32':f['capture_crc32'],'sense':f['sense'],'calibration_prefix_unchanged':True,'previous_ADC_and_post_commit_RRC_prefix_identical':True,
   'preceding_coarse_peak':peak,'NCO_commit':commit,'NCO_configuration':cfg,'source_RRC_first_center':source_first_center,'source_RRC_vectors':len(retained),
   'window_first_center':first,'window_prediction':'preceding actual coarse peak + nominal80000 samples -32','metrics':65,'best_point':best,'fraction_q16':mu,
   'center_position_capture':float(Fraction(first+best)+Fraction(mu,65536)),'normalized_peak_score':float(Fraction(metrics[best][0],96*metrics[best][1])),
   'Q24_vs_exact_ratio_parabola_error_samples':float(Fraction(mu,65536)-exact),'exact_ratio_fraction_q16':exact_q})
  old_offset+=oldend-origin
 (out/'words.hex').write_text(''.join(f'{row:020x}\n' for row in allrows));(out/'expected.hex').write_text(''.join(f'{row:057x}\n' for row in gold))
 return len(allrows),'\n'.join(initial),reports

TB=r'''`timescale 1ns/1ps
module tb;
reg clk=0;always #10 clk=~clk;
reg rst=1,cancel=0,iv=0,start=0,cv=0,ready=0,ext=0;reg [63:0] tag=0,first=0;reg [79:0] data=0;reg [31:0] id=0,gen=0;reg [6:0] points=0;reg [11:0] address=0;
wire ir,cr,ov,restart,busy;wire [79:0] word,position;wire [31:0] oid,ogen,hgen;wire [63:0] ot,latest;wire [12:0] hc;wire signed [17:0] fraction;wire [2:0] error;wire [6:0] point;
coherent_timing_cost_probe dut(clk,rst,cancel,iv,ir,start,tag,data,ext,address,word,cv,cr,id,gen,first,points,ov,ready,oid,ogen,ot,fraction,position,error,point,hgen,latest,hc,restart,busy);
reg [79:0] words[0:@LAST@];reg [227:0] expected[0:389];integer offsets[0:5],lengths[0:5],windows[0:5],best_indices[0:5];reg [17:0] fractions[0:5];
integer j,f,k,cycle=0,metrics=0,inputs=0,peaks=0,age,max_age=0,known_delay,max_known_delay=0;reg [47:0] base;reg [15:0] epoch;reg [79:0] expected_position;reg [235:0] held;
always @(negedge clk) begin ext=!rst && !cancel && hc>=8 && (cycle%17==0 || cycle%17==1);address=latest[11:0]-12'd7;end
always @(posedge clk) begin
 cycle=cycle+1;
 if(!rst && !cancel) begin
  if(!ir) $fatal(1,"real RRC writer backpressure");if(iv && ir)inputs=inputs+1;
  if(dut.window.own_read) begin age=48'(latest[47:0]-dut.window.own_read_tag[47:0]);if(age>max_age)max_age=age;end
  if(dut.metric_valid && dut.metric_ready) begin
   if(dut.metric_error || dut.metric_id!=900+j || dut.metric_generation!=1 || dut.metric_tag!=={epoch,base+48'(windows[f])+48'(dut.metric_point)} ||
      {dut.metric_e,dut.metric_n,dut.metric_c}!==expected[f*65+dut.metric_point]) $fatal(1,"real coherent metric mismatch j%0d point%0d",j,dut.metric_point);
   metrics=metrics+1;
  end
 end
end
initial begin
 $readmemh("words.hex",words);$readmemh("expected.hex",expected);
 @INIT@
 for(j=0;j<12;j=j+1) begin
  f=j%6;rst=1;iv=0;cv=0;ready=0;repeat(5) @(negedge clk);rst=0;
  base=48'hffffffff63c0+48'(j*5000);epoch=200+j;
  fork
   begin
    for(k=0;k<lengths[f];k=k+1) begin
     tag={epoch,base+48'(k)};data=words[offsets[f]+k];start=k==0;iv=1;
     @(negedge clk);iv=0;start=0;repeat(j<6?7:11+k%2) @(negedge clk);
    end
   end
   begin
    while(hc==0) @(negedge clk);
    first={epoch,base+48'(windows[f])};id=900+j;gen=hgen;points=65;cv=1;
    @(posedge clk);while(!cr) @(posedge clk);@(negedge clk);cv=0;first=0;id=~id;gen=~gen;points=0;
    while(!ov) @(negedge clk);
    expected_position={base+48'(windows[f])+48'(best_indices[f]),32'd0}+($signed({{62{fractions[f][17]}},fractions[f]})<<<16);
    if(error || oid!=900+j || ogen!=1 || point!=best_indices[f] || fraction!==fractions[f] || ot!=={epoch,base+48'(windows[f])+48'(best_indices[f])} || position!==expected_position) $fatal(1,"real coherent peak mismatch j%0d err%0d point%0d/%0d mu%0d/%0d",j,error,point,best_indices[f],fraction,$signed(fractions[f]));
    known_delay=48'(latest[47:0]-(base+48'(windows[f])+48'(best_indices[f])));if(known_delay>max_known_delay)max_known_delay=known_delay;
    held={oid,ogen,ot,fraction,position,error,point};
    repeat(120) begin @(negedge clk);if(!ov || {oid,ogen,ot,fraction,position,error,point}!==held) $fatal(1,"real held peak");end
    ready=1;@(negedge clk);ready=0;peaks=peaks+1;
   end
  join
  repeat(20) @(negedge clk);if(busy || ov) $fatal(1,"real timing endpoint");
 end
 if(peaks!=12 || metrics!=780 || inputs!=2*(@LAST@+1) || max_age>=4096) $fatal(1,"real timing coverage");
 $display("G0_COHERENT_TIMING_PASS peaks=%0d metrics=%0d RRC_inputs=%0d maximum_read_age=%0d maximum_peak_known_delay_samples=%0d",peaks,metrics,inputs,max_age,max_known_delay);$finish;
end
initial begin #400000000;$fatal(1,"real coherent timeout");end
endmodule
'''

def main(a):
 out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);bp=a.vivado_bin.resolve();project=Path(__file__).resolve().parents[1]
 before=disk_check('before-coherent-timing');(out/'disk-before.json').write_text(json.dumps(before,indent=2)+'\n')
 if before['disk']['available_bytes']<5*GIB:raise SystemExit('Under5GiB: handle storage first.')
 try:
  n,initial,reports=prepare(out,a.frontend_reference.resolve());(out/'tb.sv').write_text(TB.replace('@LAST@',str(n-1)).replace('@INIT@',initial))
  sources=[]
  for name in SOURCES:
   target=out/name;shutil.copyfile(project/'sim/g0'/name,target);sources.append(target)
  def run(step,args):
   with (out/(step+'.log')).open('w') as log:subprocess.run(args,cwd=out,stdout=log,stderr=subprocess.STDOUT,check=True)
  xpm=bp.parent/'data/ip/xpm'
  run('compile',[str(bp/'xvlog'),'--sv',str(xpm/'xpm_cdc/hdl/xpm_cdc.sv'),str(xpm/'xpm_memory/hdl/xpm_memory.sv'),*map(str,sources),str(out/'tb.sv')]);run('elaborate',[str(bp/'xelab'),'tb','-s','coherent_timing_tb','-mt','off']);run('simulate',[str(bp/'xsim'),'coherent_timing_tb','-runall'])
  log=(out/'simulate.log').read_text();assert 'G0_COHERENT_TIMING_PASS' in log and 'Fatal:' not in log,log[-2500:]
  receipt={'scope':__doc__,'simulation':next(s for s in log.splitlines() if s.startswith('G0_COHERENT_TIMING_PASS')),'fixtures':reports,'source_sha256':{s.name:hashlib.sha256(s.read_bytes()).hexdigest() for s in sources},'routed_OOC':None,'g0_passed':False}
  (out/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
  if not a.sim_only:
   tcl='create_project -in_memory -part xc7z020clg484-1\nset_property XPM_LIBRARIES {XPM_MEMORY XPM_CDC} [current_project]\nset_param general.maxThreads 4\n'+''.join('read_verilog -sv {'+str(s)+'}\n' for s in sources)+'''synth_design -top coherent_timing_cost_probe -part xc7z020clg484-1 -mode out_of_context
create_clock -name processing -period 20 [get_ports clk]
set_input_delay 2 -clock processing [get_ports -filter {DIRECTION == IN && NAME != clk}]
set_output_delay 2 -clock processing [get_ports -filter {DIRECTION == OUT}]
opt_design
place_design
route_design
report_utilization -file utilization.rpt
report_utilization -hierarchical -file hierarchy.rpt
report_timing_summary -file timing.rpt -report_unconstrained
report_drc -file drc.rpt
write_checkpoint -force coherent_timing.dcp
'''
   (out/'ooc.tcl').write_text(tcl);run('ooc',[str(bp/'vivado'),'-mode','batch','-source',str(out/'ooc.tcl'),'-nolog','-nojournal']);receipt['routed_OOC']=routed_measurements(out);(out/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
  print(json.dumps({k:v for k,v in receipt.items() if k not in ('fixtures','source_sha256')}))
 finally:(out/'disk-after.json').write_text(json.dumps(disk_check('after-coherent-timing'),indent=2)+'\n')

if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);p.add_argument('--frontend-reference',type=Path,required=True);p.add_argument('--vivado-bin',type=Path,required=True);p.add_argument('--sim-only',action='store_true');main(p.parse_args())
