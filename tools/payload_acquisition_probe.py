#!/usr/bin/env python3
"""Actual ADC acquisition through shared F1/header/pilot SG queues and payload ML.
Augments the immutable passed10-episode main bench with exact business samples,
phases and ML metrics; all previous acquisition/header/pilot oracles remain.
Not PL linkCRC/FEC/DMA/video or full G0 acceptance.
"""
import argparse,hashlib,json,os,shutil,subprocess
from pathlib import Path
from frame_acquisition_probe import SOURCES as BASE
from payload_datapath_probe import SOURCES as ARITHMETIC
from fec_inverse_cost_probe import routed_measurements
from workspace_storage_guard import check as disk_check,GIB

SOURCES=BASE+ARITHMETIC+['payload_schedule_cost_probe.sv','frame_domain_cost_probe.sv']
PORTS='''.payload_valid(dvalid),.payload_ready(dready),.payload_id(did),.payload_context(dctx),.payload_generation(dgen),.payload_epoch(depoch),.payload_symbol(dsymbol),.payload_decision(ddecision),.payload_metric(dmetric)'''
DECL=r'''
reg dready=0;wire dvalid;wire [31:0] did,dctx,dgen;wire [15:0] depoch;wire [14:0] dsymbol;wire [3:0] ddecision;wire [61:0] dmetric;
reg [83:0] business_samples[0:368255];reg [449:0] business_gold[0:368255];
integer data_model=0,data_window=0,data_block=0,frame_data=0,frame_data_consumed=0,data_results=0,data_consumed=0,data_inputs=0,data_held_checks=0,max_business_read_age=0,max_node_count=0,di,da;
reg data_seen=0,data_held=0,data_status_seen=0;reg [192:0] data_view;wire [192:0] current_data_view={did,dctx,dgen,depoch,dsymbol,ddecision,dmetric};
reg [31:0] data_owner,data_context,data_generation;reg [15:0] data_epoch;reg [79:0] business_coordinate;reg [80:0] business_rounded;
always @(negedge clk)dready=cycle%23>4;
always @(posedge clk)begin
 if(rst||cancel||fence||frame_abort)begin data_seen=0;data_held=0;data_status_seen=0;end
 else begin
  if((dut.frame.timing_cfg&&!dut.frame.timing_ready)||(dut.frame.math_cfg&&!dut.frame.math_ready))$fatal(1,"live business job launched into busy configuration slot");
  if(dut.read_enable&&!dut.read_reservation || dut.receiver.pool_read_enable&&dut.receiver.timing.coherent.window.own_read)$fatal(1,"live shared pool reservation contract");
  if(dut.frame.cfg_valid&&dut.frame.cfg_ready)begin
   data_model=model;data_window=active_window;data_block=(model*3+active_window-1)*17536;frame_data=0;frame_data_consumed=0;
   data_owner=initial_id+32'(active_window);data_context=bound_context;data_generation=bound_generation;data_epoch=epoch;
  end
  if(dut.business.payload.count0>max_node_count)max_node_count=dut.business.payload.count0;
  if(dut.business.payload.count1>max_node_count)max_node_count=dut.business.payload.count1;
  if(dut.read_enable&&dut.frame.business_owner&&dut.frame.sampler.state==1)begin
   da=48'(latest[47:0]-dut.frame.sampler.work_tag[47:0]);if(da>=4096)$fatal(1,"live business expired pool read");if(da>max_business_read_age)max_business_read_age=da;
  end
  if(dut.business_sample_valid&&dut.business_sample_ready)begin
   di=data_block+dut.business_sample_id;
   business_coordinate=wpeak[data_model*5+data_window]+80'(1538+66*(dut.business_sample_id/64)+dut.business_sample_id%64)*wstep[data_model*5+data_window];business_rounded={1'b0,business_coordinate}+81'd32768;
   if(data_window==4||dut.business_sample_error||dut.business_sample_generation!==data_generation||dut.business_sample_tag!=={epoch,base+business_rounded[79:32]}||dut.business_sample_mu!==business_rounded[31:16]||dut.business_sample_data!==business_samples[di])$fatal(1,"live business shared F1 oracle model%0d window%0d symbol%0d",data_model,data_window,dut.business_sample_id);
   if(dut.business.payload.math.phases!==business_gold[di][63:0])$fatal(1,"live business phase interpolation oracle");data_inputs=data_inputs+1;
  end
  if(dvalid&&!data_seen)begin
   di=data_block+dsymbol;
   if(data_window==4||{did,dctx,dgen,depoch}!=={data_owner,data_context,data_generation,data_epoch}||dsymbol!=frame_data||{dmetric,ddecision}!==business_gold[di][449:384])$fatal(1,"live business ML full metric oracle model%0d window%0d symbol%0d expected%0d",data_model,data_window,dsymbol,frame_data);
   data_seen=1;data_results=data_results+1;frame_data=frame_data+1;
  end
  if(data_held&&!frame_abort&&(!dvalid||current_data_view!==data_view))$fatal(1,"live business held result changed");
  if(dvalid&&dready)begin data_seen=0;data_consumed=data_consumed+1;frame_data_consumed=frame_data_consumed+1;end
  data_held=dvalid&&!dready;if(data_held)begin data_view=current_data_view;data_held_checks=data_held_checks+1;end
  if(svalid&&!data_status_seen)begin
   if(data_window!=header_window)$fatal(1,"live business terminal owner window");
   if(data_window==4)begin if(frame_data||frame_data_consumed)$fatal(1,"live business missing SG nodes fabricated data");end
   else if(frame_data!=17536||frame_data_consumed!=17536)$fatal(1,"live business terminal data census %0d/%0d",frame_data,frame_data_consumed);
   data_status_seen=1;
  end
  if(svalid&&sready)data_status_seen=0;
 end
 if(!rst&&!cancel&&!fence&&data_held&&!dvalid&&!frame_abort)$fatal(1,"live business held withdrawal without abort");
end
'''

