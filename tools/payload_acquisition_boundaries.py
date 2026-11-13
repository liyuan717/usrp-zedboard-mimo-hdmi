#!/usr/bin/env python3
"""Actual ADC loss during business F1, held ML and queued H -> fresh recovery.
Keeps all numeric/deadline oracles. Diagnostic phase_ready is tied0; the real PL
node queue must consume phases independently. Full G0/video remain pending.
"""
import argparse,hashlib,json,os,shutil,subprocess
from pathlib import Path
from header_acquisition_boundaries import replace
from payload_acquisition_probe import SOURCES
from workspace_storage_guard import check as disk_check,GIB

def boundary_tb(original,kind,held_gate=None):
 held=kind=='held_ML';queued=kind=='queued_descriptor_source_loss';j=7 if held else 6
 text=replace(original,'for(j=0;j<8;j=j+1)',f'for(j={j};j<{j+1};j=j+1)')
 text=replace(text,'.phase_ready(pready)',".phase_ready(1'b0)")
 text=replace(text,'integer data_model=0','reg boundary_fence_q=0,queued_fault_fired=0;\nalways @(posedge clk)if(rst)boundary_fence_q<=0;else boundary_fence_q<=fence;\ninteger boundary_data_prefix=0,boundary_sample_prefix=0,boundary_phase_prefix=0,data_revoked=0,phase_revoked=0,frame_work_revoked=0,queued_work_revoked=0;\ninteger data_model=0')
 text=replace(text,'if(rst||cancel||fence)begin phase_seen=0;phase_held=0;status_seen=0;status_held=0;end',
  'if(rst||cancel||fence)begin if(!rst&&!cancel&&fence&&!boundary_fence_q)begin if(dut.frame.reader_active)frame_work_revoked=frame_work_revoked+1;if(dut.handoff_pending)queued_work_revoked=queued_work_revoked+1;end if(phase_seen)phase_revoked=phase_revoked+1;phase_seen=0;phase_held=0;status_seen=0;status_held=0;end')
 text=replace(text,'if(rst||cancel||fence||frame_abort)begin data_seen=0;data_held=0;data_status_seen=0;end',
  'if(rst||cancel||fence||frame_abort)begin if(!rst&&!cancel&&fence&&data_seen)data_revoked=data_revoked+1;data_seen=0;data_held=0;data_status_seen=0;end')
 prefix='boundary_data_prefix=data_results;boundary_sample_prefix=data_inputs;boundary_phase_prefix=phase_points;'
 if held:
  text=replace(text,'else if(j==7&&stage==0)nready=0;','')
  old_gate,new_gate=held_gate or ('dready=cycle%23>4;','dready=!(j==7&&stage==0)&&cycle%23>4;')
  text=replace(text,old_gate,new_gate)
  text=replace(text,'if(pause_on_pending&&dut.receiver.job_pending&&dut.receiver.core_normal_valid)break;',
   'if(pause_on_pending&&dut.receiver.job_pending&&dut.frame.reader_active&&data_seen)break;')
  text=replace(text,'verify_episode(2,1,1,0,1);','verify_episode(2,1,1,1,0);'+prefix)
 else:
  stop='queued_fault_fired' if queued else 'dut.frame.business_owner&&dut.frame.sampler.state==5&&data_results>=96&&!dvalid'
  text=replace(text,'if(pause_on_pending&&dut.receiver.job_pending&&dut.receiver.core_normal_valid)break;',
   'if(j==6&&stage==0&&'+stop+')break;\n   if(pause_on_pending&&dut.receiver.job_pending&&dut.receiver.core_normal_valid)break;')
  if queued:
   text=replace(text,'task run_samples','always @(negedge clk)begin if(rst)queued_fault_fired=0;else if(j==6&&stage==0&&dut.handoff_pending&&!queued_fault_fired)begin loss=1;queued_fault_fired=1;end end\ntask run_samples')
  text=replace(text,'end else verify_episode(4,4,4,4,0);','end else if(stage!=0)verify_episode(4,4,4,4,0);')
  text=replace(text,"epoch=0;base=48'hffffffff1234;",prefix+"epoch=0;base=48'hffffffff1234;")
  text=replace(text,'if(update||nv||dut.receiver.startup_commit)','if(update||nv||hv||pvalid||dvalid||dut.read_enable||dut.receiver.startup_commit)')
 text=replace(text,'if(matrix_count[idx])begin','if(matrix_count[idx]&&stage!=0)begin')
 text=replace(text,'if(header_count[idx])begin','if(header_count[idx]&&stage!=0)begin')
 lines=text.splitlines()
 for i,line in enumerate(lines):
  if line.startswith(' if(starts!=10||host_searches!=8'):
   counts=('starts!=2||host_searches!=1||local_searches!=1||recovery_searches!=0||configs!=4||candidates!=2||updates!=6||jobs!=5||produced!=5||consumed!=5||revoked!=0||normal_vectors!=4480||startup_vectors!=800||metric_count!=520' if held else 'starts!=2||host_searches!=1||local_searches!=0||recovery_searches!=1||configs!=4||candidates!=2||updates!=5||jobs!=5||produced!=5||consumed!=5||revoked!=0||normal_vectors!=4480||startup_vectors!=800||metric_count!=455')
   lines[i]=f' if({counts})$fatal(1,"payload boundary tracking census");'
  if line.startswith(' if(header_jobs!=36||header_results!=36'):
   heads=4 if queued else 5
   lines[i]=f' if(header_jobs!={heads}||header_results!={heads}||header_consumed!={heads}||header_vector_count!={heads*384}||header_soft_checks!={heads*384}||header_min_data_lead<=0)$fatal(1,"payload boundary header census");'
  if line.startswith(' if(frame_success!=27||frame_partial!=9'):
   expected_work='frame_work_revoked!=0||queued_work_revoked!=1' if queued else 'frame_work_revoked!=1||queued_work_revoked!=0'
   lines[i]=' if(frame_success!=3||frame_partial!=1||phase_points!=3*548+boundary_phase_prefix||phase_consumed!=phase_points-phase_revoked||'+expected_work+')$fatal(1,"payload boundary phase/fence census");'
  if line.startswith(' if(data_results!=27*17536'):
   extra='boundary_data_prefix!=1||data_revoked!=1' if held else ('boundary_data_prefix!=0||boundary_sample_prefix!=0||data_revoked!=0' if queued else 'boundary_data_prefix<96||data_revoked!=0')
   lines[i]=' if(data_results!=3*17536+boundary_data_prefix||data_inputs!=3*17536+boundary_sample_prefix||data_consumed!=data_results-data_revoked||'+extra+'||max_business_read_age>=4096)$fatal(1,"payload boundary business census results%0d consumed%0d inputs%0d prefix%0d/%0d revoked%0d",data_results,data_consumed,data_inputs,boundary_data_prefix,boundary_sample_prefix,data_revoked);'
 text='\n'.join(lines)+'\n'
 for old,new in [('G0_LIVE_PAYLOAD_ACQUISITION_PASS','G0_PAYLOAD_BOUNDARY_PASS'),('G0_LIVE_FRAME_ACQUISITION_PASS','G0_PAYLOAD_BOUNDARY_PHASE_PASS'),('G0_LIVE_HEADER_ACQUISITION_PASS','G0_PAYLOAD_BOUNDARY_HEADER_PASS'),('G0_LIVE_TRACKING_ACQUISITION_PASS','G0_PAYLOAD_BOUNDARY_TRACKING_PASS')]:text=text.replace(old,new)
 text=text.replace('complete_frames=27 exact_business_samples','complete_frames=3 exact_business_samples')
 marker=' $display("G0_PAYLOAD_BOUNDARY_PASS'
 text=replace(text,marker,' $display("G0_PAYLOAD_BOUNDARY_OWNERSHIP prefix_results=%0d prefix_samples=%0d revoked_results=%0d active_frame_revoked=%0d queued_H_revoked=%0d prefix_phases=%0d revoked_phases=%0d",boundary_data_prefix,boundary_sample_prefix,data_revoked,frame_work_revoked,queued_work_revoked,boundary_phase_prefix,phase_revoked);\n'+marker)
 return text

