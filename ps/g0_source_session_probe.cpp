/* Actual shared DMA parser -> session policy -> NAL/AU -> owned queue tests.
 * Component payloads are deliberately synthetic, not H264/video signoff. */
#include "include/g0_source_video_ingress.hpp"
extern "C" {
#include "include/g0_dma_batch.h"
}
#include <array>
#include <cassert>
#include <cinttypes>
#include <cstdio>
#include <cstring>
#include <vector>
using namespace v3decode;
struct Stats {uint64_t inputs=0,damaged=0,source_records=0,source_session_rejected=0,source_sessions_adopted=0;};
using Ingress=SourceVideoIngress<Stats>;
static void put16(uint8_t* p,uint16_t n){p[0]=n;p[1]=n>>8;}
static void put32(uint8_t* p,uint32_t n){for(unsigned i=0;i<4;i++)p[i]=n>>(8*i);}
static void put64(uint8_t* p,uint64_t n){for(unsigned i=0;i<8;i++)p[i]=n>>(8*i);}
static std::vector<uint8_t> payload(unsigned bytes,uint8_t salt){
 std::vector<uint8_t> p(bytes);for(unsigned i=0;i<bytes;i++)p[i]=(i*17+salt)&255;
 p[0]=p[1]=p[2]=0;p[3]=1;p[4]=0x65;return p;
}
static std::array<uint8_t,512> fragment(uint32_t frame,uint32_t unit,const std::vector<uint8_t>& data,unsigned index){
 std::array<uint8_t,512> p{};unsigned offset=index*472,n=std::min(472U,unsigned(data.size())-offset),count=(data.size()+471)/472;
 put32(p.data(),frame);put64(p.data()+4,uint64_t(frame)*50000+1000);put32(p.data()+12,unit);
 put32(p.data()+16,data.size());put32(p.data()+20,offset);put16(p.data()+24,n);put16(p.data()+26,index);put16(p.data()+28,count);
 p[30]=5;p[31]=4|(index==0?1:0)|(index+1==count?2:0);put32(p.data()+32,50000);
 std::memcpy(p.data()+40,data.data()+offset,n);return p;
}
struct AuOutput {v3au_info info;std::vector<uint8_t> data;};
static int take_au(void* opaque,const v3au_info* info,const uint8_t* data){
 auto& out=*static_cast<std::vector<AuOutput>*>(opaque);
 for(unsigned i=0;i<64;i++)assert(data[info->bytes+i]==0);
 out.push_back({*info,{data,data+info->bytes}});return 1;
}
static unsigned deadline_controls(){
 const std::vector<uint8_t> p={0,0,0,1,0x65,0x91,0x37};
 for(unsigned scenario=0;scenario<5;scenario++){
  auto* au=v3au_create();assert(au);v3au_session(au,7);std::vector<AuOutput> out;
  auto push=[&](uint32_t unit,uint32_t frame,uint8_t flags,uint64_t local,bool invalid=false){
   v3vf_info n{};n.session=7;n.unit_id=unit;n.frame_id=frame;n.bytes=p.size();
   n.capture_time_us=uint64_t(frame)*50000;n.duration_us=invalid?50001:50000;n.type=5;n.flags=flags;
   return v3au_push(au,&n,p.data(),local,take_au,&out);
  };
  auto tick=[&](uint64_t local){v3au_tick(au,local,take_au,&out);};
  assert(push(0,0,1,0)==1);
  if(scenario==0){
   assert(push(1,0,0,400000)==1);tick(500000);assert(out.empty());
   assert(push(2,0,2,700000)==1);assert(out.size()==1&&!out[0].info.damaged);
   std::vector<uint8_t> expected;for(unsigned i=0;i<3;i++)expected.insert(expected.end(),p.begin(),p.end());
   assert(out[0].data==expected);
  }else if(scenario==1){
   assert(push(1,0,0,400000)==1);tick(899999);assert(out.empty());
   tick(900000);assert(out.size()==1&&out[0].info.damaged&&out[0].data.size()==2*p.size());
   assert(push(2,0,2,900001)==0);assert(out.size()==1); // expired tail cannot reopen frame
   assert(push(3,1,3,910000)==1);assert(out.size()==2&&out[1].info.frame_id==1&&out[1].data==p);
  }else if(scenario==2){
   assert(push(1,0,0,400000)==1);assert(push(2,0,0,800000)==1);
   tick(999999);assert(out.empty());tick(1000000);
   assert(out.size()==1&&out[0].info.damaged&&out[0].data.size()==3*p.size());
   assert(push(3,1,3,1000001)==1);assert(out.size()==2&&out[1].data==p);
  }else{
   assert(push(scenario==3?1:0,0,0,400000,scenario==3)==0);
   tick(499999);assert(out.empty());tick(500000);
   assert(out.size()==1&&out[0].info.damaged&&out[0].data==p);
  }
  v3au_destroy(au);
 }
 return 5;
}
int main(){
 const unsigned deadlines=deadline_controls();
 unsigned policy_checks=0;
 auto check=[&](SourceSession& gate,uint32_t sid,uint32_t epoch,SourceSession::Decision expected){
  assert(gate.observe(sid,epoch)==expected);++policy_checks;
 };
 SourceSession zero(true);check(zero,0,0,SourceSession::kAdopt);check(zero,0,0,SourceSession::kKeep);
 check(zero,1,0,SourceSession::kReject);check(zero,0,1,SourceSession::kKeep);check(zero,0,0,SourceSession::kKeep);
 SourceSession wrap(true);check(wrap,1,0xfffffffeU,SourceSession::kAdopt);
 check(wrap,1,0xffffffffU,SourceSession::kKeep);check(wrap,1,0,SourceSession::kKeep);
 check(wrap,1,0xfffffffeU,SourceSession::kKeep);check(wrap,2,1,SourceSession::kAdopt);
 check(wrap,1,0,SourceSession::kReject);check(wrap,2,0,SourceSession::kReject);check(wrap,0,2,SourceSession::kAdopt);
 SourceSession pinned(false,0);check(pinned,0,123,SourceSession::kKeep);check(pinned,1,124,SourceSession::kReject);
 Queue queue;Stats stats;Ingress ingress{queue,stats,v3vf_create(),v3au_create()};
 assert(ingress.fragments&&ingress.au&&v3vf_set_ordered(ingress.fragments,1));
 ingress.admission=SourceSession(true);v3b_reader parser{};uint64_t sequence=0;
 auto emit=[&](uint32_t sid,uint32_t epoch,const std::array<uint8_t,512>& p,bool bad_record=false){
  std::array<uint8_t,32768> batch{};auto* h=batch.data();auto* record=h+32;auto* end=h+32736;
  put32(h,0x42443356);put16(h+4,1);put16(h+6,32);put32(h+8,32768);put64(h+16,sequence);
  put32(end,0x45443356);put32(end+4,576);put32(end+8,1);put64(end+16,sequence++);
  put32(record,0x58523356);put16(record+4,1);put16(record+6,64);put32(record+8,576);
  put16(record+12,1);put16(record+14,6);put32(record+16,sid);put32(record+20,epoch);put16(record+54,bad_record?511:512);
  std::memcpy(record+64,p.data(),512);ingress.local+=1000;
  (void)v3b_consume(&parser,h,batch.size(),Ingress::output_record,&ingress);
  // No pointer into the borrowed batch may survive the callback.
  std::memset(batch.data(),0xa5,batch.size());
 };
 std::vector<Job> held;unsigned exact=0;
 auto take=[&](uint32_t sid,uint32_t frame,const std::vector<uint8_t>& expected){
  ingress.tick(ingress.local+200000);Job job;assert(queue.pop(job)==1);
  assert(job.info.id==((uint64_t(sid)<<32)|frame)&&job.info.capture_us==uint64_t(frame)*50000+1000);
  assert(job.bytes==expected.size()&&std::memcmp(job.payload->data.data(),expected.data(),expected.size())==0);
  assert(queue.accepts(job.info));held.push_back(std::move(job));++exact;
 };
 constexpr uint32_t A=0x80000000U,B=0xffffffffU;
 auto a0=payload(17,3),a1=payload(600,5),b0=payload(19,7),a_new=payload(23,11),after_bad=payload(21,13);
 emit(A,10,fragment(0,0,a0,0));take(A,0,a0);
 const uint64_t first_admission=queue.admission();
 emit(B,10,fragment(0,0,b0,0));assert(stats.source_session_rejected==1&&queue.admission()==first_admission);
 emit(A,11,fragment(1,1,a1,0));emit(A,10,fragment(1,1,a1,1));take(A,1,a1);
 assert(queue.admission()==first_admission&&stats.source_sessions_adopted==1);
 emit(B,12,fragment(0,0,b0,0));take(B,0,b0);assert(!queue.accepts(held[0].info));
 emit(A,11,fragment(0,0,a0,0));emit(B,11,fragment(0,0,b0,0));
 assert(stats.source_session_rejected==3);
 emit(A,13,fragment(0,0,a_new,0));take(A,0,a_new);
 assert(!queue.accepts(held[0].info)&&!queue.accepts(held[2].info));
 assert(queue.accepts(held[3].info)&&held[0].info.id==held[3].info.id);
 emit(A,10,fragment(1,1,a0,0));assert(stats.source_session_rejected==4);
 const uint64_t last_admission=queue.admission();
 emit(B,14,fragment(0,0,b0,0),true);assert(parser.bad_records==1&&queue.admission()==last_admission);
 auto malformed=fragment(1,1,after_bad,0);put16(malformed.data()+24,473);
 emit(A,13,malformed);emit(A,13,fragment(1,1,after_bad,0));take(A,1,after_bad);
 v3vf_stats fragments{};v3vf_get_stats(ingress.fragments,&fragments);assert(fragments.invalid_fragments==1);
 assert(stats.source_sessions_adopted==3&&stats.inputs==5&&exact==5);
 held.clear();queue.close();v3vf_destroy(ingress.fragments);v3au_destroy(ingress.au);
 assert(queue.budget->owners==0&&queue.budget->bytes==0);
 std::printf("{\"completed\":true,\"policy_checks\":%u,\"byteexact_AUs\":%u,\"SOURCE_session_adoptions\":%" PRIu64
  ",\"rejected_session_records\":%" PRIu64 ",\"bad_DMA_records\":%" PRIu64
  ",\"bad_video_fragments_local\":%" PRIu64 ",\"session_state_bytes\":%zu,\"source_ingress_context_bytes\":%zu,"
  "\"AU_deadline_cases\":%u,\"AU_context_bytes\":%zu,\"owners_after_cleanup\":0,\"bytes_after_cleanup\":0,\"RF_started\":false}\n",
  policy_checks,exact,stats.source_sessions_adopted,stats.source_session_rejected,parser.bad_records,
  fragments.invalid_fragments,sizeof(SourceSession),sizeof(Ingress),deadlines,v3au_context_bytes());
}
