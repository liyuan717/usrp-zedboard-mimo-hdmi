#!/usr/bin/env python3
"""Compile-and-run numeric/protocol negative witnesses on passed header snapshots."""
import argparse,hashlib,json,os,shutil,subprocess
from pathlib import Path
from workspace_storage_guard import check as disk_check,GIB
from header_acquisition_probe import SOURCES

def main(a):
 project=Path(__file__).resolve().parents[1];out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);ref=a.reference.resolve();bp=a.vivado_bin.resolve()
 before=disk_check('before-header-'+a.mode+'-controls');(out/'disk-before.json').write_text(json.dumps(before,indent=2)+'\n')
 if before['disk']['available_bytes']<5*GIB:raise SystemExit('Under5GiB: handle storage first.')
 try:
  positive=json.loads((ref/'receipt.json').read_text());unit=a.mode=='unit'
  if unit:
   names=['bpsk_header_cost_probe.sv'];hashes={names[0]:positive['source_sha256']};assert positive['simulation'].startswith('G0_BPSK_HEADER_PASS')
   changes={
    'limb_shift':(names[0],"issue_shift=issued[0]?6'd17:6'd0;","issue_shift=issued[0]?6'd16:6'd0;",'Fatal: header full dot oracle'),
    'CRC_bypass':(names[0],"if(completed_header[191:160]!=(crc^32'hffffffff))publish(1,completed_header);","if(1'b0)publish(1,completed_header);",'Fatal: header result oracle'),
    'bounds_bypass':(names[0],'else if(profile_bad)publish(2,completed_header);',"else if(1'b0)publish(2,completed_header);",'Fatal: header result oracle'),
    'untrusted_length':(names[0],"out_stream0_bytes<=error==0?16'd4384:16'd0;","out_stream0_bytes<=error==0?16'd4384:16'd4384;",'Fatal: header untrusted metadata escaped')}
  else:
   names=SOURCES;hashes=positive['source_sha256'];assert positive['header_simulation'].startswith('G0_LIVE_HEADER_ACQUISITION_PASS')
   changes={
    'read_owner':('tracking_acquiring_frontend_cost_probe.sv','pool_read_enable=external_busy?external_read_enable:read_enable;','pool_read_enable=read_enable;','Fatal: live header sampler oracle'),
    'double_center_shift':('header_replay_cost_probe.sv','header_position<=cfg_training_position+(',"header_position<=cfg_training_position-80'd85899345920+(",'Fatal: live header sampler oracle')}
  for name in names:assert hashlib.sha256((ref/name).read_bytes()).hexdigest()==hashes[name]==hashlib.sha256((project/'sim/g0'/name).read_bytes()).hexdigest(),name
  controls=[]
  for label,(target,old,new,witness) in changes.items():
   d=out/label;d.mkdir()
   for p in ref.glob('*.hex'):os.link(p,d/p.name)
   for name in names:shutil.copyfile(ref/name,d/name)
   shutil.copyfile(ref/'tb.sv',d/'tb.sv');p=d/target;s=p.read_text();assert s.count(old)==1,(label,old);p.write_text(s.replace(old,new))
   def run(step,args):
    with (d/(step+'.log')).open('w') as f:subprocess.run(args,cwd=d,stdout=f,stderr=subprocess.STDOUT,check=True)
   xpm=bp.parent/'data/ip/xpm';libs=[] if unit else [str(xpm/'xpm_cdc/hdl/xpm_cdc.sv'),str(xpm/'xpm_memory/hdl/xpm_memory.sv')]
   run('compile',[str(bp/'xvlog'),'--sv',*libs,*[str(d/name) for name in names],str(d/'tb.sv')]);run('elaborate',[str(bp/'xelab'),'tb','-s','header_control_tb','-mt','off'])
   try:run('simulate',[str(bp/'xsim'),'header_control_tb','-runall'])
   except subprocess.CalledProcessError:pass
   log=(d/'simulate.log').read_text();assert witness in log and 'G0_BPSK_HEADER_PASS' not in log and 'G0_LIVE_HEADER_ACQUISITION_PASS' not in log,(label,log[-3000:])
   controls.append({'name':label,'detected_by':witness});print(json.dumps(controls[-1]),flush=True)
  result={'scope':__doc__,'reference':str(ref.relative_to(project)),'mode':a.mode,'negative_controls':controls,'source_sha256':hashes,'g0_passed':False};(out/'receipt.json').write_text(json.dumps(result,indent=2)+'\n')
 finally:(out/'disk-after.json').write_text(json.dumps(disk_check('after-header-'+a.mode+'-controls'),indent=2)+'\n')
if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);p.add_argument('--reference',type=Path,required=True);p.add_argument('--mode',choices=['unit','live'],required=True);p.add_argument('--vivado-bin',type=Path,required=True);main(p.parse_args())
