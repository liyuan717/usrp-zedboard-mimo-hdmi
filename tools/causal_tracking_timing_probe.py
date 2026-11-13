#!/usr/bin/env python3
"""Actual coherent RTL -> past-only SFO -> autonomous next public window.
Captured/calibrated integer-model RRC replay, not live ADC integration or RF.
One initial arm per case, diagnostic READY held zero, no host per-frame jobs.
"""
import argparse,hashlib,json,math,shutil,subprocess,zlib
from pathlib import Path
import numpy as np
from g0_causal_acquisition import calibrated_input
from g0_fixed_differential_model import quantize_adc
from g0_packet_probe import PERIOD,public_prefix
from g0_sounder import rrc
from coherent_window_probe import metric,pack as pack_word
from coherent_peak_probe import refine
from coherent_timing_probe import unpack
from causal_sfo_probe import Oracle,pack,Q,MASK
from fec_inverse_cost_probe import routed_measurements
from workspace_storage_guard import check as disk_check,GIB

SOURCES=['coherent_window_cost_probe.sv','coherent_peak_cost_probe.sv','coherent_timing_cost_probe.sv',
 'causal_sfo_cost_probe.sv','diagnostic_latest_mailbox_cost_probe.sv','causal_tracking_timing_cost_probe.sv']

