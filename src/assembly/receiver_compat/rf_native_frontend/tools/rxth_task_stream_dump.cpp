#include "mrf1_native_frontend.h"

#include <array>
#include <fstream>
#include <iostream>
#include <iterator>
#include <vector>

// Test-only extraction of actual native frontend output.  Records are exactly
// 40 public MRF header bytes followed by 63,360 MIP0 bytes; they contain no
// transmitter truth and are fed to the independent profile-0 reference.
int main(int argc, char **argv) {
  if (argc != 3) return 2;
  std::ifstream input(argv[1], std::ios::binary);
  std::ofstream output(argv[2], std::ios::binary | std::ios::trunc);
  std::vector<uint8_t> bytes((std::istreambuf_iterator<char>(input)), {});
  if (!input || !output || bytes.empty() || bytes.size() % MRF1_RXTH_BLOCK_BYTES != 0U) return 2;
  mrf1_nf_frontend *front = nullptr;
  if (mrf1_nf_create(nullptr, &front) != MRF1_NF_OK) return 3;
  std::array<uint8_t, 40> headers{};
  std::array<uint8_t, MRF1_BATCH_TASK_BYTES> tasks{};
  const mrf1_nf_output_slot slot{headers.data(), headers.size(), tasks.data(), tasks.size()};
  size_t frames = 0;
  for (size_t at = 0; at != bytes.size(); at += MRF1_RXTH_BLOCK_BYTES) {
    mrf1_nf_batch_view batch{};
    const mrf1_nf_status status = mrf1_nf_feed_into(front, bytes.data() + at,
                                                     MRF1_RXTH_BLOCK_BYTES, &slot, &batch);
    if (status < 0) { mrf1_nf_destroy(front); return 4; }
    if (status != MRF1_NF_OK) continue;
    if (batch.header_bytes != 40U || batch.task_bytes_len != MRF1_BATCH_TASK_BYTES) {
      mrf1_nf_destroy(front); return 5;
    }
    output.write(reinterpret_cast<const char *>(batch.headers), static_cast<std::streamsize>(batch.header_bytes));
    output.write(reinterpret_cast<const char *>(batch.task_bytes), static_cast<std::streamsize>(batch.task_bytes_len));
    if (!output) { mrf1_nf_destroy(front); return 6; }
    ++frames;
  }
  mrf1_nf_destroy(front);
  if (frames == 0) return 7;
  std::cout << "{\"records\":" << frames << ",\"record_bytes\":"
            << (40U + MRF1_BATCH_TASK_BYTES) << ",\"tasks_per_record\":"
            << MRF1_TASK_COUNT << "}\n";
  return 0;
}
