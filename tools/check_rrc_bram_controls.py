#!/usr/bin/env python3
"""Compile RRC ring/bypass/prefetch faults against the independent41-tap oracle."""
import argparse,hashlib,json,shutil,subprocess
from pathlib import Path
from workspace_storage_guard import check,GIB
NAME='rrc_cost_probe.sv'
FAULTS=[('wrong_ring_wrap',"history_head==40?6'd0", "history_head==39?6'd0"),
 ('drop_first_round_bypass','phase==1?recent[slot]:history_left[slot]','history_left[slot]'),
 ('wrong_prefetch_phase','next_tap=phase*3+bank','next_tap=(phase-1)*3+bank'),
 ('wrong_mirrored_tap',"6'd40-next_tap", "6'd39-next_tap")]
def main(a):
    out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);ref=a.reference.resolve();bp=a.vivado_bin.resolve()
    c=check('before-RRC-BRAM-fault-controls');(out/'disk-before.json').write_text(json.dumps(c,indent=2));assert c['disk']['available_bytes']>=5*GIB
    try:
        positive=json.loads((ref/'receipt.json').read_text());assert positive['integer_component_comparisons']>=24000
        source=(ref/NAME).read_text();rows=[];xpm=bp.parent/'data/ip/xpm'
        for name,old,new in FAULTS:
            assert source.count(old)==1,(name,source.count(old));p=out/name;p.mkdir();(p/NAME).write_text(source.replace(old,new))
            for f in ('tb.sv','input.txt','expected.txt','rrc.hex'):shutil.copyfile(ref/f,p/f)
            for label,args in [('compile',[bp/'xvlog','--sv',xpm/'xpm_cdc/hdl/xpm_cdc.sv',xpm/'xpm_memory/hdl/xpm_memory.sv',NAME,'tb.sv']),('elaborate',[bp/'xelab','tb','-s','fault_tb','-mt','off']),('simulate',[bp/'xsim','fault_tb','-runall'])]:
                with (p/(label+'.log')).open('w') as f:subprocess.run([str(x) for x in args],cwd=p,stdout=f,stderr=subprocess.STDOUT,check=True)
            s=(p/'simulate.log').read_text();assert 'Fatal: RRC mismatch' in s and 'G0_RRC_PASS' not in s,(name,s[-1500:])
            rows.append({'fault':name,'compiled':True,'rejected_by_independent_oracle':True,'reason':next(x for x in s.splitlines() if x.startswith('Fatal:'))})
        result={'scope':__doc__,'source_sha256':hashlib.sha256((ref/NAME).read_bytes()).hexdigest(),'controls':rows,'full_G0_passed':False};(out/'receipt.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result),flush=True)
    finally:(out/'disk-after.json').write_text(json.dumps(check('after-RRC-BRAM-fault-controls'),indent=2)+'\n')
if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for n in ('output','reference','vivado-bin'):p.add_argument('--'+n,type=Path,required=True)
    main(p.parse_args())
