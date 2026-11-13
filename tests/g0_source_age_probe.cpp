#include "g0_source_age.hpp"

#include <array>
#include <cassert>
#include <iostream>

static void put(uint8_t* p,uint64_t value,unsigned bytes){
 for(unsigned i=0;i<bytes;++i)p[i]=value>>(i*8);
}

static void deadline_edges(std::array<uint8_t,96> record){
 constexpr uint64_t ns=1000000000, sid=0x12345678, tag=0xabcd0000000003e8;
 // Independent boundary values for a 3s source offset and an exact RX tag.
 // 1ns RX quantization, 1ms path bound, and ceil(3e9 * 400 / 999600) = 1200481ns.
 constexpr uint64_t last_within=10*ns-1-1000000-1200481-999;
 constexpr uint64_t last_not_definite=10*ns+1+1200481;
 constexpr uint64_t last_fresh_reference=11*ns+1;
 // At the last calibrated instant this frame is about two seconds old.
 constexpr uint64_t held_deadline=12*ns-1-1000000-400161-999;
 put(record.data()+56,0,4);
 put(record.data()+88,3000000,8);
 const v3_source_ring_clock exact{10*ns,10*ns,tag,0,0};
 v3decode::SourceAge age(1000000);
 assert(age.observe(record.data(),record.size(),exact));
 assert(!age.presentation_expired(sid,0,last_within));
 assert(age.presentation_expired(sid,0,last_within+1));
 assert(age.presentation_expired(sid,0,last_not_definite+1));
 assert(age.presentation_expired(sid,0,last_fresh_reference));
 assert(age.presentation_expired(sid,0,last_fresh_reference+1));
 assert(age.presentation_expired(sid,0,60*ns));
 assert(!age.presentation_expired(sid,2*ns,held_deadline));
 assert(age.presentation_expired(sid,2*ns,held_deadline+1));
 assert(!age.presentation_expired(sid,6*ns,60*ns)); // capture was still in the future at the horizon
 assert(!age.presentation_expired(sid,UINT64_MAX,10*ns));
 age.ack(sid,0,last_within);
 age.ack(sid,0,last_within+1);
 age.ack(sid,0,last_not_definite);
 age.ack(sid,0,last_not_definite+1);
 age.ack(sid,0,last_fresh_reference+1);
 age.ack(sid,6*ns,10*ns); // timestamp clearly in the future
 const auto counters=age.counts();
 assert(counters.acks==6 && counters.within==1 && counters.late==1 &&
        counters.unknown==1 && counters.ambiguous==3);
 put(record.data()+16,sid+1,4);
 assert(!age.observe(record.data(),record.size(),exact)); // same delivery, foreign SID
 put(record.data()+20,10,4);
 assert(age.observe(record.data(),record.size(),exact));
 assert(!age.presentation_expired(sid,0,last_not_definite+1));
 assert(age.presentation_expired(sid+1,0,last_not_definite+1));
 put(record.data()+16,sid,4);
 put(record.data()+20,9,4);
 assert(!age.observe(record.data(),record.size(),exact)); // retired delivery
 assert(age.presentation_expired(sid+1,0,last_not_definite+1));
}

static void source_lifetimes(std::array<uint8_t,96> record){
 constexpr uint32_t sid=0x12345678;
 constexpr uint64_t ns=1000000000, tag=0xabcd0000000003e8;
 const v3_source_ring_clock snapshot{10*ns,10*ns,tag,0,0};
 put(record.data()+56,0,4);
 put(record.data()+20,UINT32_MAX-1,4);
 const auto old_a=record;
 v3decode::SourceAge age(1000000);
 assert(age.observe(record.data(),record.size(),snapshot));
 age.adopt_source(sid,UINT32_MAX-1);
 assert(age.presentation_expired(sid,0,60*ns)); // VRT3 before first SOURCE is usable

 age.adopt_source(sid+1,UINT32_MAX);
 age.adopt_source(sid,0);
 assert(!age.presentation_expired(sid,0,60*ns));
 assert(!age.observe(old_a.data(),old_a.size(),snapshot));
 put(record.data()+20,0,4);
 put(record.data()+16,sid+1,4);
 assert(!age.observe(record.data(),record.size(),snapshot)); // foreign SID in admitted epoch
 put(record.data()+16,sid,4);
 assert(age.observe(record.data(),record.size(),snapshot));
 assert(age.presentation_expired(sid,0,60*ns));
}

