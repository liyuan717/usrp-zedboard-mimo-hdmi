#!/usr/bin/env python3
"""Exercise the actual compiled IQ reader without opening a USRP."""
import argparse,json,os,struct,subprocess,threading,time
from pathlib import Path

def main(a):
    out=a.output.resolve();out.mkdir(exist_ok=False);binary=str(a.binary.resolve());results=[]
    def run(name,path,expected=0,args=(),timeout=35):
        start=time.monotonic()
        r=subprocess.run([binary,'--iq',str(path),*args],capture_output=True,text=True,timeout=timeout)
        (out/(name+'.log')).write_text(r.stdout+r.stderr)
        assert r.returncode==expected,(name,r.stdout,r.stderr)
        results.append({'case':name,'exit':r.returncode,'elapsed_s':time.monotonic()-start,'output':r.stdout+r.stderr})
    good=struct.pack('<4f',.01,-.02,.03,-.04)*37
    for name,data,code in [('valid',good,0),('nan',struct.pack('<4f',float('nan'),0,0,0),2),
                           ('amplitude',struct.pack('<4f',.13,0,0,0),2),('partial',good+b'X',2)]:
        p=out/(name+'.iq');p.write_bytes(data);run(name,p,code)
    fifo=out/'split.fifo';os.mkfifo(fifo)
    errors=[]
    def writer():
        try:
            with fifo.open('wb',buffering=0) as f:
                for i in range(0,len(good),7):f.write(good[i:i+7])
        except BaseException as error:errors.append(repr(error))
    t=threading.Thread(target=writer,daemon=True);t.start();run('split-FIFO',fifo);t.join(2);assert not t.is_alive() and not errors
    # No writer ever connects: poll must expire, not spin, accept EOF or hang.
    idle=out/'idle.fifo';os.mkfifo(idle);run('idle-timeout',idle,2,timeout=35)
    assert 29<=results[-1]['elapsed_s']<35 and 'timed out' in results[-1]['output']
    for name,extra in [('zero',('--max-samples','0')),('over-limit',('--max-samples','256000001')),
                       ('gain',('--max-samples','1','--gain-db','19')),
                       ('frequency',('--max-samples','1','--freq-hz','5400000000'))]:
        run(name,out/'nonexistent',2,('--enable-tx',*extra))
        assert 'cannot open IQ' not in results[-1]['output']
    run('finite-limit-admitted-input-missing',out/'nonexistent',2,('--enable-tx','--max-samples','256000000'))
    assert 'cannot open IQ input' in results[-1]['output']
    proof={'actual_compiled_reader_cases':results,'all_passed':True,'USRP_opened':False,'RF_emitted':False,
           'RF_underflow_EOB_or_capacity_passed':False}
    (out/'receipt.json').write_text(json.dumps(proof,indent=2)+'\n');print(json.dumps(proof),flush=True)

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--binary',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True);main(p.parse_args())
