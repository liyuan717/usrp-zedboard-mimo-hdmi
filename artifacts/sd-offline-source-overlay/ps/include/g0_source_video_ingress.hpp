#pragma once
extern "C" {
#include "g0_video_fragment.h"
#include "g0_video_au.h"
}
#include "g0_au_decode_queue.hpp"
#include "g0_source_session.hpp"
#include "g0_source_age.hpp"
#include <cerrno>
#include <sys/ioctl.h>

namespace v3decode {
/* One bounded ingress producer; all RF CRC/FEC/session descriptor validation
 * is still in PL. The caller has already copied/released its DMA USER lease. */
template<class Stats> struct SourceVideoIngress {
 Queue& queue;Stats& stats;v3vf* fragments;v3au* au;uint64_t local=0;unsigned drop_nals=0;
 SourceSession admission{};
 SourceAge* age=nullptr;int clock_fd=-1;bool clock_unsupported=false;
 static uint32_t le32(const uint8_t* p){return uint32_t(p[0])|uint32_t(p[1])<<8|uint32_t(p[2])<<16|uint32_t(p[3])<<24;}
 static int output_au(void* p,const v3au_info* a,const uint8_t* data){
  auto& s=*static_cast<SourceVideoIngress*>(p);++s.stats.inputs;s.stats.damaged+=a->damaged;
  return s.queue.push({(static_cast<uint64_t>(a->session)<<32)|a->frame_id,a->capture_time_us,a->damaged},data,a->bytes);
 }
 static int output_nal(void* p,const v3vf_info* n,const uint8_t* data){
  auto& s=*static_cast<SourceVideoIngress*>(p);
  if(s.drop_nals){--s.drop_nals;return 0;}
  return v3au_push(s.au,n,data,s.local,output_au,&s);
 }
 static int output_record(void* p,const uint8_t* record,uint32_t bytes,uint16_t kind){
  auto& s=*static_cast<SourceVideoIngress*>(p);
  if(kind==3 && s.age && s.clock_fd>=0 && !s.clock_unsupported && SourceAge::is_anchor(record,bytes)){
   v3_source_ring_clock snapshot{};
   if(ioctl(s.clock_fd,V3_SOURCE_RING_CLOCK,&snapshot)==0)s.age->observe(record,bytes,snapshot);
   else if(errno==ENOTTY || errno==EOPNOTSUPP)s.clock_unsupported=true;
  }
  if(kind!=1)return 1;
  ++s.stats.source_records;const uint32_t sid=le32(record+16),epoch=le32(record+20);
  const auto decision=s.admission.observe(sid,epoch);
  if(decision==SourceSession::kReject){++s.stats.source_session_rejected;return 0;}
  if(decision==SourceSession::kAdopt){
   if(s.age)s.age->adopt_source(sid,epoch);
   v3vf_session(s.fragments,sid);v3au_session(s.au,sid);s.queue.admit(sid);++s.stats.source_sessions_adopted;
  }
  return v3vf_push(s.fragments,sid,record+64,512,s.local,0,0,output_nal,&s);
 }
 void tick(uint64_t now){local=now;v3vf_drain(fragments,local,0,0,output_nal,this);v3au_tick(au,local,output_au,this);}
};
}
