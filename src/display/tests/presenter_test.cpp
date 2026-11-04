#include "hdmi_presenter/presenter.h"

#include <cassert>
#include <chrono>
#include <condition_variable>
#include <cstdio>
#include <fstream>
#include <deque>
#include <mutex>
#include <thread>
#include <vector>

using namespace hdmi_presenter;

class LatchBackend final : public PresentBackend {
 public:
  bool supports_physical_presentation() const override { return true; }
  std::string unavailable_reason() const override { return {}; }
  int initial_displayed_slot() const override { return 2; }
  uint64_t mode_refresh_millihz() const override { return 60000; }
  uint8_t* xrgb_slot(unsigned index, size_t bytes) override {
    return index < pixels_.size() && bytes == kXrgb8888Bytes ? pixels_[index].data() : nullptr;
  }
  BackendSubmitResult submit(const SlotView& view, uint64_t token, std::string*) override {
    std::lock_guard<std::mutex> lock(mutex_);
    submitted_.push_back({token, view.slot_index});
    cv_.notify_all();
    if (busy_on_submit_) return BackendSubmitResult::kBusy;
    return failure_on_submit_ ? BackendSubmitResult::kFailure : BackendSubmitResult::kAccepted;
  }
  BackendAckResult wait_for_physical_ack(PhysicalAck* acknowledgement, int timeout_ms, std::string* error) override {
    std::unique_lock<std::mutex> lock(mutex_);
    if (!cv_.wait_for(lock, std::chrono::milliseconds(timeout_ms), [&] { return failed_ || !acks_.empty(); }))
      return BackendAckResult::kTimeout;
    if (failed_) { if (error) *error = "simulated child exit"; return BackendAckResult::kFailure; }
    *acknowledgement = acks_.front();
    acks_.pop_front();
    return BackendAckResult::kAck;
  }
  void shutdown() override { std::lock_guard<std::mutex> lock(mutex_); stopped_ = true; cv_.notify_all(); }
  void wait_submits(size_t count) {
    std::unique_lock<std::mutex> lock(mutex_);
    assert(cv_.wait_for(lock, std::chrono::seconds(1), [&] { return submitted_.size() >= count; }));
  }
  uint64_t token_at(size_t index) { std::lock_guard<std::mutex> lock(mutex_); return submitted_.at(index).first; }
  unsigned slot_at(size_t index) { std::lock_guard<std::mutex> lock(mutex_); return submitted_.at(index).second; }
  void ack(uint64_t token) { std::lock_guard<std::mutex> lock(mutex_); acks_.push_back({token, ++sequence_, 12, sequence_, 77}); cv_.notify_all(); }
  void child_exit() { std::lock_guard<std::mutex> lock(mutex_); failed_ = true; cv_.notify_all(); }
  void return_busy() { std::lock_guard<std::mutex> lock(mutex_); busy_on_submit_ = true; }
  void return_failure() { std::lock_guard<std::mutex> lock(mutex_); failure_on_submit_ = true; }
  bool stopped() const { std::lock_guard<std::mutex> lock(mutex_); return stopped_; }

 private:
  mutable std::mutex mutex_;
  std::condition_variable cv_;
  std::array<std::vector<uint8_t>, 3> pixels_{{std::vector<uint8_t>(kXrgb8888Bytes),
                                                std::vector<uint8_t>(kXrgb8888Bytes),
                                                std::vector<uint8_t>(kXrgb8888Bytes)}};
  std::vector<std::pair<uint64_t, unsigned>> submitted_;
  std::deque<PhysicalAck> acks_;
  uint32_t sequence_{};
  bool failed_{};
  bool stopped_{};
  bool failure_on_submit_{};
  bool busy_on_submit_{};
};

static FrameIdentity identity(uint64_t frame, const std::vector<uint8_t>& data) {
  return {7, frame, frame * 10, ieee_crc32(data.data(), data.size())};
}

