#include "hdmi_presenter/app_runner.h"

#include <array>
#include <cassert>
#include <cerrno>
#include <chrono>
#include <condition_variable>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <mutex>
#include <thread>
#include <unistd.h>
#include <vector>

using namespace hdmi_presenter;

namespace {
enum class BackendMode { kAutoAck, kNeverAck, kSubmitFailure };
enum class StreamEnd { kFatal, kMalformed, kEof, kStopAck };

class HostOnlyPhysicalBackend final : public PresentBackend {
 public:
  explicit HostOnlyPhysicalBackend(BackendMode mode) : mode_(mode) {}
  bool supports_physical_presentation() const override { return true; }
  std::string unavailable_reason() const override { return {}; }
  int initial_displayed_slot() const override { return 2; }
  uint64_t mode_refresh_millihz() const override { return 60000; }
  uint8_t* xrgb_slot(unsigned index, size_t bytes) override {
    return index < pixels_.size() && bytes == kXrgb8888Bytes ? pixels_[index].data() : nullptr;
  }
  BackendSubmitResult submit(const SlotView&, uint64_t token, std::string* error) override {
    std::lock_guard<std::mutex> lock(mutex_);
    if (mode_ == BackendMode::kSubmitFailure) {
      if (error) *error = "host-only injected submit failure";
      return BackendSubmitResult::kFailure;
    }
    pending_token_ = token;
    cv_.notify_all();
    return BackendSubmitResult::kAccepted;
  }
  BackendAckResult wait_for_physical_ack(PhysicalAck* acknowledgement, int timeout_ms,
                                          std::string*) override {
    std::unique_lock<std::mutex> lock(mutex_);
    if (mode_ == BackendMode::kNeverAck) {
      cv_.wait_for(lock, std::chrono::milliseconds(timeout_ms));
      return BackendAckResult::kTimeout;
    }
    if (!cv_.wait_for(lock, std::chrono::milliseconds(timeout_ms), [&] { return pending_token_ != 0; }))
      return BackendAckResult::kTimeout;
    acknowledgement->token = pending_token_;
    acknowledgement->drm_sequence = ++sequence_;
    acknowledgement->drm_tv_sec = 1;
    acknowledgement->drm_tv_usec = sequence_;
    acknowledgement->observer_session = 77;
    pending_token_ = 0;
    return BackendAckResult::kAck;
  }
  void shutdown() override { std::lock_guard<std::mutex> lock(mutex_); shutdown_ = true; cv_.notify_all(); }
  bool wait_for_submit() {
    std::unique_lock<std::mutex> lock(mutex_);
    return cv_.wait_for(lock, std::chrono::seconds(2), [&] { return pending_token_ != 0 || shutdown_; });
  }

