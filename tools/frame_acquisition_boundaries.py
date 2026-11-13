#!/usr/bin/env python3
"""Actual ADC loss during pilot work and held phase -> bounded fresh-origin recovery.
Retains ADC/NCO/RRC/training/header/pilot/OLS oracles, not a payload/video proof.
"""
import argparse,hashlib,json,os,shutil,subprocess
from pathlib import Path
from header_acquisition_boundaries import replace
from frame_acquisition_probe import SOURCES
from workspace_storage_guard import check as disk_check,GIB

def boundary_tb(original,kind):
 held=kind=='held_phase';j=7 if held else 6
 text=replace(original,'for(j=0;j<8;j=j+1)',f'for(j={j};j<{j+1};j=j+1)')
 text=replace(text,'integer pilot_samples=0','integer boundary_phase_prefix=0,phase_revoked=0,frame_work_revoked=0;\ninteger pilot_samples=0')
 text=replace(text,'if(rst||cancel||fence)begin phase_seen=0;phase_held=0;status_seen=0;status_held=0;end','if(rst||cancel||fence)begin if(!rst&&!cancel&&dut.frame.reader_active)frame_work_revoked=frame_work_revoked+1;if(phase_seen)phase_revoked=phase_revoked+1;phase_seen=0;phase_held=0;status_seen=0;status_held=0;end')
 if held:
  text=replace(text,'else if(j==7&&stage==0)nready=0;','')
  text=replace(text,'pready=cycle%7>2;','pready=!(j==7&&stage==0)&&cycle%7>2;')
  text=replace(text,'if(pause_on_pending&&dut.receiver.job_pending&&dut.receiver.core_normal_valid)break;','if(pause_on_pending&&dut.receiver.job_pending&&dut.frame.reader_active&&phase_seen)break;')
  text=replace(text,'verify_episode(2,1,1,0,1);','verify_episode(2,1,1,1,0);boundary_phase_prefix=phase_points;')
 else:
  text=replace(text,'if(pause_on_pending&&dut.receiver.job_pending&&dut.receiver.core_normal_valid)break;','if(j==6&&stage==0&&dut.frame.state==5&&dut.frame.point>=12)break;\n   if(pause_on_pending&&dut.receiver.job_pending&&dut.receiver.core_normal_valid)break;')
  text=replace(text,'end else verify_episode(4,4,4,4,0);','end else if(stage!=0)verify_episode(4,4,4,4,0);')
  text=replace(text,'epoch=0;base=48\'hffffffff1234;','boundary_phase_prefix=phase_points;epoch=0;base=48\'hffffffff1234;')
  text=replace(text,'if(update||nv||dut.receiver.startup_commit)','if(update||nv||hv||pvalid||dut.frame.read_enable||dut.receiver.startup_commit)')
 # Recovered frames keep every original deadline; interrupted frame has none.
 text=replace(text,'if(matrix_count[idx])begin','if(matrix_count[idx]&&stage!=0)begin')
 text=replace(text,'if(header_count[idx])begin','if(header_count[idx]&&stage!=0)begin')
 lines=text.splitlines()
 for i,line in enumerate(lines):
  if line.startswith(' if(starts!=10||host_searches!=8'):
   condition=('starts!=2||host_searches!=1||local_searches!=1||recovery_searches!=0||configs!=4||candidates!=2||updates!=6||jobs!=5||produced!=5||consumed!=5||revoked!=0||normal_vectors!=4480||startup_vectors!=800||metric_count!=520' if held else 'starts!=2||host_searches!=1||local_searches!=0||recovery_searches!=1||configs!=4||candidates!=2||updates!=5||jobs!=5||produced!=5||consumed!=5||revoked!=0||normal_vectors!=4480||startup_vectors!=800||metric_count!=455')
   lines[i]=f' if({condition})$fatal(1,"frame boundary tracking census");'
  if line.startswith(' if(header_jobs!=36||header_results!=36'):
   lines[i]=' if(header_jobs!=5||header_results!=5||header_consumed!=5||header_vector_count!=1920||header_soft_checks!=1920||header_min_data_lead<=0)$fatal(1,"frame boundary header census");'
  if line.startswith(' if(frame_success!=27||frame_partial!=9'):
   extra='||boundary_phase_prefix!=1||phase_revoked!=1' if held else '||boundary_phase_prefix<8||phase_revoked>1'
   lines[i]=' if(frame_success!=3||frame_partial!=1||phase_points!=3*548+boundary_phase_prefix||phase_consumed!=phase_points-phase_revoked||frame_work_revoked!=1'+extra+')$fatal(1,"frame boundary phase/fence census success%0d partial%0d phases%0d consumed%0d prefix%0d revoked%0d work%0d",frame_success,frame_partial,phase_points,phase_consumed,boundary_phase_prefix,phase_revoked,frame_work_revoked);'
 text='\n'.join(lines)+'\n';text=text.replace('G0_LIVE_FRAME_ACQUISITION_PASS','G0_FRAME_BOUNDARY_PASS').replace('G0_LIVE_HEADER_ACQUISITION_PASS','G0_FRAME_BOUNDARY_HEADER_PASS').replace('G0_LIVE_TRACKING_ACQUISITION_PASS','G0_FRAME_BOUNDARY_TRACKING_PASS')
 marker=' $display("G0_FRAME_BOUNDARY_PASS';text=replace(text,marker,' $display("G0_FRAME_BOUNDARY_OWNERSHIP prefix=%0d revoked=%0d active_work_revoked=%0d",boundary_phase_prefix,phase_revoked,frame_work_revoked);\n'+marker)
 return text

