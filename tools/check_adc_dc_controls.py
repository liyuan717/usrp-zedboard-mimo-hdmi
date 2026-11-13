#!/usr/bin/env python3
"""Compile actual DC rounding/ownership faults; independent vectors must reject."""
import argparse,hashlib,json,shutil,subprocess
from pathlib import Path
from workspace_storage_guard import check,GIB
SOURCE='adc_dc_calibration_cost_probe.sv'
FAULTS=[
 ('negative_floor','-$signed(next_q[16:0])-(reduced!=0)','-$signed(next_q[16:0])'),
 ('ties_away','fraction_tie[k] && difference[0]','fraction_tie[k]'),
 ('tag_epoch_ignored','upstream_loss || tag_bad || expired','upstream_loss || expired'),
 ('no_setup_guard','state==SETTLE && seen>=N+GUARD','state==SETTLE'),
 ('range_wrap','if(overflow)begin','if(1\'b0)begin'),
 ('wrong_tag_wrap',"{raw_tag[63:48],raw_tag[47:0]+48'd1}","raw_tag+64'd1")]
def main(a):
 r=Path(__file__).resolve().parents[1];out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);ref=a.reference.resolve();bp=a.vivado_bin.resolve()
 c=check('before-DC-fault-controls');(out/'disk-before.json').write_text(json.dumps(c,indent=2));assert c['disk']['available_bytes']>=5*GIB
 try:
  text=(r/'sim/g0'/SOURCE).read_text();positive=json.loads((ref/'receipt.json').read_text());assert hashlib.sha256(text.encode()).hexdigest()==positive['source_sha256']
  results=[]
  cases=[('signed32_sum_overflow','reg signed [32:0] sums','reg signed [31:0] sums')] if a.wide_sum_only else FAULTS
  for fault,old,new in cases:
   assert text.count(old)==1,(fault,text.count(old));job=out/fault;job.mkdir();(job/SOURCE).write_text(text.replace(old,new));shutil.copyfile(ref/'tb.sv',job/'tb.sv');shutil.copyfile(ref/'vectors.txt',job/'vectors.txt')
   for label,args in [('compile',[bp/'xvlog','--sv',SOURCE,'tb.sv']),('elaborate',[bp/'xelab','tb','-s','dc_tb','-mt','off']),('simulate',[bp/'xsim','dc_tb','-runall'])]:
    with (job/(label+'.log')).open('w') as f:subprocess.run([str(x) for x in args],cwd=job,stdout=f,stderr=subprocess.STDOUT,check=True)
   s=(job/'simulate.log').read_text();assert 'Fatal: DC oracle' in s and 'G0_ADC_DC_PASS' not in s,(fault,s[-1200:]);results.append({'fault':fault,'compiled':True,'rejected_by_mathematical_vector_comparison':True,'reason':next(x for x in s.splitlines() if x.startswith('Fatal:'))})
  receipt={'scope':__doc__,'source_sha256':positive['source_sha256'],'controls':results,'full_G0_passed':False};(out/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n');print(json.dumps(receipt),flush=True)
 finally:(out/'disk-after.json').write_text(json.dumps(check('after-DC-fault-controls'),indent=2)+'\n')
if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__)
 for n in ['output','reference','vivado-bin']:p.add_argument('--'+n,type=Path,required=True)
 p.add_argument('--wide-sum-only',action='store_true');main(p.parse_args())
