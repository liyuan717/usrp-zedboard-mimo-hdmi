#include "g0_source_video_ingress.hpp"
#include "hdmi_presenter/protocol.h"
#include <array>
#include <cassert>
#include <fstream>
#include <iostream>
#include <vector>

using namespace v3decode;
struct Stats {uint64_t inputs{},damaged{},source_records{},source_session_rejected{},source_sessions_adopted{};};
struct Frame {Info info;std::vector<uint8_t> bytes;};
static void put(uint8_t* p,uint64_t v,unsigned n){for(unsigned i=0;i<n;++i)p[i]=v>>(8*i);}
static uint32_t uncertainty(uint32_t frame){return frame==7?UINT32_MAX:5000+frame%1000;}

static std::vector<Frame> ingress(const char* path,bool interval){
 Queue queue;Stats stats;
 SourceVideoIngress<Stats> s{queue,stats,v3vf_create(),v3au_create()};
 s.admission=SourceSession(true);
 assert(s.fragments&&s.au&&v3vf_set_ordered(s.fragments,1));
 std::ifstream stream(path,std::ios::binary);assert(stream);
 std::array<uint8_t,576> record{};put(record.data()+16,0x12345678,4);put(record.data()+20,1,4);
 std::vector<Frame> result;uint64_t popped=0;unsigned injected=0;bool inconsistent=false;
 while(stream.read(reinterpret_cast<char*>(record.data()+64),512)){
  auto* symbol=record.data()+64;
  if(!symbol[30])continue;
  const auto frame=SourceVideoIngress<Stats>::le32(symbol);
  s.local=uint64_t(frame)*50000+200000;
  if(interval){
   symbol[31]|=V3VF_CAPTURE_INTERVAL;put(symbol+36,uncertainty(frame),4);
   // Each malformed fragment is rejected locally; the original correct
   // fragment follows immediately, without a new process or session.
   if(injected<5){
    auto bad=record;auto* p=bad.data()+64;
    switch(injected){
     case 0:p[31]&=~V3VF_CAPTURE_INTERVAL;break;
     case 1:put(p+36,0,4);break;
     case 2:put(p+36,50001,4);break;
     case 3:p[31]|=16;break;
     case 4:p[31]&=~V3VF_CAPTURE_INTERVAL;put(p+36,UINT32_MAX,4);break;
    }
    assert(SourceVideoIngress<Stats>::output_record(&s,bad.data(),bad.size(),1)<0);++injected;
   }
   if(!inconsistent&&(symbol[26]||symbol[27])&&frame!=7){
    auto bad=record;put(bad.data()+64+36,uncertainty(frame)+1,4);
    assert(SourceVideoIngress<Stats>::output_record(&s,bad.data(),bad.size(),1)<0);
    inconsistent=true;
   }
  }
  SourceVideoIngress<Stats>::output_record(&s,record.data(),record.size(),1);
  while(popped<queue.accepted){
   Job job;assert(queue.pop(job)==1&&queue.accepts(job.info));
   assert(job.info.capture_uncertainty_us==(interval?uncertainty(job.info.id):0));
   result.push_back({job.info,{job.payload->data.begin(),job.payload->data.begin()+job.bytes}});++popped;
  }
 }
 assert(stream.eof()&&stream.gcount()==0);
 v3vf_stats fragments{};v3vf_get_stats(s.fragments,&fragments);
 assert(fragments.invalid_fragments==(interval?6U:0U));
 assert(!interval||inconsistent);
 assert(queue.dropped==0&&stats.source_sessions_adopted==1);
 assert(queue.budget->owners==0&&queue.budget->bytes==0);
 v3vf_destroy(s.fragments);v3au_destroy(s.au);return result;
}

