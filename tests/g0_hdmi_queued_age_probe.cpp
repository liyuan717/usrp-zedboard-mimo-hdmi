#include "g0_source_age.hpp"
#include "hdmi_presenter/presenter.h"

#include <algorithm>
#include <array>
#include <atomic>
#include <cassert>
#include <chrono>
#include <cstring>
#include <cstdlib>
#include <iostream>
#include <memory>
#include <string>
#include <vector>

namespace {
using namespace hdmi_presenter;
using namespace std::chrono_literals;

class HeldAckBackend final : public PresentBackend {
 public:
  explicit HeldAckBackend(std::unique_ptr<PresentBackend> actual) : actual_(std::move(actual)) {}
  bool supports_physical_presentation() const override {
    return !actual_ || actual_->supports_physical_presentation();
  }
  std::string unavailable_reason() const override { return actual_ ? actual_->unavailable_reason() : ""; }
  int initial_displayed_slot() const override { return actual_ ? actual_->initial_displayed_slot() : -1; }
  uint64_t mode_refresh_millihz() const override { return actual_ ? actual_->mode_refresh_millihz() : 60000; }
  uint8_t* xrgb_slot(unsigned index, size_t bytes) override {
    return actual_ ? actual_->xrgb_slot(index, bytes) : nullptr;
  }
  bool shutdown(std::string* error) override { return !actual_ || actual_->shutdown(error); }

  BackendSubmitResult submit(const SlotView& view, uint64_t token, std::string* error) override {
    if (actual_) {
      const auto result = actual_->submit(view, token, error);
      if (result != BackendSubmitResult::kAccepted) return result;
    }
    std::lock_guard<std::mutex> lock(mutex_);
    assert(!pending_);
    frames_.push_back(view.identity.frame_id);
    pending_ = token;
    changed_.notify_all();
    return BackendSubmitResult::kAccepted;
  }

  BackendAckResult wait_for_physical_ack(PhysicalAck* ack, int ms, std::string* error) override {
    std::unique_lock<std::mutex> lock(mutex_);
    if (!changed_.wait_for(lock, std::chrono::milliseconds(ms), [&] { return releasing_; }))
      return BackendAckResult::kTimeout;
    assert(pending_);
    if (actual_) {
      lock.unlock();
      const auto result = actual_->wait_for_physical_ack(ack, ms, error);
      if (result != BackendAckResult::kAck) return result;
      lock.lock();
      assert(ack->token == pending_);
      pending_ = 0;
      return result;
    }
    *ack = {pending_, ++sequence_, 10, sequence_, 1};
    pending_ = 0;
    return BackendAckResult::kAck;
  }

  void wait_for_first_submit() {
    std::unique_lock<std::mutex> lock(mutex_);
    assert(changed_.wait_for(lock, 2s, [&] { return frames_.size() == 1; }));
  }

  void release() {
    std::lock_guard<std::mutex> lock(mutex_);
    releasing_ = true;
    changed_.notify_all();
  }

  std::vector<uint32_t> frames() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return frames_;
  }

 private:
  std::unique_ptr<PresentBackend> actual_;
  mutable std::mutex mutex_;
  std::condition_variable changed_;
  std::vector<uint32_t> frames_;
  uint64_t pending_{};
  uint32_t sequence_{};
  bool releasing_{};
};

constexpr uint64_t kSid = 0x12345678, kTag = 0xabcd0000000003e8;
constexpr uint64_t kAnchorNs = 10000000000, kLatestSafe = kAnchorNs - 1 - 1000000 - 1200481 - 999;

struct ClockProbe {
  v3decode::SourceAge age{1000000};
  std::atomic<uint64_t> now{kAnchorNs-100000000};
  uint64_t refresh_ns{};
  std::vector<uint32_t> acked;
  uint32_t held_uncertainty{};
};

void put(uint8_t* p, uint64_t value, unsigned bytes) {
  for (unsigned i = 0; i < bytes; ++i) p[i] = value >> (8 * i);
}

