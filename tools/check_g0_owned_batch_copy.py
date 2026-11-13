#!/usr/bin/env python3
"""Compare private-prefix snapshots with full copies on actual DMA and malformed boundaries."""
import argparse,hashlib,json,subprocess
from pathlib import Path
C=r'''#include "ps/include/g0_dma_batch.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
struct summary {unsigned long long bytes,records,hash;};
static int collect(void *p,const uint8_t *r,uint32_t n,uint16_t k){
 struct summary *s=p;s->records++;s->bytes+=n;
 for(uint32_t i=0;i<n;i++)s->hash=(s->hash^r[i])*1099511628211ULL;
 (void)k;return 1;
}
static uint32_t u32(const uint8_t *p){return (uint32_t)p[0]|(uint32_t)p[1]<<8|(uint32_t)p[2]<<16|(uint32_t)p[3]<<24;}
static void put(uint8_t *p,uint32_t v){for(unsigned i=0;i<4;i++)p[i]=(uint8_t)(v>>(8*i));}
static unsigned long long total=0,full=0,cases=0;
static void compare(uint8_t *in,struct v3b_reader *a,struct v3b_reader *b){
 uint8_t saved[V3B_BYTES];memset(saved,0xa7,sizeof saved);
 uint32_t valid=u32(in+32740);size_t copied=v3b_copy_owned(saved,in,V3B_BYTES);
 assert(copied>=64&&copied<=V3B_BYTES);
 assert(!memcmp(in,saved,32)&&!memcmp(in+32736,saved+32736,32));
 if(valid<=32704&&!(valid&7))assert(!memcmp(in+32,saved+32,valid));
 struct summary x={0},y={0};unsigned first=v3b_consume(a,in,V3B_BYTES,collect,&x);
 /* RELEASE/reuse is simulated before consuming the private snapshot. */
 memset(in,0xdd,V3B_BYTES);
 unsigned second=v3b_consume(b,saved,V3B_BYTES,collect,&y);
 assert(first==second&&!memcmp(a,b,sizeof *a)&&!memcmp(&x,&y,sizeof x));
 if(first&V3B_BAD_METADATA)assert(y.records==0);
 total+=copied;full+=V3B_BYTES;cases++;
}
int main(int argc,char **argv){
 assert(argc==2);FILE *f=fopen(argv[1],"rb");assert(f);uint8_t h[16],body[V3B_BYTES],batch[V3B_BYTES];
 assert(fread(h,1,16,f)==16&&!memcmp(h,"V3BC",4));struct v3b_reader a={0},b={0};
 while(fread(h,1,16,f)==16){uint32_t n=u32(h);assert(n>=64&&n<=V3B_BYTES);assert(fread(body,1,n,f)==n);
  memset(batch,0,sizeof batch);if(n==V3B_BYTES)memcpy(batch,body,n);else{memcpy(batch,body,n-32);memcpy(batch+32736,body+n-32,32);}
  compare(batch,&a,&b);
 }
 assert(feof(f));fclose(f);
 /* Copy limits include zero/max, values just outside bounds, wrap and
  * misalignment. Header/trailer/sequence and record faults must still reject
  * locally and never publish stale bytes from the previous saved batch. */
 const uint32_t sizes[]={0,8,64,576,32704,32705,32712,0xffffffffU};
 for(unsigned s=0;s<sizeof sizes/sizeof *sizes;s++)for(unsigned fault=0;fault<9;fault++){
  memset(batch,0,sizeof batch);memcpy(batch,"V3DB",4);batch[4]=1;batch[6]=32;put(batch+8,V3B_BYTES);memcpy(batch+32736,"V3DE",4);put(batch+32740,sizes[s]);
  if(fault==1)batch[0]^=1;if(fault==2)batch[4]=2;if(fault==3)batch[6]=31;
  if(fault==4)put(batch+8,1);if(fault==5)batch[32736]^=1;
  if(fault==6)batch[32752]=1;if(fault==7)put(batch+32744,512);if(fault==8)put(batch+32744,1);
  memset(&a,0,sizeof a);memset(&b,0,sizeof b);compare(batch,&a,&b);
 }
 assert(v3b_copy_owned(batch,body,0)==0&&v3b_copy_owned(batch,body,V3B_BYTES-1)==0);
 assert(v3b_copy_owned(NULL,body,V3B_BYTES)==0&&v3b_copy_owned(batch,NULL,V3B_BYTES)==0);
 printf("OWNED_BATCH_COPY_PASS cases=%llu copied_bytes=%llu full_copy_bytes=%llu\n",cases,total,full);return 0;
}
'''
def main(a):
 v=Path(__file__).resolve().parents[1];out=a.output.resolve();out.mkdir(exist_ok=False)
 (out/'check.c').write_text(C)
 cmd=['gcc','-std=c11','-O2','-g','-fsanitize=address,undefined','-fno-omit-frame-pointer','-I'+str(v),str(out/'check.c'),str(v/'ps/g0_dma_batch.c'),'-o',str(out/'check')]
 subprocess.run(cmd,check=True)
 r=subprocess.run([str(out/'check'),str(a.capture.resolve())],capture_output=True,text=True,check=True)
 (out/'check.log').write_text(r.stdout+r.stderr)
 assert 'OWNED_BATCH_COPY_PASS' in r.stdout
 result={'scope':__doc__,'passed':True,'ASan_UBSan':True,'actual_capture':str(a.capture),'actual_capture_sha256':hashlib.sha256(a.capture.read_bytes()).hexdigest(),'summary':r.stdout.strip(),'source_sha256':{p:hashlib.sha256((v/p).read_bytes()).hexdigest() for p in ['ps/g0_dma_batch.c','ps/include/g0_dma_batch.h']},'RF_or_native_performance_passed':False}
 (out/'receipt.json').write_text(json.dumps(result,indent=2)+'\n');print(r.stdout,flush=True)
if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--capture',type=Path,required=True);p.add_argument('--output',type=Path,required=True);main(p.parse_args())
