#pragma once
#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <cstring>
#include <memory>
#include <mutex>
#include <vector>

namespace v3decode {
using Clock=std::chrono::steady_clock;
constexpr size_t AU_MAX=1024*1024, PADDING=64, POOL_MAX=2*(AU_MAX+PADDING);
struct Info {uint64_t id,capture_us;uint32_t damaged;uint64_t admission=0;};
struct Budget {std::atomic<size_t> bytes{0},owners{0};};
struct Payload {
 std::vector<uint8_t> data;std::shared_ptr<Budget> budget;size_t reserved_after,owners_after;
 Payload(const uint8_t* p,size_t n,const std::shared_ptr<Budget>& b):data(n+PADDING,0),budget(b),
  reserved_after(b->bytes.fetch_add(n+PADDING)+n+PADDING),owners_after(b->owners.fetch_add(1)+1){std::memcpy(data.data(),p,n);}
 ~Payload(){
  const size_t bytes=data.size();
  // Return budget only after storage is actually freed, so another producer
  // cannot transiently allocate against a still-live former payload.
  std::vector<uint8_t>().swap(data);
  budget->bytes.fetch_sub(bytes);budget->owners.fetch_sub(1);
 }
 Payload(const Payload&)=delete;Payload& operator=(const Payload&)=delete;
};
struct Job {Info info{};size_t bytes{};std::shared_ptr<Payload> payload;Clock::time_point enqueued;};
/* One ingress producer. Atomic SPSC copy-or-drop: no decoder or display wait on
 * the caller. Payload ownership survives queue pop and AVPacket references. */
class Queue {
 std::mutex mutex_;std::condition_variable cv_;std::array<Job,8> ready_{};
 std::atomic<uint32_t> head_{0},tail_{0};std::atomic<bool> closed_{false};
 std::atomic<bool> cancelled_{false};
 std::atomic<uint64_t> admission_{0};
public:
 std::shared_ptr<Budget> budget=std::make_shared<Budget>();
 uint64_t accepted=0,dropped=0;size_t peak_bytes=0,peak_owners=0,peak_ready=0;
 bool push(const Info& info,const uint8_t* data,size_t bytes){
  if(closed_||cancelled_||!data||!bytes||bytes>AU_MAX||info.damaged>1){++dropped;return false;}
  const uint32_t tail=tail_.load(std::memory_order_relaxed),head=head_.load(std::memory_order_acquire);
  if(tail-head>=8||budget->owners.load()>=10||budget->bytes.load()>POOL_MAX-bytes-PADDING){++dropped;return false;}
  try{
   auto p=std::make_shared<Payload>(data,bytes,budget);
   Job job{info,bytes,p,Clock::now()};job.info.admission=admission_.load();ready_[tail&7U]=std::move(job);
   tail_.store(tail+1U,std::memory_order_release);
   peak_bytes=std::max(peak_bytes,p->reserved_after);peak_owners=std::max(peak_owners,p->owners_after);
   peak_ready=std::max(peak_ready,static_cast<size_t>(tail-head+1U));++accepted;
  }catch(const std::bad_alloc&){++dropped;return false;}
  cv_.notify_one();return true;
 }
 int pop(Job& job){
  std::unique_lock<std::mutex> lock(mutex_);
  cv_.wait_for(lock,std::chrono::milliseconds(50),[&]{return closed_||cancelled_||head_.load()!=tail_.load();});
  if(cancelled_)return -1;
  const uint32_t head=head_.load(std::memory_order_relaxed),tail=tail_.load(std::memory_order_acquire);
  if(head==tail)return closed_?-1:0;
  lock.unlock();job=std::move(ready_[head&7U]);head_.store(head+1U,std::memory_order_release);return 1;
 }
 bool pace(Clock::time_point deadline){
  std::unique_lock<std::mutex> lock(mutex_);
  cv_.wait_until(lock,deadline,[&]{return cancelled_.load();});return !cancelled_;
 }
 bool cancelled()const{return cancelled_;}
 uint64_t admission()const{return admission_.load();}
 void admit(uint32_t sid){
  const uint64_t old=admission_.load();
  if((old>>32)&&static_cast<uint32_t>(old)==sid)return;
  uint32_t generation=static_cast<uint32_t>(old>>32)+1U;if(!generation)generation=1;
  admission_.store((static_cast<uint64_t>(generation)<<32)|sid);
 }
 bool accepts(const Info& info)const{const uint64_t a=admission();return !(a>>32)||(info.admission==a&&static_cast<uint32_t>(a)==static_cast<uint32_t>(info.id>>32));}
 void close(){std::lock_guard<std::mutex> lock(mutex_);closed_=true;cv_.notify_all();}
 void cancel(){std::lock_guard<std::mutex> lock(mutex_);cancelled_=true;cv_.notify_all();}
};
}
