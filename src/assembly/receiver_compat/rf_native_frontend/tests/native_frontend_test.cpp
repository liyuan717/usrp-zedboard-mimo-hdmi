#include "mrf1_native_frontend.h"

#include <array>
#include <cstdlib>
#include <cstdint>
#include <cstring>
#include <iostream>
#include <vector>

namespace {
#define CHECK(condition) do { \
  if (!(condition)) { \
    std::cerr << "CHECK failed: " #condition " at " << __FILE__ << ':' << __LINE__ << '\n'; \
    std::exit(EXIT_FAILURE); \
  } \
} while (false)

uint32_t crc32(const uint8_t *p, size_t n) {
  uint32_t c=0xffffffffU; for(size_t i=0;i<n;++i){c^=p[i];for(unsigned b=0;b!=8;++b)c=(c>>1U)^((c&1U)?0xedb88320U:0U);} return c^0xffffffffU;
}
void put32(uint8_t *p,uint32_t v){for(unsigned i=0;i<4;++i)p[i]=uint8_t(v>>(8U*i));}
void put64(uint8_t *p,uint64_t v){for(unsigned i=0;i<8;++i)p[i]=uint8_t(v>>(8U*i));}
std::vector<uint8_t> block(uint32_t epoch,uint32_t seq,uint64_t first) {
  std::vector<uint8_t> v(MRF1_RXTH_BLOCK_BYTES,0);std::memcpy(v.data(),"RXTH",4);v[4]=1;v[5]=32;v[6]=1;put32(v.data()+8,epoch);put32(v.data()+12,seq);put64(v.data()+16,first);put32(v.data()+24,1024);put32(v.data()+28,crc32(v.data(),28));return v;
}
void task_codec() {
  const double h[]={1,0,0,0,0,0,1,0}; const double y[]={1,0,0,1}; std::array<uint8_t,MRF1_TASK_BYTES> out{};
  CHECK(mrf1_nf_pack_q16_task(1,2,64,1,32768,h,y,out.data())==MRF1_NF_OK);
  CHECK(out[0]=='M'&&out[1]=='I'&&out[2]=='P'&&out[3]=='0');
  CHECK(out[4]==1&&out[6]==1&&out[7]==8); // v1/input type/15 beats log2
  CHECK(out[8]==1&&out[12]==2); // task frame_id/config_id little endian
  CHECK(out[18]==1&&out[19]==3&&out[20]==64&&out[36]==64&&out[38]==64);
  CHECK(mrf1_nf_pack_q16_task(0,2,64,1,0,h,y,out.data())==MRF1_NF_ARGUMENT);
}
void transport_codec() {
  mrf1_nf_frontend *f=nullptr;CHECK(mrf1_nf_create(nullptr,&f)==MRF1_NF_OK&&f);
  const auto b0=block(7,9,4096);mrf1_nf_batch_view batch{};
  std::array<uint8_t,40> headers{};
  std::array<uint8_t,MRF1_BATCH_TASK_BYTES> tasks{};
  const mrf1_nf_output_slot slot{headers.data(),headers.size(),tasks.data(),tasks.size()};
  CHECK(mrf1_nf_feed_into(f,b0.data(),100,&slot,&batch)==MRF1_NF_NEED_MORE);
  CHECK(mrf1_nf_feed_into(f,b0.data()+100,b0.size()-100,&slot,&batch)==MRF1_NF_NEED_MORE);
  mrf1_nf_stats st{};mrf1_nf_get_stats(f,&st);CHECK(st.rxth_blocks==1&&st.current_epoch==7&&st.buffered_samples==1024);
  auto bad=block(7,10,5120);bad[32]=1; // low bits non-zero, a prohibited legacy-IIO scan.
  CHECK(mrf1_nf_feed(f,bad.data(),bad.size(),&batch)==MRF1_NF_TRANSPORT);
  mrf1_nf_destroy(f);
}
}
int main() { task_codec(); transport_codec(); std::cout << "native_frontend_tests: pass\n"; }
