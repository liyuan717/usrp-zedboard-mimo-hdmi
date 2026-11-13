#!/usr/bin/env python3
"""Build the finite SOURCE reader; compilation alone is not native DMA proof."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
from workspace_storage_guard import check, GIB

def main(a):
    v=Path(__file__).resolve().parents[1]
    out=a.output.resolve();out.mkdir(parents=True,exist_ok=False)
    before=check('before-SOURCE-client-build')
    (out/'disk-before.json').write_text(json.dumps(before,indent=2)+'\n')
    assert before['disk']['available_bytes']>=5*GIB
    try:
        sources=['ps/g0_source_ring_probe.c','ps/g0_dma_batch.c',
                 'ps/include/g0_dma_batch.h','ps/include/g0_source_ring.h']
        with (out/'build.log').open('w') as log:
            subprocess.run([a.cross_prefix+'gcc','-O2','-std=c11','-Wall','-Wextra',
                            '-Werror','-static',str(v/sources[0]),str(v/sources[1]),
                            '-o',str(out/'g0_source_ring_probe')],check=True,stdout=log,stderr=subprocess.STDOUT)
        result={'ARM_static_compiled':True,'native_or_physical_DMA_passed':False,
                'source_sha256':{p:hashlib.sha256((v/p).read_bytes()).hexdigest() for p in sources}}
        (out/'receipt.json').write_text(json.dumps(result,indent=2)+'\n')
        print(json.dumps(result),flush=True)
    finally:
        (out/'disk-after.json').write_text(json.dumps(check('after-SOURCE-client-build'),indent=2)+'\n')

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--cross-prefix',required=True)
    main(p.parse_args())