 private:
  BackendMode mode_;
  std::array<std::vector<uint8_t>, 3> pixels_{{std::vector<uint8_t>(kXrgb8888Bytes),
                                                std::vector<uint8_t>(kXrgb8888Bytes),
                                                std::vector<uint8_t>(kXrgb8888Bytes)}};
  std::mutex mutex_;
  std::condition_variable cv_;
  uint64_t pending_token_{};
  uint32_t sequence_{};
  bool shutdown_{};
};

bool write_all(int fd, const uint8_t* data, size_t size) {
  size_t offset = 0;
  while (offset != size) {
    const ssize_t count = write(fd, data + offset, size - offset);
    if (count > 0) { offset += static_cast<size_t>(count); continue; }
    if (count < 0 && errno == EINTR) continue;
    return false;
  }
  return true;
}

std::array<uint8_t, kVcodecHeaderBytes> header(VcdoRecordType type,
                                                const FrameIdentity& id = {},
                                                uint64_t token = 0,
                                                uint32_t crc = 0) {
  std::array<uint8_t, kVcodecHeaderBytes> raw{};
  std::memcpy(raw.data(), "VCDO", 4);
  store_le16(raw.data() + 4, 1);
  store_le16(raw.data() + 6, kVcodecHeaderBytes);
  store_le32(raw.data() + 8, static_cast<uint32_t>(type));
  if (type == VcdoRecordType::kFrame) {
    store_le64(raw.data() + 16, id.session_id);
    store_le64(raw.data() + 24, id.frame_id);
    store_le64(raw.data() + 32, token);
    store_le64(raw.data() + 40, id.pts_ns / 1000000U);
    store_le32(raw.data() + 48, kWidth);
    store_le32(raw.data() + 52, kHeight);
    store_le32(raw.data() + 56, kRgb565Le);
    store_le32(raw.data() + 60, kRgb565Bytes);
    store_le32(raw.data() + 64, crc);
  } else {
    store_le32(raw.data() + 12, kVcodecNeedIdr);
  }
  return raw;
}

std::string proof_path(const char* tag) {
  return std::string("/tmp/codec-session-runner-") + tag + "-" + std::to_string(getpid()) + ".json";
}

std::string read_text(const std::string& path) {
  std::ifstream input(path);
  return {std::istreambuf_iterator<char>(input), std::istreambuf_iterator<char>()};
}

void run_error_after_one_physical_frame(const char* tag, StreamEnd end) {
  int pipefd[2]{};
  assert(pipe(pipefd) == 0);
  std::vector<uint8_t> payload(kRgb565Bytes, 0x4a);
  const FrameIdentity id{91, 3, 40000000U, ieee_crc32(payload.data(), payload.size())};
  const auto frame = header(VcdoRecordType::kFrame, id, 8, id.payload_crc32);
  const std::string proof = proof_path(tag);
  std::remove(proof.c_str());
  auto backend = std::make_unique<HostOnlyPhysicalBackend>(BackendMode::kAutoAck);
  std::thread writer([&] {
    assert(write_all(pipefd[1], frame.data(), frame.size()));
    assert(write_all(pipefd[1], payload.data(), payload.size()));
    /* The host-only backend emits a physical-style ACK before the injected
     * terminal fault, proving runner invalidation covers an existing ledger. */
    std::this_thread::sleep_for(std::chrono::milliseconds(150));
    if (end == StreamEnd::kFatal) {
      const auto fatal = header(VcdoRecordType::kFatal);
      assert(write_all(pipefd[1], fatal.data(), fatal.size()));
    } else if (end == StreamEnd::kMalformed) {
      std::array<uint8_t, kVcodecHeaderBytes> malformed{};
      assert(write_all(pipefd[1], malformed.data(), malformed.size()));
    }
    close(pipefd[1]);
  });
  ReceiverConfig config{};
  config.read_timeout_ms = 1000;
  config.queue_wait_ms = 1000;
  config.drain_timeout_ms = 1000;
  config.stage_timing = true;
  const CodecSessionResult result = run_codec_presenter_session(std::move(backend), pipefd[0], config, proof);
  close(pipefd[0]);
  writer.join();
  assert(result.status != 0 && result.metrics.proof_records == 1);
  assert(result.metrics.proof_failures == 1 && result.proof_status == "written");
  assert(result.stage_timing_summary.find("CLOCK_MONOTONIC") != std::string::npos);
  assert(result.stage_timing_summary.find("CLOCK_THREAD_CPUTIME_ID") != std::string::npos);
  assert(result.stage_timing_summary.find("\"payload_read\":{\"count\":1") != std::string::npos);
  assert(result.stage_timing_summary.find("\"crc\":{\"count\":1") != std::string::npos);
  const std::string json = read_text(proof);
  assert(json.find("\"physical\":true") != std::string::npos);
  assert(json.find("\"capture_status\":\"invalid\"") != std::string::npos);
  assert(json.find("\"acceptance_valid\":false") != std::string::npos);
  assert(json.find("\"proof_failures\":1") != std::string::npos);
  assert(json.find("\"records\":[{") != std::string::npos);
  std::remove(proof.c_str());
}

void test_drain_timeout_invalidates_proof() {
  int pipefd[2]{};
  assert(pipe(pipefd) == 0);
  std::vector<uint8_t> payload(kRgb565Bytes, 0x5c);
  const FrameIdentity id{92, 4, 41000000U, ieee_crc32(payload.data(), payload.size())};
  const auto frame = header(VcdoRecordType::kFrame, id, 9, id.payload_crc32);
  const auto stop = header(VcdoRecordType::kStopAck);
  const std::string proof = proof_path("drain");
  std::remove(proof.c_str());
  std::thread writer([&] {
    assert(write_all(pipefd[1], frame.data(), frame.size()));
    assert(write_all(pipefd[1], payload.data(), payload.size()));
    assert(write_all(pipefd[1], stop.data(), stop.size()));
    close(pipefd[1]);
  });
  ReceiverConfig config{};
  config.read_timeout_ms = 1000;
  config.queue_wait_ms = 1000;
  config.drain_timeout_ms = 20;
  const CodecSessionResult result = run_codec_presenter_session(
      std::make_unique<HostOnlyPhysicalBackend>(BackendMode::kNeverAck), pipefd[0], config, proof);
  close(pipefd[0]);
  writer.join();
  assert(result.status != 0 && result.receiver.saw_stop_ack);
  const std::string json = read_text(proof);
  assert(json.find("\"physical\":true") != std::string::npos);
  assert(json.find("\"capture_status\":\"invalid\"") != std::string::npos);
  assert(json.find("presentation drain timeout") != std::string::npos);
  std::remove(proof.c_str());
}

void test_submit_failure_invalidates_proof() {
  int pipefd[2]{};
  assert(pipe(pipefd) == 0);
  std::vector<uint8_t> payload(kRgb565Bytes, 0x6d);
  const FrameIdentity id{93, 5, 42000000U, ieee_crc32(payload.data(), payload.size())};
  const auto frame = header(VcdoRecordType::kFrame, id, 10, id.payload_crc32);
  const auto stop = header(VcdoRecordType::kStopAck);
  const std::string proof = proof_path("submit-failure");
  std::remove(proof.c_str());
  std::thread writer([&] {
    assert(write_all(pipefd[1], frame.data(), frame.size()));
    assert(write_all(pipefd[1], payload.data(), payload.size()));
    std::this_thread::sleep_for(std::chrono::milliseconds(100));
    assert(write_all(pipefd[1], stop.data(), stop.size()));
    close(pipefd[1]);
  });
  ReceiverConfig config{};
  config.read_timeout_ms = 1000;
  config.queue_wait_ms = 1000;
  config.drain_timeout_ms = 1000;
  const CodecSessionResult result = run_codec_presenter_session(
      std::make_unique<HostOnlyPhysicalBackend>(BackendMode::kSubmitFailure), pipefd[0], config, proof);
  close(pipefd[0]);
  writer.join();
  assert(result.status != 0 && result.metrics.backend_failures == 1);
  const std::string json = read_text(proof);
  assert(json.find("\"acceptance_valid\":false") != std::string::npos);
  assert(json.find("host-only injected submit failure") != std::string::npos);
  std::remove(proof.c_str());
}
}  // namespace

int main() {
  run_error_after_one_physical_frame("fatal", StreamEnd::kFatal);
  run_error_after_one_physical_frame("malformed", StreamEnd::kMalformed);
  run_error_after_one_physical_frame("eof", StreamEnd::kEof);
  test_drain_timeout_invalidates_proof();
  test_submit_failure_invalidates_proof();
  return 0;
}
