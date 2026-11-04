#include "mrf1_native_frontend.h"

#include <fstream>
#include <iostream>
#include <iterator>
#include <vector>

int main(int argc, char **argv) {
  if (argc != 2) { std::cerr << "usage: mrf1_rxth_file_check RXTH.bin\n"; return 2; }
  std::ifstream input(argv[1], std::ios::binary);
  if (!input) { std::cerr << "cannot open input\n"; return 2; }
  std::vector<uint8_t> bytes((std::istreambuf_iterator<char>(input)), std::istreambuf_iterator<char>());
  if (bytes.empty()) { std::cerr << "empty input\n"; return 2; }
  mrf1_nf_frontend *front=nullptr;
  if (mrf1_nf_create(nullptr,&front)!=MRF1_NF_OK) return 2;
  mrf1_nf_batch_view batch{};
  mrf1_nf_status status=MRF1_NF_NEED_MORE;
  for (size_t at=0;at<bytes.size();at+=137) {
    const size_t n=std::min<size_t>(137,bytes.size()-at);
    status=mrf1_nf_feed(front,bytes.data()+at,n,&batch);
    if (status<0) break;
  }
  if (status!=MRF1_NF_OK) status=mrf1_nf_finish(front,&batch);
  std::cout << "status=" << mrf1_nf_status_string(status);
  if (status==MRF1_NF_OK) std::cout << " frame_id=" << batch.provenance.frame_id << " tasks=" << batch.task_bytes_len << " headers=" << batch.header_bytes;
  std::cout << '\n';
  mrf1_nf_destroy(front);
  return status==MRF1_NF_OK ? 0 : 1;
}
