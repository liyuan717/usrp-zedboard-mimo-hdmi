#!/usr/bin/env python3
"""Actual calibrated ADC/acquisition/timing/H -> shared-pool header replay/CRC.
Reuse the complete prior10-episode acquisition oracle, add every header sampler
coordinate/value/full-precision soft statistic, trusted metadata and data deadline.
No header truth feeds DUT; physical IO/calibration/pilot/payload/video pending.
"""
import argparse,hashlib,itertools,json,os,shutil,subprocess
from pathlib import Path
from tracking_acquisition_probe import SOURCES as BASE_SOURCES,TB as BASE_TB,UNIT
from fec_inverse_cost_probe import routed_measurements
from workspace_storage_guard import check as disk_check,GIB
SOURCES=BASE_SOURCES+['bpsk_header_cost_probe.sv','header_replay_cost_probe.sv','header_acquiring_frontend_cost_probe.sv']

EXTRA=r'''
reg hr=0;wire hv;wire [31:0] hi,hctx,hgenout,hsid,hsf;wire [15:0] hep,hl0,hl1;wire [2:0] herr,hdetail;wire [191:0] hraw;
reg [191:0] gold_header[0:34];reg [83:0] header_vectors[0:10751];reg [165:0] header_math[0:10751];
integer header_model=0,header_window=0,header_offset=0,header_ref=0,header_jobs=0,header_results=0,header_consumed=0,header_vector_count=0,header_soft_checks=0,header_held_checks=0,header_wait=0,e_header_results=0,e_header_consumed=0;
integer header_result_cycle[0:49],header_count[0:49],payload_right[0:34],payload_cycle[0:49],header_max_after_first_pilot=0,header_min_data_lead=100000000,header_max_read_age=0,header_age;
reg header_seen=0,header_held=0,header_valid_q=0,header_fence_q=0;reg [405:0] header_view;
wire [405:0] header_observed={hi,hctx,hgenout,hep,herr,hdetail,hraw,hsid,hsf,hl0,hl1};
reg [79:0] header_coordinate;reg [80:0] header_rounded;
always @(negedge clk)begin
 if(rst||cancel||header_fence_q||!header_valid_q)begin hr=0;header_wait=0;end
 else if(header_wait==37)hr=1;else begin hr=0;header_wait=header_wait+1;end
end
always @(posedge clk)begin
 header_valid_q<=hv;header_fence_q<=fence;
 if(rst||cancel||fence)begin header_seen=0;header_held=0;end
 else begin
  if(dut.receiver.reader_conflict)$fatal(1,"live header read ownership conflict");
  if(dut.receiver.rrc_valid&&dut.receiver.pool_ready)begin
   for(w=1;w<5;w=w+1)begin q=model*5+w;idx=episode*5+w;if(48'(dut.receiver.rrc_tag[47:0]-base)==payload_right[q])payload_cycle[idx]=cycle;end
  end
  if(dut.header.cfg_valid&&dut.header.cfg_ready)begin
   header_model=model;header_window=active_window;header_offset=(model*4+active_window-1)*384;
   if(dut.training_position_q-{base,32'd0}!==wtrain[model*5+active_window]||dut.step_q!==wstep[model*5+active_window])$fatal(1,"live header training descriptor lost");
   header_jobs=header_jobs+1;
  end
  if(dut.header.sample_valid&&dut.header.sample_ready)begin
   header_ref=header_offset+dut.header.received;header_coordinate=wpeak[header_model*5+header_window]+80'(1152+dut.header.received)*wstep[header_model*5+header_window];header_rounded={1'b0,header_coordinate}+81'd32768;
   if(dut.header.sample_error||dut.header.sample_bad||dut.header.sample_data!==header_vectors[header_ref]||dut.header.sample_tag!=={epoch,base+header_rounded[79:32]}||dut.header.sample_mu!==header_rounded[31:16])$fatal(1,"live header sampler oracle");
   header_age=48'(latest[47:0]-dut.header.sample_tag[47:0]);if(header_age>header_max_read_age)header_max_read_age=header_age;
   header_vector_count=header_vector_count+1;
  end
  if(dut.header.decoder.state==3&&dut.header.decoder.issued==0)begin
   header_ref=header_offset+dut.header.decoder.index;
   if({dut.header.decoder.zimag,dut.header.decoder.zreal}!==header_math[header_ref][97:0])$fatal(1,"live header full dot oracle");
  end
  if(dut.header.decoder.state==4)begin
   header_ref=header_offset+dut.header.decoder.index;if(dut.header.decoder.soft_value!==header_math[header_ref][165:98])$fatal(1,"live header full soft oracle");header_soft_checks=header_soft_checks+1;
  end
  if(hv&&!header_seen)begin
   if(herr||hdetail||hi!=initial_id+32'(header_window)||hctx!=bound_context||hgenout!=bound_generation||hep!=epoch||hraw!==gold_header[header_model*5+header_window]||hsid!==hraw[63:32]||hsf!==hraw[95:64]||hl0!=4384||hl1!=4384)$fatal(1,"live header decoded metadata oracle");
   idx=episode*5+header_window;header_result_cycle[idx]=cycle;header_count[idx]=header_count[idx]+1;
   header_results=header_results+1;header_seen=1;$display("LIVE_HEADER case=%0d stage=%0d window=%0d session=%h sf=%0d",j,stage,header_window,hsid,hsf);
  end
  if(header_held&&(!hv||header_observed!==header_view))$fatal(1,"live held header changed");
  if(hv&&hr)begin header_consumed=header_consumed+1;header_seen=0;end
  header_held=hv&&!hr;if(header_held)begin header_view=header_observed;header_held_checks=header_held_checks+1;end
 end
end
'''

