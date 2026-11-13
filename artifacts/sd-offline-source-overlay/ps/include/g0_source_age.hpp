#pragma once

#include "g0_source_ring.h"
#include <algorithm>
#include <array>
#include <cstdint>
#include <cstring>
#include <mutex>

namespace v3decode {

/* G0 conditional estimator. Its path budget and 400ppm allowance need
 * hardware qualification before they can enforce an on-air deadline. */
class SourceAge {
 static constexpr int64_t kSecond=1000000000, kDeadline=3*kSecond;
 static constexpr uint64_t kSampleMask=(UINT64_C(1)<<48)-1;
 static constexpr int64_t kRatePpm=400;
 static uint64_t word(const uint8_t* p,unsigned bytes){
  uint64_t value=0;
  for(unsigned i=0;i<bytes;++i)value|=uint64_t(p[i])<<(8*i);
  return value;
 }
 static int64_t signed_word(const uint8_t* p){
  const uint64_t u=word(p,8);int64_t s;
  std::memcpy(&s,&u,sizeof(s));return s;
 }
 static int64_t allowance(int64_t n){
  return (n*kRatePpm+999599)/999600;
 }
 struct Reference {
  uint32_t sid{},delivery{},frame{};
  int64_t tx_low{},tx_high{},source_low{},source_high{};
  bool valid{};
 };
 struct SourceLifetime {uint32_t sid{},delivery{};bool valid{};};
 static bool precedes(uint32_t delivery,uint32_t sid,const SourceLifetime& lifetime){
  return lifetime.valid && (((delivery-lifetime.delivery)>>31) ||
   (delivery==lifetime.delivery && sid!=lifetime.sid));
 }
public:
 struct Counts {
  uint64_t anchors{},rejected{},acks{},unknown{},within{},late{},ambiguous{};
  int64_t min_age_ns{},max_age_ns{};
 };
 struct CalibrationAnchor {
  uint32_t sid{},frame{};
  int64_t board_low_ns{},board_high_ns{};
 };
 struct Calibration {
  // First eligible observation in each 8-second stratum, plus the latest.
  // Fixed evidence space, independent of the number of video/ACK records.
  std::array<CalibrationAnchor,8> strata{};
  uint32_t mask{};
  CalibrationAnchor last{};
 };
 explicit SourceAge(uint32_t path_budget_ns):path_budget_(path_budget_ns){}

 void adopt_source(uint32_t sid,uint32_t delivery){
  std::lock_guard<std::mutex> lock(mutex_);
  lifetime_={sid,delivery,true};
  // A fresh anchor may arrive before its first SOURCE record.
  if(reference_.valid && precedes(reference_.delivery,reference_.sid,lifetime_)){
   reference_.valid=false;
   calibration_={};
  }
 }

 static bool is_anchor(const uint8_t* record,uint32_t bytes){
  return bytes==96 && word(record+14,2)==4 && word(record+40,8)==0 &&
   word(record+52,2)==65535 && word(record+54,2)==32 &&
   std::memcmp(record+64,"VRT3",4)==0 && word(record+68,4)==0x0001f001;
 }

