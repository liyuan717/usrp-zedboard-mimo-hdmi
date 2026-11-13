#pragma once
/* RF algorithms and CRC/FEC remain in PL.
 * Copy one batch, RELEASE its USER lease, then call the bounded video owner. */
extern "C" {
#include "g0_source_ring.h"
#include "g0_dma_batch.h"
}
#include <array>
#include <cerrno>
#include <cstring>
#include <fcntl.h>
#include <poll.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <unistd.h>
#include "g0_au_decode_queue.hpp"
namespace v3decode {
struct SourceDmaStats {
 v3b_reader parser{};v3_source_ring_status stopped{};
 uint64_t copied_bytes=0,acquired=0;
 uint32_t tail_batches=0;bool started=false,timed_out=false,lease_error=false,interrupted=false;
};
struct SourceDmaOwner {
 SourceDmaStats& stats;int fd=-1;void* map=MAP_FAILED;bool started=false;
 void stop(){if(started){if(ioctl(fd,V3_SOURCE_RING_STOP))stats.lease_error=true;started=false;}}
 ~SourceDmaOwner(){
  stop();
  if(fd>=0){
   if(ioctl(fd,V3_SOURCE_RING_STATUS,&stats.stopped))stats.lease_error=true;
   else if(stats.stopped.running||stats.stopped.queued||stats.stopped.user||stats.stopped.ready||stats.stopped.poisoned||stats.stopped.pinned)stats.lease_error=true;
  }
  if(map!=MAP_FAILED)munmap(map,8U*V3B_BYTES);
  if(fd>=0)close(fd);
 }
};
template<class Tick,class Stop> void source_dma(Queue& queue,uint32_t batches,uint32_t wait_ms,
 v3b_record_fn output,void* opaque,Tick tick,Stop stopping,SourceDmaStats& stats){
 SourceDmaOwner owner{stats};auto& fd=owner.fd;auto& map=owner.map;
 uint32_t generation=0;
 const bool continuous=batches==0;
 std::array<uint8_t,V3B_BYTES> saved{};
 do {
  if(stopping()){stats.interrupted=true;break;}
  fd=open("/dev/v3-g0-source-ring",O_RDONLY|O_CLOEXEC);
  if(fd<0){stats.lease_error=true;break;}
  v3_source_ring_status status{};
  if(ioctl(fd,V3_SOURCE_RING_STATUS,&status)||status.abi!=V3_SOURCE_RING_ABI||
     status.slots!=8||status.slot_bytes!=V3B_BYTES||status.running||status.user||status.poisoned||status.pinned){stats.lease_error=true;break;}
  map=mmap(nullptr,8U*V3B_BYTES,PROT_READ,MAP_SHARED,fd,0);
  if(map==MAP_FAILED){stats.lease_error=true;break;}
  v3_source_ring_start start{batches,0};
  const int started=continuous?ioctl(fd,V3_SOURCE_RING_START_CONTINUOUS,0UL):ioctl(fd,V3_SOURCE_RING_START,&start);
  if(started){stats.lease_error=true;break;}
  stats.started=owner.started=true;
  if(ioctl(fd,V3_SOURCE_RING_STATUS,&status)||status.abi!=V3_SOURCE_RING_ABI){stats.lease_error=true;break;}
  generation=status.generation;
  const auto deadline=Clock::now()+std::chrono::milliseconds(wait_ms);
  while(!queue.cancelled()&&!stopping()&&(continuous||(stats.acquired<batches&&Clock::now()<deadline))){
   v3_source_ring_desc desc{};
   if(ioctl(fd,V3_SOURCE_RING_ACQUIRE,&desc)){
    if(errno!=EAGAIN){stats.lease_error=true;break;}
    tick(fd);
    pollfd p{fd,POLLIN,0};const int n=poll(&p,1,10);
    if((n<0&&errno!=EINTR)||(n>0&&(p.revents&(POLLERR|POLLHUP|POLLNVAL)))){stats.lease_error=true;break;}
    continue;
   }
   if(desc.slot>=8||desc.bytes!=V3B_BYTES||desc.reserved||desc.generation!=status.generation){stats.lease_error=true;break;}
   stats.copied_bytes+=v3b_copy_owned(saved.data(),static_cast<const uint8_t*>(map)+desc.slot*V3B_BYTES,desc.bytes);
   if(ioctl(fd,V3_SOURCE_RING_RELEASE,&desc)){stats.lease_error=true;break;}
   ++stats.acquired;
   tick(fd);
   // Packet/record damage remains local. Never retain pointers into the mmap.
   (void)v3b_consume(&stats.parser,saved.data(),saved.size(),output,opaque);
  }
  stats.interrupted=stopping()||queue.cancelled();
  stats.timed_out=!continuous&&stats.acquired<batches&&!stats.interrupted&&!stats.lease_error;
 }while(false);
 // STOP joins DMA first. At most the eight already-READY slots remain; copy
 // and release those useful complete batches without restarting submission.
 const bool was_started=owner.started;owner.stop();
 if(was_started&&!stats.lease_error)for(unsigned i=0;i<8;++i){
  v3_source_ring_desc desc{};
  if(ioctl(fd,V3_SOURCE_RING_ACQUIRE,&desc)){
   if(errno!=EAGAIN)stats.lease_error=true;
   break;
  }
  if(desc.slot>=8||desc.bytes!=V3B_BYTES||desc.reserved||desc.generation!=generation){stats.lease_error=true;break;}
  stats.copied_bytes+=v3b_copy_owned(saved.data(),static_cast<const uint8_t*>(map)+desc.slot*V3B_BYTES,desc.bytes);
  if(ioctl(fd,V3_SOURCE_RING_RELEASE,&desc)){stats.lease_error=true;break;}
  ++stats.acquired;++stats.tail_batches;tick(-1);
  (void)v3b_consume(&stats.parser,saved.data(),saved.size(),output,opaque);
 }
}
}
