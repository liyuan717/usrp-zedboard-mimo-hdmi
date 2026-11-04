#include "mrf1_native_frontend.h"

#include <chrono>
#include <fstream>
#include <iostream>
#include <iterator>
#include <vector>

int main(int argc, char **argv) {
  if (argc != 3) { std::cerr << "usage: rxth_benchmark RXTH.bin repetitions\n"; return 2; }
  std::ifstream input(argv[1], std::ios::binary);
  if (!input) return 2;
  std::vector<uint8_t> data((std::istreambuf_iterator<char>(input)), std::istreambuf_iterator<char>());
  const int repetitions = std::stoi(argv[2]);
  if (data.empty() || repetitions < 2 || repetitions > 1000) return 2;
  double milliseconds = 0.0;
  for (int iteration = 0; iteration != repetitions; ++iteration) {
    mrf1_nf_frontend *front = nullptr;
    if (mrf1_nf_create(nullptr, &front) != MRF1_NF_OK) return 3;
    mrf1_nf_batch_view batch{};
    const auto begin = std::chrono::steady_clock::now();
    const mrf1_nf_status status = mrf1_nf_feed(front, data.data(), data.size(), &batch);
    const auto end = std::chrono::steady_clock::now();
    mrf1_nf_destroy(front);
    if (status != MRF1_NF_OK || batch.task_bytes_len != MRF1_BATCH_TASK_BYTES) return 4;
    if (iteration != 0) milliseconds += std::chrono::duration<double, std::milli>(end - begin).count();
  }
  std::cout << "{\"scope\":\"native-host-cold-acquisition-per-frame\",\"samples\":"
            << (repetitions - 1) << ",\"mean_ms\":" << milliseconds / double(repetitions - 1)
            << ",\"note\":\"not ARM or board evidence\"}\n";
  return 0;
}
