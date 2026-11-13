#!/usr/bin/env python3
"""Real ADC boundary replays: held header output and source loss mid-header.
Each retains complete value/identity oracles and proves fresh-origin autonomous
recovery. Aborted-frame data deadlines are not claimed; recovered frames retain
all original deadline checks. Finite G0 only.
"""
import argparse,hashlib,json,os,shutil,subprocess
from pathlib import Path
from header_acquisition_probe import SOURCES
from workspace_storage_guard import check as disk_check,GIB

def replace(text,old,new):
 assert text.count(old)==1,(old,text.count(old));return text.replace(old,new)

def boundary_tb(original,kind):
 held=kind=='held_header';j=7 if held else 6
 text=replace(original,'for(j=0;j<8;j=j+1)',f'for(j={j};j<{j+1};j=j+1)')
 text=replace(text,'integer header_result_cycle[0:49]','integer header_result_revoked=0,header_work_revoked=0,blocked_H_checks=0;\ninteger header_result_cycle[0:49]')
 text=replace(text,'if(rst||cancel||fence)begin header_seen=0;header_held=0;end',
  'if(rst||cancel||fence)begin if(header_seen)header_result_revoked=header_result_revoked+1;if(!rst&&!cancel&&dut.header.reader_active)header_work_revoked=header_work_revoked+1;header_seen=0;header_held=0;end')
 if held:
  text=replace(text,'else if(j==7&&stage==0)nready=0;','')
  text=replace(text,'else if(header_wait==37)hr=1;','else if(j==7&&stage==0)hr=0;\n else if(header_wait==37)hr=1;')
  text=replace(text,'verify_episode(2,1,1,0,1);','verify_episode(3,2,1,1,0);')
  guard='''if(j==7&&stage==0&&dut.core_normal_valid&&!dut.header_cfg_ready)begin
    q=model*5+2;if(nv||nid!=initial_id+2||nerr||ndetail||nh!==gold_h[q]||nsteps!==gold_phase[q]||energy!==gold_energy[q]||selected!==gold_selected[q])$fatal(1,"held header second matrix oracle");blocked_H_checks=blocked_H_checks+1;
   end
   '''
  text=replace(text,'if(dut.receiver.reader_conflict)',guard+'if(dut.receiver.reader_conflict)')
 else:
  text=replace(text,'if(pause_on_pending&&dut.receiver.job_pending&&dut.receiver.core_normal_valid)break;','if(j==6&&stage==0&&dut.header.received>=8)break;\n   if(pause_on_pending&&dut.receiver.job_pending&&dut.receiver.core_normal_valid)break;')
  text=replace(text,'end else verify_episode(4,4,4,4,0);','end else if(stage==0)verify_episode(1,1,1,1,0);else verify_episode(4,4,4,4,0);')
  text=replace(text,'if(update||nv||dut.receiver.startup_commit)','if(update||nv||hv||dut.header.read_enable||dut.receiver.startup_commit)')
  text=replace(text,'if(matrix_count[idx])begin','if(matrix_count[idx]&&stage!=0)begin')
 old='if(header_results-e_header_results!=expected_consumed||header_consumed-e_header_consumed!=expected_consumed)$fatal(1,"live header episode census");'
 stage0='header_results-e_header_results!=1||header_consumed-e_header_consumed!=0' if held else 'header_results-e_header_results!=0||header_consumed-e_header_consumed!=0'
 new=f'if(stage==0)begin if({stage0})$fatal(1,"header boundary aborted/held census");end else {old}'
 text=replace(text,old,new)
 lines=text.splitlines()
 for i,line in enumerate(lines):
  if line.startswith(' if(starts!=10||host_searches!=8'):
   if held:condition='starts!=2||host_searches!=1||local_searches!=1||recovery_searches!=0||configs!=4||candidates!=2||updates!=7||jobs!=6||produced!=5||consumed!=5||revoked!=0||normal_vectors!=5376||startup_vectors!=800||metric_count!=585||blocked_H_checks==0'
   else:condition='starts!=2||host_searches!=1||local_searches!=0||recovery_searches!=1||configs!=4||candidates!=2||updates!=5||jobs!=5||produced!=5||consumed!=5||revoked!=0||normal_vectors!=4480||startup_vectors!=800||metric_count!=455'
   lines[i]=f' if({condition})$fatal(1,"header boundary tracking census");'
  if line.startswith(' if(header_jobs!=36||header_results!=36'):
   if held:condition='header_jobs!=5||header_results!=5||header_consumed!=4||header_result_revoked!=1||header_work_revoked!=0||header_vector_count!=1920||header_soft_checks!=1920||header_min_data_lead<=0'
   else:condition='header_jobs!=5||header_results!=4||header_consumed!=4||header_result_revoked!=0||header_work_revoked!=1||header_vector_count<1544||header_vector_count>1664||header_soft_checks<1536||header_min_data_lead<=0'
   lines[i]=f' if({condition})$fatal(1,"header boundary header census jobs%0d results%0d consumed%0d resultrev%0d workrev%0d vectors%0d soft%0d",header_jobs,header_results,header_consumed,header_result_revoked,header_work_revoked,header_vector_count,header_soft_checks);'
 text='\n'.join(lines)+'\n';text=text.replace('G0_LIVE_HEADER_ACQUISITION_PASS','G0_HEADER_BOUNDARY_PASS').replace('G0_LIVE_TRACKING_ACQUISITION_PASS','G0_HEADER_BOUNDARY_TRACKING_PASS')
 return text

