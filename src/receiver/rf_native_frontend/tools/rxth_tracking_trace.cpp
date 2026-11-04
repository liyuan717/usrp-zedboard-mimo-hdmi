#include "mrf1_native_frontend.h"

#include <array>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <iterator>
#include <vector>

// Diagnostic-only trace for deterministic fixture failures.  It deliberately
// observes public frontend state after every RXTH block; no payload decisions
// or transmitter truth enter the receive implementation.
int main(int argc, char **argv) {
  if (argc != 2) return 2;
  std::ifstream input(argv[1], std::ios::binary);
  std::vector<uint8_t> bytes((std::istreambuf_iterator<char>(input)), {});
  if (bytes.empty() || bytes.size() % MRF1_RXTH_BLOCK_BYTES != 0U) return 2;
  mrf1_nf_frontend *front = nullptr;
  if (mrf1_nf_create(nullptr, &front) != MRF1_NF_OK) return 3;
  std::array<uint8_t, 40> headers{};
  std::array<uint8_t, MRF1_BATCH_TASK_BYTES> tasks{};
  const mrf1_nf_output_slot slot{headers.data(), headers.size(), tasks.data(), tasks.size()};
  for (size_t at = 0; at != bytes.size(); at += MRF1_RXTH_BLOCK_BYTES) {
    mrf1_nf_batch_view batch{};
    const auto status = mrf1_nf_feed_into(front, bytes.data() + at,
                                            MRF1_RXTH_BLOCK_BYTES, &slot, &batch);
    mrf1_nf_stats stats{};
    mrf1_nf_debug_snapshot debug{};
    mrf1_nf_get_stats(front, &stats);
    mrf1_nf_get_debug_snapshot(front, &debug);
    if (status == MRF1_NF_OK || status == MRF1_NF_REACQUIRING) {
      std::cout << std::setprecision(10) << "block=" << at / MRF1_RXTH_BLOCK_BYTES
                << " status=" << mrf1_nf_status_string(status)
                << " frame=" << (status == MRF1_NF_OK ? batch.provenance.frame_id : 0U)
                << " acq=" << stats.acquisition_attempts
                << " reset=" << stats.continuity_resets
                << " state=" << stats.state
                << " buffered=" << stats.buffered_samples
                << " cfo=" << debug.cfo_hz
                << " timing=" << debug.timing_offset_samples
                << " sfo=" << debug.sfo_ppm << '\n';
    }
    if (status < 0) { mrf1_nf_destroy(front); return 4; }
  }
  mrf1_nf_destroy(front);
  return 0;
}