def main(a):
 project=Path(__file__).resolve().parents[1];out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);ref=a.reference.resolve();bp=a.vivado_bin.resolve()
 before=disk_check('before-live-frame-boundaries');(out/'disk-before.json').write_text(json.dumps(before,indent=2)+'\n')
 if before['disk']['available_bytes']<5*GIB:raise SystemExit('Under5GiB: handle storage first.')
 try:
  positive=json.loads((ref/'receipt.json').read_text());assert positive['frame_simulation'].startswith('G0_LIVE_FRAME_ACQUISITION_PASS')
  snapshot_hashes={};changed=[]
  for n in SOURCES:
   assert hashlib.sha256((ref/n).read_bytes()).hexdigest()==positive['source_sha256'][n],n
   current=project/'sim/g0'/n;actual=hashlib.sha256(current.read_bytes()).hexdigest()
   if actual!=positive['source_sha256'][n]:changed.append(n)
   if not a.current_rtl:assert actual==positive['source_sha256'][n],n
   shutil.copyfile(current if a.current_rtl else ref/n,out/n);snapshot_hashes[n]=hashlib.sha256((out/n).read_bytes()).hexdigest()
  if a.current_rtl:assert changed==['frame_replay_cost_probe.sv'],changed
  results=[];xpm=bp.parent/'data/ip/xpm'
  for kind in ['active_pilot_source_loss','held_phase']:
   d=out/kind;d.mkdir()
   for p in ref.glob('*.hex'):os.link(p,d/p.name)
   for n in SOURCES:shutil.copyfile(out/n,d/n)
   (d/'tb.sv').write_text(boundary_tb((ref/'tb.sv').read_text(),kind))
   def run(step,args):
    with (d/(step+'.log')).open('w') as f:subprocess.run(args,cwd=d,stdout=f,stderr=subprocess.STDOUT,check=True)
   run('compile',[str(bp/'xvlog'),'--sv',str(xpm/'xpm_cdc/hdl/xpm_cdc.sv'),str(xpm/'xpm_memory/hdl/xpm_memory.sv'),*[str(d/n) for n in SOURCES],str(d/'tb.sv')]);run('elaborate',[str(bp/'xelab'),'tb','-s','frame_boundary_tb','-mt','off']);run('simulate',[str(bp/'xsim'),'frame_boundary_tb','-runall'])
   log=(d/'simulate.log').read_text();assert 'G0_FRAME_BOUNDARY_PASS' in log and 'Fatal:' not in log,(kind,log[-4500:])
   r={'name':kind,**{key:next(s for s in log.splitlines() if s.startswith(prefix)) for key,prefix in [('ownership','G0_FRAME_BOUNDARY_OWNERSHIP'),('frame','G0_FRAME_BOUNDARY_PASS'),('header','G0_FRAME_BOUNDARY_HEADER_PASS'),('tracking','G0_FRAME_BOUNDARY_TRACKING_PASS')]}};results.append(r);print(json.dumps(r),flush=True)
  (out/'receipt.json').write_text(json.dumps({'scope':__doc__,'reference':str(ref.relative_to(project)),'boundaries':results,'source_sha256':snapshot_hashes,'changes_from_reference':changed,'current_source_correction_tested':a.current_rtl,'g0_passed':False},indent=2)+'\n')
 finally:(out/'disk-after.json').write_text(json.dumps(disk_check('after-live-frame-boundaries'),indent=2)+'\n')
if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);p.add_argument('--reference',type=Path,required=True);p.add_argument('--vivado-bin',type=Path,required=True);p.add_argument('--current-rtl',action='store_true',help='Independently test corrected current RTL against prior unchanged numerical/recovery oracles');main(p.parse_args())