static void lower_bound_only(std::array<uint8_t,96> record){
 constexpr uint64_t ns=1000000000, sid=0x12345678, tag=0xabcd0000000003e8;
 constexpr uint64_t last_not_definite=10*ns+1+1200481;
 put(record.data()+56,0,4);
 put(record.data()+88,3000000,8);
 const v3_source_ring_clock exact{10*ns,10*ns,tag,0,0};
 v3decode::SourceAge age(0,true);
 v3decode::SourceAge disabled(0);
 assert(!disabled.observe(record.data(),record.size(),exact));
 assert(!age.presentation_expired(sid,0,last_not_definite+1));
 assert(age.observe(record.data(),record.size(),exact));
 assert(!age.presentation_expired(sid,0,last_not_definite));
 assert(age.presentation_expired(sid,0,last_not_definite+1));
 assert(age.presentation_expired(sid,0,60*ns));
 assert(!age.presentation_expired(sid,2*ns,12*ns+1+400161));
 assert(age.presentation_expired(sid,2*ns,12*ns+1+400162));
 assert(!age.presentation_expired(sid,6*ns,60*ns));
 age.ack(sid,0,10*ns-100000000);
 age.ack(sid,0,last_not_definite);
 age.ack(sid,0,last_not_definite+1);
 age.ack(sid+1,0,last_not_definite+1);
 const auto counters=age.counts();
 assert(counters.acks==4 && counters.within==0 && counters.late==1 &&
        counters.ambiguous==2 && counters.unknown==1 && counters.max_age_ns==0);
 age.adopt_source(sid+1,10);
 assert(!age.presentation_expired(sid,0,60*ns));
 assert(!age.observe(record.data(),record.size(),exact));
}

int main(){
 constexpr uint64_t second=1000000000, epoch=uint64_t(0xabcd)<<48;
 std::array<uint8_t,96> record{};
 put(record.data()+14,4,2);
 put(record.data()+16,0x12345678,4);
 put(record.data()+20,9,4);
 put(record.data()+32,epoch|1000,8);
 put(record.data()+48,3,4);
 put(record.data()+52,65535,2);
 put(record.data()+54,32,2);
 put(record.data()+56,0x80000000,4);
 std::memcpy(record.data()+64,"VRT3",4);
 put(record.data()+68,0x0001f001,4);
 put(record.data()+72,3*80000+1684,8);
 put(record.data()+80,3000000,8);
 put(record.data()+88,3000100,8);
 v3_source_ring_clock snapshot{10*second,10*second+1200,epoch|1400,12,0};
 deadline_edges(record);
 source_lifetimes(record);
 lower_bound_only(record);
 v3decode::SourceAge age(1000000);
 assert(!age.presentation_expired(0x12345678,0,10*second));
 age.ack(0x12345678,0,10*second);
 assert(age.observe(record.data(),record.size(),snapshot));
 assert(!age.observe(record.data(),record.size(),snapshot)); // duplicate
 assert(!age.presentation_expired(0x12345678,0,10*second-100000000));
 assert(age.presentation_expired(0x12345678,0,10*second+100000000));
 assert(age.presentation_expired(0x12345678,0,10*second-500000));
 assert(!age.presentation_expired(0x12345679,0,10*second+100000000));
 assert(age.presentation_expired(0x12345678,0,12*second));
 age.ack(0x12345678,0,10*second-100000000); // within
 age.ack(0x12345678,0,10*second+100000000); // definitely late
 age.ack(0x12345678,0,10*second-500000);    // straddles deadline
 age.ack(0x12345679,0,10*second);          // unrelated video
 age.ack(0x12345678,0,12*second);          // reference expired
 assert(!age.observe(record.data(),95,snapshot));
 put(record.data()+48,4,4);
 assert(!age.observe(record.data(),record.size(),snapshot)); // wrong TX index
 put(record.data()+72,4*80000+1684,8);
 auto bad=snapshot;bad.adc_tag^=uint64_t(1)<<48;
 assert(!age.observe(record.data(),record.size(),bad));
 bad=snapshot;bad.age_cycles=63;
 assert(!age.observe(record.data(),record.size(),bad));
 put(record.data()+20,8,4);
 assert(!age.observe(record.data(),record.size(),snapshot));
 put(record.data()+20,10,4);
 assert(age.observe(record.data(),record.size(),snapshot));
 const auto counters=age.counts();
 assert(counters.anchors==2 && counters.rejected==6);
 assert(counters.acks==6 && counters.unknown==3 && counters.within==1 &&
        counters.late==1 && counters.ambiguous==1);
 assert(counters.min_age_ns<3000000000LL && counters.max_age_ns>3000000000LL);
 const auto calibration=age.calibration();
 assert(calibration.mask==1 && calibration.strata[0].frame==3 && calibration.last.frame==4);
 assert(calibration.last.board_low_ns<calibration.last.board_high_ns);
 assert(calibration.last.board_low_ns>9999800000LL && calibration.last.board_high_ns<10000000000LL);
 std::cout<<"SOURCE_AGE_CONDITIONAL_CONTROLS_PASS\nSOURCE_AGE_EDGE_CONTROLS_PASS\nSOURCE_AGE_LOWER_BOUND_CONTROLS_PASS\n";
}