def tb_text(initial,meta,actual):
 text=BASE_TB.replace('tracking_acquiring_frontend_cost_probe dut(','header_acquiring_frontend_cost_probe dut(').replace('dut.','dut.receiver.')
 old='.startup_locked(startlocked),.retry_count(retries),.recovery_count(recoveries),.recovery_waiting(waiting));'
 new='.startup_locked(startlocked),.retry_count(retries),.recovery_count(recoveries),.recovery_waiting(waiting),.header_valid(hv),.header_ready(hr),.header_id(hi),.header_context(hctx),.header_generation(hgenout),.header_epoch(hep),.header_error(herr),.header_detail(hdetail),.header_raw(hraw),.header_session(hsid),.header_superframe(hsf),.header_stream0_bytes(hl0),.header_stream1_bytes(hl1));'
 assert text.count(old)==1;text=text.replace(old,new)
 declarations,logic=EXTRA.split('always @(negedge clk)',1)
 marker='header_acquiring_frontend_cost_probe dut(';assert text.count(marker)==1;text=text.replace(marker,declarations+'\n'+marker)
 marker='task run_samples';assert text.count(marker)==1;text=text.replace(marker,'always @(negedge clk)'+logic+'\n'+marker)
 marker=' $display("G0_LIVE_TRACKING_EPISODE_PASS';assert text.count(marker)==1
 verify=''' if(header_results-e_header_results!=expected_consumed||header_consumed-e_header_consumed!=expected_consumed)$fatal(1,"live header episode census");
  for(w=1;w<5;w=w+1)begin idx=episode*5+w;if(header_count[idx])begin if(header_count[idx]!=1||payload_cycle[idx]<=header_result_cycle[idx])$fatal(1,"live header/payload deadline");delta=payload_cycle[idx]-header_result_cycle[idx];if(delta<header_min_data_lead)header_min_data_lead=delta;delta=header_result_cycle[idx]-data_cycle[idx];if(delta>header_max_after_first_pilot)header_max_after_first_pilot=delta;end end
'''
 text=text.replace(marker,verify+marker)
 marker='begin e_starts=starts;';assert text.count(marker)==1;text=text.replace(marker,'begin e_header_results=header_results;e_header_consumed=header_consumed;'+marker[len('begin '):])
 init=initial+'\n'+''.join(f"gold_header[{h['model']*5+h['window']}]=192'h{int.from_bytes(bytes.fromhex(h['raw_header']),'little'):048x};\n" for h in actual)
 init+=''.join(f"payload_right[{m*5+w}]={(entry['peak']+1538*entry['step']+32768)//(1<<32)+1};\n" for m,model in enumerate(meta['models']) for w,entry in enumerate(model['windows']))
 init+='\n$readmemh("header_vectors.hex",header_vectors);$readmemh("header_math.hex",header_math);\nfor(idx=0;idx<50;idx=idx+1)begin header_count[idx]=0;header_result_cycle[idx]=-1;payload_cycle[idx]=-1;end\n'
 text=text.replace('@INIT@',init)
 for key,count in [('A',meta['total_ADC_vectors']),('R',meta['total_mixed_filtered_vectors']),('M',meta['metric_vectors']),('S',meta['startup_vectors']),('N',meta['normal_vectors'])]:text=text.replace('@'+key+'LAST@',str(count-1))
 marker=' $display("G0_LIVE_TRACKING_ACQUISITION_PASS';assert text.count(marker)==1
 final=''' if(header_jobs!=36||header_results!=36||header_consumed!=36||header_vector_count!=36*384||header_soft_checks!=36*384||header_min_data_lead<=0||header_max_read_age>=4096)$fatal(1,"live header global coverage");
 $display("G0_LIVE_HEADER_ACQUISITION_PASS jobs=%0d headers=%0d consumed=%0d exact_vectors=%0d full_soft_checks=%0d minimum_header_payload_lead_cycles=%0d maximum_header_after_first_pilot_cycles=%0d maximum_header_read_age=%0d held_checks=%0d",header_jobs,header_results,header_consumed,header_vector_count,header_soft_checks,header_min_data_lead,header_max_after_first_pilot,header_max_read_age,header_held_checks);
'''
 return text.replace(marker,final+marker)

