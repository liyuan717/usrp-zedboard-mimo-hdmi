#include "hdmi_presenter/presenter.h"

#include <cassert>
#include <atomic>
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
  explicit LatchBackend(bool physical = true) : physical_(physical) {}
  bool supports_physical_presentation() const override { return physical_; }
  std::string unavailable_reason() const override { return {}; }
  int initial_displayed_slot() const override { return 2; }
  uint64_t mode_refresh_millihz() const override { return 60000; }
  uint8_t* xrgb_slot(unsigned index, size_t bytes) override {
    return index < pixels_.size() && bytes == kXrgb8888Bytes ? pixels_[index].data() : nullptr;
  }
  BackendSubmitResult submit(const SlotView& view, uint64_t token, std::string*) override {
    std::lock_guard<std::mutex> lock(mutex_);
    submitted_.push_back({token, view.slot_index});
    identities_.push_back(view.identity);
    times_.push_back(std::chrono::steady_clock::now());
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
  bool has_submit(size_t count) {
    std::unique_lock<std::mutex> lock(mutex_);
    return cv_.wait_for(lock, std::chrono::milliseconds(10), [&] { return submitted_.size() >= count; });
  }
  uint64_t frame_at(size_t i) { std::lock_guard<std::mutex> lock(mutex_); return identities_.at(i).frame_id; }
  auto time_at(size_t i) { std::lock_guard<std::mutex> lock(mutex_); return times_.at(i); }
  void ack_session(uint64_t token, uint64_t session) { std::lock_guard<std::mutex> lock(mutex_); sequence_ += 2; acks_.push_back({token, sequence_, 12, sequence_, session}); cv_.notify_all(); }
  uint64_t token_at(size_t index) { std::lock_guard<std::mutex> lock(mutex_); return submitted_.at(index).first; }
  unsigned slot_at(size_t index) { std::lock_guard<std::mutex> lock(mutex_); return submitted_.at(index).second; }
  void ack(uint64_t token) { std::lock_guard<std::mutex> lock(mutex_); sequence_ += 2; acks_.push_back({token, sequence_, 12, sequence_, 77}); cv_.notify_all(); }
  void child_exit() { std::lock_guard<std::mutex> lock(mutex_); failed_ = true; cv_.notify_all(); }
  void return_busy() { std::lock_guard<std::mutex> lock(mutex_); busy_on_submit_ = true; }
  void return_failure() { std::lock_guard<std::mutex> lock(mutex_); failure_on_submit_ = true; }
  bool stopped() const { std::lock_guard<std::mutex> lock(mutex_); return stopped_; }

 private:
  const bool physical_;
  std::vector<FrameIdentity> identities_;
  std::vector<std::chrono::steady_clock::time_point> times_;
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
  return {7, frame, frame * 200000000ULL, ieee_crc32(data.data(), data.size())};
}


