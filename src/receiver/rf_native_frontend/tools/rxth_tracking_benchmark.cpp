#include "mrf1_native_frontend.h"

#include <algorithm>
#include <array>
#include <chrono>
#include <cstdint>
#include <cstring>
#include <fstream>
#include <iostream>
#include <iterator>
#include <vector>

namespace {
uint32_t crc32(const uint8_t *data, size_t size) {
  uint32_t crc = 0xffffffffU;
  for (size_t i = 0; i != size; ++i) {
    crc ^= data[i];
    for (unsigned bit = 0; bit != 8; ++bit)
      crc = (crc >> 1U) ^ ((crc & 1U) ? 0xedb88320U : 0U);
  }
  return crc ^ 0xffffffffU;
}
bool valid_header(const uint8_t *header, uint8_t stream, uint32_t frame_id) {
  const uint32_t crc = uint32_t(header[16]) | (uint32_t(header[17]) << 8U) |
                       (uint32_t(header[18]) << 16U) | (uint32_t(header[19]) << 24U);
  const uint32_t id = uint32_t(header[8]) | (uint32_t(header[9]) << 8U) |
                      (uint32_t(header[10]) << 16U) | (uint32_t(header[11]) << 24U);
  const uint32_t length = uint32_t(header[12]) | (uint32_t(header[13]) << 8U) |
                          (uint32_t(header[14]) << 16U) | (uint32_t(header[15]) << 24U);
  return std::memcmp(header, "MRF1", 4) == 0 && header[4] == 1U && header[5] == stream &&
         header[6] == 1U && header[7] == 0U && id == frame_id && length == 128U && crc32(header, 16) == crc;
}
}  // namespace

int main(int argc, char **argv) {
  if (argc != 2) return 2;
  std::ifstream input(argv[1], std::ios::binary);
  if (!input) return 2;
  std::vector<uint8_t> bytes((std::istreambuf_iterator<char>(input)), std::istreambuf_iterator<char>());
  if (bytes.size() < 8U * MRF1_RXTH_BLOCK_BYTES) return 2;
  mrf1_nf_frontend *front = nullptr;
  if (mrf1_nf_create(nullptr, &front) != MRF1_NF_OK) return 3;
  std::array<uint8_t, 40> headers{};
  std::array<uint8_t, MRF1_BATCH_TASK_BYTES> tasks{};
  const mrf1_nf_output_slot slot{headers.data(), headers.size(), tasks.data(), tasks.size()};
  mrf1_nf_batch_view batch{};
  std::vector<double> completed_ms;
  std::vector<uint32_t> task_crc;
  std::vector<uint32_t> frame_ids;
  uint64_t max_buffered_samples = 0;
  bool valid_batches = true;
  for (size_t offset = 0; offset < bytes.size(); offset += MRF1_RXTH_BLOCK_BYTES) {
    const auto before = std::chrono::steady_clock::now();
    const auto status = mrf1_nf_feed_into(front, bytes.data() + offset, MRF1_RXTH_BLOCK_BYTES,
                                           &slot, &batch);
    const auto after = std::chrono::steady_clock::now();
    if (status < 0) return 4;
    mrf1_nf_stats running{};
    mrf1_nf_get_stats(front, &running);
    max_buffered_samples = std::max(max_buffered_samples, running.buffered_samples);
    if (status == MRF1_NF_OK) {
      valid_batches = valid_batches && batch.header_bytes == 40U &&
                      batch.task_bytes_len == MRF1_BATCH_TASK_BYTES &&
                      valid_header(batch.headers, 0U, batch.provenance.frame_id) &&
                      valid_header(batch.headers + 20U, 1U, batch.provenance.frame_id);
      completed_ms.push_back(std::chrono::duration<double, std::milli>(after - before).count());
      uint32_t crc = 0xffffffffU;
      for (size_t i = 0; i != batch.task_bytes_len; ++i) {
        crc ^= batch.task_bytes[i];
        for (unsigned bit = 0; bit != 8; ++bit) crc = (crc >> 1U) ^ ((crc & 1U) ? 0xedb88320U : 0U);
      }
      task_crc.push_back(crc ^ 0xffffffffU);
      frame_ids.push_back(batch.provenance.frame_id);
    }
  }
  // Read state before destruction.  This tool is intentionally diagnostic,
  // but its acquisition/reset/buffer figures are evidence for the real
  // streaming state machine and must not come from a freed handle.
  mrf1_nf_stats stats{}; mrf1_nf_get_stats(front, &stats);
  mrf1_nf_profile profile{}; mrf1_nf_get_profile(front, &profile);
  mrf1_nf_destroy(front);
  if (completed_ms.size() < 2 || !valid_batches) return 5;
  std::vector<double> tracked_samples(completed_ms.begin() + 1, completed_ms.end());
  std::sort(tracked_samples.begin(), tracked_samples.end());
  const size_t tracked_count = tracked_samples.size();
  const double tracked_p50 = tracked_samples[(tracked_count - 1U) / 2U];
  const double tracked_p95 = tracked_samples[(95U * tracked_count + 99U) / 100U - 1U];
  const double tracked_max = tracked_samples.back();
  std::cout << "{\"scope\":\"native-host-single-owner-tracking\",\"acquisition_ms\":"
            << completed_ms[0] << ",\"tracked_ms\":" << completed_ms[1]
            << ",\"tracked_count\":" << tracked_count
            << ",\"tracked_p50_ms\":" << tracked_p50
            << ",\"tracked_p95_ms\":" << tracked_p95
            << ",\"tracked_max_ms\":" << tracked_max
            << ",\"acquisition_task_crc32\":" << task_crc[0]
            << ",\"tracked_task_crc32\":" << task_crc[1]
            << ",\"frame_ids\":[";
  for (size_t i=0;i<frame_ids.size();++i) { if(i) std::cout << ','; std::cout << frame_ids[i]; }
  std::cout << "],\"acquisition_attempts\":" << stats.acquisition_attempts
            << ",\"continuity_resets\":" << stats.continuity_resets
            << ",\"buffered_samples\":" << stats.buffered_samples
            << ",\"max_buffered_samples\":" << max_buffered_samples
            << ",\"valid_headers\":true,\"tasks_per_frame\":" << MRF1_TASK_COUNT
            << ",\"profile_ns\":{\"acquisition\":" << profile.acquisition_ns
            << ",\"nco\":" << profile.nco_ns
            << ",\"rrc\":" << profile.rrc_ns
            << ",\"training_header\":" << profile.training_header_ns
            << ",\"pilot_task\":" << profile.pilot_task_ns
            << ",\"frames\":" << profile.completed_frames
            << ",\"repetition_candidates\":" << profile.repetition_candidates
            << ",\"preamble_refinement_candidates\":" << profile.preamble_refinement_candidates
            << ",\"header_rejects\":" << profile.header_rejects
            << ",\"tracking_rejects\":" << profile.tracking_rejects << "}"
            << ",\"note\":\"not ARM or board evidence\"}\n";
  return 0;
}
