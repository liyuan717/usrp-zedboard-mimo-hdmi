#!/usr/bin/env python3
"""Compile actual BRAM address/phase faults against independent direct127 oracle."""
import argparse,hashlib,json,shutil,subprocess
from pathlib import Path
from workspace_storage_guard import check,GIB
NAME='differential_corr_cost_probe.sv'
FAULTS=[
 ('wrong_write_cursor','u_write?active_cursor[5:0]:',"u_write?(active_cursor[5:0]+6'd1):"),
 ('wrong_old_u_address','fire?write_cursor[5:0]:tap_a',"fire?6'd0:tap_a"),
 ('wrong_second_read_tap','((phase-3)*16+bank*8+4);','((phase-3)*16+bank*8+8);'),
 ('missing_last_read','if(u_tap_read) b_q<=','if(u_tap_read && phase!=6) b_q<=')]
def main(a):
    out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);ref=a.reference.resolve();bp=a.vivado_bin.resolve()
    c=check('before-BRAM-correlation-fault-controls');(out/'disk-before.json').write_text(json.dumps(c,indent=2));assert c['disk']['available_bytes']>=5*GIB
    try:
        pos=json.loads((ref/'receipt.json').read_text());assert pos['input_word_bits']==33 and pos['correlations_compared']>=3000
        source=(ref/NAME).read_text();rows=[]
        for name,old,new in FAULTS:
            assert source.count(old)==1,(name,source.count(old));p=out/name;p.mkdir();(p/NAME).write_text(source.replace(old,new))
            for f in ('tb.sv','input.txt','expected.txt'):shutil.copyfile(ref/f,p/f)
            for label,args in [('compile',[bp/'xvlog','--sv',NAME,'tb.sv']),('elaborate',[bp/'xelab','tb','-s','fault_tb','-mt','off']),('simulate',[bp/'xsim','fault_tb','-runall'])]:
                with (p/(label+'.log')).open('w') as f:subprocess.run([str(x) for x in args],cwd=p,stdout=f,stderr=subprocess.STDOUT,check=True)
            s=(p/'simulate.log').read_text();assert 'Fatal: correlation mismatch' in s and 'G0_DIFF_CORR_PASS' not in s,(name,s[-1200:])
            rows.append({'fault':name,'compiled':True,'rejected_by_independent_data_oracle':True,'reason':next(s for s in s.splitlines() if s.startswith('Fatal:'))})
        result={'scope':__doc__,'source_sha256':hashlib.sha256((ref/NAME).read_bytes()).hexdigest(),'controls':rows,'full_G0_passed':False};(out/'receipt.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result),flush=True)
    finally:(out/'disk-after.json').write_text(json.dumps(check('after-BRAM-correlation-fault-controls'),indent=2)+'\n')
if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for n in ('output','reference','vivado-bin'):p.add_argument('--'+n,type=Path,required=True)
    main(p.parse_args())