 bool observe(const uint8_t* record,uint32_t bytes,const v3_source_ring_clock& snapshot){
  std::lock_guard<std::mutex> lock(mutex_);
  if(!is_anchor(record,bytes)){++counts_.rejected;return false;}
  const auto sid=static_cast<uint32_t>(word(record+16,4));
  const auto delivery=static_cast<uint32_t>(word(record+20,4));
  const auto frame=static_cast<uint32_t>(word(record+48,4));
  const uint64_t adc=word(record+32,8);
  const int64_t source_low=signed_word(record+80),source_high=signed_word(record+88);
  const uint64_t samples=(snapshot.adc_tag-adc)&kSampleMask;
  if(!path_budget_ || path_budget_>10000000 || frame>=3200 ||
     word(record+72,8)!=uint64_t(frame)*80000+1684 ||
     source_low < -1000000 || source_high>66000000 || source_high<source_low ||
     source_high-source_low>50000 || snapshot.reserved || snapshot.age_cycles>=63 ||
     snapshot.before_ns<kSecond || snapshot.after_ns<snapshot.before_ns ||
     snapshot.after_ns>uint64_t(INT64_MAX)-100*kSecond ||
     snapshot.after_ns-snapshot.before_ns>2000000 || (snapshot.adc_tag>>48)!=(adc>>48) ||
     samples>400000 || (!samples && word(record+56,4)) || precedes(delivery,sid,lifetime_) ||
     (reference_.valid && ((delivery-reference_.delivery)>>31 ||
      (delivery==reference_.delivery && (sid!=reference_.sid || frame<=reference_.frame))))){
   ++counts_.rejected;return false;
  }
  const int64_t elapsed=static_cast<int64_t>(samples*250-((word(record+56,4)*250)>>32));
  const int64_t spread=allowance(elapsed)+1;
  const int64_t received_low=static_cast<int64_t>(snapshot.before_ns)-elapsed-spread-
   static_cast<int64_t>(snapshot.age_cycles)*20;
  const int64_t hi=static_cast<int64_t>(snapshot.after_ns)-elapsed+spread;
  if(reference_.valid && sid!=reference_.sid)calibration_={};
  calibration_.last={sid,frame,received_low,hi};
  const unsigned stratum=frame/400;
  if(!(calibration_.mask&(1U<<stratum))){
   calibration_.strata[stratum]=calibration_.last;
   calibration_.mask|=1U<<stratum;
  }
  reference_={sid,delivery,frame,received_low-path_budget_,hi,source_low*1000,source_high*1000,true};
  ++counts_.anchors;return true;
 }

 /* Called once per accepted physical new-frame ACK. Unknown/ambiguous
  * observations are counted without extending a presentation lifetime. */
 void ack(uint64_t sid,uint64_t capture_ns,uint64_t drm_ns){
  std::lock_guard<std::mutex> lock(mutex_);
  ++counts_.acks;
  int64_t age_low=0,age_high=0;
  if(!estimate(sid,capture_ns,drm_ns,age_low,age_high)){
   ++counts_.unknown;return;
  }
  if(counts_.acks-counts_.unknown==1){counts_.min_age_ns=age_low;counts_.max_age_ns=age_high;}
  else{
   counts_.min_age_ns=std::min(counts_.min_age_ns,age_low);
   counts_.max_age_ns=std::max(counts_.max_age_ns,age_high);
  }
  if(age_low>=0 && age_high<=kDeadline)++counts_.within;
  else if(age_low>kDeadline)++counts_.late;
  else ++counts_.ambiguous;
 }
 bool definitely_expired(uint64_t sid,uint64_t capture_ns,uint64_t now_ns) const {
  std::lock_guard<std::mutex> lock(mutex_);
  int64_t low=0,high=0;
  if(now_ns>uint64_t(INT64_MAX)-100*kSecond)return false;
  // An age already proven at the end of the calibrated window only grows.
  // Use that proof during a stall without projecting an age past the window.
  const uint64_t last_valid=static_cast<uint64_t>(reference_.tx_high+kSecond);
  return estimate(sid,capture_ns,std::min(now_ns,last_valid),low,high) && low>kDeadline;
 }
 Counts counts() const {std::lock_guard<std::mutex> lock(mutex_);return counts_;}
 Calibration calibration() const {std::lock_guard<std::mutex> lock(mutex_);return calibration_;}
private:
 bool estimate(uint64_t sid,uint64_t capture_ns,uint64_t now_ns,int64_t& low,int64_t& high) const {
  const auto& ref=reference_;
  if(!ref.valid || sid!=ref.sid || !now_ns || capture_ns>66000000ULL*1000 ||
     now_ns>uint64_t(INT64_MAX)-100*kSecond || now_ns+kSecond<static_cast<uint64_t>(ref.tx_low) ||
     now_ns>static_cast<uint64_t>(ref.tx_high+kSecond))return false;
  const int64_t delta_low=static_cast<int64_t>(capture_ns)-ref.source_high;
  const int64_t delta_high=static_cast<int64_t>(capture_ns)-ref.source_low;
  const int64_t margin=allowance(std::max(delta_high,-delta_low));
  low=static_cast<int64_t>(now_ns)-ref.tx_high-delta_high-margin;
  high=static_cast<int64_t>(now_ns)+999-ref.tx_low-delta_low+margin;
  return true;
 }
 const uint32_t path_budget_;
 mutable std::mutex mutex_;
 Reference reference_{};
 SourceLifetime lifetime_{};
 Counts counts_{};
 Calibration calibration_{};
};
}
