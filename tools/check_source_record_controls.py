#!/usr/bin/env python3
"""Compile real faults in ignored copies; prove the atomic-record tests reject them."""
import argparse,hashlib,json,shutil,subprocess
from pathlib import Path
from source_record_bridge_probe import SOURCE
from workspace_storage_guard import check as disk_check,GIB

FAULTS=[
 ('revoke_held_record_on_fence','    default:state<=IDLE;\n   endcase','    default:state<=IDLE;\n   endcase\n   if(fence)state<=IDLE;','atomic AXIS held beat changed'),
 ('unbounded_admission','admission_age<=admission_age+1\'b1;','admission_age<=admission_age;','unbounded source admission'),
 ('commit_short_source','source_last==(rx_next==511)',"1'b1",'bridge final census'),
]

def main(a):
    v=Path(__file__).resolve().parents[1];out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);ref=a.reference.resolve();bp=a.vivado_bin.resolve()
    check=disk_check('before-source-record-controls');(out/'disk-before.json').write_text(json.dumps(check,indent=2)+'\n');assert check['disk']['available_bytes']>=5*GIB
    try:
        positive=json.loads((ref/'receipt.json').read_text());content=(v/'sim/g0'/SOURCE).read_text()
        assert hashlib.sha256(content.encode()).hexdigest()==positive['source_sha256']
        results=[]
        for name,old,new,reason in FAULTS:
            assert content.count(old)==1,(name,content.count(old));job=out/name;job.mkdir()
            (job/SOURCE).write_text(content.replace(old,new));shutil.copyfile(ref/'tb.sv',job/'tb.sv')
            def run(label,args,check=True):
                with (job/(label+'.log')).open('w') as log:return subprocess.run(args,cwd=job,stdout=log,stderr=subprocess.STDOUT,check=check).returncode
            run('compile',[str(bp/'xvlog'),'--sv',SOURCE,'tb.sv']);run('elaborate',[str(bp/'xelab'),'tb','-s','fault_tb','-mt','off'])
            code=run('simulate',[str(bp/'xsim'),'fault_tb','-runall'],False);log=(job/'simulate.log').read_text()
            assert reason in log and 'G0_SOURCE_RECORD_BRIDGE_PASS' not in log,(name,log[-3000:])
            results.append({'fault':name,'compiled':True,'exit_code':code,'expected_failure':reason,'rejected':True})
        receipt={'scope':__doc__,'reference':str(ref.relative_to(v)),'source_sha256':positive['source_sha256'],'controls':results,'g0_passed':False}
        (out/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n');print(json.dumps(receipt),flush=True)
    finally:(out/'disk-after.json').write_text(json.dumps(disk_check('after-source-record-controls'),indent=2)+'\n')

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for n in ['output','reference','vivado-bin']:p.add_argument('--'+n,type=Path,required=True)
    main(p.parse_args())
