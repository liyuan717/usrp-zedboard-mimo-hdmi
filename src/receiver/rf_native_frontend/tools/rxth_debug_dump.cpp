#include "mrf1_native_frontend.h"

#include <array>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <iterator>
#include <vector>

int main(int argc, char **argv) {
  if (argc != 2) return 2;
  std::ifstream input(argv[1], std::ios::binary);
  std::vector<uint8_t> bytes((std::istreambuf_iterator<char>(input)), std::istreambuf_iterator<char>());
  mrf1_nf_frontend *front = nullptr;
  if (bytes.empty() || mrf1_nf_create(nullptr, &front) != MRF1_NF_OK) return 3;
  std::array<uint8_t, 40> headers{}; std::array<uint8_t, MRF1_BATCH_TASK_BYTES> tasks{};
  const mrf1_nf_output_slot slot{headers.data(),headers.size(),tasks.data(),tasks.size()};
  mrf1_nf_batch_view batch{}; mrf1_nf_status status=MRF1_NF_NEED_MORE;
  for (size_t at=0; at<bytes.size(); at+=MRF1_RXTH_BLOCK_BYTES) {
    status=mrf1_nf_feed_into(front,bytes.data()+at,MRF1_RXTH_BLOCK_BYTES,&slot,&batch);
    if(status<0) break;
    if(status==MRF1_NF_OK) break;
  }
  mrf1_nf_debug_snapshot debug{};mrf1_nf_get_debug_snapshot(front,&debug);mrf1_nf_destroy(front);
  if(status!=MRF1_NF_OK)return 4;
  std::cout<<std::setprecision(17)<<"{\"cfo_hz\":"<<debug.cfo_hz<<",\"timing\":"<<debug.timing_offset_samples<<",\"sfo_ppm\":"<<debug.sfo_ppm<<",\"h\":[";
  for(unsigned i=0;i<8;++i){if(i)std::cout<<',';std::cout<<debug.training_h_re_im_rowmajor[i];}
  std::cout<<"],\"y0\":[";for(unsigned i=0;i<4;++i){if(i)std::cout<<',';std::cout<<debug.first_detector_y_re_im[i];}
  std::cout<<"],\"pilot_phase\":"<<debug.first_pilot_phase_radians<<"}\n";
  return 0;
}
