#!/usr/bin/env python3
"""Actual ADC acquisition/H -> ONE shared4096 F1/header/pilot phase tracking.
Retain all old10-episode data/recovery oracles; compare every new pilot sample,
complex statistic and bounded OLS output. Last partial capture tails must erase.
Physical IO/calibration/payload/link/FEC/DMA/video/full G0 remain pending.
"""
import argparse,hashlib,itertools,json,os,shutil,subprocess
from pathlib import Path
import numpy as np
from header_acquisition_probe import tb_text as header_tb
from tracking_acquisition_probe import SOURCES as BASE
from pilot_tracking_probe import SOURCES as PILOT
from bpsk_header_probe import unpack,pack
from pilot_math_probe import observation
from pilot_smoothing_probe import smooth
from fec_inverse_cost_probe import routed_measurements
from workspace_storage_guard import check as disk_check,GIB
SOURCES=BASE+[n for n in PILOT if n not in BASE]+['frame_replay_cost_probe.sv','frame_acquiring_frontend_cost_probe.sv']
EXTRA=r'''
reg pready=0,sready=0;wire pvalid,pouttx,svalid,frame_abort;wire [31:0] poutid,poutctx,poutgen,stid,stctx,stgen;wire [15:0] poutepoch,stepoch;wire [8:0] poutpoint,poutknown,stpoint;wire signed [40:0] poutphase;wire [2:0] sterror,stdetail;
reg [83:0] pilot_vectors[0:15343];reg [167:0] pilot_math[0:15343];reg [59:0] pilot_smooth[0:15343];integer available[0:27];
integer pilot_samples=0,pilot_complex=0,pilot_angles=0,phase_points=0,phase_consumed=0,phase_holds=0,frame_success=0,frame_partial=0,status_wait=0,frame_phase_count=0,frame_phase_consumed=0,frame_pilot_count=0,pilot_block=0,pilot_ref=0,pilot_read_age=0,pilot_max_read_age=0;
reg phase_seen=0,phase_held=0,status_seen=0,status_held=0,status_valid_q=0;reg [171:0] phase_view;reg [126:0] status_view;
wire [171:0] observed_phase={poutepoch,poutgen,poutctx,poutid,pouttx,poutknown,poutpoint,poutphase};wire [126:0] observed_status={stid,stctx,stgen,stepoch,sterror,stdetail,stpoint};
reg [79:0] pilot_coordinate;reg [80:0] pilot_rounded;
always @(negedge clk)begin
 pready=cycle%7>2;
 sready=!rst && !cancel && !fence && cycle%53==52;
end
always @(posedge clk)begin
 status_valid_q<=svalid;
 if(rst||cancel||fence)begin phase_seen=0;phase_held=0;status_seen=0;status_held=0;end
 else begin
  if(dut.receiver.core_normal_valid&&!dut.header_cfg_ready&&cycle%256==0)$display("FRAME_CFG_DEBUG state%0d ready%0d math%0d smooth%0d request%0d timing%0d held%0d/%0d cancel%0d ownerlost%0d innercfgpilot%0d active%0d pending%0d valid%0d ready%0d",dut.frame.state,dut.frame.cfg_ready,dut.frame.math_ready,dut.frame.smooth_ready,dut.frame.request_idle,dut.frame.timing_ready,dut.frame.header_held,dut.frame.status_held,dut.frame.abort_work,dut.frame.owner_lost,dut.frame.piloting,dut.frame.tracking.active,dut.frame.tracking.pending,svalid,sready);
  if(dut.frame.cfg_valid&&dut.frame.cfg_ready)begin
   pilot_block=model*4+active_window-1;frame_phase_count=0;frame_phase_consumed=0;frame_pilot_count=0;
  end
  if(dut.frame.sample_valid&&dut.frame.sample_ready&&dut.frame.state==5)begin
   pilot_ref=pilot_block*548+2*dut.frame.point+dut.frame.tx;
   if(dut.frame.sample_error)begin
    if(header_window!=4||dut.frame.sample_error!=3||2*dut.frame.point+dut.frame.tx!=available[pilot_block])$fatal(1,"live frame unexpected pilot sampling error");
   end else begin
    pilot_coordinate=wpeak[header_model*5+header_window]+80'(1536+66*dut.frame.point+dut.frame.tx)*wstep[header_model*5+header_window];pilot_rounded={1'b0,pilot_coordinate}+81'd32768;
    if(dut.frame.sample_bad||2*dut.frame.point+dut.frame.tx>=available[pilot_block]||dut.frame.sample_tag!=={epoch,base+pilot_rounded[79:32]}||dut.frame.sample_mu!==pilot_rounded[31:16]||dut.frame.sample_data!==pilot_vectors[pilot_ref])$fatal(1,"live frame pilot sampler oracle");
    pilot_read_age=48'(latest[47:0]-dut.frame.sample_tag[47:0]);if(pilot_read_age>pilot_max_read_age)pilot_max_read_age=pilot_read_age;
    pilot_samples=pilot_samples+1;frame_pilot_count=frame_pilot_count+1;
   end
  end
  if(dut.frame.tracking.math.math_valid&&dut.frame.tracking.math.math_pilot&&dut.frame.tracking.math.math_error==0)begin
   pilot_ref=pilot_block*548+2*dut.frame.point+dut.frame.tx;
   if({dut.frame.tracking.math.math_imag,dut.frame.tracking.math.math_real}!==pilot_math[pilot_ref][135:0])$fatal(1,"live frame full complex pilot oracle");pilot_complex=pilot_complex+1;
  end
  if(dut.frame.observation_accepted)begin
   pilot_ref=pilot_block*548+2*dut.frame.point+dut.frame.tx;
   if(dut.frame.tracking.pilot_error||dut.frame.tracking.pilot_phase!==pilot_math[pilot_ref][167:136])$fatal(1,"live frame pilot angle oracle");pilot_angles=pilot_angles+1;
  end
  if(pvalid&&!phase_seen)begin
   if(header_window==4||frame_phase_count>=548||{pouttx,poutknown,poutpoint,poutphase}!==pilot_smooth[pilot_block*548+frame_phase_count]||poutid!==initial_id+32'(header_window)||poutctx!==bound_context||poutgen!==bound_generation||poutepoch!==epoch)$fatal(1,"live frame phase Fraction oracle");
   phase_seen=1;phase_points=phase_points+1;frame_phase_count=frame_phase_count+1;
  end
  if(phase_held&&!frame_abort&&(!pvalid||observed_phase!==phase_view))$fatal(1,"live frame held phase changed");
  if(pvalid&&pready)begin phase_seen=0;phase_consumed=phase_consumed+1;frame_phase_consumed=frame_phase_consumed+1;end
  phase_held=pvalid&&!pready&&!frame_abort;if(phase_held)begin phase_view=observed_phase;phase_holds=phase_holds+1;end
  if(svalid&&!status_seen)begin
   if(stid!==initial_id+32'(header_window)||stctx!==bound_context||stgen!==bound_generation||stepoch!==epoch||frame_pilot_count!=available[pilot_block])$fatal(1,"live frame terminal identity/pilot census");
   if(header_window==4)begin if(sterror!=2||stdetail!=3||frame_phase_count||frame_phase_consumed)$fatal(1,"live frame missing capture tail not erased");frame_partial=frame_partial+1;end
   else begin if(sterror||stdetail||frame_phase_count!=548||frame_phase_consumed!=548)$fatal(1,"live frame complete census");frame_success=frame_success+1;end
   status_seen=1;$display("LIVE_FRAME case=%0d stage=%0d window=%0d error=%0d:%0d phases=%0d pilots=%0d",j,stage,header_window,sterror,stdetail,frame_phase_count,frame_pilot_count);
  end
  if(status_held&&(!svalid||observed_status!==status_view))$fatal(1,"live frame held status changed");
  if(svalid&&sready)status_seen=0;
  status_held=svalid&&!sready;if(status_held)status_view=observed_status;
 end
end
'''
def main(a):
 project=Path(__file__).resolve().parents[1];out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);ref=a.fixture_directory.resolve();hr=a.header_reference.resolve();bp=a.vivado_bin.resolve()
 before=disk_check('before-live-frame-acquisition');(out/'disk-before.json').write_text(json.dumps(before,indent=2)+'\n')
 if before['disk']['available_bytes']<5*GIB:raise SystemExit('Under5GiB: handle storage first.')
 try:
  meta=json.loads((ref/'fixture_metadata.json').read_text());hdr=json.loads((hr/'receipt.json').read_text());assert hdr['source_sha256']==hashlib.sha256((project/'sim/g0/bpsk_header_cost_probe.sv').read_bytes()).hexdigest();actual=hdr['actual_capture_headers']
  for p in ref.glob('*.hex'):os.link(p,out/p.name)
  for source,target in [('vectors.hex','header_vectors.hex'),('math.hex','header_math.hex')]:
   with (hr/source).open() as f:(out/target).write_text(''.join(itertools.islice(f,28*384)))
  filtered=np.memmap(ref/'filtered.hex',dtype='S21',mode='r');pv=[];pm=[];ps=[];initial=[];counts=[]
  for m,model in enumerate(meta['models']):
   for w in range(1,5):
    entry=model['windows'][w];raw=[];available=0
    for g in range(274):
     phases=[]
     for tx in (0,1):
      offset=1536+66*g+tx;tag,mu=divmod((entry['peak']+offset*entry['step']+32768)//65536,65536)
      if tag+21<model['length']:
       aa=unpack(int(filtered[model['offset']+tag+20],16),20);bb=unpack(int(filtered[model['offset']+tag+21],16),20);samples=[(2*(x*(65536-mu)+y*mu)+32768)//65536 for x,y in zip(aa,bb)]
       hh=unpack(entry['H']>>(104*tx),26);st=(entry['phase_steps']>>(32*tx))&0xffffffff;stat,_=observation(hh,samples,(-st*offset)&0xffffffff);real,imag,nx,ny,shift,phase,error=stat;assert error==0
       pv.append(pack(samples,21));pm.append((real%(1<<68))+((imag%(1<<68))<<68)+(phase<<136));available+=1;phases.append(phase)
      else:pv.append(0);pm.append(0);phases.append(None)
     raw.append(phases)
    counts.append(available);initial.append(f'available[{m*4+w-1}]={available};')
    if w<4:
     assert available==548;rows,_=smooth(raw);ps.extend((phi%(1<<41))+(i<<41)+(known<<50)+(tx<<59) for known,tx,i,phi in rows)
    else:assert available<12;ps.extend([0]*548)
  for name,rows,width in [('pilot_vectors',pv,21),('pilot_math',pm,42),('pilot_smooth',ps,15)]: (out/(name+'.hex')).write_text(''.join(f'{v:0{width}x}\n' for v in rows))
  text=header_tb((ref/'initialization.sv').read_text(),meta,actual).replace('header_acquiring_frontend_cost_probe dut(','frame_acquiring_frontend_cost_probe dut(').replace('dut.header.','dut.frame.').replace('dut.frame.decoder.','dut.frame.tracking.math.math_core.')
  text=text.replace('.header_stream1_bytes(hl1));','.header_stream1_bytes(hl1),.phase_valid(pvalid),.phase_ready(pready),.phase_id(poutid),.phase_context(poutctx),.phase_generation(poutgen),.phase_epoch(poutepoch),.phase_point(poutpoint),.phase_known_point(poutknown),.phase_tx(pouttx),.phase_value(poutphase),.frame_abort(frame_abort),.status_valid(svalid),.status_ready(sready),.status_id(stid),.status_context(stctx),.status_generation(stgen),.status_epoch(stepoch),.status_error(sterror),.status_detail(stdetail),.status_point(stpoint));')
  decl,logic=EXTRA.split('always @(negedge clk)',1);text=text.replace('frame_acquiring_frontend_cost_probe dut(',decl+'\nframe_acquiring_frontend_cost_probe dut(').replace('task run_samples','always @(negedge clk)'+logic+'\ntask run_samples')
  text=text.replace('if(dut.frame.sample_valid&&dut.frame.sample_ready)begin','if(dut.frame.sample_valid&&dut.frame.sample_ready&&dut.frame.state==2)begin')
  text=text.replace('if(dut.frame.tracking.math.math_core.state==3&&','if(!dut.frame.tracking.math.math_core.pilot&&dut.frame.tracking.math.math_core.state==3&&')
  text=text.replace('  repeat(100)@(negedge clk);','  repeat(5000)@(negedge clk);')
  text=text.replace(' $readmemh("adc.hex",adc);','$readmemh("pilot_vectors.hex",pilot_vectors);$readmemh("pilot_math.hex",pilot_math);$readmemh("pilot_smooth.hex",pilot_smooth);\n'+ '\n'.join(initial)+'\n $readmemh("adc.hex",adc);')
  marker=' $display("G0_LIVE_TRACKING_ACQUISITION_PASS';assert text.count(marker)==1
  text=text.replace(marker,''' if(frame_success!=27||frame_partial!=9||phase_points!=27*548||phase_consumed!=27*548||pilot_max_read_age>=4096||pilot_complex<pilot_samples||pilot_angles!=pilot_samples)$fatal(1,"live frame global coverage good%0d partial%0d phase%0d/%0d sample%0d complex%0d angles%0d",frame_success,frame_partial,phase_points,phase_consumed,pilot_samples,pilot_complex,pilot_angles);
 $display("G0_LIVE_FRAME_ACQUISITION_PASS complete_frames=%0d explicit_partial_tail_errors=%0d exact_pilot_samples=%0d complex_checks=%0d angle_checks=%0d phase_points=%0d consumed=%0d maximum_pilot_read_age=%0d held_phase_checks=%0d",frame_success,frame_partial,pilot_samples,pilot_complex,pilot_angles,phase_points,phase_consumed,pilot_max_read_age,phase_holds);
'''+marker)
  (out/'tb.sv').write_text(text)
  for n in SOURCES:shutil.copyfile(project/'sim/g0'/n,out/n)
  xpm=bp.parent/'data/ip/xpm'
  def run(step,args):
   with (out/(step+'.log')).open('w') as f:subprocess.run(args,cwd=out,stdout=f,stderr=subprocess.STDOUT,check=True)
  run('compile',[str(bp/'xvlog'),'--sv',str(xpm/'xpm_cdc/hdl/xpm_cdc.sv'),str(xpm/'xpm_memory/hdl/xpm_memory.sv'),*[str(out/n) for n in SOURCES],str(out/'tb.sv')]);run('elaborate',[str(bp/'xelab'),'tb','-s','live_frame_tb','-mt','off']);run('simulate',[str(bp/'xsim'),'live_frame_tb','-runall'])
  log=(out/'simulate.log').read_text();assert 'G0_LIVE_FRAME_ACQUISITION_PASS' in log and 'Fatal:' not in log,log[-5000:]
  result={'scope':__doc__,'frame_simulation':next(s for s in log.splitlines() if s.startswith('G0_LIVE_FRAME_ACQUISITION_PASS')),'header_simulation':next(s for s in log.splitlines() if s.startswith('G0_LIVE_HEADER_ACQUISITION_PASS')),'acquisition_simulation':next(s for s in log.splitlines() if s.startswith('G0_LIVE_TRACKING_ACQUISITION_PASS')),'source_sha256':{n:hashlib.sha256((out/n).read_bytes()).hexdigest() for n in SOURCES},'fixtures':str(ref.relative_to(project)),'pilot_counts_by_model_and_window':counts,'routed_OOC':None,'g0_passed':False};(out/'receipt.json').write_text(json.dumps(result,indent=2)+'\n')
  if not a.sim_only:
   tcl='create_project -in_memory -part xc7z020clg484-1\nset_property XPM_LIBRARIES {XPM_MEMORY XPM_CDC} [current_project]\nset_param general.maxThreads 4\n'+''.join('read_verilog -sv {'+str(out/n)+'}\n' for n in SOURCES)+'''synth_design -top frame_acquiring_frontend_cost_probe -part xc7z020clg484-1 -mode out_of_context
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
puts $fp "frame_math_DSP [llength [get_cells -hier -filter {REF_NAME =~ DSP* && NAME =~ frame/tracking/math/math_core/*}]]"
puts $fp "frame_sampler_DSP [llength [get_cells -hier -filter {REF_NAME =~ DSP* && NAME =~ frame/sampler/*}]]"
close $fp
write_checkpoint -force live_frame.dcp
'''
   (out/'ooc.tcl').write_text(tcl);run('ooc',[str(bp/'vivado'),'-mode','batch','-source',str(out/'ooc.tcl'),'-nolog','-nojournal']);result['routed_OOC']=routed_measurements(out);memory=(out/'memory-ownership.rpt').read_text();assert memory=='shared4096 9\nprivate512 0\nframe_math_DSP 1\nframe_sampler_DSP 1\n',memory;result['memory_ownership']={'shared4096_BRAM_count':9,'private512_BRAM_count':0,'shared_header_pilot_DSP_count':1,'shared_header_pilot_sampler_DSP_count':1}
  (out/'receipt.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps({k:v for k,v in result.items() if k!='source_sha256'}),flush=True)
 finally:(out/'disk-after.json').write_text(json.dumps(disk_check('after-live-frame-acquisition'),indent=2)+'\n')
if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);p.add_argument('--fixture-directory',type=Path,required=True);p.add_argument('--header-reference',type=Path,required=True);p.add_argument('--vivado-bin',type=Path,required=True);p.add_argument('--sim-only',action='store_true');main(p.parse_args())