void run(bool enable, bool expires, uint64_t projected_ack_time, const std::string& proof_path,
         uint64_t held_pts = 0, uint32_t held_uncertainty = 0) {
  ClockProbe clock;
  clock.held_uncertainty=held_uncertainty;
  std::array<uint8_t, 96> record{};
  put(record.data()+14, 4, 2);
  put(record.data()+16, kSid, 4);
  put(record.data()+20, 9, 4);
  put(record.data()+32, kTag, 8);
  put(record.data()+52, 65535, 2);
  put(record.data()+54, 32, 2);
  std::memcpy(record.data()+64, "VRT3", 4);
  put(record.data()+68, 0x0001f001, 4);
  put(record.data()+72, 1684, 8);
  put(record.data()+80, 3000000, 8);
  put(record.data()+88, 3000000, 8);
  const v3_source_ring_clock snapshot{kAnchorNs, kAnchorNs, kTag, 0, 0};
  assert(clock.age.observe(record.data(), record.size(), snapshot));
  assert(!clock.age.presentation_expired(kSid, 0, kLatestSafe));
  assert(clock.age.presentation_expired(kSid, 0, kLatestSafe+1));

  auto actual = proof_path.empty() ? nullptr : make_drm_backend("/dev/dri/card0");
  if (actual && (!actual->supports_physical_presentation() || !actual->monotonic_ack_timestamps())) {
    std::cerr << "Physical HDMI backend unavailable: " << actual->unavailable_reason() << '\n';
    std::exit(1);
  }
  auto owned = std::make_unique<HeldAckBackend>(std::move(actual));
  auto* backend = owned.get();
  const uint64_t refresh_millihz = backend->mode_refresh_millihz();
  assert(refresh_millihz);
  clock.refresh_ns = 4*((UINT64_C(1000000000000)+refresh_millihz-1)/refresh_millihz);
  Presenter presenter(std::move(owned));
  if (enable) presenter.set_ready_frame_expired([](void* owner, const FrameIdentity& id) {
    const auto& clock = *static_cast<ClockProbe*>(owner);
    return clock.age.presentation_expired(id.session_id, id.pts_ns, clock.now.load()+clock.refresh_ns,id.capture_uncertainty_us);
  }, &clock);
  presenter.set_new_frame_ack([](void* owner, const FrameIdentity& id, uint64_t) {
    auto& clock=*static_cast<ClockProbe*>(owner);
    assert(id.capture_uncertainty_us==(id.frame_id==2?clock.held_uncertainty:0));
    clock.acked.push_back(id.frame_id);
  }, &clock);
  std::string error;
  if (!proof_path.empty()) assert(presenter.set_proof_capture_path(proof_path, &error));
  presenter.start();
  auto queue = [&](uint32_t id, uint64_t pts, uint32_t width = 0) {
    FillSlot fill{};
    assert(presenter.begin_fill(20, &fill, &error) == SubmitResult::kQueued);
    std::memset(fill.rgb565, static_cast<int>(id)*53, kRgb565Bytes);
    const auto crc = ieee_crc32(fill.rgb565, kRgb565Bytes);
    assert(presenter.finish_fill(fill, {kSid, id, pts, crc, width}, &error) == SubmitResult::kQueued);
  };
  queue(1, 100000000);
  backend->wait_for_first_submit();
  assert(!clock.age.presentation_expired(kSid, 0, clock.now.load()));
  queue(2, held_pts, held_uncertainty);
  // The physical backend initially owns the third slot for black scanout.
  const auto held = presenter.metrics();
  assert(std::count(held.slots.begin(), held.slots.end(), SlotState::kReady) == 1);
  clock.now.store(projected_ack_time-clock.refresh_ns);
  backend->release();
  assert(presenter.drain(2000, &error));
  const bool drop = enable && expires;
  auto expected = drop ? std::vector<uint32_t>{1} : std::vector<uint32_t>{1,2};
  assert(backend->frames() == expected && clock.acked == expected);
  queue(3, 2100000000);
  queue(4, 2200000000);
  assert(presenter.drain(2000, &error));
  expected.insert(expected.end(), {3,4});
  assert(backend->frames() == expected && clock.acked == expected);
  const auto metrics = presenter.metrics();
  assert(metrics.queued == 4 && metrics.queued_expired == (drop ? 1U : 0U));
  assert(metrics.kms_submitted == expected.size() && metrics.presented_at_hdmi_boundary == expected.size());
  assert(!metrics.backend_failures && !metrics.rejected);
  assert(clock.acked.back() == 4);
  assert(presenter.stop(&error));
  if (!proof_path.empty()) assert(presenter.proof_status() == "written");
}
}  // namespace

int main(int argc, char** argv) {
  if (argc != 1 && argc != 2) return 2;
  const auto proof = [&](const char* name) {
    return argc == 2 ? std::string(argv[1]) + "/" + name + ".json" : std::string{};
  };
  run(true, true, kLatestSafe+1, proof("expired"));
  run(true, true, kAnchorNs+1200000000, proof("expired-after-anchor"));
  run(true, true, kAnchorNs+2000000000-1-1000000-400161-999+1, proof("held-through-stale-anchor"), 2000000000);
  run(true, false, kLatestSafe, proof("exact-edge"));
  run(false, true, kLatestSafe+1, proof("disabled"));
  run(true, false, kLatestSafe, proof("bounded-capture"), 0, 5000);
  run(true, false, kLatestSafe+1, proof("unknown-capture"), 0, UINT32_MAX);
  std::cout << (argc == 2 ? "HDMI_QUEUED_AGE_DRM_PASS" : "HDMI_QUEUED_AGE_CONTROLS_PASS")
            << " expired_stale_anchor_exact_edge_disabled_slot_reuse\n";
}