def prepare(out,reference):
 old=json.loads((reference/'receipt.json').read_text());assert old['simulation'].startswith('G0_ACQUIRING_FRONTEND_PASS') and len(old['fixtures'])==6
 old_filtered=(reference/'filtered.hex').read_text().splitlines();old_adcs=(reference/'adc.hex').read_text().splitlines()
 pre=public_prefix()[256:448,0];signs=np.column_stack((np.where(pre.real>0,1,-1),np.where(pre.imag>0,1,-1)))
 (out/'coherent_signs.hex').write_text(''.join(f'{int(i<0)+2*int(q<0):x}\n' for i,q in signs))
 sine=np.array([round(65536*math.sin(2*math.pi*k/4096)) for k in range(4096)],dtype=np.int64)
 cosine=np.array([round(65536*math.cos(2*math.pi*k/4096)) for k in range(4096)],dtype=np.int64)
 taps=np.rint(rrc()*131072).astype(np.int64);cache={};sources=[];old_offset=0;total=0;init=[];metadata=[]
 with (out/'words.hex').open('w') as words:
  for j,f in enumerate(old['fixtures']):
   rawpath=Path(f['capture']);origin,oldend=f['source_slice'];peak=f['coarse']['selected_start_index'];commit=peak+2960
   end=peak+(8 if j==0 else 5)*PERIOD*4+5000
   if rawpath not in cache:
    raw=np.memmap(rawpath,dtype=np.dtype([('tag','<u8'),('iq','<i2',(4,))]),mode='r');assert f'{zlib.crc32(raw):08x}'==f['capture_crc32']
    iq=np.asarray(raw['iq'][:end],dtype=np.float64);y,cal=calibrated_input(iq[:,0::2]+1j*iq[:,1::2],PERIOD);adc,_=quantize_adc(y);cache[rawpath]=adc
   values=cache[rawpath][:end].astype(np.int64).copy()
   if f['sense']:values[:,1::2]*=-1
   prefix=unpack(old_adcs[old_offset:old_offset+oldend-origin],16);assert np.array_equal(values[origin:oldend],prefix)
   cfg=f['final_configuration'];phase=(np.arange(end-commit,dtype=np.uint64)*np.uint64(cfg['step_u32'])+np.uint64(cfg['phase_u32']))&np.uint64(0xffffffff)
   bins=(((phase+np.uint64(0x80000))&np.uint64(0xffffffff))>>np.uint64(20)).astype(np.int64);co,si=cosine[bins],sine[bins]
   mixed=np.empty((end-commit,4),dtype=np.int64);nco_input=values[commit:].copy()
   if f['sense']:nco_input[:,1::2]*=-1
   for rx in (0,1):
    i=nco_input[:,rx*2];q=nco_input[:,rx*2+1]
    mixed[:,rx*2]=(i*co-q*si+16384)//32768;mixed[:,rx*2+1]=(i*si+q*co+16384)//32768
   filtered=np.column_stack([(np.convolve(mixed[:,l],taps,'full')[:end-commit]+65536)//131072 for l in range(4)])[40:]
   assert np.max(filtered)<1<<19 and np.min(filtered)>=-(1<<19)
   previous=unpack(old_filtered[old_offset+commit-origin+40:old_offset+oldend-origin],20);assert np.array_equal(filtered[:len(previous)],previous)
   for row in filtered:words.write(f'{pack_word(row,20):020x}\n')
   sources.append(dict(offset=total,data=filtered,first=commit+20,peak=peak));total+=len(filtered);old_offset+=oldend-origin
   metadata.append({'capture':str(rawpath),'capture_crc32':f['capture_crc32'],'sense':f['sense'],'NCO_configuration':cfg,
    'integer_model_RRC_vectors':len(filtered),'previous_ADC_and_post_commit_RRC_prefix_identical':True})
 gold_metrics=[];windows=[];cases=[]
 for j in range(9):
  f=j if j<6 else 0;s=sources[f];target=5 if j<6 else (6 if j==6 else (8 if j==7 else 2))
  length=target*80000+2000;data=s['data'][:length];assert len(data)==length
  kind='normal' if j<6 else ('one_erased_window' if j==6 else ('eight_erased_windows' if j==7 else 'fence_during_SFO'))
  damage_first=s['peak']+3*80000-100-s['first'];damage_last=damage_first+1300
  if j==6:data=data.copy();data[damage_first:damage_last]=0
  if j==7:data=np.zeros_like(data)
  epoch=0xffff if j==8 else 200+j;base=(0xffffffff63c0+j*5000)&((1<<48)-1);first=s['peak']+80000-32-s['first']
  center=(base+first+32)*Q&MASK;period=80000*Q;oracle=Oracle();offset=len(windows);sfo_count=0;updates=0;misses=0;observations=[]
  for w in range(target):
   current=(center//Q-base)&((1<<48)-1);start=current-32
   metrics=[metric(data,signs,start+k) for k in range(65)];pairs=[((v>>116)&((1<<60)-1),v>>176) for v in metrics]
   best,mu,error=refine(pairs);metric_offset=len(gold_metrics);gold_metrics.extend(metrics)
   peakpos=(((base+start+best)<<32)+(mu<<16))&MASK
   sfogold=0;has_sfo=error==0 and not (j==8 and w==1)
   if error:
    assert j in (6,7) and error==4;misses+=1;center=(center+period)&MASK
   else:
    result=oracle.update(900+j+w,1,epoch,peakpos,0);assert result[-1]==0,(j,w,result)
    if has_sfo:
     sfo_count+=1;updates+=int(result[7]);sfogold=pack(result,[32,32,16,80,80,80,35,1,3,3]);observations.append({'window':w,'peaks_used':result[8],'step_Q32':result[6],'SFO_ppm':(result[6]/Q/4-1)*1e6})
    center=result[5];period=(result[5]-result[3])&MASK
   windows.append({'case':j,'first':((base+start)&((1<<48)-1))|(epoch<<48),'metric_offset':metric_offset,'error':error,
    'point':0 if error else best,'fraction':0 if error else mu,'tag':0 if error else ((base+start+best)&((1<<48)-1))|(epoch<<48),
    'position':0 if error else peakpos,'has_sfo':has_sfo,'sfo_gold':sfogold,'pending':False})
  if j<7:
   windows.append({'case':j,'first':(((center//Q)-32)&((1<<48)-1))|(epoch<<48),'pending':True})
  cases.append({'kind':kind,'source':f,'offset':s['offset'],'length':length,'epoch':epoch,'base':base,'initial_window':first,
   'window_offset':offset,'configs':target+int(j<7),'computed_windows':target,'sfo_results':sfo_count,'updates':updates,'misses':misses,
   'damage_first':damage_first,'damage_last':damage_last,'observed_steps':observations})
 for j,c in enumerate(cases):
  init.append(f"offsets[{j}]={c['offset']};lengths[{j}]={c['length']};bases[{j}]=48'h{c['base']:012x};epochs[{j}]=16'h{c['epoch']:04x};firsts[{j}]={c['initial_window']};wbase[{j}]={c['window_offset']};configs[{j}]={c['configs']};computed[{j}]={c['computed_windows']};sfo_counts[{j}]={c['sfo_results']};updates[{j}]={c['updates']};misses[{j}]={c['misses']};damage_first[{j}]={c['damage_first']};damage_last[{j}]={c['damage_last']};")
 for i,w in enumerate(windows):
  init.append(f"wfirst[{i}]=64'h{w['first']:016x};pending[{i}]={int(w['pending'])};")
  if not w['pending']:
   init.append(f"moffset[{i}]={w['metric_offset']};werror[{i}]={w['error']};wpoint[{i}]={w['point']};wfraction[{i}]=18'h{w['fraction']&0x3ffff:05x};wtag[{i}]=64'h{w['tag']:016x};wposition[{i}]=80'h{w['position']:020x};has_sfo[{i}]={int(w['has_sfo'])};sgold[{i}]=362'h{w['sfo_gold']:091x};")
 (out/'metrics.hex').write_text(''.join(f'{v:057x}\n' for v in gold_metrics))
 return total,'\n'.join(init),metadata,cases,len(windows),len(gold_metrics)

TB=r'''`timescale 1ns/1ps
module tb;
reg clk=0;always #10 clk=~clk;
reg rst=1,cancel=0,iv=0,start=0,av=0,dr=0,ext=0;reg [63:0] tag=0,first=0;reg [79:0] data=0;reg [31:0] id=0,gen=0;reg [11:0] address=0;
wire ir,ar,tu,dv,locked,request,restart,active;wire [31:0] tid,tgen,replaced,wstarted,missed,hgen;wire [15:0] te;wire [79:0] tp,train,nextpos,word;wire [34:0] step;wire [2:0] used,reason;wire [121:0] diag;wire [63:0] latest;wire [12:0] hc;
causal_tracking_timing_cost_probe dut(clk,rst,cancel,iv,ir,start,tag,data,ext,address,word,av,ar,id,gen,first,tu,tid,tgen,te,tp,train,nextpos,step,used,dv,dr,diag,replaced,locked,request,reason,wstarted,missed,hgen,latest,hc,restart,active);
reg [79:0] words[0:@LAST@];reg [227:0] expected[0:@MLAST@];
integer offsets[0:8],lengths[0:8],firsts[0:8],wbase[0:8],configs[0:8],computed[0:8],sfo_counts[0:8],updates[0:8],misses[0:8],damage_first[0:8],damage_last[0:8];reg [47:0] bases[0:8];reg [15:0] epochs[0:8];
reg [63:0] wfirst[0:@WLAST@],wtag[0:@WLAST@];reg [79:0] wposition[0:@WLAST@];reg [17:0] wfraction[0:@WLAST@];integer moffset[0:@WLAST@],werror[0:@WLAST@],wpoint[0:@WLAST@];reg pending[0:@WLAST@],has_sfo[0:@WLAST@];reg [361:0] sgold[0:@WLAST@];
integer j,k,cycle=0,ci=0,cs=0,cp=0,cu=0,current=0,next_deadline=0,max_age=0,age,max_update_delay=0,delay;
integer total_inputs=0,total_configs=0,total_peaks=0,total_metrics=0,total_sfo=0,total_updates=0,fences=0;
reg [15:0] epoch;reg fence_pending=0,fence_sent=0,diag_held=0,sfo_was_valid=0;reg [121:0] held;
wire [361:0] sfo_view={tid,tgen,te,tp,train,nextpos,step,dut.sfo_locked,used,dut.sfo_error};
always @(negedge clk)begin
 ext=!rst && !cancel && hc>=8 && (cycle%17==0 || cycle%17==1);address=latest[11:0]-12'd7;
 if(j==8 && dut.sfo.state==8 && dut.sfo.division_bit==31 && !fence_sent)fence_pending=1;
end
always @(posedge clk)begin
 cycle=cycle+1;
 if(rst || cancel)begin diag_held=0;sfo_was_valid=0;next_deadline=0;end
 else begin
  if(!ir)$fatal(1,"tracking writer backpressure");if(iv)total_inputs=total_inputs+1;
  if(restart&&ci>0)begin fences=fences+1;diag_held=0;next_deadline=0;end
  if(dut.sfo_valid&&sfo_was_valid)$fatal(1,"tracking internal SFO receipt not drained");sfo_was_valid=dut.sfo_valid;
  if(dut.coherent.cfg_valid&&dut.cfg_ready)begin
   if(ci>=configs[j])$fatal(1,"tracking extra config");current=wbase[j]+ci;
   if(dut.window_first_tag!==wfirst[current] || dut.window_id!=900+j+ci || dut.owner_generation!=1)$fatal(1,"tracking predicted window oracle case%0d win%0d",j,ci);
   ci=ci+1;total_configs=total_configs+1;next_deadline=0;
  end
  if(next_deadline && cycle>next_deadline)$fatal(1,"tracking next window stalled");
  if(dut.coherent.window.own_read)begin age=48'(latest[47:0]-dut.coherent.window.own_read_tag[47:0]);if(age>max_age)max_age=age;end
  if(dut.coherent.metric_valid&&dut.coherent.metric_ready)begin
   if(pending[current] || dut.coherent.metric_error || {dut.coherent.metric_e,dut.coherent.metric_n,dut.coherent.metric_c}!==expected[moffset[current]+dut.coherent.metric_point] || dut.coherent.metric_tag!=={wfirst[current][63:48],wfirst[current][47:0]+48'(dut.coherent.metric_point)})$fatal(1,"tracking coherent data oracle case%0d win%0d point%0d",j,ci-1,dut.coherent.metric_point);
   total_metrics=total_metrics+1;
  end
  if(dut.peak_valid&&dut.peak_ready)begin
   if(pending[current] || dut.peak_error!=werror[current] || dut.peak_point!=wpoint[current] || dut.peak_fraction!==wfraction[current] || dut.peak_tag!==wtag[current] || dut.peak_position!==wposition[current])$fatal(1,"tracking peak oracle case%0d win%0d",j,ci-1);
   cp=cp+1;total_peaks=total_peaks+1;
  end
  if(dut.valid_sfo)begin
   if(!has_sfo[current] || sfo_view!==sgold[current])$fatal(1,"tracking SFO oracle case%0d win%0d",j,ci-1);
   cs=cs+1;total_sfo=total_sfo+1;next_deadline=cycle+200;
  end
  if(tu)begin
   if(!dut.sfo_locked || dut.sfo_error)$fatal(1,"tracking invalid update");cu=cu+1;total_updates=total_updates+1;
   delay=48'(latest[47:0]-tp[79:32]);if(delay>max_update_delay)max_update_delay=delay;
  end
  if(dv)begin
   if(diag_held && diag!==held)$fatal(1,"tracking held diagnostic changed");held=diag;diag_held=1;
  end
 end
end
initial begin
 $readmemh("words.hex",words);$readmemh("metrics.hex",expected);@INIT@
 for(j=0;j<9;j=j+1)begin
  rst=1;iv=0;av=0;cancel=0;ci=0;cs=0;cp=0;cu=0;fence_pending=0;fence_sent=0;epoch=epochs[j];repeat(5)@(negedge clk);rst=0;
  fork
   begin
    for(k=0;k<lengths[j];k=k+1)begin
     start=k==0;
     if(j==8&&fence_pending&&!fence_sent)begin start=1;epoch=epoch+1'b1;fence_sent=1;end
     tag={epoch,bases[j]+48'(k)};data=(j==7 || (j==6&&k>=damage_first[j]&&k<damage_last[j]))?80'd0:words[offsets[j]+k];iv=1;
     @(negedge clk);iv=0;start=0;repeat(j%2==0?7:11+k%2)@(negedge clk);
    end
   end
   begin
    while(!ar)@(negedge clk);first={epoch,bases[j]+48'(firsts[j])};id=900+j;gen=hgen;av=1;
    @(posedge clk);while(!ar)@(posedge clk);@(negedge clk);av=0;first=~first;id=~id;gen=~gen;
    repeat(50)@(negedge clk);if(wstarted!=1)$fatal(1,"tracking first window not issued");
   end
  join
  repeat(10)@(negedge clk);
  if(ci!=configs[j] || cp!=computed[j] || cs!=sfo_counts[j] || cu!=updates[j] || wstarted!=configs[j] || missed!=misses[j])$fatal(1,"tracking case coverage case%0d cfg%0d peak%0d sfo%0d updates%0d misses%0d",j,ci,cp,cs,cu,missed);
  if(j<7)begin if(!active||!locked||request||!dv||replaced!=sfo_counts[j]-2)$fatal(1,"tracking healthy final state");end
  else if(j==7)begin if(active||locked||!request||reason!=2||dv)$fatal(1,"tracking bounded miss recovery request");end
  else begin if(!fence_sent||active||locked||!request||reason!=1||dv||hgen!=2||latest[63:48]!=0)$fatal(1,"tracking source fence");end
  $display("G0_CAUSAL_TRACKING_CASE_PASS case=%0d configs=%0d peaks=%0d SFO=%0d updates=%0d misses=%0d",j,ci,cp,cs,cu,missed);
  cancel=1;#1;if(tu||dv||ir||ar)$fatal(1,"tracking cancel edge");repeat(2)@(negedge clk);cancel=0;
  repeat(5)begin @(negedge clk);if(active||locked||dv||request||tu)$fatal(1,"tracking cancelled state revived");end
 end
 if(total_inputs!=3698000||total_configs!=53||total_peaks!=46||total_metrics!=2990||total_sfo!=36||total_updates!=28||fences!=1||max_age>=4096)$fatal(1,"tracking global coverage cfg%0d peaks%0d metrics%0d sfo%0d updates%0d inputs%0d fences%0d",total_configs,total_peaks,total_metrics,total_sfo,total_updates,total_inputs,fences);
 $display("G0_CAUSAL_TRACKING_TIMING_PASS configs=%0d peaks=%0d metrics=%0d SFO_results=%0d timing_updates=%0d RRC_inputs=%0d fences=%0d maximum_read_age=%0d maximum_update_delay_samples=%0d",total_configs,total_peaks,total_metrics,total_sfo,total_updates,total_inputs,fences,max_age,max_update_delay);$finish;
end
initial begin #1500000000;$fatal(1,"tracking timeout");end
endmodule
'''

def main(a):
 out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);bp=a.vivado_bin.resolve();project=Path(__file__).resolve().parents[1]
 before=disk_check('before-causal-tracking-timing');(out/'disk-before.json').write_text(json.dumps(before,indent=2)+'\n')
 if before['disk']['available_bytes']<5*GIB:raise SystemExit('Under5GiB: handle storage first.')
 try:
  n,initial,fixtures,cases,nw,nm=prepare(out,a.frontend_reference.resolve());(out/'tb.sv').write_text(TB.replace('@LAST@',str(n-1)).replace('@INIT@',initial).replace('@MLAST@',str(nm-1)).replace('@WLAST@',str(nw-1)))
  sources=[]
  for name in SOURCES:
   target=out/name;shutil.copyfile(project/'sim/g0'/name,target);sources.append(target)
  def run(step,args):
   with (out/(step+'.log')).open('w') as f:subprocess.run(args,cwd=out,stdout=f,stderr=subprocess.STDOUT,check=True)
  xpm=bp.parent/'data/ip/xpm'
  run('compile',[str(bp/'xvlog'),'--sv',str(xpm/'xpm_cdc/hdl/xpm_cdc.sv'),str(xpm/'xpm_memory/hdl/xpm_memory.sv'),*map(str,sources),str(out/'tb.sv')]);run('elaborate',[str(bp/'xelab'),'tb','-s','tracking_timing_tb','-mt','off']);run('simulate',[str(bp/'xsim'),'tracking_timing_tb','-runall'])
  log=(out/'simulate.log').read_text();assert 'G0_CAUSAL_TRACKING_TIMING_PASS' in log and 'Fatal:' not in log,log[-3000:]
  receipt={'scope':__doc__,'simulation':next(s for s in log.splitlines() if s.startswith('G0_CAUSAL_TRACKING_TIMING_PASS')),'fixtures':fixtures,'cases':cases,'source_sha256':{s.name:hashlib.sha256(s.read_bytes()).hexdigest() for s in sources},'routed_OOC':None,'g0_passed':False}
  (out/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
  if not a.sim_only:
   tcl='create_project -in_memory -part xc7z020clg484-1\nset_property XPM_LIBRARIES {XPM_MEMORY XPM_CDC} [current_project]\nset_param general.maxThreads 4\n'+''.join('read_verilog -sv {'+str(s)+'}\n' for s in sources)+'''synth_design -top causal_tracking_timing_cost_probe -part xc7z020clg484-1 -mode out_of_context
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
write_checkpoint -force causal_tracking_timing.dcp
'''
   (out/'ooc.tcl').write_text(tcl);run('ooc',[str(bp/'vivado'),'-mode','batch','-source',str(out/'ooc.tcl'),'-nolog','-nojournal']);receipt['routed_OOC']=routed_measurements(out)
  (out/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n');print(json.dumps({k:v for k,v in receipt.items() if k not in ('fixtures','cases','source_sha256')}),flush=True)
 finally:(out/'disk-after.json').write_text(json.dumps(disk_check('after-causal-tracking-timing'),indent=2)+'\n')

if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);p.add_argument('--frontend-reference',type=Path,required=True);p.add_argument('--vivado-bin',type=Path,required=True);p.add_argument('--sim-only',action='store_true');main(p.parse_args())
