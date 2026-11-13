/* G0 native ARM DMA ownership/PRBS probe, not RF video acceptance. */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <poll.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <time.h>
#include <unistd.h>
#include "include/g0_dma_ring.h"
#define MAP_BYTES (V3_RING_SLOT_BYTES * V3_RING_SLOTS)
static int fd=-1;static const uint8_t *map;
static uint16_t reverse12[4096],next_state[65536];
static uint64_t samples,transitions,gap_samples,gap_events,last_tag;
static uint16_t last_state[2];static int have_last;
static uint32_t crc_table[256];
static void fail(const char *s) { perror(s); if(fd>=0)ioctl(fd,V3_RING_STOP); exit(1); }
static void require(int yes,const char *s){if(!yes){errno=EPROTO;fail(s);}}
static double now(void){struct timespec t;clock_gettime(CLOCK_MONOTONIC,&t);return t.tv_sec+t.tv_nsec*1e-9;}
static void init(void){
 for(unsigned i=0;i<4096;i++){unsigned x=i,r=0;for(unsigned j=0;j<12;j++){r=(r<<1)|(x&1);x>>=1;}reverse12[i]=r;}
 for(unsigned i=0;i<65536;i++){unsigned p=0;for(unsigned j=4;j<16;j++)p^=(i>>j)&1;p^=(i>>1)&1;p^=(i>>2)&1;next_state[i]=((i<<1)|p)&65535;}
 for(unsigned i=0;i<256;i++){uint32_t c=i;for(unsigned j=0;j<8;j++)c=(c>>1)^((c&1)?0xedb88320:0);crc_table[i]=c;}
}
static uint32_t checksum(const struct v3_ring_desc *d){
 uint32_t c=~0U;const volatile uint8_t *p=map+d->slot*V3_RING_SLOT_BYTES;
 for(unsigned i=0;i<d->bytes;i++){c=(c>>8)^crc_table[(c^p[i])&255];}return ~c;
}
static struct v3_ring_status status(void){struct v3_ring_status s; if(ioctl(fd,V3_RING_STATUS,&s))fail("status");require(s.abi==1&&s.slots==8&&s.slot_bytes==32768,"ring ABI");return s;}
static void start(unsigned n){struct v3_ring_start s={n,0};if(ioctl(fd,V3_RING_START,&s))fail("start");have_last=0;}
static struct v3_ring_desc acquire(void){
 struct v3_ring_desc d;double end=now()+3;
 for(;;){if(!ioctl(fd,V3_RING_ACQUIRE,&d))break;if(errno!=EAGAIN)fail("acquire");
  struct pollfd p={fd,POLLIN,0};int r=poll(&p,1,100);if(r<0&&errno!=EINTR)fail("poll");require(now()<end,"bounded acquire timeout");}
 require(d.slot<8&&d.bytes==32768&&d.reserved==0,"descriptor bounds");return d;
}
static void release(struct v3_ring_desc d){if(ioctl(fd,V3_RING_RELEASE,&d))fail("release");}
static void rejected_release(struct v3_ring_desc d,int expected){errno=0;require(ioctl(fd,V3_RING_RELEASE,&d)==-1&&errno==expected,"invalid release admitted");}
static void validate(struct v3_ring_desc d){
 const volatile uint64_t *p=(const volatile uint64_t *)(map+d.slot*V3_RING_SLOT_BYTES);
 for(unsigned n=0;n<d.bytes/16;n++){
  uint64_t tag=p[n*2],iq=p[n*2+1];uint16_t states[2];
  uint64_t delta=(tag-last_tag)&0xffffffffffffULL;
  if(have_last){require((tag>>48)==(last_tag>>48)&&delta>0&&delta<(1ULL<<47),"stale/reordered raw record");if(delta!=1){gap_events++;gap_samples+=delta-1;}}
  for(unsigned rx=0;rx<2;rx++){
   unsigned i=(iq>>(32*rx))&4095,q=reverse12[(iq>>(32*rx+16))&4095];
   states[rx]=(i<<4)|(q&15);require(states[rx]!=0&&(i&255)==(q>>4),"raw PRBS I/Q relation");
   if(have_last&&delta==1){require(next_state[last_state[rx]]==states[rx],"raw PRBS next state");transitions++;}
   last_state[rx]=states[rx];
  }
  last_tag=tag;have_last=1;samples++;
 }
}
static void stop(void){double begin=now();if(ioctl(fd,V3_RING_STOP))fail("stop");struct v3_ring_status s=status();require(!s.running&&!s.queued&&!s.poisoned&&!s.pinned,"stop quiescence");require(now()-begin<.25,"stop exceeded bounded budget");}
static void run_normal(unsigned count){
 double begin=now();uint64_t before=samples,gaps=gap_samples;struct v3_ring_desc held;
 start(count);held=acquire();validate(held);uint32_t crc=checksum(&held);
 for(unsigned n=1;n<count;n++){struct v3_ring_desc d=acquire();require(d.sequence==held.sequence+n,"completion sequence");validate(d);if(n%8==0)require(checksum(&held)==crc,"held buffer overwritten");release(d);}
 require(checksum(&held)==crc,"held buffer changed at end");release(held);stop();
 struct v3_ring_status s=status();require(s.completed==count&&!s.user&&!s.ready,"normal ring census");
 printf("G0_RING_NORMAL_PASS batches=%u bytes=%"PRIu64" seconds=%.6f tag_gap_samples=%"PRIu64" overflow=%u\n",count,(samples-before)*16,now()-begin,gap_samples-gaps,s.overflow);fflush(stdout);
}
static void held_all(void){
 struct v3_ring_desc held[8];uint32_t crc[8];uint64_t gaps=gap_samples;
 start(24);for(unsigned i=0;i<8;i++){held[i]=acquire();validate(held[i]);crc[i]=checksum(&held[i]);}
 usleep(50000);struct v3_ring_status s=status();require(s.user==8&&!s.queued&&!s.ready&&s.completed==8&&s.submitted==8,"full owner ring reused without release");
 for(unsigned i=0;i<8;i++)require(checksum(&held[i])==crc[i],"full-ring held memory changed");
 struct v3_ring_start st={1,0};errno=0;require(ioctl(fd,V3_RING_START,&st)==-1&&errno==EBUSY,"start overlapped user ownership");
 struct v3_ring_desc wrong=held[0];wrong.generation++;rejected_release(wrong,ESTALE);
 wrong=held[0];wrong.slot=8;rejected_release(wrong,EINVAL);
 wrong=held[0];wrong.bytes=16;rejected_release(wrong,EINVAL);
 release(held[0]);rejected_release(held[0],ESTALE);
 for(unsigned i=1;i<8;i++)release(held[i]);
 for(unsigned i=8;i<24;i++){struct v3_ring_desc d=acquire();require(d.sequence==held[0].sequence+i,"held-ring resumption order");validate(d);release(d);}
 stop();s=status();require(s.completed==24&&!s.ready&&!s.user&&gap_samples>gaps,"full-ring bounded loss/resumption census");
 printf("G0_RING_HELD_ALL_PASS held_ms=50 immutable_slots=8 invalid_release_rejects=4 resumed_batches=16 tag_gap_samples=%"PRIu64" overflow=%u\n",gap_samples-gaps,s.overflow);fflush(stdout);
}
static void cancel_restart(void){
 struct v3_ring_desc held[8];uint32_t crc[8];
 start(1024);for(unsigned i=0;i<8;i++){held[i]=acquire();validate(held[i]);crc[i]=checksum(&held[i]);}
 usleep(5000);double begin=now();stop();double stop_elapsed=now()-begin;
 for(unsigned i=0;i<8;i++){require(checksum(&held[i])==crc[i],"STOP modified user buffer");release(held[i]);}
 struct v3_ring_status s=status();require(s.completed<1024&&!s.queued&&!s.user&&!s.ready,"cancel census");
 printf("G0_RING_CANCEL_PASS partial_session_batches=%u stop_and_status_seconds=%.6f user_buffers_stable=8\n",s.completed,stop_elapsed);fflush(stdout);
 run_normal(8);
}
static void mapping_lifetime(void){
 start(8);struct v3_ring_desc old=acquire();validate(old);uint32_t crc=checksum(&old);
 int owned=fd;fd=-1;close(owned);usleep(10000);
 int other=open("/dev/v3-g0-dma-ring",O_RDWR|O_CLOEXEC);
 require(other<0&&errno==EBUSY,"live mapping lost exclusive owner after fd close");
 require(checksum(&old)==crc,"mapping-backed user lease changed after fd close");
 munmap((void *)map,MAP_BYTES);
 fd=open("/dev/v3-g0-dma-ring",O_RDWR|O_CLOEXEC);if(fd<0)fail("reopen after last mapping");
 map=mmap(NULL,MAP_BYTES,PROT_READ,MAP_SHARED,fd,0);if(map==MAP_FAILED)fail("map after abandoned leases");
 struct v3_ring_status s=status();require(!s.running&&!s.user&&!s.ready&&!s.queued,"last mapping did not reclaim abandoned leases");
 start(8);struct v3_ring_desc fresh=acquire();require(fresh.slot==old.slot&&fresh.generation!=old.generation,"new ownership generation");
 rejected_release(old,ESTALE);validate(fresh);release(fresh);
 for(unsigned i=1;i<8;i++){struct v3_ring_desc d=acquire();validate(d);release(d);}stop();
 printf("G0_RING_MAPPING_LIFETIME_PASS close_fd_keeps_owner=1 last_unmap_reclaims_leases=1 old_generation_rejected=1\n");fflush(stdout);
}
int main(int argc,char **argv){
 _Static_assert(sizeof(struct v3_ring_desc)==24,"descriptor ABI size");
 _Static_assert(sizeof(struct v3_ring_status)==80,"status ABI size");
 init();fd=open("/dev/v3-g0-dma-ring",O_RDWR|O_CLOEXEC);if(fd<0)fail("open ring");
 if(argc==2&&!strcmp(argv[1],"--status")){
  struct v3_ring_status s=status();
  printf("G0_RING_STATUS running=%u poisoned=%u pinned=%u error=%d queued=%u ready=%u user=%u completed=%u overflow=%u bad_keep=%u\n",s.running,s.poisoned,s.pinned,(int32_t)s.error,s.queued,s.ready,s.user,s.completed,s.overflow,s.bad_keep);
  require(!s.poisoned&&!s.pinned,"unsafe DMA terminal state");close(fd);fd=-1;return 0;
 }
 require(argc==1,"invalid arguments");
 int second=open("/dev/v3-g0-dma-ring",O_RDWR|O_CLOEXEC);require(second<0&&errno==EBUSY,"exclusive DMA owner");
 void *bad=mmap(NULL,MAP_BYTES,PROT_READ|PROT_WRITE,MAP_SHARED,fd,0);require(bad==MAP_FAILED,"writable DMA mapping admitted");
 bad=mmap(NULL,MAP_BYTES,PROT_READ,MAP_SHARED,fd,4096);require(bad==MAP_FAILED,"offset DMA mapping admitted");
 map=mmap(NULL,MAP_BYTES,PROT_READ,MAP_SHARED,fd,0);if(map==MAP_FAILED)fail("read-only DMA mmap");
 require(mprotect((void *)map,MAP_BYTES,PROT_READ|PROT_WRITE)==-1,"DMA mapping write escalation admitted");
 run_normal(64);held_all();cancel_restart();
 munmap((void *)map,MAP_BYTES);close(fd);fd=-1;
 fd=open("/dev/v3-g0-dma-ring",O_RDWR|O_CLOEXEC);if(fd<0)fail("reopen ring");
 map=mmap(NULL,MAP_BYTES,PROT_READ,MAP_SHARED,fd,0);if(map==MAP_FAILED)fail("reopen mmap");run_normal(8);mapping_lifetime();
 munmap((void *)map,MAP_BYTES);close(fd);fd=-1;
 printf("G0_NATIVE_DMA_RING_PASS exact_PRBS_samples=%"PRIu64" checked_contiguous_transitions=%"PRIu64" tag_gap_events=%"PRIu64" tag_gap_samples=%"PRIu64" mmap_readonly=1 exclusive_owner=1 cancel_reentry=1 no_RF_video_claim=1\n",samples,transitions,gap_events,gap_samples);return 0;
}
