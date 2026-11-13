#!/usr/bin/env python3
"""Real calibrated-capture input -> RTL coarse -> crop/schedule -> same-NCO commit.
Calibration/notch remain software; no post-NCO floating DC blocker is inserted.
No complete production PHY/video acceptance claim.
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

def pack(values,w):return sum((int(x)&((1<<w)-1))<<(j*w) for j,x in enumerate(values))
def prepare(out,directory,*,phase_origin_at_slice=False):
 public=public_prefix();_,signs=training_signs(public,0);taps=np.rint(rrc()*131072).astype(np.int64)
 sine=[round(65536*math.sin(2*math.pi*k/4096)) for k in range(4096)];cos=[round(65536*math.cos(2*math.pi*k/4096)) for k in range(4096)]
 (out/'sine.hex').write_text(''.join(f'{x:05x}\n' for x in sine[:1024]));(out/'rrc.hex').write_text(''.join(f'{x&0x3ffff:05x}\n' for x in taps[:21]))
 allsign=[]
 for tx in (0,1):allsign.extend(int(v.real<0)+2*int(v.imag<0) for v in public[[384,896][tx]:[384,896][tx]+512,tx])
 (out/'training_signs.hex').write_text(''.join(f'{x:x}\n' for x in allsign))
 adcrows=[];ncorows=[];rrcrows=[];trainrows=[];init=[];reports=[]
 for index,path in enumerate(sorted(directory.glob('packet-air-g12-long-r*.npz'))):
  meta=json.loads(path.with_suffix('.json').read_text());old=np.load(path);origin=int(meta['source']['capture_slice'][0]);peak=meta['integer_candidates'][0]['selected_start_index'];end=peak+2960+128
  rawpath=Path(meta['source']['capture']);raw=np.memmap(rawpath,dtype=np.dtype([('tag','<u8'),('iq','<i2',(4,))]),mode='r')
  assert f'{zlib.crc32(raw):08x}'==meta['source']['crc32']
  iq=np.asarray(raw['iq'][:end],dtype=np.float64);y,calibration=calibrated_input(iq[:,0::2]+1j*iq[:,1::2],PERIOD);adc,_=quantize_adc(y[origin:])
  assert np.array_equal(adc[:len(old['adc'])],old['adc']), 'extended capture changed calibrated prefix'
  for sense in (0,1):
   j=2*index+sense;values=adc.astype(np.int64).copy();values[:,1::2]*=(-1 if sense else 1);assert np.max(abs(values))<=32767
   c,e,n=direct_scores(values);candidate=candidates(c,e,n,origin=origin,arm_start=origin+4)[0]
   first=candidate['first_hit_index'];k=candidate['selected_start_index'];known=first+1152
   first_symbol=max(416,257+(known-k+62)//4);count=864-first_symbol;commit=k+2960
   assert count>=128 and candidate['sense']==sense
   epoch=30+j;base=(1<<48)-71000;phase_origin=base if phase_origin_at_slice else (base-origin)&((1<<48)-1)
   def tag(sample):return (epoch<<48)|((base+sample-origin)&((1<<48)-1))
   cfg=coarse_nco_configuration(candidate['phase']['phase_u32'],sense,tag(known)&((1<<48)-1),phase_origin)
   coarse_mixed=np.zeros((len(values),4),dtype=np.int64)
   phase=cfg['phase_u32']
   for q in range(known-origin,len(values)):
    row=values[q].copy();row[1::2]*=(-1 if sense else 1);b=((phase+0x80000)&0xffffffff)>>20;co,si=cos[b],sine[b]
    for rx in (0,1):
     i,v=map(int,row[2*rx:2*rx+2]);coarse_mixed[q,2*rx:2*rx+2]=[(i*co-v*si+16384)//32768,(i*si+v*co+16384)//32768]
    phase=(phase+cfg['step_u32'])&0xffffffff
   filtered=np.column_stack([(np.convolve(coarse_mixed[:,l],taps,'full')[:len(values)]+65536)//131072 for l in range(4)])
   endpoints=np.array([k+20+4*(m-256)-origin for m in range(first_symbol,864)])
   train=2*filtered[endpoints];startup=estimate_startup(train,signs[first_symbol-416:],first_symbol,cfg['step_u32'],tag(commit)&((1<<48)-1),phase_origin)
   assert startup['accepted'], (path.name,sense,startup['error'],startup.get('quality_float_diagnostic'))
   final=startup['final_nco_config'];mixed=coarse_mixed.copy();phase=final['phase_u32']
   for q in range(commit-origin,len(values)):
    row=values[q].copy();row[1::2]*=(-1 if sense else 1);b=((phase+0x80000)&0xffffffff)>>20;co,si=cos[b],sine[b]
    for rx in (0,1):
     i,v=map(int,row[2*rx:2*rx+2]);mixed[q,2*rx:2*rx+2]=[(i*co-v*si+16384)//32768,(i*si+v*co+16384)//32768]
    phase=(phase+final['step_u32'])&0xffffffff
   # Exact RRC restart at final commit, rather than convolution across it.
   filtered_final=np.column_stack([(np.convolve(mixed[commit-origin:,l],taps,'full')[:len(values)-(commit-origin)]+65536)//131072 for l in range(4)])
   filtered[commit-origin:]=filtered_final
   offset=len(adcrows);toffset=len(trainrows)
   adcrows.extend(pack(row,16) for row in values);ncorows.extend(pack(row,18) for row in mixed);rrcrows.extend(pack(row,20) for row in filtered);trainrows.extend(pack(row,21) for row in train)
   init.append(f"offsets[{j}]={offset};lengths[{j}]={len(values)};train_offsets[{j}]={toffset};counts[{j}]={count};symbols[{j}]={first_symbol};bases[{j}]=64'h{tag(origin):016x};origins[{j}]=48'h{phase_origin:012x};firsts[{j}]=64'h{tag(first):016x};peaks[{j}]=64'h{tag(k):016x};knowns[{j}]=64'h{tag(known):016x};commits[{j}]=64'h{tag(commit):016x};coarse_steps[{j}]=32'h{cfg['step_u32']:08x};coarse_phases[{j}]=32'h{cfg['phase_u32']:08x};final_steps[{j}]=32'h{final['step_u32']:08x};final_phases[{j}]=32'h{final['phase_u32']:08x};")
   reports.append(dict(capture=str(rawpath),capture_crc32=meta['source']['crc32'],prefix_byte_identical=True,source_slice=[origin,end],sense=sense,coarse=candidate,first_symbol=first_symbol,count=count,coarse_configuration=cfg,final_configuration=final,quality=startup['quality_float_diagnostic'],num=startup['num_s48'],D=startup['D_u16']))
 for name,rows,width in [('adc.hex',adcrows,16),('mixed.hex',ncorows,18),('filtered.hex',rrcrows,20),('training.hex',trainrows,21)]: (out/name).write_text(''.join(f'{x:0{width}x}\n' for x in rows))
 assert len(reports)==6
 return len(adcrows),len(trainrows),'\n'.join(init),reports

TB=r'''`timescale 1ns/1ps
module tb;
reg clk=0;always #10 clk=~clk;
reg rst=1,cancel=0,rearm=0,rv=0,sv=0,ready=0;reg [63:0] rt=0,rd=0,search_tag=0;reg [31:0] sid=0;reg [47:0] origin=0;
wire sr,ov,locked,running,normal_ready;wire [31:0] oid,ogen,step,phase,faults;wire [2:0] error,detail,reason;wire [9:0] first_symbol;wire [8:0] count;wire [63:0] known,commit;wire [7:0] peak;
acquiring_continuous_frontend_cost_probe dut(.clk(clk),.rst(rst),.cancel(cancel),.rearm(rearm),.raw_valid(rv),.raw_tag(rt),.raw_data(rd),.upstream_loss(1'b0),
.search_valid(sv),.search_ready(sr),.search_id(sid),.search_tag(search_tag),.phase_origin(origin),
.result_valid(ov),.result_ready(ready),.result_id(oid),.result_generation(ogen),.result_error(error),.result_detail(detail),.result_step(step),.result_phase(phase),
.first_symbol(first_symbol),.training_count(count),.coarse_known(known),.final_commit(commit),.locked(locked),.ingress_running(running),.ingress_faults(faults),.ingress_reason(reason),.ingress_peak(peak),
.normal_job_valid(1'b0),.normal_job_ready(normal_ready),.normal_job_id(32'd0),.normal_job_context(32'd0),.normal_job_generation(32'd0),.normal_job_epoch(16'd0),.normal_job_position(80'd0),.normal_job_step(35'd17179869184),.normal_ready(1'b1));
reg [63:0] adc[0:@LAST@];reg [71:0] mixed_gold[0:@LAST@];reg [79:0] filtered_gold[0:@LAST@];reg [83:0] train_gold[0:@TRAINLAST@];
integer offsets[0:5],lengths[0:5],train_offsets[0:5],counts[0:5],symbols[0:5];reg [63:0] bases[0:5],firsts[0:5],peaks[0:5],knowns[0:5],commits[0:5];reg [47:0] origins[0:5];reg [31:0] coarse_steps[0:5],coarse_phases[0:5],final_steps[0:5],final_phases[0:5];
integer j,k,slot,endpoint,received=0,filtered=0,training=0,candidates=0,configs=0,success=0,raw_inputs=0,max_occupancy=0,cycle=0,min_lead=10000,lead;reg seen_job=0;integer expected_generation;
reg [31:0] held_step,held_phase;
always @(posedge clk) begin
 cycle=cycle+1;
 if(!rst && !dut.ingress_cancel) begin
  if(rv) raw_inputs=raw_inputs+1;
  if(dut.cfg_applied) begin
   configs=configs+1;
   if(!dut.core.automatic_config) begin
    if(dut.buffer_tag!==knowns[j] || dut.cfg_tag!==knowns[j] || dut.cfg_step!==coarse_steps[j] || dut.cfg_phase!==coarse_phases[j]) $fatal(1,"coarse application");
   end else if(dut.buffer_tag!==commits[j] || dut.core.effective_phase!==final_phases[j] || dut.core.effective_step!==final_steps[j]) $fatal(1,"final application");
  end
  if(dut.coarse_valid && dut.coarse_accept) begin
   if(dut.candidate_error || dut.candidate_first!==firsts[j] || dut.candidate_peak!==peaks[j] || dut.candidate_known!==knowns[j] || dut.candidate_sense!==1'(j%2)) $fatal(1,"coarse candidate");candidates=candidates+1;
  end
  if(dut.job_valid && dut.job_ready) begin
   if(first_symbol!=symbols[j] || count!=counts[j] || known!==knowns[j] || commit!==commits[j]) $fatal(1,"schedule crop");seen_job=1;
  end
  if(dut.core.state==5 && seen_job) begin
   lead=48'(commits[j][47:0]-rt[47:0]);if(lead<min_lead) min_lead=lead;seen_job=0;
   if(lead<=0 || lead>512) $fatal(1,"startup deadline");
  end
  if(dut.core.frontend.mixed_valid && dut.core.frontend.mixed_ready) begin
   slot=48'(dut.core.frontend.mixed_tag[47:0]-bases[j][47:0]);
   if(slot>=lengths[j] || dut.core.frontend.mixed_data!==mixed_gold[offsets[j]+slot]) $fatal(1,"mixed data j%0d slot%0d",j,slot);received=received+1;
  end
  if(dut.core.frontend.filtered_valid && dut.core.frontend.filtered_ready) begin
   endpoint=48'(dut.core.frontend.filtered_tag[47:0]-bases[j][47:0]+48'd20);
   if(endpoint>=lengths[j] || dut.core.frontend.filtered_data!==filtered_gold[offsets[j]+endpoint]) $fatal(1,"filtered data j%0d endpoint%0d",j,endpoint);filtered=filtered+1;
  end
  if(dut.core.train_input_valid && dut.core.train_ready) begin
   if(dut.core.sample_data!==train_gold[train_offsets[j]+dut.core.received]) $fatal(1,"startup train %0d/%0d",j,dut.core.received);training=training+1;
  end
 end
end
initial begin
$readmemh("adc.hex",adc);$readmemh("mixed.hex",mixed_gold);$readmemh("filtered.hex",filtered_gold);$readmemh("training.hex",train_gold);
@INIT@
for(j=0;j<6;j=j+1) begin
 rst=1;repeat(5) @(negedge clk);rst=0;rearm=1;@(negedge clk);rearm=0;while(!running) @(negedge clk);
 sid=300+j;search_tag={bases[j][63:48],bases[j][47:0]+48'd4};origin=origins[j];sv=1;
 @(posedge clk);while(!sr) @(posedge clk);expected_generation=dut.coarse_generation+(dut.buffer_start?1:0);@(negedge clk);sv=0;sid=~sid;search_tag=0;origin=0;ready=0;seen_job=0;
 fork
  begin
   for(k=0;k<lengths[j];k=k+1) begin
    rt={bases[j][63:48],bases[j][47:0]+48'(k)};rd=adc[offsets[j]+k];rv=1;
    @(negedge clk);rv=0;repeat(11+k%2) @(negedge clk);
   end
  end
  begin
   while(!ov) @(negedge clk);
   if(error || detail || oid!=300+j || ogen!=expected_generation || step!==final_steps[j] || phase!==final_phases[j]) $fatal(1,"startup result j%0d err%0d:%0d step%h/%h phase%h/%h",j,error,detail,step,final_steps[j],phase,final_phases[j]);
   held_step=step;held_phase=phase;
   repeat(600) begin @(posedge clk);if(!ov || step!==held_step || phase!==held_phase) $fatal(1,"held status");end
   // Diagnostic backpressure must not block a locked normal-training path.
   if(!locked || !normal_ready) $fatal(1,"diagnostic ACK dependency");
   @(negedge clk);ready=1;@(negedge clk);ready=0;success=success+1;
  end
 join
 repeat(50) @(negedge clk);
 if(faults) $fatal(1,"ingress loss");if(peak>max_occupancy) max_occupancy=peak;
end
if(success!=6 || candidates!=6 || configs!=12 || training!=2400) $fatal(1,"coverage %0d/%0d/%0d/%0d",success,candidates,configs,training);
$display("G0_ACQUIRING_FRONTEND_PASS successful_starts=%0d coarse_candidates=%0d configurations=%0d raw_inputs=%0d nco_outputs=%0d rrc_outputs=%0d training_vectors=%0d minimum_ready_lead_samples=%0d maximum_fifo_words=%0d",success,candidates,configs,raw_inputs,received,filtered,training,min_lead,max_occupancy);$finish;
end
initial begin #200000000;$fatal(1,"timeout");end
endmodule
'''

def main(a):
 out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);project=Path(__file__).resolve().parents[1];n,nt,init,reports=prepare(out,a.fixture_directory.resolve())
 tb=TB.replace('@LAST@',str(n-1)).replace('@TRAINLAST@',str(nt-1)).replace('@INIT@',init);(out/'tb.sv').write_text(tb)
 names=['diagnostic_latest_mailbox_cost_probe.sv','ingress_recovery_control_cost_probe.sv','compact_stream_fifo_cost_probe.sv','rf_rx_elastic_fifo.v','continuous_adc_ingress_cost_probe.sv','differential_input_cost_probe.sv','differential_corr_cost_probe.sv','differential_frontend_cost_probe.sv','differential_score_cost_probe.sv','differential_acquisition_cost_probe.sv','phase_angle_cost_probe.sv','fine_nco_config_cost_probe.sv','nco_cost_probe.sv','rrc_cost_probe.sv','timing_request_cost_probe.sv','interpolation_cost_probe.sv','frontend_chain_cost_probe.sv','training_channel_cost_probe.sv','training_frontend_cost_probe.sv','coarse_startup_schedule_cost_probe.sv','acquiring_continuous_frontend_cost_probe.sv'];sources=[]
 for name in names:
  p=out/name;shutil.copyfile(project/'sim/g0'/name,p);sources.append(p)
 bp=a.vivado_bin.resolve();xpm=bp.parent/'data/ip/xpm';commands=[]
 def run(name,args):
  commands.append(args);(out/'commands.json').write_text(json.dumps(commands,indent=2)+'\n')
  with (out/(name+'.log')).open('w') as f:subprocess.run(args,cwd=out,stdout=f,stderr=subprocess.STDOUT,check=True)
 run('compile',[str(bp/'xvlog'),'--sv',str(xpm/'xpm_cdc/hdl/xpm_cdc.sv'),str(xpm/'xpm_memory/hdl/xpm_memory.sv'),*map(str,sources),str(out/'tb.sv')]);run('elaborate',[str(bp/'xelab'),'tb','-s','acquiring_tb','-mt','off']);run('simulate',[str(bp/'xsim'),'acquiring_tb','-runall'])
 log=(out/'simulate.log').read_text();assert 'G0_ACQUIRING_FRONTEND_PASS' in log and 'Fatal:' not in log,log[-2000:]
 receipt={'scope':__doc__,'simulation':log[log.index('G0_ACQUIRING_FRONTEND_PASS'):].splitlines()[0],'fixtures':reports,'routed_OOC':None,'source_sha256':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sources},'g0_passed':False}
 (out/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
 if not a.sim_only:
  tcl='create_project -in_memory -part xc7z020clg484-1\nset_property XPM_LIBRARIES {XPM_MEMORY XPM_CDC} [current_project]\nset_param general.maxThreads 4\n'+''.join('read_verilog -sv {'+str(p)+'}\n' for p in sources)+'''synth_design -top acquiring_continuous_frontend_cost_probe -part xc7z020clg484-1 -mode out_of_context
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
write_checkpoint -force acquiring.dcp
'''
  (out/'ooc.tcl').write_text(tcl);run('ooc',[str(bp/'vivado'),'-mode','batch','-source',str(out/'ooc.tcl'),'-nolog','-nojournal']);receipt['routed_OOC']=routed_measurements(out);(out/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
 print(json.dumps({'simulation':receipt['simulation'],'routed_OOC':receipt['routed_OOC']}))
if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);p.add_argument('--fixture-directory',type=Path,required=True);p.add_argument('--vivado-bin',type=Path,required=True);p.add_argument('--sim-only',action='store_true');main(p.parse_args())
