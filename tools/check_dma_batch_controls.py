#!/usr/bin/env python3
"""Compiled faults must fail whole-record/batch/held-beat tests, not compilation."""
import argparse,hashlib,json,os,subprocess
from pathlib import Path
from dma_batch_stream_probe import SOURCE,decode_batch
from workspace_storage_guard import check as disk_check,GIB
FAULTS=[
 ('cross_batch','else if(declared_bytes>space_bytes)',"else if(1'b0)",'carry not exercised'),
 ('commit_short',"s_axis_tlast!=(record_word==record_words-1'b1)","1'b0",'batch final census'),
 ('cancel_held','    default:state<=IDLE;\n   endcase','    default:state<=IDLE;\n   endcase\n   if(!run)state<=IDLE;','batch held AXIS changed'),
]

def main(a):
    v=Path(__file__).resolve().parents[1];out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);ref=a.reference.resolve();bp=a.vivado_bin.resolve()
    check=disk_check('before-DMA-batch-controls');(out/'disk-before.json').write_text(json.dumps(check,indent=2)+'\n');assert check['disk']['available_bytes']>=5*GIB
    try:
        positive=json.loads((ref/'receipt.json').read_text());source=(v/'sim/g0'/SOURCE).read_text();assert hashlib.sha256(source.encode()).hexdigest()==positive['source_sha256']
        results=[]
        for name,old,new,reason in FAULTS:
            if name=='cross_batch' and 'else if(declared_bytes[9:3]>space_words)' in source:
                old='else if(declared_bytes[9:3]>space_words)'
            assert source.count(old)==1,(name,source.count(old));job=out/name;job.mkdir();(job/SOURCE).write_text(source.replace(old,new));(job/'tb.sv').write_bytes((ref/'tb.sv').read_bytes())
            for p in ref.glob('*.hex'):os.link(p,job/p.name)
            def run(label,args,check=True):
                with (job/(label+'.log')).open('w') as log:return subprocess.run(args,cwd=job,stdout=log,stderr=subprocess.STDOUT,check=check).returncode
            run('compile',[str(bp/'xvlog'),'--sv',SOURCE,'tb.sv']);run('elaborate',[str(bp/'xelab'),'tb','-s','fault_tb','-mt','off']);rc=run('simulate',[str(bp/'xsim'),'fault_tb','-runall'],False)
            log=(job/'simulate.log').read_text();assert reason in log and 'G0_DMA_BATCH_STREAM_PASS' not in log,(name,log[-4000:])
            item={'fault':name,'compiled':True,'rejected':True,'reason':reason,'exit_code':rc}
            if name=='cross_batch':
                data=bytearray()
                for row in (job/'batches.txt').read_text().splitlines():
                    phase,word,last=row.split();data.extend(int(word,16).to_bytes(8,'little'))
                    if int(last):break
                try:decode_batch(bytes(data),0)
                except AssertionError as error:item['independent_boundary_rejection']=str(error)
                else:raise AssertionError('cross-boundary fault escaped independent parser')
            results.append(item)
        receipt={'scope':__doc__,'source_sha256':positive['source_sha256'],'controls':results,'physical_DMA_or_G0_passed':False}
        (out/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n');print(json.dumps(receipt),flush=True)
    finally:(out/'disk-after.json').write_text(json.dumps(disk_check('after-DMA-batch-controls'),indent=2)+'\n')

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for n in ['output','reference','vivado-bin']:p.add_argument('--'+n,type=Path,required=True)
    main(p.parse_args())
