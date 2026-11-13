#!/usr/bin/env python3
"""Negative witnesses on the passed live acquisition/timing/training snapshot."""
import argparse,hashlib,json,os,shutil,subprocess
from pathlib import Path
from tracking_acquisition_probe import SOURCES
from workspace_storage_guard import check as disk_check,GIB

def main(a):
 out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);ref=a.reference.resolve();bp=a.vivado_bin.resolve();project=Path(__file__).resolve().parents[1]
 before=disk_check('before-live-tracking-controls');(out/'disk-before.json').write_text(json.dumps(before,indent=2)+'\n')
 if before['disk']['available_bytes']<5*GIB:raise SystemExit('Under5GiB: handle storage first.')
 try:
  positive=json.loads((ref/'receipt.json').read_text());assert positive['simulation'].startswith('G0_LIVE_TRACKING_ACQUISITION_PASS')
  for name in SOURCES:assert hashlib.sha256((ref/name).read_bytes()).hexdigest()==positive['source_sha256'][name]==hashlib.sha256((project/'sim/g0'/name).read_bytes()).hexdigest()
  changes={
   'private512':('.EXTERNAL_HISTORY(1)) acquisition(','.EXTERNAL_HISTORY(0)) acquisition(','Fatal: live normal task error',False),
   'diagnostic_arm':('wire arm_valid=arm_pending && window_ready','wire arm_valid=arm_pending && result_ready && window_ready','Fatal: live timing arm blocked',False),
   'stale_reacquisition_origin':('search_valid?phase_origin:raw_tag[47:0]','phase_origin','Fatal: live coarse configuration',True)}
  controls=[];xpm=bp.parent/'data/ip/xpm'
  for name,(old,new,witness,only_recovery) in changes.items():
   d=out/name;d.mkdir()
   for p in ref.glob('*.hex'):os.link(p,d/p.name)
   for source in SOURCES:shutil.copyfile(ref/source,d/source)
   src=d/'tracking_acquiring_frontend_cost_probe.sv';text=src.read_text();assert text.count(old)==1,(name,old);src.write_text(text.replace(old,new))
   tb=(ref/'tb.sv').read_text()
   if only_recovery:
    oldloop='for(j=0;j<8;j=j+1)';assert tb.count(oldloop)==1;tb=tb.replace(oldloop,'for(j=7;j<8;j=j+1)')
    # Let the changed phase origin reach the real coarse/NCO arithmetic.
    # The positive test retains this direct wiring assertion; this negative
    # must fail on the independently computed configuration instead.
    direct='||dut.effective_origin!==rt[47:0]';assert tb.count(direct)==1;tb=tb.replace(direct,'')
   (d/'tb.sv').write_text(tb)
   def run(step,args):
    with (d/(step+'.log')).open('w') as f:subprocess.run(args,cwd=d,stdout=f,stderr=subprocess.STDOUT,check=True)
   run('compile',[str(bp/'xvlog'),'--sv',str(xpm/'xpm_cdc/hdl/xpm_cdc.sv'),str(xpm/'xpm_memory/hdl/xpm_memory.sv'),*[str(d/s) for s in SOURCES],str(d/'tb.sv')]);run('elaborate',[str(bp/'xelab'),'tb','-s','live_control_tb','-mt','off'])
   try:run('simulate',[str(bp/'xsim'),'live_control_tb','-runall'])
   except subprocess.CalledProcessError:pass
   log=(d/'simulate.log').read_text();assert witness in log and 'G0_LIVE_TRACKING_ACQUISITION_PASS' not in log,(name,log[-3000:])
   controls.append({'name':name,'detected_by':witness});print(json.dumps(controls[-1]),flush=True)
  result={'scope':__doc__,'reference':str(ref.relative_to(project)),'negative_controls':controls,'source_sha256':positive['source_sha256'],'g0_passed':False}
  (out/'receipt.json').write_text(json.dumps(result,indent=2)+'\n')
 finally:(out/'disk-after.json').write_text(json.dumps(disk_check('after-live-tracking-controls'),indent=2)+'\n')

if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);p.add_argument('--reference',type=Path,required=True);p.add_argument('--vivado-bin',type=Path,required=True);main(p.parse_args())
