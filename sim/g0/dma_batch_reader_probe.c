/* Independent host/ARM driver for the bounded PS batch adapter. */
#define _POSIX_C_SOURCE 200809L
#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "g0_dma_batch.h"
struct oracle { uint32_t crc;unsigned drop_first; };
static uint32_t table[256];
static int consume(void *opaque,const uint8_t *record,uint32_t bytes,uint16_t kind)
{
 struct oracle *o=opaque;(void)kind;
 if(o->drop_first){o->drop_first=0;return 0;}
 for(uint32_t p=0;p<bytes;p++)o->crc=(o->crc>>8)^table[(o->crc^record[p])&255];
 return 1;
}
static uint32_t le32(const uint8_t *p){return (uint32_t)p[0]|(uint32_t)p[1]<<8|(uint32_t)p[2]<<16|(uint32_t)p[3]<<24;}
int main(int argc,char **argv)
{
 if(argc!=2)return 2;
 FILE *f=fopen(argv[1],"rb");if(!f)return 3;
 for(unsigned i=0;i<256;i++){uint32_t c=i;for(unsigned p=0;p<8;p++)c=(c>>1)^((c&1)?0xedb88320:0);table[i]=c;}
 struct v3b_reader state={0};struct oracle o={~0U,0};uint8_t header[8],batch[V3B_BYTES];unsigned index=0;
 while(fread(header,1,8,f)==8){
  if(fread(batch,1,sizeof(batch),f)!=sizeof(batch))return 4;
  uint32_t supplied=le32(header),drop=le32(header+4);size_t allocated=supplied>V3B_BYTES?1:supplied;
  uint8_t *p=allocated?malloc(allocated):NULL;if(allocated&&!p)return 5;
  if(allocated)memcpy(p,batch,allocated);
  o.drop_first=drop;uint64_t before=state.records;
  unsigned flags=v3b_consume(&state,p,supplied,consume,&o);
  printf("CASE %u flags=%u records=%"PRIu64" crc=%08"PRIx32"\n",index++,flags,state.records-before,~o.crc);free(p);
 }
 if(ferror(f))return 6;
 fclose(f);
 printf("TOTAL batches=%"PRIu64" records=%"PRIu64" bad_metadata=%"PRIu64" bad_records=%"PRIu64" duplicate=%"PRIu64" late=%"PRIu64" missing=%"PRIu64" damaged=%"PRIu64" unknown=%"PRIu64" consumer_drop=%"PRIu64" crc=%08"PRIx32"\n",state.batches,state.records,state.bad_metadata,state.bad_records,state.duplicate_batches,state.late_batches,state.missing_batches,state.damaged_batches,state.unknown_records,state.consumer_dropped,~o.crc);
 return 0;
}
