/* Finite native G0 SOURCE DMA reader. Not the production video runtime.
 * RF packet errors remain local statistics; DMA lease/bounds errors stop safely.
 */
#define _POSIX_C_SOURCE 200809L
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <poll.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <time.h>
#include <unistd.h>
#include "include/g0_source_ring.h"
#include "include/g0_dma_batch.h"
static volatile sig_atomic_t interrupted;
static void stop_signal(int n){(void)n;interrupted=1;}
static uint64_t milliseconds(void){struct timespec t;if(clock_gettime(CLOCK_MONOTONIC,&t))return 0;return (uint64_t)t.tv_sec*1000+t.tv_nsec/1000000;}
static uint64_t nanoseconds(void){struct timespec t;if(clock_gettime(CLOCK_MONOTONIC,&t))return 0;return (uint64_t)t.tv_sec*1000000000+t.tv_nsec;}
static uint32_t le32(const uint8_t *p){return (uint32_t)p[0]|(uint32_t)p[1]<<8|(uint32_t)p[2]<<16|(uint32_t)p[3]<<24;}
static void put32(uint8_t *p,uint32_t x){for(unsigned i=0;i<4;i++)p[i]=(uint8_t)(x>>(8*i));}
static void put64(uint8_t *p,uint64_t x){for(unsigned i=0;i<8;i++)p[i]=(uint8_t)(x>>(8*i));}
/* Local evidence container only; RF and DMA ABIs are unchanged. Preserve the
 * original header, complete valid prefix and trailer; malformed metadata is
 * retained as a full batch. Copy before RELEASE, write after RELEASE. */
