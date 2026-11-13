/* G0 bounded batch adapter: no RF algorithms, allocation or whole-AU gate. */
#include "include/g0_dma_batch.h"
#include <string.h>
static uint16_t le16(const uint8_t *p){return (uint16_t)p[0]|(uint16_t)p[1]<<8;}
static uint32_t le32(const uint8_t *p){return (uint32_t)le16(p)|(uint32_t)le16(p+2)<<16;}
static uint64_t le64(const uint8_t *p){return (uint64_t)le32(p)|(uint64_t)le32(p+4)<<32;}
size_t v3b_copy_owned(void *saved,const void *memory,size_t bytes)
{
 uint8_t *out=saved;const uint8_t *in=memory;uint32_t valid;
 if(!out || !in || bytes!=V3B_BYTES)return 0;
 memcpy(out,in,32);memcpy(out+32736,in+32736,32);
 valid=le32(out+32740);
 /* Validate only the copy bound here. The unchanged consumer rejects every
  * other malformed metadata condition before reading records/calling out. */
 if(valid>32704 || (valid&7))return 64;
 memcpy(out+32,in+32,valid);
 return 64+valid;
}
unsigned v3b_consume(struct v3b_reader *s,const void *memory,size_t bytes,v3b_record_fn callback,void *opaque)
{
 const uint8_t *p=memory;uint32_t valid,count,flags,position,end,seen=0;uint64_t sequence,delta;unsigned result=0;
 if(bytes!=V3B_BYTES || !p)goto bad_metadata;
 if(le32(p)!=0x42443356 || le16(p+4)!=1 || le16(p+6)!=32 || le32(p+8)!=V3B_BYTES)goto bad_metadata;
 if(le32(p+32736)!=0x45443356)goto bad_metadata;
 valid=le32(p+32740);count=le32(p+32744);flags=le32(p+32748);sequence=le64(p+16);
 if(valid>32704 || (valid&7) || sequence!=le64(p+32752) || count>511)goto bad_metadata;
 if(s->seen_sequence){
  delta=sequence-s->last_sequence;
  if(!delta){s->duplicate_batches++;return V3B_DUPLICATE;}
  if(delta>>63){s->late_batches++;return V3B_LATE_BATCH;}
  if(delta!=1){s->missing_batches+=delta-1;result|=V3B_SEQUENCE_GAP;}
 }
 s->seen_sequence=1;s->last_sequence=sequence;s->batches++;
 if(flags&1){s->damaged_batches++;result|=V3B_DAMAGED_PREFIX;}
 position=32;end=position+valid;
 while(position<end){
  uint32_t size;uint16_t kind,payload;const uint8_t *record=p+position;
  if(end-position<64 || le32(record)!=0x58523356)goto bad_record_prefix;
  size=le32(record+8);
  if(size<64 || size>576 || (size&7) || size>end-position)goto bad_record_prefix;
  seen++;kind=le16(record+12);payload=le16(record+54);
  if(le16(record+4)!=1 || le16(record+6)!=64 || payload>512 || 64+((payload+7U)&~7U)!=size ||
     (kind==1 && payload!=512) || (kind==2 && payload!=0) || (kind==4 && payload!=16)){
   s->bad_records++;result|=V3B_BAD_RECORD;
  }else if(kind<1 || kind>5){
   /* Unknown noncritical record types are skipped by their bounded size. */
   s->unknown_records++;
  }else if(callback && !callback(opaque,record,size,kind)){
   s->consumer_dropped++;
  }else s->records++;
  position+=size;
 }
 if(seen!=count){s->bad_records++;result|=V3B_BAD_RECORD;}
 return result;
bad_record_prefix:
 /* Earlier complete records remain useful; never reinterpret later bytes
  * after a broken length. The next DMA batch is independently recoverable. */
 s->bad_records++;return result|V3B_BAD_RECORD;
bad_metadata:
 s->bad_metadata++;return V3B_BAD_METADATA;
}