def main(a):
 project=Path(__file__).resolve().parents[1];out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);ref=a.reference.resolve();numeric=a.payload_reference.resolve();bp=a.vivado_bin.resolve()
 before=disk_check('before-live-payload');(out/'disk-before.json').write_text(json.dumps(before,indent=2)+'\n')
 if before['disk']['available_bytes']<5*GIB:raise SystemExit('Under5GiB: handle storage first.')
 try:
  old=json.loads((ref/'receipt.json').read_text());assert old['frame_simulation'].startswith('G0_LIVE_FRAME_ACQUISITION_PASS')
  for p in ref.glob('*.hex'):os.link(p,out/p.name)
  for name,target in [('samples.hex','business_samples.hex'),('gold.hex','business_gold.hex')]:os.link(numeric/name,out/target)
  text=(ref/'tb.sv').read_text()
  declarations,logic=DECL.split('always @(negedge clk)',1)
  edits=[('reg pready=0,sready=0;','wire pready;reg sready=0;assign pready=dut.node_ready;'),
   (' pready=cycle%7>2;',''),
   ('frame_acquiring_frontend_cost_probe dut(',declarations+'\nframe_acquiring_frontend_cost_probe #(.ENABLE_PAYLOAD(1)) dut('),
   ('task run_samples','always @(negedge clk)'+logic+'\ntask run_samples'),
   ('.status_point(stpoint));','.status_point(stpoint),'+PORTS+');'),
   (' $readmemh("adc.hex",adc);',' $readmemh("business_samples.hex",business_samples);$readmemh("business_gold.hex",business_gold);\n $readmemh("adc.hex",adc);')]
  for oldtext,newtext in edits:assert text.count(oldtext)==1,oldtext;text=text.replace(oldtext,newtext)
  marker=' $display("G0_LIVE_FRAME_ACQUISITION_PASS';assert text.count(marker)==1
  text=text.replace(marker,''' if(data_results!=27*17536||data_consumed!=data_results||data_inputs!=data_results||max_business_read_age>=4096||max_business_read_age<512||max_node_count>8)$fatal(1,"live business global census %0d/%0d/%0d age%0d nodes%0d",data_results,data_consumed,data_inputs,max_business_read_age,max_node_count);
 $display("G0_LIVE_PAYLOAD_ACQUISITION_PASS complete_frames=27 exact_business_samples=%0d exact_ML_results=%0d consumed=%0d maximum_actual_pool_read_age=%0d maximum_nodes_per_TX=%0d held_checks=%0d",data_inputs,data_results,data_consumed,max_business_read_age,max_node_count,data_held_checks);
'''+marker)
  (out/'tb.sv').write_text(text)
  for name in SOURCES:shutil.copyfile(project/'sim/g0'/name,out/name)
  if a.prepare_only:
   prepared={'scope':'Prepared immutable current RTL/input/TB snapshot ONLY. No compilation, simulation or OOC pass.',
    'source_sha256':{n:hashlib.sha256((out/n).read_bytes()).hexdigest() for n in SOURCES},'g0_passed':False,'functional_run_required':True}
   (out/'preparation.json').write_text(json.dumps(prepared,indent=2)+'\n');print(prepared['scope'],flush=True);return
  xpm=bp.parent/'data/ip/xpm'
  def run(label,args):
   with (out/(label+'.log')).open('w') as f:subprocess.run(args,cwd=out,stdout=f,stderr=subprocess.STDOUT,check=True)
  run('compile',[str(bp/'xvlog'),'--sv',str(xpm/'xpm_cdc/hdl/xpm_cdc.sv'),str(xpm/'xpm_memory/hdl/xpm_memory.sv'),*[str(out/n) for n in SOURCES],str(out/'tb.sv')])
  run('elaborate',[str(bp/'xelab'),'tb','-s','live_payload_tb','-mt','off'])
  run('simulate',[str(bp/'xsim'),'live_payload_tb','-runall'])
  log=(out/'simulate.log').read_text();assert 'G0_LIVE_PAYLOAD_ACQUISITION_PASS' in log and 'Fatal:' not in log,log[-6000:]
  result={'scope':__doc__,'payload_simulation':next(s for s in log.splitlines() if s.startswith('G0_LIVE_PAYLOAD_ACQUISITION_PASS')),
   'frame_simulation':next(s for s in log.splitlines() if s.startswith('G0_LIVE_FRAME_ACQUISITION_PASS')),
   'header_simulation':next(s for s in log.splitlines() if s.startswith('G0_LIVE_HEADER_ACQUISITION_PASS')),
   'acquisition_simulation':next(s for s in log.splitlines() if s.startswith('G0_LIVE_TRACKING_ACQUISITION_PASS')),
   'source_sha256':{n:hashlib.sha256((out/n).read_bytes()).hexdigest() for n in SOURCES},'routed_OOC':None,'g0_passed':False}
  (out/'receipt.json').write_text(json.dumps(result,indent=2)+'\n')
  if not a.sim_only:
   before_ooc=disk_check('before-live-payload-OOC');(out/'disk-before-ooc.json').write_text(json.dumps(before_ooc,indent=2)+'\n');assert before_ooc['disk']['available_bytes']>=5*GIB
   try:
    tcl=(ref/'ooc.tcl').read_text() if (ref/'ooc.tcl').exists() else (a.ooc_reference.resolve()/'ooc.tcl').read_text()
    oldroot=str(ref if (ref/'ooc.tcl').exists() else a.ooc_reference.resolve());tcl=tcl.replace(oldroot,str(out))
    marker='synth_design -top';assert tcl.count(marker)==1
    additional=''.join('read_verilog -sv {'+str(out/n)+'}\n' for n in ARITHMETIC+['payload_schedule_cost_probe.sv','frame_domain_cost_probe.sv'])
    tcl=tcl.replace(marker,additional+marker).replace('-mode out_of_context','-mode out_of_context -generic ENABLE_PAYLOAD=1')
    (out/'ooc.tcl').write_text(tcl);run('ooc',[str(bp/'vivado'),'-mode','batch','-source',str(out/'ooc.tcl'),'-nolog','-nojournal'])
    result['routed_OOC']=routed_measurements(out);memory=(out/'memory-ownership.rpt').read_text();assert memory=='shared4096 9\nprivate512 0\nframe_math_DSP 1\nframe_sampler_DSP 1\n',memory
    result['memory_ownership']={'shared4096_BRAM_count':9,'private512_BRAM_count':0,'shared_header_pilot_DSP_count':1,'shared_header_pilot_business_sampler_DSP_count':1}
   finally:(out/'disk-after-ooc.json').write_text(json.dumps(disk_check('after-live-payload-OOC'),indent=2)+'\n')
  (out/'receipt.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps({k:v for k,v in result.items() if k!='source_sha256'}),flush=True)
 finally:(out/'disk-after.json').write_text(json.dumps(disk_check('after-live-payload'),indent=2)+'\n')

if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__)
 for name in ('output','reference','payload-reference','ooc-reference','vivado-bin'):p.add_argument('--'+name,type=Path,required=True)
 mode=p.add_mutually_exclusive_group();mode.add_argument('--sim-only',action='store_true');mode.add_argument('--prepare-only',action='store_true');main(p.parse_args())
