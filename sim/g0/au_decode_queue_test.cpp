#include "../../ps/include/g0_au_decode_queue.hpp"
#include <cassert>
#include <thread>
#include <iostream>
using namespace v3decode;
int main(){
 std::vector<uint8_t> data(AU_MAX,0x5a);Info info{0xf633460100000001ULL,123,1};
 {
  Queue q;assert(q.push(info,data.data(),AU_MAX));Job held;assert(q.pop(held)==1);
  assert(held.info.id==info.id&&held.info.damaged==1&&held.bytes==AU_MAX);
  data[0]=1;assert(held.payload->data[0]==0x5a);
  for(size_t i=AU_MAX;i<AU_MAX+64;++i)assert(!held.payload->data[i]);
  assert(q.push(info,data.data(),AU_MAX));assert(!q.push(info,data.data(),1));
  assert(q.budget->bytes==POOL_MAX);held.payload.reset();
  assert(q.push(info,data.data(),AU_MAX));assert(q.peak_bytes==POOL_MAX);
  Job other;assert(q.pop(other)==1);other.payload.reset();assert(q.pop(other)==1);other.payload.reset();
  assert(q.budget->bytes==0&&q.budget->owners==0);q.close();assert(q.pop(other)==-1);
 }
 {
  Queue q;std::vector<Job> held;
  for(unsigned i=0;i<8;++i)assert(q.push(info,data.data(),1));
  assert(!q.push(info,data.data(),1));
  for(unsigned i=0;i<8;++i){Job j;assert(q.pop(j)==1);held.push_back(std::move(j));}
  assert(q.push(info,data.data(),1));assert(q.push(info,data.data(),1));
  assert(!q.push(info,data.data(),1));assert(q.budget->owners==10);held.clear();
  Job j;assert(q.pop(j)==1);j.payload.reset();assert(q.pop(j)==1);j.payload.reset();
  assert(!q.budget->owners);assert(q.peak_ready==8&&q.peak_owners==10);
 }
 {
  Queue q;q.admit(0xf6334601);assert(q.push(info,data.data(),32));Job old;assert(q.pop(old)==1);
  assert(q.accepts(old.info));const auto first=q.admission();
  q.admit(123);assert(!q.accepts(old.info));q.admit(0xf6334601);
  assert(q.admission()!=first&&!q.accepts(old.info));
  assert(q.push(info,data.data(),32));Job current;assert(q.pop(current)==1);assert(q.accepts(current.info));
  old.payload.reset();current.payload.reset();assert(!q.budget->owners);
 }
 {
  Queue q;std::atomic<bool> entered{false};
  std::thread consumer([&]{entered=true;assert(!q.pace(Clock::now()+std::chrono::hours(1)));});
  while(!entered)std::this_thread::yield();
  auto begin=Clock::now();q.cancel();consumer.join();
  assert(Clock::now()-begin<std::chrono::seconds(1));
 }
 {
  Queue q;std::thread consumer([&]{Job j;while(q.pop(j)>=0)j.payload.reset();});
  for(unsigned i=0;i<10000;++i)q.push(info,data.data(),32);
  q.close();consumer.join();assert(q.accepted+q.dropped==10000);
  assert(!q.budget->owners&&q.peak_owners<=10&&q.peak_bytes<=POOL_MAX);
 }
 std::cout<<"QUEUE_OWNERSHIP_CAP_BACKPRESSURE_CANCEL_CONTINUE_PASS\n";
}
