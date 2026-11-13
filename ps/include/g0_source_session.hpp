#pragma once
#include <cstdint>

namespace v3decode {
/* The PL validates CRC/profile/session descriptors before producing SOURCE.
 * This is video admission only, never an ARM RF/session packet parser.
 * Epoch advances across PL cancellation/session changes; zero is valid. */
class SourceSession {
 uint32_t sid_=0,epoch_=0,first_epoch_=0;
 bool automatic_=false,seen_=false;
 static bool older(uint32_t a,uint32_t b){return ((a-b)&0x80000000U)!=0;}
public:
 enum Decision {kReject,kKeep,kAdopt};
 explicit SourceSession(bool automatic=false,uint32_t pinned=0):sid_(pinned),automatic_(automatic){}
 Decision observe(uint32_t sid,uint32_t epoch){
  if(!automatic_)return sid==sid_?kKeep:kReject;
  if(!seen_){seen_=true;sid_=sid;epoch_=first_epoch_=epoch;return kAdopt;}
  // Keep useful repairs from earlier epochs of this SAME session lifetime.
  // A->B->A cannot re-admit an ancient A record under the new A generation.
  if(older(epoch,first_epoch_))return kReject;
  if(sid==sid_){if(!older(epoch,epoch_))epoch_=epoch;return kKeep;}
  if(epoch==epoch_||older(epoch,epoch_))return kReject;
  sid_=sid;epoch_=first_epoch_=epoch;return kAdopt;
 }
};
}