def main(a):
 project=Path(__file__).resolve().parents[1];out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);ref=a.fixture_directory.resolve();headerref=a.header_reference.resolve();bp=a.vivado_bin.resolve()
 before=disk_check('before-live-header-acquisition');(out/'disk-before.json').write_text(json.dumps(before,indent=2)+'\n')
 if before['disk']['available_bytes']<5*GIB:raise SystemExit('Under5GiB: handle storage first.')
 try:
  meta=json.loads((ref/'fixture_metadata.json').read_text());hdr=json.loads((headerref/'receipt.json').read_text());assert hdr['simulation'].startswith('G0_BPSK_HEADER_PASS') and hdr['source_sha256']==hashlib.sha256((project/'sim/g0/bpsk_header_cost_probe.sv').read_bytes()).hexdigest()
  actual=hdr['actual_capture_headers'];assert len(actual)==28 and all(h['error']==0 for h in actual)
  for p in ref.glob('*.hex'):os.link(p,out/p.name)
  for source,target in [('vectors.hex','header_vectors.hex'),('math.hex','header_math.hex')]:
   with (headerref/source).open() as f:(out/target).write_text(''.join(itertools.islice(f,28*384)))
  sources=[]
  for name in SOURCES:
   target=out/name;shutil.copyfile(project/'sim/g0'/name,target);sources.append(target)
  (out/'tb.sv').write_text(tb_text((ref/'initialization.sv').read_text(),meta,actual));xpm=bp.parent/'data/ip/xpm'
  def run(step,args):
   with (out/(step+'.log')).open('w') as f:subprocess.run(args,cwd=out,stdout=f,stderr=subprocess.STDOUT,check=True)
  run('compile',[str(bp/'xvlog'),'--sv',str(xpm/'xpm_cdc/hdl/xpm_cdc.sv'),str(xpm/'xpm_memory/hdl/xpm_memory.sv'),*map(str,sources),str(out/'tb.sv')]);run('elaborate',[str(bp/'xelab'),'tb','-s','live_header_tb','-mt','off']);run('simulate',[str(bp/'xsim'),'live_header_tb','-runall'])
  log=(out/'simulate.log').read_text();assert 'G0_LIVE_HEADER_ACQUISITION_PASS' in log and 'G0_LIVE_TRACKING_ACQUISITION_PASS' in log and 'Fatal:' not in log,log[-4500:]
  result={'scope':__doc__,'header_simulation':next(s for s in log.splitlines() if s.startswith('G0_LIVE_HEADER_ACQUISITION_PASS')),'acquisition_simulation':next(s for s in log.splitlines() if s.startswith('G0_LIVE_TRACKING_ACQUISITION_PASS')),'source_sha256':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sources},'fixtures':str(ref.relative_to(project)),'header_reference':str(headerref.relative_to(project)),'routed_OOC':None,'g0_passed':False}
  (out/'receipt.json').write_text(json.dumps(result,indent=2)+'\n')
  if not a.sim_only:
   tcl='create_project -in_memory -part xc7z020clg484-1\nset_property XPM_LIBRARIES {XPM_MEMORY XPM_CDC} [current_project]\nset_param general.maxThreads 4\n'+''.join('read_verilog -sv {'+str(s)+'}\n' for s in sources)+'''synth_design -top header_acquiring_frontend_cost_probe -part xc7z020clg484-1 -mode out_of_context
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
set fp [open memory-ownership.rpt w]
puts $fp "shared4096 [llength [get_cells -hier -filter {REF_NAME =~ RAMB* && NAME =~ *timing/coherent/window/history*}]]"
puts $fp "private512 [llength [get_cells -hier -filter {REF_NAME =~ RAMB* && NAME =~ *private_history*}]]"
puts $fp "header_DSP [llength [get_cells -hier -filter {REF_NAME =~ DSP* && NAME =~ header/decoder/*}]]"
close $fp
write_checkpoint -force live_header.dcp
'''
   (out/'ooc.tcl').write_text(tcl);run('ooc',[str(bp/'vivado'),'-mode','batch','-source',str(out/'ooc.tcl'),'-nolog','-nojournal']);result['routed_OOC']=routed_measurements(out)
   memory=(out/'memory-ownership.rpt').read_text();assert memory=='shared4096 9\nprivate512 0\nheader_DSP 1\n',memory;result['memory_ownership']={'shared4096_BRAM_count':9,'private512_BRAM_count':0,'header_decoder_DSP_count':1}
  (out/'receipt.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps({k:v for k,v in result.items() if k!='source_sha256'}),flush=True)
 finally:(out/'disk-after.json').write_text(json.dumps(disk_check('after-live-header-acquisition'),indent=2)+'\n')

if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);p.add_argument('--fixture-directory',type=Path,required=True);p.add_argument('--header-reference',type=Path,required=True);p.add_argument('--vivado-bin',type=Path,required=True);p.add_argument('--sim-only',action='store_true');main(p.parse_args())