int main() {
  std::vector<uint8_t> frame(kRgb565Bytes, 0x5a);
  std::string error;

  /* Initial black is displayed. One standard DRM flip may be pending while a
   * second decoded frame waits READY; there is no producer overwrite. */
  auto first = std::make_unique<LatchBackend>();
  LatchBackend* fake = first.get();
  Presenter presenter(std::move(first));
  const char* physical_proof = "presenter_test_physical_proof.json";
  std::remove(physical_proof);
  assert(presenter.set_proof_capture_path(physical_proof, &error));
  presenter.start();
  assert(presenter.metrics().slots[2] == SlotState::kDisplayed);
  assert(presenter.submit_rgb565(identity(1, frame), frame.data(), frame.size(), 0, &error) == SubmitResult::kQueued);
  assert(presenter.submit_rgb565(identity(2, frame), frame.data(), frame.size(), 0, &error) == SubmitResult::kQueued);
  fake->wait_submits(1);
  assert(fake->slot_at(0) != 2);
  assert(presenter.submit_rgb565(identity(3, frame), frame.data(), frame.size(), 1, &error) == SubmitResult::kQueueFull);
  fake->ack(fake->token_at(0));
  for (int i = 0; i != 100 && presenter.metrics().presented_at_hdmi_boundary != 1; ++i)
    std::this_thread::sleep_for(std::chrono::milliseconds(1));
  const Metrics one = presenter.metrics();
  assert(one.presented_at_hdmi_boundary == 1 && one.slots[2] == SlotState::kFree);
  assert(presenter.submit_rgb565(identity(3, frame), frame.data(), frame.size(), 10, &error) == SubmitResult::kQueued);
  fake->wait_submits(2);
  assert(fake->slot_at(1) != 2);
  fake->ack(fake->token_at(1));
  fake->wait_submits(3);
  assert(fake->slot_at(2) == 2);  // black only becomes writable after frame 1 boundary
  fake->ack(fake->token_at(2));
  for (int i = 0; i != 100 && presenter.metrics().presented_at_hdmi_boundary != 3; ++i)
    std::this_thread::sleep_for(std::chrono::milliseconds(1));
  assert(presenter.metrics().presented_at_hdmi_boundary == 3);
  presenter.stop();
  assert(fake->stopped());
  assert(presenter.proof_status() == "written");
  std::ifstream physical_input(physical_proof);
  const std::string physical_json((std::istreambuf_iterator<char>(physical_input)), std::istreambuf_iterator<char>());
  assert(physical_json.find("\"physical\":true") != std::string::npos);
  assert(physical_json.find("\"acceptance_valid\":true") != std::string::npos);
  assert(physical_json.find("\"capacity_exceeded\":false") != std::string::npos);
  assert(physical_json.find("\"mode_refresh_millihz\":60000") != std::string::npos);
  assert(physical_json.find("\"token\":1") != std::string::npos);
  std::remove(physical_proof);

  /* The optional idle repeat is a same-buffer physical refresh, not a new
   * source frame.  Draining pauses future repeats and waits for the current
   * flip to retire. */
  auto repeat_device = std::make_unique<LatchBackend>();
  LatchBackend* repeat_backend = repeat_device.get();
  Presenter repeating(std::move(repeat_device));
  repeating.set_repeat_displayed_on_idle(true);
  repeating.start();
  assert(repeating.submit_rgb565(identity(8, frame), frame.data(), frame.size(), 0, &error) == SubmitResult::kQueued);
  repeat_backend->wait_submits(1);
  repeat_backend->ack(repeat_backend->token_at(0));
  for (int i = 0; i != 100 && repeating.metrics().presented_at_hdmi_boundary != 1; ++i)
    std::this_thread::sleep_for(std::chrono::milliseconds(1));
  repeat_backend->wait_submits(2);
  const unsigned displayed = repeat_backend->slot_at(0);
  assert(repeat_backend->slot_at(1) == displayed);
  repeat_backend->ack(repeat_backend->token_at(1));
  for (int i = 0; i != 100 && repeating.metrics().scanout_repeats == 0; ++i)
    std::this_thread::sleep_for(std::chrono::milliseconds(1));
  assert(repeating.metrics().presented_at_hdmi_boundary == 1);
  assert(repeating.metrics().scanout_repeats >= 1);
  repeat_backend->wait_submits(3);
  bool repeat_drained = false;
  std::thread repeat_drain([&] { repeat_drained = repeating.drain(1000, &error); });
  std::this_thread::sleep_for(std::chrono::milliseconds(10));
  repeat_backend->ack(repeat_backend->token_at(2));
  repeat_drain.join();
  assert(repeat_drained);
  repeating.stop();

  /* A repeated late acknowledgement fails closed and cannot increase the
   * fresh frame metric. This also covers a child/backend exit wakeup path. */
  auto second = std::make_unique<LatchBackend>();
  LatchBackend* broken = second.get();
  Presenter fail_closed(std::move(second));
  fail_closed.start();
  assert(fail_closed.submit_rgb565(identity(4, frame), frame.data(), frame.size(), 0, &error) == SubmitResult::kQueued);
  broken->wait_submits(1);
  const uint64_t retired = broken->token_at(0);
  broken->ack(retired);
  for (int i = 0; i != 100 && fail_closed.metrics().presented_at_hdmi_boundary != 1; ++i)
    std::this_thread::sleep_for(std::chrono::milliseconds(1));
  assert(fail_closed.submit_rgb565(identity(5, frame), frame.data(), frame.size(), 10, &error) == SubmitResult::kQueued);
  broken->wait_submits(2);
  broken->ack(retired);  // duplicate event from old page flip
  for (int i = 0; i != 100 && fail_closed.metrics().backend_failures == 0; ++i)
    std::this_thread::sleep_for(std::chrono::milliseconds(1));
  assert(fail_closed.metrics().presented_at_hdmi_boundary == 1);
  assert(fail_closed.metrics().backend_failures == 1);
  FillSlot unavailable{};
  assert(fail_closed.begin_fill(0, &unavailable, &error) == SubmitResult::kStopped);
  fail_closed.stop();

  auto third = std::make_unique<LatchBackend>();
  LatchBackend* exited = third.get();
  Presenter child_exit(std::move(third));
  child_exit.start();
  assert(child_exit.submit_rgb565(identity(5, frame), frame.data(), frame.size(), 0, &error) == SubmitResult::kQueued);
  exited->wait_submits(1);
  exited->child_exit();
  for (int i = 0; i != 100 && child_exit.metrics().backend_failures == 0; ++i)
    std::this_thread::sleep_for(std::chrono::milliseconds(1));
  assert(child_exit.metrics().backend_failures == 1);
  child_exit.stop();

  /* An EBUSY cannot be paired with one of our tokens. It must fail closed,
   * not put a slot back in READY and wait indefinitely for an unrelated DRM
   * event. */
  auto fourth = std::make_unique<LatchBackend>();
  LatchBackend* busy = fourth.get();
  Presenter busy_presenter(std::move(fourth));
  busy->return_busy();
  busy_presenter.start();
  assert(busy_presenter.submit_rgb565(identity(6, frame), frame.data(), frame.size(), 0, &error) == SubmitResult::kQueued);
  busy->wait_submits(1);
  for (int i = 0; i != 100 && busy_presenter.metrics().backend_failures == 0; ++i)
    std::this_thread::sleep_for(std::chrono::milliseconds(1));
  assert(busy_presenter.metrics().backend_failures == 1);
  assert(busy_presenter.begin_fill(0, &unavailable, &error) == SubmitResult::kStopped);
  busy_presenter.stop();

  /* A generic submission failure is just as uncertain as EBUSY: drain must
   * fail rather than see a freed slot and claim the final frame completed. */
  auto fifth = std::make_unique<LatchBackend>();
  LatchBackend* rejected = fifth.get();
  Presenter rejected_presenter(std::move(fifth));
  const char* failed_backend_proof = "presenter_test_failed_backend_proof.json";
  std::remove(failed_backend_proof);
  assert(rejected_presenter.set_proof_capture_path(failed_backend_proof, &error));
  rejected->return_failure();
  rejected_presenter.start();
  assert(rejected_presenter.submit_rgb565(identity(7, frame), frame.data(), frame.size(), 0, &error) == SubmitResult::kQueued);
  rejected->wait_submits(1);
  for (int i = 0; i != 100 && rejected_presenter.metrics().backend_failures == 0; ++i)
    std::this_thread::sleep_for(std::chrono::milliseconds(1));
  assert(rejected_presenter.metrics().backend_failures == 1);
  assert(!rejected_presenter.drain(1, &error));
  rejected_presenter.stop();
  std::ifstream failed_backend_input(failed_backend_proof);
  const std::string failed_backend_json((std::istreambuf_iterator<char>(failed_backend_input)), std::istreambuf_iterator<char>());
  assert(failed_backend_json.find("\"physical\":true") != std::string::npos);
  assert(failed_backend_json.find("\"acceptance_valid\":false") != std::string::npos);
  assert(failed_backend_json.find("\"failed_closed\":true") != std::string::npos);
  assert(failed_backend_json.find("backend rejected page-flip submission") != std::string::npos);
  std::remove(failed_backend_proof);

  /* Null backend has three free software slots and never claims physical HDMI. */
  Presenter null_presenter(make_null_backend());
  const char* null_proof = "presenter_test_null_proof.json";
  std::remove(null_proof);
  assert(null_presenter.set_proof_capture_path(null_proof, &error));
  null_presenter.start();
  for (uint64_t i = 1; i <= 3; ++i)
    assert(null_presenter.submit_rgb565(identity(i, frame), frame.data(), frame.size(), 20, &error) == SubmitResult::kQueued);
  null_presenter.stop();
  assert(null_presenter.metrics().presented_at_hdmi_boundary == 0);
  std::ifstream null_input(null_proof);
  const std::string null_json((std::istreambuf_iterator<char>(null_input)), std::istreambuf_iterator<char>());
  assert(null_json.find("\"physical\":false") != std::string::npos);
  assert(null_json.find("\"acceptance_valid\":false") != std::string::npos);
  assert(null_json.find("\"presented_at_hdmi_boundary\":0") != std::string::npos);
  std::remove(null_proof);

  /* A parsing or drain failure occurs outside the worker, so it explicitly
   * invalidates a configured proof instead of leaving a misleading ledger. */
  Presenter invalid_proof(make_null_backend());
  const char* invalid_proof_path = "presenter_test_invalid_proof.json";
  std::remove(invalid_proof_path);
  assert(invalid_proof.set_proof_capture_path(invalid_proof_path, &error));
  invalid_proof.start();
  invalid_proof.invalidate_capture("synthetic receiver failure");
  invalid_proof.stop();
  std::ifstream invalid_input(invalid_proof_path);
  const std::string invalid_json((std::istreambuf_iterator<char>(invalid_input)), std::istreambuf_iterator<char>());
  assert(invalid_json.find("\"capture_status\":\"invalid\"") != std::string::npos);
  assert(invalid_json.find("\"capture_invalidated\":true") != std::string::npos);
  assert(invalid_json.find("\"acceptance_valid\":false") != std::string::npos);
  assert(invalid_json.find("\"proof_failures\":1") != std::string::npos);
  assert(invalid_json.find("synthetic receiver failure") != std::string::npos);
  std::remove(invalid_proof_path);
  return 0;
}