static size_t compact_batch(uint8_t *out,const uint8_t *batch,uint64_t arrival)
{
 uint32_t valid=le32(batch+32740),size=V3B_BYTES;
 if(le32(batch)==0x42443356 && le32(batch+32736)==0x45443356 && valid<=32704 && !(valid&7))size=valid+64;
 put32(out,size);put32(out+4,0);put64(out+8,arrival);
 if(size==V3B_BYTES)memcpy(out+16,batch,size);
 else{memcpy(out+16,batch,32+valid);memcpy(out+48+valid,batch+32736,32);}
 return size+16;
}
struct payload_check {uint64_t sources,restored,bad_sources,bytes;uint32_t crc;};
static int check_payload(void *opaque,const uint8_t *p,uint32_t size,uint16_t kind)
{
 struct payload_check *s=opaque;unsigned bad=0;
 if(kind==1){
  uint32_t b=le32(p+48);unsigned index=p[52]|(unsigned)p[53]<<8;
  for(unsigned o=0;o<512;o++)if(p[64+o]!=(uint8_t)(b*71+index*29+o*17+o/11+(b+1)*(o%13)))bad=1;
  if(bad){s->bad_sources++;return 0;}
  s->sources++;s->bytes+=512;if(p[14]&1)s->restored+=512;
 }
 for(uint32_t i=0;i<size;i++){s->crc^=p[i];for(unsigned j=0;j<8;j++)s->crc=(s->crc>>1)^((s->crc&1)?0xedb88320U:0);}
 return 1;
}
int main(int argc,char **argv)
{
 const unsigned compact=argc==5 && !strcmp(argv[4],"--compact");
 const unsigned check_clock=argc==5 && !strcmp(argv[4],"--clock");
 if(argc!=4 && !compact && !check_clock){fprintf(stderr,"usage: source-ring-probe BATCHES WAIT_MS DUMP [--compact|--clock]\n");return 2;}
 char *end;unsigned long count=strtoul(argv[1],&end,10);if(*end||!count||count>(compact?V3_SOURCE_RING_MAX_BATCHES:1024))return 2;
 unsigned long wait=strtoul(argv[2],&end,10);if(*end||wait<100||wait>(compact?120000:60000))return 2;
 int fd=open("/dev/v3-g0-source-ring",O_RDONLY|O_CLOEXEC);if(fd<0){perror("SOURCE device");return 3;}
 void *memory=MAP_FAILED;FILE *dump=NULL;int code=0,started=0;
 struct v3_source_ring_status status={0};struct v3b_reader reader={0};struct payload_check check={.crc=~0U};uint32_t acquired=0;
 struct v3_source_ring_clock snapshot;
 unsigned clock_before_idle=0,clock_after_idle=0,clock_valid=0,clock_empty=0;
 uint8_t saved[V3B_BYTES+16];uint64_t dump_bytes=0,dump_omitted=0,first_ns=0,last_ns=0,started_ns=0;
 if(ioctl(fd,V3_SOURCE_RING_STATUS,&status)||status.abi!=V3_SOURCE_RING_ABI||status.slots!=8||status.slot_bytes!=V3B_BYTES){code=4;goto done;}
 if(check_clock){
  clock_before_idle=ioctl(fd,V3_SOURCE_RING_CLOCK,&snapshot)==-1 && errno==EAGAIN;
  if(!clock_before_idle){code=19;goto done;}
 }
 memory=mmap(NULL,8U*V3B_BYTES,PROT_READ,MAP_SHARED,fd,0);if(memory==MAP_FAILED){code=5;goto done;}
 dump=fopen(argv[3],"wb");if(!dump){code=6;goto done;}
 if(compact){const uint8_t header[16]={'V','3','B','C',1,0,16,0,0,128,0,0,0,0,0,0};if(fwrite(header,1,16,dump)!=16){code=6;goto done;}dump_bytes=16;}
 signal(SIGINT,stop_signal);signal(SIGTERM,stop_signal);
 struct v3_source_ring_start start={.batches=(uint32_t)count};
 if(ioctl(fd,V3_SOURCE_RING_START,&start)){perror("SOURCE start");code=7;goto done;}
 started=1;started_ns=nanoseconds();uint64_t deadline=milliseconds()+wait;
 while(acquired<count && !interrupted && milliseconds()<deadline){
  struct v3_source_ring_desc desc={0};
  if(ioctl(fd,V3_SOURCE_RING_ACQUIRE,&desc)){
   if(errno!=EAGAIN){perror("SOURCE acquire");code=8;break;}
   struct pollfd p={.fd=fd,.events=POLLIN};int n=poll(&p,1,100);
   if(n<0 && errno!=EINTR){code=9;break;}if(n>0 && (p.revents&(POLLERR|POLLHUP|POLLNVAL))){code=10;break;}continue;
  }
  if(desc.slot>=8 || desc.bytes!=V3B_BYTES || desc.reserved){code=11;break;}
  uint64_t arrival=nanoseconds();if(!acquired)first_ns=arrival;last_ns=arrival;
  const uint8_t *batch=(const uint8_t *)memory+desc.slot*V3B_BYTES;
  (void)v3b_consume(&reader,batch,desc.bytes,check_payload,&check);
  size_t size=compact?compact_batch(saved,batch,arrival):0;
  int written=compact?1:fwrite(batch,1,V3B_BYTES,dump)==V3B_BYTES;
  if(ioctl(fd,V3_SOURCE_RING_RELEASE,&desc)){code=12;break;}
  if(check_clock){
   if(ioctl(fd,V3_SOURCE_RING_CLOCK,&snapshot)==0){
    if(!snapshot.before_ns||snapshot.after_ns<snapshot.before_ns||snapshot.reserved||snapshot.age_cycles>=63){code=20;break;}
    clock_valid++;
   }else if(errno==EAGAIN)clock_empty++;
   else {code=20;break;}
  }
  if(compact){
   /* A full evidence sink never keeps a DMA lease or stops reception. An
    * omitted batch makes the evidence incomplete and must fail review. */
   if(dump_bytes+size<=64U*1024U*1024U){written=fwrite(saved,1,size,dump)==size;if(written)dump_bytes+=size;}
   else dump_omitted++;
  }
  acquired++;if(!written){code=13;break;}
 }
 if(acquired!=count && !code)code=interrupted?14:15;
done:
 if(started && ioctl(fd,V3_SOURCE_RING_STOP)){perror("SOURCE stop");code=16;}
 if(check_clock && started){
  clock_after_idle=ioctl(fd,V3_SOURCE_RING_CLOCK,&snapshot)==-1 && errno==EAGAIN;
  if(!clock_after_idle && !code)code=21;
 }
 if(ioctl(fd,V3_SOURCE_RING_STATUS,&status)){code=17;}
 printf("SOURCE_DMA batches=%u records=%"PRIu64" source=%"PRIu64" exact_bytes=%"PRIu64" restored_bytes=%"PRIu64" bad_source=%"PRIu64" bad_metadata=%"PRIu64" bad_record=%"PRIu64" missing_batch=%"PRIu64" duplicate=%"PRIu64" late=%"PRIu64" crc=%08"PRIx32" running=%u queued=%u user=%u poisoned=%u pinned=%u hw_batches=%"PRIu64" exit=%d\n",acquired,reader.records,check.sources,check.bytes,check.restored,check.bad_sources,reader.bad_metadata,reader.bad_records,reader.missing_batches,reader.duplicate_batches,reader.late_batches,~check.crc,status.running,status.queued,status.user,status.poisoned,status.pinned,(uint64_t)status.delivered_batches,code);
 if(compact)printf("SOURCE_COMPACT dump_bytes=%"PRIu64" omitted_batches=%"PRIu64" start_ns=%"PRIu64" first_ns=%"PRIu64" last_ns=%"PRIu64"\n",dump_bytes,dump_omitted,started_ns,first_ns,last_ns);
 if(check_clock)printf("SOURCE_CLOCK before_idle=%u after_idle=%u valid=%u unavailable=%u\n",clock_before_idle,clock_after_idle,clock_valid,clock_empty);
 if(dump && fclose(dump) && !code)code=18;
 if(memory!=MAP_FAILED)munmap(memory,8U*V3B_BYTES);
 close(fd);return code;
}
