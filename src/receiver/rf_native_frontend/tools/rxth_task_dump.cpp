#include "mrf1_native_frontend.h"

#include <array>
#include <fstream>
#include <iterator>
#include <vector>

int main(int argc, char **argv) {
  if (argc != 3) return 2;
  std::ifstream input(argv[1], std::ios::binary);
  if (!input) return 2;
  std::vector<uint8_t> capture((std::istreambuf_iterator<char>(input)), std::istreambuf_iterator<char>());
  mrf1_nf_frontend *front = nullptr;
  if (mrf1_nf_create(nullptr, &front) != MRF1_NF_OK) return 3;
  std::array<uint8_t, 40> headers{};
  std::array<uint8_t, MRF1_BATCH_TASK_BYTES> tasks{};
  const mrf1_nf_output_slot slot{headers.data(), headers.size(), tasks.data(), tasks.size()};
  mrf1_nf_batch_view batch{};
  mrf1_nf_status status = MRF1_NF_NEED_MORE;
  for (size_t offset = 0; offset < capture.size(); offset += MRF1_RXTH_BLOCK_BYTES) {
    if (capture.size() - offset < MRF1_RXTH_BLOCK_BYTES) { mrf1_nf_destroy(front); return 4; }
    status = mrf1_nf_feed_into(front, capture.data() + offset, MRF1_RXTH_BLOCK_BYTES, &slot, &batch);
    if (status < 0) { mrf1_nf_destroy(front); return 5; }
    if (status == MRF1_NF_OK) break;
  }
  if (status != MRF1_NF_OK) { mrf1_nf_destroy(front); return 6; }
  std::ofstream output(argv[2], std::ios::binary | std::ios::trunc);
  output.write(reinterpret_cast<const char *>(batch.headers), batch.header_bytes);
  output.write(reinterpret_cast<const char *>(batch.task_bytes), batch.task_bytes_len);
  mrf1_nf_destroy(front);
  return output ? 0 : 7;
}