def main(a):
 project=Path(__file__).resolve().parents[1];out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);ref=a.reference.resolve();bp=a.vivado_bin.resolve()
 before=disk_check('before-live-header-boundaries');(out/'disk-before.json').write_text(json.dumps(before,indent=2)+'\n')
 if before['disk']['available_bytes']<5*GIB:raise SystemExit('Under5GiB: handle storage first.')
 try:
  positive=json.loads((ref/'receipt.json').read_text());assert positive['header_simulation'].startswith('G0_LIVE_HEADER_ACQUISITION_PASS')
  for name in SOURCES:assert hashlib.sha256((ref/name).read_bytes()).hexdigest()==positive['source_sha256'][name]==hashlib.sha256((project/'sim/g0'/name).read_bytes()).hexdigest(),name
  results=[];xpm=bp.parent/'data/ip/xpm'
  for kind in ['held_header','active_source_loss']:
   d=out/kind;d.mkdir()
   for p in ref.glob('*.hex'):os.link(p,d/p.name)
   for name in SOURCES:shutil.copyfile(ref/name,d/name)
   (d/'tb.sv').write_text(boundary_tb((ref/'tb.sv').read_text(),kind))
   def run(step,args):
    with (d/(step+'.log')).open('w') as f:subprocess.run(args,cwd=d,stdout=f,stderr=subprocess.STDOUT,check=True)
   run('compile',[str(bp/'xvlog'),'--sv',str(xpm/'xpm_cdc/hdl/xpm_cdc.sv'),str(xpm/'xpm_memory/hdl/xpm_memory.sv'),*[str(d/s) for s in SOURCES],str(d/'tb.sv')]);run('elaborate',[str(bp/'xelab'),'tb','-s','header_boundary_tb','-mt','off']);run('simulate',[str(bp/'xsim'),'header_boundary_tb','-runall'])
   log=(d/'simulate.log').read_text();assert 'G0_HEADER_BOUNDARY_PASS' in log and 'Fatal:' not in log,(kind,log[-4000:])
   r={'name':kind,'header':next(s for s in log.splitlines() if s.startswith('G0_HEADER_BOUNDARY_PASS')),'tracking':next(s for s in log.splitlines() if s.startswith('G0_HEADER_BOUNDARY_TRACKING_PASS'))};results.append(r);print(json.dumps(r),flush=True)
  result={'scope':__doc__,'reference':str(ref.relative_to(project)),'boundaries':results,'source_sha256':positive['source_sha256'],'g0_passed':False};(out/'receipt.json').write_text(json.dumps(result,indent=2)+'\n')
 finally:(out/'disk-after.json').write_text(json.dumps(disk_check('after-live-header-boundaries'),indent=2)+'\n')
if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);p.add_argument('--reference',type=Path,required=True);p.add_argument('--vivado-bin',type=Path,required=True);main(p.parse_args())