static void age_bounds(){
 constexpr uint64_t ns=1000000000,tag=0xabcd0000000003e8,sid=0x12345678;
 std::array<uint8_t,96> r{};
 put(r.data()+14,4,2);put(r.data()+16,sid,4);put(r.data()+20,1,4);
 put(r.data()+32,tag,8);put(r.data()+48,0,4);put(r.data()+52,65535,2);put(r.data()+54,32,2);
 std::memcpy(r.data()+64,"VRT3",4);put(r.data()+68,0x0001f001,4);
 put(r.data()+72,1684,8);put(r.data()+80,3000000,8);put(r.data()+88,3000000,8);
 const v3_source_ring_clock snapshot{10*ns,10*ns,tag,0,0};
 SourceAge point(0,true),bounded(0,true),unknown(1000000);
 for(auto* age:{&point,&bounded,&unknown})assert(age->observe(r.data(),r.size(),snapshot));
 const uint64_t now=10*ns+5000000;
 point.ack(sid,0,now);bounded.ack(sid,0,now,10000);
 assert(point.counts().late==1&&bounded.counts().late==0);
 assert(point.presentation_expired(sid,0,now));
 assert(!bounded.presentation_expired(sid,0,now,10000));
 // Enclose exact independent ages: earliest capture 0 and latest 10ms.
 assert(bounded.counts().min_age_ns<=2995000000LL);
 unknown.ack(sid,0,now,UINT32_MAX);unknown.ack(sid,0,now,50001);
 assert(unknown.counts().unknown==2);
 assert(!unknown.presentation_expired(sid,0,60*ns,UINT32_MAX));
 SourceAge full(1000000);assert(full.observe(r.data(),r.size(),snapshot));
 full.ack(sid,0,now,10000);
 assert(full.counts().min_age_ns<=2995000000LL);
 assert(full.counts().max_age_ns>=3006000000LL);
 assert(full.presentation_expired(sid,0,now,10000));
}

static void AU_metadata_consistency(){
 auto* au=v3au_create();assert(au);v3au_session(au,1);
 const uint8_t aud[]={0,0,1,9,16},vcl[]={0,0,1,1,42};
 v3vf_info first{};first.session=1;first.frame_id=0;first.unit_id=0;first.bytes=5;
 first.duration_us=50000;first.type=9;first.flags=1|8;first.capture_uncertainty_us=5000;
 unsigned delivered=0;
 auto callback=[](void* p,const v3au_info* info,const uint8_t*){
  assert(info->bytes==10&&info->capture_uncertainty_us==5000&&!info->damaged);
  ++*static_cast<unsigned*>(p);return 1;
 };
 assert(v3au_push(au,&first,aud,0,callback,&delivered));
 auto next=first;next.unit_id=1;next.type=1;next.flags=2|8;
 next.capture_uncertainty_us=5001;
 assert(!v3au_push(au,&next,vcl,1,callback,&delivered));
 next.capture_uncertainty_us=5000;
 assert(v3au_push(au,&next,vcl,2,callback,&delivered));
 v3au_stats stats{};v3au_get_stats(au,&stats);assert(stats.invalid==1&&delivered==1);
 v3au_destroy(au);
}

int main(int argc,char** argv){
 assert(argc==2);const auto legacy=ingress(argv[1],false),bounded=ingress(argv[1],true);
 assert(legacy.size()==200&&bounded.size()==legacy.size());
 for(size_t i=0;i<legacy.size();++i){
  assert(legacy[i].info.id==bounded[i].info.id&&legacy[i].info.capture_us==bounded[i].info.capture_us);
  assert(legacy[i].info.damaged==bounded[i].info.damaged&&legacy[i].bytes==bounded[i].bytes);
  hdmi_presenter::FrameIdentity identity{bounded[i].info.id>>32,uint32_t(bounded[i].info.id),
   bounded[i].info.capture_us*1000,0,bounded[i].info.capture_uncertainty_us};
  assert(identity.capture_uncertainty_us==uncertainty(i));
 }
 age_bounds();AU_metadata_consistency();
 std::cout<<"{\"frames\":"<<bounded.size()<<",\"payloads_identical\":true,\"invalid_fragments_rejected\":6,"
  <<"\"unknown_time_video_continues\":true,\"Info_bytes\":"<<sizeof(Info)
  <<",\"FrameIdentity_bytes\":"<<sizeof(hdmi_presenter::FrameIdentity)
  <<",\"fragment_context_bytes\":"<<v3vf_context_bytes()
  <<",\"AU_context_bytes\":"<<v3au_context_bytes()<<",\"completed\":true}\n";
}