int main() {
  std::vector<uint8_t> data(kRgb565Bytes, 0x5a);
  std::string error;
  {
    // Saturate real Presenter slots. Warm up first ACK, then keep READY frames
    // queued while same-buffer refreshes advance the actual ACK sequence.
    auto device = std::make_unique<LatchBackend>(); auto* b = device.get();
    Presenter p(std::move(device)); p.set_repeat_displayed_on_idle(true); p.set_source_pts_pacing(true);
    const char* proof="paced-proof.json";std::remove(proof);assert(p.set_proof_capture_path(proof,&error));p.start();
    std::thread producer([&] { for (unsigned n=0;n<5;++n) {
      std::string e; assert(p.submit_rgb565(identity(n,data),data.data(),data.size(),1000,&e)==SubmitResult::kQueued);
    }});
    b->wait_submits(1);std::this_thread::sleep_for(std::chrono::milliseconds(40));
    size_t i=0; std::vector<uint64_t> source_order;
    uint64_t last=UINT64_MAX;
    while(p.metrics().presented_at_hdmi_boundary<5) {
      assert(i<100);if(!b->has_submit(i+1))continue;
      const auto frame=b->frame_at(i);
      if(frame!=last) { source_order.push_back(frame);last=frame; }
      b->ack(b->token_at(i++));
      std::this_thread::sleep_for(std::chrono::milliseconds(33));  // Pinned driver ACKs two 60Hz vblanks apart.
    }
    producer.join();assert((source_order==std::vector<uint64_t>{0,1,2,3,4}));
    std::atomic<bool> finished{false};bool drained=false;
    std::thread drain([&]{std::string e;drained=p.drain(1000,&e);finished=true;});
    while(!finished) { if(b->has_submit(i+1))b->ack(b->token_at(i++)); }
    drain.join();assert(drained);p.stop();
    std::ifstream input(proof);std::string text((std::istreambuf_iterator<char>(input)),{});
    assert(text.find("\"source_pts_pacing_enabled\":true")!=std::string::npos);
    assert(text.find("\"presented_at_hdmi_boundary\":5")!=std::string::npos);
    // source completions at DRM sequence2,14,26,38,50: 12vsync/200ms.
    for(unsigned n=0;n<5;++n)assert(text.find("\"drm_sequence\":"+std::to_string(2+12*n)+",")!=std::string::npos);
    std::remove(proof);
  }
  {
    // drain while a future source frame is waiting must continue only the
    // same displayed buffer until that source frame can retire.
    auto device=std::make_unique<LatchBackend>();auto* b=device.get();
    Presenter p(std::move(device));p.set_repeat_displayed_on_idle(true);p.set_source_pts_pacing(true);p.start();
    assert(p.submit_rgb565(identity(0,data),data.data(),data.size(),1000,&error)==SubmitResult::kQueued);
    assert(p.submit_rgb565(identity(1,data),data.data(),data.size(),1000,&error)==SubmitResult::kQueued);
    std::atomic<bool> finished{false};bool drained=false;
    std::thread drain([&]{std::string e;drained=p.drain(1000,&e);finished=true;});
    size_t i=0;while(!finished) { if(b->has_submit(i+1))b->ack(b->token_at(i++));assert(i<30); }
    drain.join();assert(drained && p.metrics().presented_at_hdmi_boundary==2 && p.metrics().scanout_repeats>=5);p.stop();
  }
  for(bool changed_session:{false,true}) {
    auto device=std::make_unique<LatchBackend>();auto* b=device.get();
    Presenter p(std::move(device));p.set_repeat_displayed_on_idle(true);p.set_source_pts_pacing(true);p.start();
    assert(p.submit_rgb565(identity(1,data),data.data(),data.size(),1000,&error)==SubmitResult::kQueued);
    b->wait_submits(1);b->ack(b->token_at(0));b->wait_submits(2);
    if(changed_session)b->ack_session(b->token_at(1),99);
    else {
      auto bad=identity(2,data);bad.pts_ns=0;
      assert(p.submit_rgb565(bad,data.data(),data.size(),1000,&error)==SubmitResult::kQueued);
      b->ack(b->token_at(1));
    }
    for(unsigned n=0;n<100 && p.backend_status().find("source PTS pacing")==std::string::npos;++n)
      std::this_thread::sleep_for(std::chrono::milliseconds(1));
    assert(p.backend_status().find("source PTS pacing")!=std::string::npos);p.stop();
  }
  {
    auto device=std::make_unique<LatchBackend>(false);auto* b=device.get();
    Presenter p(std::move(device));p.set_source_pts_pacing(true);p.start();
    for(unsigned n=0;n<3;++n)assert(p.submit_rgb565(identity(n,data),data.data(),data.size(),1000,&error)==SubmitResult::kQueued);
    assert(p.drain(2000,&error));
    for(unsigned n=0;n<2;++n) {
      auto ms=std::chrono::duration_cast<std::chrono::milliseconds>(b->time_at(n+1)-b->time_at(n)).count();
      assert(ms>=199 && ms<350);
    }
    assert(p.metrics().presented_at_hdmi_boundary==0);p.stop();
  }
  {
    auto device=std::make_unique<LatchBackend>(false);auto* b=device.get();
    Presenter p(std::move(device));p.set_source_pts_pacing(true);p.start();
    assert(p.submit_rgb565(identity(0,data),data.data(),data.size(),1000,&error)==SubmitResult::kQueued);b->wait_submits(1);
    auto later=identity(1,data);later.pts_ns=5000000000ULL;
    assert(p.submit_rgb565(later,data.data(),data.size(),1000,&error)==SubmitResult::kQueued);
    std::this_thread::sleep_for(std::chrono::milliseconds(10));const auto begin=std::chrono::steady_clock::now();p.stop();
    assert(std::chrono::steady_clock::now()-begin<std::chrono::milliseconds(100));
  }
}
