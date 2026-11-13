#!/usr/bin/env python3
"""Exercise actual C compaction on saved physical DMA, with independent readback."""
import argparse
import hashlib
import json
from pathlib import Path
import struct
import subprocess
from g0_source_compact import HEADER,batches
from dma_batch_stream_probe import decode_batch

def main(a):
    v=Path(__file__).resolve().parents[1];out=a.output.resolve();out.mkdir(parents=True,exist_ok=False)
    # Include the real writer in the same translation unit, not a copied model.
    harness='''#define main source_device_main
#include "'''+str(v/'ps/g0_source_ring_probe.c')+'''"
#undef main
int main(int argc,char **argv){
 if(argc!=3)return 2;
 FILE *in=fopen(argv[1],"rb"),*out=fopen(argv[2],"wb");if(!in||!out)return 3;
 const uint8_t header[16]={'V','3','B','C',1,0,16,0,0,128,0,0,0,0,0,0};
 if(fwrite(header,1,16,out)!=16)return 4;
 uint8_t raw[V3B_BYTES],packed[V3B_BYTES+16];uint64_t n=0;
 for(;;){size_t got=fread(raw,1,sizeof(raw),in);if(!got)break;if(got!=sizeof(raw))return 5;
  size_t size=compact_batch(packed,raw,1000000000ULL+(++n)*1024000ULL);
  if(fwrite(packed,1,size,out)!=size)return 6;
 }
 return fclose(in)||fclose(out);
}
'''
    (out/'harness.c').write_text(harness)
    subprocess.run(['gcc','-std=c11','-O1','-g','-Wall','-Wextra','-Werror',
                    '-fsanitize=address,undefined','-fno-omit-frame-pointer',
                    str(out/'harness.c'),str(v/'ps/g0_dma_batch.c'),'-o',str(out/'harness')],check=True)
    compact=out/'physical.v3bc'
    subprocess.run([str(out/'harness'),str(a.fixture.resolve()),str(compact)],check=True)
    n=records=0;digest=hashlib.sha256()
    with a.fixture.open('rb') as source:
        for timestamp,restored in batches(compact):
            original=source.read(32768);assert len(original)==32768
            # Existing independent full-DMA oracle validates both boundaries
            # and record bytes; arbitrary inert padding is intentionally absent.
            before,meta=decode_batch(original,n);after,meta2=decode_batch(restored,n)
            assert before==after and meta==meta2
            assert original[:32]==restored[:32] and original[-32:]==restored[-32:]
            for record in after:digest.update(record)
            n+=1;records+=len(after)
        assert not source.read(1)
    assert n and records
    # Invalid DMA metadata must survive verbatim, enabling later validation.
    malformed=bytearray(a.fixture.read_bytes()[:32768]);malformed[32740:32744]=struct.pack('<I',32705)
    (out/'invalid.raw').write_bytes(malformed)
    subprocess.run([str(out/'harness'),str(out/'invalid.raw'),str(out/'invalid.v3bc')],check=True)
    assert list(batches(out/'invalid.v3bc'))[0][1]==malformed
    # Independent file-level corruption controls must fail with bounded reads.
    data=compact.read_bytes();bad=[data[:15],data[:-1],data[:16]+struct.pack('<IIQ',32776,0,1),
                                 HEADER+struct.pack('<IIQ',64,1,1)+bytes(64),
                                 HEADER+struct.pack('<IIQ',64,0,0)+bytes(64)]
    for i,content in enumerate(bad):
        p=out/f'bad-{i}.v3bc';p.write_bytes(content)
        try:list(batches(p))
        except ValueError:pass
        else:raise AssertionError('invalid compact stream accepted')
    result={'actual_C_ASan_UBSan_passed':True,'physical_fixture':str(a.fixture.resolve()),
            'physical_fixture_sha256':hashlib.sha256(a.fixture.read_bytes()).hexdigest(),
            'batches':n,'records':records,'record_bytes_sha256':digest.hexdigest(),
            'raw_bytes':a.fixture.stat().st_size,'compact_bytes':compact.stat().st_size,
            'malformed_DMA_full_batch_preserved':True,'container_corruptions_rejected':len(bad),
            'new_native_long_run_or_RF_passed':False}
    (out/'receipt.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result),flush=True)

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--fixture',type=Path,required=True);main(p.parse_args())
