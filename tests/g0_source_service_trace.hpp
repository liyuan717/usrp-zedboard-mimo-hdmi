#pragma once

// Finite diagnostic build only. The normal receiver does not include this file.
#include <algorithm>
#include <array>
#include <cerrno>
#include <cstdint>
#include <fcntl.h>
#include <sstream>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <time.h>
#include <unistd.h>

struct G0SourceServiceTrace {
 struct Event {
  uint64_t at_ns{},release_gap_ns{},previous_processing_ns{},sequence{};
  uint32_t dropped{},queued{},ready{},completed{};
 };
 static constexpr size_t limit=64;
 std::array<Event,limit> events{};
 size_t count{};
 uint64_t omitted{},last_release_ns{},processing_begin_ns{},previous_processing_ns{};
 uint64_t max_release_gap_ns{},max_processing_ns{},release_count{};
 uint32_t last_dropped{},initial_dropped{},error{};
 void* mapping=MAP_FAILED;

 static uint64_t now(){
  timespec t{};
  return clock_gettime(CLOCK_MONOTONIC,&t)==0 ? uint64_t(t.tv_sec)*1000000000+t.tv_nsec : 0;
 }
 G0SourceServiceTrace(){
  int fd=open("/dev/mem",O_RDONLY|O_SYNC|O_CLOEXEC);
  if(fd<0){error=errno;return;}
  mapping=mmap(nullptr,4096,PROT_READ,MAP_SHARED,fd,0x7c430000);
  const int saved=errno;
  close(fd);
  if(mapping==MAP_FAILED){error=saved;return;}
  if(registers()[0]!=0x50533356){error=ENODEV;munmap(mapping,4096);mapping=MAP_FAILED;}
 }
 ~G0SourceServiceTrace(){if(mapping!=MAP_FAILED)munmap(mapping,4096);}
 G0SourceServiceTrace(const G0SourceServiceTrace&)=delete;
 G0SourceServiceTrace& operator=(const G0SourceServiceTrace&)=delete;
 volatile const uint32_t* registers() const {return static_cast<volatile const uint32_t*>(mapping);}
 void start(){if(mapping!=MAP_FAILED)initial_dropped=last_dropped=registers()[17];}
 void released(int fd,uint64_t sequence){
  const uint64_t at=now();
  const uint64_t gap=last_release_ns && at>=last_release_ns ? at-last_release_ns : 0;
  max_release_gap_ns=std::max(max_release_gap_ns,gap);
  last_release_ns=at;
  ++release_count;
  if(mapping==MAP_FAILED)return;
  const uint32_t dropped=registers()[17];
  if(dropped==last_dropped)return;
  last_dropped=dropped;
  v3_source_ring_status status{};
  if(ioctl(fd,V3_SOURCE_RING_STATUS,&status)){error=errno;return;}
  if(count==limit){++omitted;return;}
  events[count++]={at,gap,previous_processing_ns,sequence,dropped,status.queued,status.ready,status.completed};
 }
 void processing_begin(){processing_begin_ns=now();}
 void processing_end(){
  const auto at=now();
  previous_processing_ns=at>=processing_begin_ns ? at-processing_begin_ns : 0;
  max_processing_ns=std::max(max_processing_ns,previous_processing_ns);
 }
 std::string json() const {
  std::ostringstream out;
  out<<"{\"enabled\":"<<(mapping!=MAP_FAILED?"true":"false")<<",\"error\":"<<error
     <<",\"release_count\":"<<release_count<<",\"max_release_gap_ns\":"<<max_release_gap_ns
     <<",\"max_processing_ns\":"<<max_processing_ns<<",\"initial_dropped\":"<<initial_dropped
     <<",\"last_dropped\":"<<last_dropped<<",\"omitted_events\":"<<omitted<<",\"events\":[";
  for(size_t i=0;i<count;++i){
   if(i)out<<',';
   const auto& e=events[i];
   out<<"{\"at_ns\":"<<e.at_ns<<",\"release_gap_ns\":"<<e.release_gap_ns
      <<",\"previous_processing_ns\":"<<e.previous_processing_ns<<",\"sequence\":"<<e.sequence
      <<",\"dropped\":"<<e.dropped<<",\"queued\":"<<e.queued<<",\"ready\":"<<e.ready
      <<",\"completed\":"<<e.completed<<'}';
  }
  out<<"]}";
  return out.str();
 }
};