def main(a):
 project=Path(__file__).resolve().parents[1];out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);ref=a.reference.resolve();bp=a.vivado_bin.resolve()
 before=disk_check('before-live-payload-boundaries');(out/'disk-before.json').write_text(json.dumps(before,indent=2)+'\n')
 if before['disk']['available_bytes']<5*GIB:raise SystemExit('Under5GiB: handle storage first.')
 try:
  hashes={}
  for n in SOURCES:
   hashes[n]=hashlib.sha256((ref/n).read_bytes()).hexdigest();assert hashlib.sha256((project/'sim/g0'/n).read_bytes()).hexdigest()==hashes[n]
   shutil.copyfile(ref/n,out/n)
  rows=[];xpm=bp.parent/'data/ip/xpm'
  for kind in ['active_business_source_loss','held_ML','queued_descriptor_source_loss']:
   d=out/kind;d.mkdir()
   for p in ref.glob('*.hex'):os.link(p,d/p.name)
   for n in SOURCES:shutil.copyfile(out/n,d/n)
   (d/'tb.sv').write_text(boundary_tb((ref/'tb.sv').read_text(),kind))
   def run(step,args):
    with (d/(step+'.log')).open('w') as f:subprocess.run(args,cwd=d,stdout=f,stderr=subprocess.STDOUT,check=True)
   run('compile',[str(bp/'xvlog'),'--sv',str(xpm/'xpm_cdc/hdl/xpm_cdc.sv'),str(xpm/'xpm_memory/hdl/xpm_memory.sv'),*[str(d/n) for n in SOURCES],str(d/'tb.sv')])
   run('elaborate',[str(bp/'xelab'),'tb','-s','payload_boundary_tb','-mt','off']);run('simulate',[str(bp/'xsim'),'payload_boundary_tb','-runall'])
   log=(d/'simulate.log').read_text();assert 'G0_PAYLOAD_BOUNDARY_PASS' in log and 'Fatal:' not in log,(kind,log[-5000:])
   row={'name':kind,**{k:next(s for s in log.splitlines() if s.startswith(v)) for k,v in [('ownership','G0_PAYLOAD_BOUNDARY_OWNERSHIP'),('payload','G0_PAYLOAD_BOUNDARY_PASS'),('phase','G0_PAYLOAD_BOUNDARY_PHASE_PASS'),('header','G0_PAYLOAD_BOUNDARY_HEADER_PASS'),('tracking','G0_PAYLOAD_BOUNDARY_TRACKING_PASS')]}};rows.append(row);print(json.dumps(row),flush=True)
  (out/'receipt.json').write_text(json.dumps({'scope':__doc__,'reference':str(ref.relative_to(project)),'source_sha256':hashes,'boundaries':rows,'full_reference_run_must_pass_separately':True,'g0_passed':False},indent=2)+'\n')
 finally:(out/'disk-after.json').write_text(json.dumps(disk_check('after-live-payload-boundaries'),indent=2)+'\n')

if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__)
 for name in ('output','reference','vivado-bin'):p.add_argument('--'+name,type=Path,required=True)
 main(p.parse_args())
