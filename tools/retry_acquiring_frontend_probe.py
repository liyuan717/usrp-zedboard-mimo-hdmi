#!/usr/bin/env python3
"""Continuous calibrated captures with first training erased: RTL rejects then
searches current input and locks next real preamble without a second host arm.
Diagnostic READY stays low throughout retry and success. Not RF/video signoff.
"""
import argparse,json,math,shutil,subprocess,hashlib,zlib
from pathlib import Path
import numpy as np
from g0_causal_acquisition import calibrated_input
from g0_sounder import rrc
from g0_fixed_differential_model import quantize_adc,direct_scores,candidates,coarse_nco_configuration
from g0_fixed_startup_model import estimate_startup
from g0_fixed_training_model import training_signs
from g0_packet_probe import PERIOD,public_prefix
from fec_inverse_cost_probe import routed_measurements
from acquiring_frontend_probe import pack

def prepare(out,directory):
 public=public_prefix();_,signs=training_signs(public,0);taps=np.rint(rrc()*131072).astype(np.int64)
 sin=[round(65536*math.sin(2*math.pi*k/4096)) for k in range(4096)];cos=[round(65536*math.cos(2*math.pi*k/4096)) for k in range(4096)]
 (out/'sine.hex').write_text(''.join(f'{v:05x}\n' for v in sin[:1024]));(out/'rrc.hex').write_text(''.join(f'{v&0x3ffff:05x}\n' for v in taps[:21]))
 allsign=[]
 for tx in (0,1):allsign.extend(int(v.real<0)+2*int(v.imag<0) for v in public[[384,896][tx]:[384,896][tx]+512,tx])
 (out/'training_signs.hex').write_text(''.join(f'{v:x}\n' for v in allsign))
 adcrows=[];mixedrows=[];filteredrows=[];trainrows=[];init=[];reports=[];total_training=0
 for f,path in enumerate(sorted(directory.glob('packet-air-g12-long-r*.npz'))):
  meta=json.loads(path.with_suffix('.json').read_text());old=np.load(path);origin=int(meta['source']['capture_slice'][0]);firstold=meta['integer_candidates'][0];stop=firstold['selected_start_index']+PERIOD*4+2960+160
  rawpath=Path(meta['source']['capture']);raw=np.memmap(rawpath,dtype=np.dtype([('tag','<u8'),('iq','<i2',(4,))]),mode='r');assert f'{zlib.crc32(raw):08x}'==meta['source']['crc32']
  iq=np.asarray(raw['iq'][:stop],dtype=np.float64);y,calibration=calibrated_input(iq[:,0::2]+1j*iq[:,1::2],PERIOD);adc,_=quantize_adc(y[origin:]);assert np.array_equal(adc[:len(old['adc'])],old['adc'])
  erased_start=firstold['first_hit_index']+1152;erased_end=firstold['selected_start_index']+2960
  adc[erased_start-origin:erased_end-origin]=0
  for sense in (0,1):
   j=2*f+sense;values=adc.astype(np.int64).copy();values[:,1::2]*=(-1 if sense else 1)
   c,e,n=direct_scores(values);found=candidates(c,e,n,origin=origin,arm_start=origin+4)
   first=found[0];second=next(x for x in found[1:] if x['first_hit_index']>=erased_end)
   assert first['selected_start_index']==firstold['selected_start_index'] and first['sense']==second['sense']==sense
   assert not any(x['first_hit_index']>=first['selected_start_index']+2400 and x['first_hit_index']<erased_end for x in found[1:])
   epoch=40+j;base=(1<<48)-71000;phase_origin=(base-origin)&((1<<48)-1)
   def tag(sample):return (epoch<<48)|((base+sample-origin)&((1<<48)-1))
   coarse_cfg=[];start_syms=[];counts=[]
   for candidate in (first,second):
    peak=candidate['selected_start_index'];known=candidate['first_hit_index']+1152
    coarse_cfg.append(coarse_nco_configuration(candidate['phase']['phase_u32'],sense,tag(known)&((1<<48)-1),phase_origin));symbol=max(416,257+(known-peak+62)//4);start_syms.append(symbol);counts.append(864-symbol)
   peak=second['selected_start_index'];known=second['first_hit_index']+1152;commit=peak+2960;cfg=coarse_cfg[1]
   mixed=np.zeros((len(values),4),dtype=np.int64);phase=cfg['phase_u32']
   def rotate(q,phase):
    row=values[q].copy();row[1::2]*=(-1 if sense else 1);b=((phase+0x80000)&0xffffffff)>>20;co,si=cos[b],sin[b];v=[]
    for rx in (0,1):
     i,t=map(int,row[2*rx:2*rx+2]);v.extend(((i*co-t*si+16384)//32768,(i*si+t*co+16384)//32768))
    return v
   for q in range(known-origin,len(values)):mixed[q]=rotate(q,phase);phase=(phase+cfg['step_u32'])&0xffffffff
   filtered=np.column_stack([(np.convolve(mixed[:,l],taps,'full')[:len(values)]+65536)//131072 for l in range(4)])
   endpoints=np.array([peak+20+4*(m-256)-origin for m in range(start_syms[1],864)]);train=2*filtered[endpoints]
   startup=estimate_startup(train,signs[start_syms[1]-416:],start_syms[1],cfg['step_u32'],tag(commit)&((1<<48)-1),phase_origin);assert startup['accepted'],startup['error'];final=startup['final_nco_config'];phase=final['phase_u32']
   for q in range(commit-origin,len(values)):mixed[q]=rotate(q,phase);phase=(phase+final['step_u32'])&0xffffffff
   filtered[commit-origin:]=np.column_stack([(np.convolve(mixed[commit-origin:,l],taps,'full')[:len(values)-(commit-origin)]+65536)//131072 for l in range(4)])
   offset=len(adcrows);toffset=len(trainrows);trainrows.extend([0]*counts[0]);trainrows.extend(pack(v,21) for v in train);total_training+=sum(counts)
   adcrows.extend(pack(v,16) for v in values);mixedrows.extend(pack(v,18) for v in mixed);filteredrows.extend(pack(v,20) for v in filtered)
   line=f"offsets[{j}]={offset};lengths[{j}]={len(values)};train_offsets[{j}]={toffset};bases[{j}]=64'h{tag(origin):016x};origins[{j}]=48'h{phase_origin:012x};final_steps[{j}]=32'h{final['step_u32']:08x};final_phases[{j}]=32'h{final['phase_u32']:08x};commits[{j}]=64'h{tag(commit):016x};"
   for attempt,candidate in enumerate((first,second)):
    index=2*j+attempt;cc=coarse_cfg[attempt]
    line+=f"firsts[{index}]=64'h{tag(candidate['first_hit_index']):016x};peaks[{index}]=64'h{tag(candidate['selected_start_index']):016x};knowns[{index}]=64'h{tag(candidate['first_hit_index']+1152):016x};coarse_steps[{index}]=32'h{cc['step_u32']:08x};coarse_phases[{index}]=32'h{cc['phase_u32']:08x};counts[{index}]={counts[attempt]};symbols[{index}]={start_syms[attempt]};"
   init.append(line);reports.append(dict(capture=str(rawpath),capture_crc32=meta['source']['crc32'],software_calibration_prefix_unchanged_before_erasure=True,source_slice=[origin,stop],erased_ADC_samples=[erased_start,erased_end],sense=sense,candidates=[first,second],counts=counts,final=final,quality=startup['quality_float_diagnostic']))
 for name,rows,width in [('adc.hex',adcrows,16),('mixed.hex',mixedrows,18),('filtered.hex',filteredrows,20),('training.hex',trainrows,21)]: (out/name).write_text(''.join(f'{v:0{width}x}\n' for v in rows))
 assert len(reports)==6
 return len(adcrows),len(trainrows),total_training,'\n'.join(init),reports

TB=r'''`timescale 1ns/1ps
module tb;
reg clk=0;always #10 clk=~clk;
reg rst=1,cancel=0,rearm=0,rv=0,sv=0,ready=0;reg [63:0] rt=0,rd=0,search_tag=0;reg [31:0] sid=0;reg [47:0] origin=0;
wire sr,ov,locked,running,normal_ready;wire [31:0] oid,ogen,step,phase,faults,retries,drops;wire [63:0] current_search;wire [2:0] error,detail,reason;wire [9:0] first_symbol;wire [8:0] count;wire [63:0] known,commit;wire [7:0] peak;
acquiring_continuous_frontend_cost_probe #(.AUTO_RETRY(1)) dut(.clk(clk),.rst(rst),.cancel(cancel),.rearm(rearm),.raw_valid(rv),.raw_tag(rt),.raw_data(rd),.upstream_loss(1'b0),
.search_valid(sv),.search_ready(sr),.search_id(sid),.search_tag(search_tag),.phase_origin(origin),
.result_valid(ov),.result_ready(ready),.result_id(oid),.result_generation(ogen),.result_error(error),.result_detail(detail),.result_step(step),.result_phase(phase),
.first_symbol(first_symbol),.training_count(count),.coarse_known(known),.final_commit(commit),.locked(locked),.ingress_running(running),.ingress_faults(faults),.ingress_reason(reason),.ingress_peak(peak),
.retry_count(retries),.diagnostic_replaced(drops),.current_search_tag(current_search),
.normal_job_valid(1'b0),.normal_job_ready(normal_ready),.normal_job_id(32'd0),.normal_job_context(32'd0),.normal_job_generation(32'd0),.normal_job_epoch(16'd0),.normal_job_position(80'd0),.normal_job_step(35'd17179869184),.normal_ready(1'b1));
reg [63:0] adc[0:@LAST@];reg [71:0] mixed_gold[0:@LAST@];reg [79:0] filtered_gold[0:@LAST@];reg [83:0] train_gold[0:@TRAINLAST@];
integer offsets[0:5],lengths[0:5],train_offsets[0:5];integer counts[0:11],symbols[0:11];reg [63:0] bases[0:5],origins_unused[0:5],commits[0:5];reg [47:0] origins[0:5];reg [63:0] firsts[0:11],peaks[0:11],knowns[0:11];reg [31:0] coarse_steps[0:11],coarse_phases[0:11],final_steps[0:5],final_phases[0:5];
integer j,k,attempt=0,index,slot,endpoint,training=0,received=0,filtered=0,coarse_events=0,configs=0,success=0,raw_inputs=0,auto_arms=0,status_events=0,expected_generation;reg [63:0] latest_raw=0,retry_expect=0;reg [133:0] held_view;reg head_held=0;
wire [133:0] view={oid,ogen,error,detail,step,phase};
always @(posedge clk) begin
 if(rst) begin latest_raw<=0;head_held=0;end
 else if(!dut.ingress_cancel) begin
  if(rv) begin raw_inputs=raw_inputs+1;latest_raw<=rt;end
  if(head_held && (!ov || view!==held_view)) $fatal(1,"held diagnostic changed during retry");
  head_held=ov&&!ready;if(head_held) held_view=view;
  if(dut.status_valid && dut.status_ready) begin
   status_events=status_events+1;
   if(dut.status_error!=0) begin
    if(dut.status_error!=4 || dut.status_detail!=2 || dut.status_id!=300+j) $fatal(1,"first rejection");
    retry_expect=rv?{rt[63:48],rt[47:0]+48'd1}:{latest_raw[63:48],latest_raw[47:0]+48'd1};
   end else if(dut.status_id!=301+j) $fatal(1,"retry identity");
  end
  if(dut.automatic_search_fire) begin
   if(dut.effective_search_tag!==retry_expect || dut.effective_search_id!=301+j || sv) $fatal(1,"retry replayed old samples");auto_arms=auto_arms+1;
  end
  if(dut.coarse_valid && dut.coarse_accept) begin
   index=2*j+attempt;
   if(attempt>=2 || dut.candidate_error || dut.candidate_first!==firsts[index] || dut.candidate_peak!==peaks[index] || dut.candidate_known!==knowns[index] || dut.candidate_sense!==1'(j%2)) $fatal(1,"retry coarse candidate j%0d attempt%0d",j,attempt);
   coarse_events=coarse_events+1;attempt=attempt+1;
  end
  if(dut.cfg_applied) begin
   configs=configs+1;
   if(!dut.core.automatic_config) begin
    index=2*j+attempt-1;
    if(dut.cfg_tag!==knowns[index] || dut.cfg_step!==coarse_steps[index] || dut.cfg_phase!==coarse_phases[index]) $fatal(1,"retry coarse commit");
   end else if(dut.buffer_tag!==commits[j] || dut.core.effective_phase!==final_phases[j] || dut.core.effective_step!==final_steps[j]) $fatal(1,"retry final commit");
  end
  if(dut.job_valid && dut.job_ready) begin index=2*j+attempt-1;if(count!=counts[index] || first_symbol!=symbols[index]) $fatal(1,"retry crop");end
  if(dut.core.frontend.mixed_valid && dut.core.frontend.mixed_ready) begin
   slot=48'(dut.core.frontend.mixed_tag[47:0]-bases[j][47:0]);
   if(slot>=lengths[j] || dut.core.frontend.mixed_data!==mixed_gold[offsets[j]+slot]) $fatal(1,"retry mixed j%0d slot%0d",j,slot);received=received+1;
  end
  if(dut.core.frontend.filtered_valid && dut.core.frontend.filtered_ready) begin
   endpoint=48'(dut.core.frontend.filtered_tag[47:0]-bases[j][47:0]+48'd20);
   if(endpoint>=lengths[j] || dut.core.frontend.filtered_data!==filtered_gold[offsets[j]+endpoint]) $fatal(1,"retry RRC");filtered=filtered+1;
  end
  if(dut.core.train_input_valid && dut.core.train_ready) begin
   index=train_offsets[j]+(attempt==2?counts[2*j]:0)+dut.core.received;
   if(dut.core.sample_data!==train_gold[index]) $fatal(1,"retry training");training=training+1;
  end
 end
end
initial begin
$readmemh("adc.hex",adc);$readmemh("mixed.hex",mixed_gold);$readmemh("filtered.hex",filtered_gold);$readmemh("training.hex",train_gold);
@INIT@
for(j=0;j<6;j=j+1) begin
 rst=1;repeat(5) @(negedge clk);rst=0;rearm=1;@(negedge clk);rearm=0;while(!running) @(negedge clk);
 sid=300+j;search_tag={bases[j][63:48],bases[j][47:0]+48'd4};origin=origins[j];sv=1;
 @(posedge clk);while(!sr) @(posedge clk);expected_generation=dut.coarse_generation+(dut.buffer_start?1:0);@(negedge clk);sv=0;sid=~sid;origin=0;search_tag=0;ready=0;attempt=0;
 fork
  begin
   for(k=0;k<lengths[j];k=k+1) begin rt={bases[j][63:48],bases[j][47:0]+48'(k)};rd=adc[offsets[j]+k];rv=1;@(negedge clk);rv=0;repeat(11+k%2) @(negedge clk);end
  end
  begin
   while(!ov) @(negedge clk);
   if(oid!=300+j || error!=4 || detail!=2 || step || phase) $fatal(1,"missing first failure diagnostic");
   // No ACK is sent to the failed event; recovery must run anyway.
   while(!locked) @(negedge clk);
   if(!ov || oid!=300+j || error!=4 || retries!=1 || drops) $fatal(1,"diagnostic stalled retry");
   repeat(600) @(negedge clk);if(!normal_ready) $fatal(1,"diagnostic stalled normal jobs");
   ready=1;@(posedge clk);@(negedge clk);
   if(!ov || oid!=301+j || ogen!=expected_generation || error || detail || step!==final_steps[j] || phase!==final_phases[j]) $fatal(1,"success diagnostic tail");
   @(posedge clk);@(negedge clk);ready=0;success=success+1;
  end
 join
 repeat(50) @(negedge clk);if(faults || retries!=1 || drops || peak>128) $fatal(1,"retry endpoint state");
end
if(success!=6 || auto_arms!=6 || coarse_events!=12 || configs!=18 || status_events!=12 || training!=@TRAIN_TOTAL@) $fatal(1,"retry coverage");
$display("G0_AUTONOMOUS_RETRY_PASS successful_recoveries=%0d automatic_searches=%0d coarse_candidates=%0d configurations=%0d raw_inputs=%0d nco_outputs=%0d rrc_outputs=%0d training_vectors=%0d diagnostic_events=%0d",success,auto_arms,coarse_events,configs,raw_inputs,received,filtered,training,status_events);$finish;
end
initial begin #350000000;$fatal(1,"timeout");end
endmodule
'''
def main(a):
 out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);project=Path(__file__).resolve().parents[1];n,nt,total,init,reports=prepare(out,a.fixture_directory.resolve());tb=TB.replace('@LAST@',str(n-1)).replace('@TRAINLAST@',str(nt-1)).replace('@TRAIN_TOTAL@',str(total)).replace('@INIT@',init);(out/'tb.sv').write_text(tb)
 names=['diagnostic_latest_mailbox_cost_probe.sv','ingress_recovery_control_cost_probe.sv','compact_stream_fifo_cost_probe.sv','rf_rx_elastic_fifo.v','continuous_adc_ingress_cost_probe.sv','differential_input_cost_probe.sv','differential_corr_cost_probe.sv','differential_frontend_cost_probe.sv','differential_score_cost_probe.sv','differential_acquisition_cost_probe.sv','phase_angle_cost_probe.sv','fine_nco_config_cost_probe.sv','nco_cost_probe.sv','rrc_cost_probe.sv','timing_request_cost_probe.sv','interpolation_cost_probe.sv','frontend_chain_cost_probe.sv','training_channel_cost_probe.sv','training_frontend_cost_probe.sv','coarse_startup_schedule_cost_probe.sv','acquiring_continuous_frontend_cost_probe.sv'];sources=[]
 for name in names:
  p=out/name;shutil.copyfile(project/'sim/g0'/name,p);sources.append(p)
 bp=a.vivado_bin.resolve();xpm=bp.parent/'data/ip/xpm';commands=[]
 def run(name,args):
  commands.append(args);(out/'commands.json').write_text(json.dumps(commands,indent=2)+'\n')
  with (out/(name+'.log')).open('w') as log:subprocess.run(args,cwd=out,stdout=log,stderr=subprocess.STDOUT,check=True)
 run('compile',[str(bp/'xvlog'),'--sv',str(xpm/'xpm_cdc/hdl/xpm_cdc.sv'),str(xpm/'xpm_memory/hdl/xpm_memory.sv'),*map(str,sources),str(out/'tb.sv')]);run('elaborate',[str(bp/'xelab'),'tb','-s','retry_tb','-mt','off']);run('simulate',[str(bp/'xsim'),'retry_tb','-runall']);log=(out/'simulate.log').read_text();assert 'G0_AUTONOMOUS_RETRY_PASS' in log and 'Fatal:' not in log,log[-2000:]
 r={'scope':__doc__,'simulation':log[log.index('G0_AUTONOMOUS_RETRY_PASS'):].splitlines()[0],'fixtures':reports,'source_sha256':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sources},'routed_OOC':None,'g0_passed':False};(out/'receipt.json').write_text(json.dumps(r,indent=2)+'\n')
 if not a.sim_only:
  tcl='create_project -in_memory -part xc7z020clg484-1\nset_property XPM_LIBRARIES {XPM_MEMORY XPM_CDC} [current_project]\nset_param general.maxThreads 4\n'+''.join('read_verilog -sv {'+str(p)+'}\n' for p in sources)+'''synth_design -top acquiring_continuous_frontend_cost_probe -part xc7z020clg484-1 -mode out_of_context -generic AUTO_RETRY=1
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
write_checkpoint -force retry.dcp
'''
  (out/'ooc.tcl').write_text(tcl);run('ooc',[str(bp/'vivado'),'-mode','batch','-source',str(out/'ooc.tcl'),'-nolog','-nojournal']);r['routed_OOC']=routed_measurements(out);(out/'receipt.json').write_text(json.dumps(r,indent=2)+'\n')
 print(json.dumps({'simulation':r['simulation'],'routed_OOC':r['routed_OOC']}))
if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);p.add_argument('--fixture-directory',type=Path,required=True);p.add_argument('--vivado-bin',type=Path,required=True);p.add_argument('--sim-only',action='store_true');main(p.parse_args())
