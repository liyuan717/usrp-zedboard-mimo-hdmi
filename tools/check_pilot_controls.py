#!/usr/bin/env python3
"""Compiled numerical/protocol mutation witnesses for pilot arithmetic/tracking."""
import argparse,hashlib,json,os,shutil,subprocess
from pathlib import Path
from workspace_storage_guard import check as disk_check,GIB
from pilot_math_probe import SOURCES as MATH
from pilot_tracking_probe import SOURCES as TRACKING

def main(a):
 project=Path(__file__).resolve().parents[1];out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);ref=a.reference.resolve();bp=a.vivado_bin.resolve()
 before=disk_check('before-pilot-'+a.mode+'-controls');(out/'disk-before.json').write_text(json.dumps(before,indent=2)+'\n')
 if before['disk']['available_bytes']<5*GIB:raise SystemExit('Under5GiB: handle storage first.')
 try:
  positive=json.loads((ref/'receipt.json').read_text())
  if a.mode=='math':
   names=MATH;hashes=positive['source_sha256'];assert positive['simulation'].startswith('G0_PILOT_MATH_PASS')
   changes={
    'imaginary_rotation_sign':('bpsk_header_cost_probe.sv','pilot_imag<=pilot_imag+rotation_partial;','pilot_imag<=pilot_imag-rotation_partial;','Fatal: pilot full complex oracle'),
    'normalization_rounding':('pilot_angle_cost_probe.sv','+bias)',"+70'sd0)",'Fatal: pilot normalized phase oracle'),
    'unequal_component_shift':('pilot_angle_cost_probe.sv','imag_value})+bias)>>>shift;','imag_value})+bias)>>>(shift+1);','Fatal: pilot normalized phase oracle')}
  elif a.mode=='smoothing':
   names=['pilot_smoothing_cost_probe.sv'];hashes={names[0]:positive['source_sha256']};assert positive['simulation'].startswith('G0_PILOT_SMOOTHING_PASS')
   changes={
    'edge_fit_removed':(names[0],'numerator=(sum<<<2)+fit_product;','numerator=(sum<<<2);','Fatal: pilot smoothing Fraction oracle'),
    'negative_half_tie':(names[0],"-$signed(numerator)+49'd13:","-$signed(numerator)+49'd14:",'Fatal: pilot smoothing Fraction oracle'),
    'generation_high_bit':(names[0],'out_generation<=cfg_generation;',"out_generation<=cfg_generation & 32'h7fffffff;",'Fatal: pilot smoothing Fraction oracle'),
    'premature_expiry':(names[0],'age==FRAME_LIMIT-1;','age==FRAME_LIMIT-2;','Fatal: pilot smoothing census')}
  else:
   names=TRACKING;hashes=positive['source_sha256'];assert positive['simulation'].startswith('G0_PILOT_TRACKING_PASS')
   changes={
    'live_sideband_instead_of_owner':('pilot_tracking_cost_probe.sv','.in_generation(owner_generation)','.in_generation(cfg_generation)','Fatal: joint pilot unexpected frame abort'),
    'abort_fence_removed':('pilot_tracking_cost_probe.sv','wire fence=cancel || frame_abort;','wire fence=cancel;','Fatal: joint pilot abort/fence census')}
  for name in names:assert hashlib.sha256((ref/name).read_bytes()).hexdigest()==hashes[name]==hashlib.sha256((project/'sim/g0'/name).read_bytes()).hexdigest(),name
  controls=[]
  for label,(target,old,new,witness) in changes.items():
   d=out/label;d.mkdir()
   for p in ref.glob('*.hex'):os.link(p,d/p.name)
   for name in names:shutil.copyfile(ref/name,d/name)
   shutil.copyfile(ref/'tb.sv',d/'tb.sv');p=d/target;s=p.read_text();assert s.count(old)==(2 if label=='normalization_rounding' else 1),(label,old);p.write_text(s.replace(old,new))
   def run(step,args):
    with (d/(step+'.log')).open('w') as f:subprocess.run(args,cwd=d,stdout=f,stderr=subprocess.STDOUT,check=True)
   run('compile',[str(bp/'xvlog'),'--sv',*[str(d/name) for name in names],str(d/'tb.sv')]);run('elaborate',[str(bp/'xelab'),'tb','-s','pilot_control_tb','-mt','off'])
   try:run('simulate',[str(bp/'xsim'),'pilot_control_tb','-runall'])
   except subprocess.CalledProcessError:pass
   log=(d/'simulate.log').read_text();assert witness in log and 'G0_PILOT_MATH_PASS' not in log and 'G0_PILOT_SMOOTHING_PASS' not in log and 'G0_PILOT_TRACKING_PASS' not in log,(label,log[-3000:])
   controls.append({'name':label,'detected_by':next(s for s in log.splitlines() if s.startswith(witness))});print(json.dumps(controls[-1]),flush=True)
  result={'scope':__doc__,'reference':str(ref.relative_to(project)),'mode':a.mode,'negative_controls':controls,'source_sha256':hashes,'g0_passed':False};(out/'receipt.json').write_text(json.dumps(result,indent=2)+'\n')
 finally:(out/'disk-after.json').write_text(json.dumps(disk_check('after-pilot-'+a.mode+'-controls'),indent=2)+'\n')
if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);p.add_argument('--reference',type=Path,required=True);p.add_argument('--mode',choices=['math','smoothing','tracking'],required=True);p.add_argument('--vivado-bin',type=Path,required=True);main(p.parse_args())
