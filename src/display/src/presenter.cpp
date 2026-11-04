#include "hdmi_presenter/presenter.h"

#include <algorithm>
#include <cerrno>
#include <chrono>
#include <cstring>
#include <fcntl.h>
#include <sstream>
#include <time.h>
#include <unistd.h>

namespace hdmi_presenter {
namespace {
constexpr int kAckPollMs = 20;
/* Legacy KMS rejects a second unfinished page flip on one CRTC.  The other
 * two buffers still provide a displayed frame and a producer/converted-ready
 * frame while the event thread waits; do not fake a second DRM completion. */
constexpr size_t kMaxInFlight = 1;
constexpr size_t kMaxProofRecords = 4096;

std::string json_escape(const std::string& input) {
  static constexpr char kHex[] = "0123456789abcdef";
  std::string output;
  output.reserve(input.size());
  for (const unsigned char ch : input) {
    switch (ch) {
      case '"': output += "\\\""; break;
      case '\\': output += "\\\\"; break;
      case '\b': output += "\\b"; break;
      case '\f': output += "\\f"; break;
      case '\n': output += "\\n"; break;
      case '\r': output += "\\r"; break;
      case '\t': output += "\\t"; break;
      default:
        if (ch < 0x20U) {
          output += "\\u00";
          output += kHex[(ch >> 4) & 0x0fU];
          output += kHex[ch & 0x0fU];
        } else {
          output += static_cast<char>(ch);
        }
    }
  }
  return output;
}


TimingCounter* timing_counter(StageTimingTotals* totals, TimingStage stage) {
  switch (stage) {
    case TimingStage::kHeaderRead: return &totals->header_read;
    case TimingStage::kPayloadRead: return &totals->payload_read;
    case TimingStage::kCrc: return &totals->crc;
    case TimingStage::kQueueWait: return &totals->queue_wait;
    case TimingStage::kConvert: return &totals->convert;
    case TimingStage::kSubmit: return &totals->submit;
    case TimingStage::kAckWait: return &totals->ack_wait;
  }
  return nullptr;
}

void append_timing_json(std::ostringstream& output, const char* name, const TimingCounter& counter) {
  output << '\"' << name << "\":{\"count\":" << counter.count
         << ",\"wall_monotonic_ns\":" << counter.wall_ns
         << ",\"thread_cpu_ns\":" << counter.thread_cpu_ns << '}';
}
}  // namespace

TimingSnapshot timing_snapshot() {
  timespec wall{};
  timespec cpu{};
  const bool wall_ok = clock_gettime(CLOCK_MONOTONIC, &wall) == 0;
  const bool cpu_ok = clock_gettime(CLOCK_THREAD_CPUTIME_ID, &cpu) == 0;
  return {wall_ok ? static_cast<uint64_t>(wall.tv_sec) * 1000000000ULL + static_cast<uint64_t>(wall.tv_nsec) : 0,
          cpu_ok ? static_cast<uint64_t>(cpu.tv_sec) * 1000000000ULL + static_cast<uint64_t>(cpu.tv_nsec) : 0};
}

uint64_t timing_delta(uint64_t begin, uint64_t end) {
  return end >= begin ? end - begin : 0;
}

Presenter::Presenter(std::unique_ptr<PresentBackend> backend) : backend_(std::move(backend)) {
  for (auto& state : metrics_.slots) state = SlotState::kFree;
}
Presenter::~Presenter() { stop(); }

void Presenter::publish_slot_state_locked(unsigned index, SlotState state) {
  slots_[index].state = state;
  metrics_.slots[index] = state;
}

void Presenter::start() {
  std::lock_guard<std::mutex> lock(mutex_);
  if (started_) return;
  for (unsigned i = 0; i < slots_.size(); ++i) {
    slots_[i].xrgb_ptr = backend_->xrgb_slot(i, kXrgb8888Bytes);
    if (!slots_[i].xrgb_ptr) {
      slots_[i].xrgb.resize(kXrgb8888Bytes);
      slots_[i].xrgb_ptr = slots_[i].xrgb.data();
    }
    publish_slot_state_locked(i, SlotState::kFree);
  }
  displayed_slot_ = backend_->initial_displayed_slot();
  if (displayed_slot_ < -1 || displayed_slot_ >= static_cast<int>(slots_.size())) {
    fail_closed_locked("backend reported an invalid initial displayed slot");
  } else if (displayed_slot_ >= 0) {
    publish_slot_state_locked(static_cast<unsigned>(displayed_slot_), SlotState::kDisplayed);
  }
  token_to_slot_.clear();
  submit_order_.clear();
  next_token_ = 1;
  next_ready_order_ = 0;
  have_source_ack_ = have_pacing_ack_ = false;
  pacing_advance_ = 2;
  backend_busy_ = false;
  stop_requested_ = false;
  started_ = true;
  worker_ = std::thread(&Presenter::worker, this);
}

void Presenter::set_stage_timing_enabled(bool enabled) {
  std::lock_guard<std::mutex> lock(mutex_);
  if (!started_) stage_timing_enabled_ = enabled;
}

void Presenter::set_repeat_displayed_on_idle(bool enabled) {
  std::lock_guard<std::mutex> lock(mutex_);
  if (!started_) repeat_displayed_on_idle_ = enabled;
}

void Presenter::set_source_pts_pacing(bool enabled) {
  std::lock_guard<std::mutex> lock(mutex_);
  if (!started_) source_pts_pacing_ = enabled;
}

void Presenter::set_rgb_expand_mode(RgbExpandMode mode) {
  std::lock_guard<std::mutex> lock(mutex_);
  if (!started_) rgb_expand_mode_ = mode;
}

bool Presenter::stage_timing_enabled() const {
  std::lock_guard<std::mutex> lock(mutex_);
  return stage_timing_enabled_;
}

void Presenter::add_stage_timing(TimingStage stage, const TimingSnapshot& begin,
                                 const TimingSnapshot& end) {
  std::lock_guard<std::mutex> lock(mutex_);
  if (!stage_timing_enabled_) return;
  TimingCounter* counter = timing_counter(&stage_timing_, stage);
  if (!counter) return;
  ++counter->count;
  counter->wall_ns += timing_delta(begin.monotonic_ns, end.monotonic_ns);
  counter->thread_cpu_ns += timing_delta(begin.thread_cpu_ns, end.thread_cpu_ns);
}

StageTimingTotals Presenter::stage_timing_totals() const {
  std::lock_guard<std::mutex> lock(mutex_);
  return stage_timing_;
}

std::string Presenter::stage_timing_summary_json() const {
  const StageTimingTotals totals = stage_timing_totals();
  std::ostringstream output;
  output << "{\"schema\":\"hdmi-presenter-stage-timing-v1\","
         << "\"wall_clock\":\"CLOCK_MONOTONIC\","
         << "\"cpu_clock\":\"CLOCK_THREAD_CPUTIME_ID\","
         << "\"crc_kernel\":\"slice8-unroll4\","
         << "\"rgb_expand\":\"" << rgb_expand_mode_name(rgb_expand_mode_) << "\","
         << "\"reader_stages\":{";
  append_timing_json(output, "header_read", totals.header_read); output << ',';
  append_timing_json(output, "payload_read", totals.payload_read); output << ',';
  append_timing_json(output, "crc", totals.crc); output << ',';
  append_timing_json(output, "queue_wait", totals.queue_wait); output << ',';
  append_timing_json(output, "rgb565_to_xrgb8888", totals.convert);
  output << "},\"display_worker_stages\":{";
  append_timing_json(output, "submit", totals.submit); output << ',';
  append_timing_json(output, "ack_wait", totals.ack_wait);
  output << "}}";
  return output.str();
}

bool Presenter::drain(int timeout_ms, std::string* error) {
  if (timeout_ms < 0) {
    if (error) *error = "negative presentation drain timeout";
    return false;
  }
  std::unique_lock<std::mutex> lock(mutex_);
  if (!started_) {
    if (error) *error = "presenter is not running";
    return false;
  }
  /* Draining is the application quiescence boundary.  The current optional
   * repeat may finish, but no new same-buffer scanout refresh may start. */
  repeat_paused_ = true;
  state_changed_.notify_all();
  const auto complete = [&] {
    if (!token_to_slot_.empty()) return false;
    for (const Slot& slot : slots_)
      if (slot.state == SlotState::kFilling || slot.state == SlotState::kReady ||
          slot.state == SlotState::kSubmitting || slot.state == SlotState::kInFlight)
        return false;
    return true;
  };
  const auto deadline = std::chrono::steady_clock::now() + std::chrono::milliseconds(timeout_ms);
  if (failed_closed_) {
    if (error) *error = failure_reason_;
    return false;
  }
  while (!complete() && !failed_closed_ && !stop_requested_) {
    if (state_changed_.wait_until(lock, deadline) == std::cv_status::timeout) break;
  }
  if (failed_closed_) {
    if (error) *error = failure_reason_;
    return false;
  }
  if (complete()) return true;
  if (error) {
    if (failed_closed_) *error = failure_reason_;
    else if (stop_requested_) *error = "presenter stopped before drain completed";
    else *error = "presentation drain timeout before all queued frames were acknowledged";
  }
  return false;
}

void Presenter::stop() {
  {
    std::lock_guard<std::mutex> lock(mutex_);
    if (!started_) return;
    stop_requested_ = true;
    repeat_paused_ = true;
  }
  state_changed_.notify_all();
  if (worker_.joinable()) worker_.join();
  write_proof_capture();
  /* backend shutdown replaces/disables CRTC before dumb buffers are removed;
   * we never mark an unacknowledged in-flight slot reusable during teardown. */
  backend_->shutdown();
  std::lock_guard<std::mutex> lock(mutex_);
  started_ = false;
}

int Presenter::reserve_free_slot_locked() {
  for (unsigned i = 0; i < slots_.size(); ++i) {
    if (slots_[i].state == SlotState::kFree) {
      publish_slot_state_locked(i, SlotState::kFilling);
      return static_cast<int>(i);
    }
  }
  return -1;
}

int Presenter::next_ready_slot_locked() const {
  int selected = -1;
  for (unsigned i = 0; i < slots_.size(); ++i) {
    if (slots_[i].state != SlotState::kReady) continue;
    if (!source_pts_pacing_) return static_cast<int>(i);  // Preserve default r6 behavior.
    if (selected < 0 || slots_[i].ready_order < slots_[static_cast<unsigned>(selected)].ready_order)
      selected = static_cast<int>(i);
  }
  return selected;
}

bool Presenter::source_pts_ready_locked(unsigned index, std::chrono::steady_clock::time_point* due,
                                        std::string* error) const {
  if (!have_source_ack_) return true;
  const auto& id = slots_[index].identity;
  if (id.session_id != last_paced_source_.session_id || id.frame_id <= last_paced_source_.frame_id ||
      id.pts_ns <= last_paced_source_.pts_ns || id.pts_ns - last_paced_source_.pts_ns > 10000000000ULL) {
    *error = "source PTS pacing identity/order or bounded interval invalid";
    return false;
  }
  const uint64_t delta = id.pts_ns - last_paced_source_.pts_ns;
  if (!backend_->supports_physical_presentation()) {
    *due = last_source_ack_time_ + std::chrono::nanoseconds(delta);
    return std::chrono::steady_clock::now() >= *due;
  }
  if (!repeat_displayed_on_idle_) {
    *error = "physical source PTS pacing requires same-buffer idle refresh";
    return false;
  }
  const uint64_t refresh = backend_->mode_refresh_millihz();
  if (refresh < 10000 || refresh > 240000) {
    *error = "source PTS pacing refresh rate outside supported bound";
    return false;
  }
  // Source PTS is nanoseconds; refresh is millihertz. No cross-clock subtraction.
  // The pinned driver retires flips two vblanks apart. Use at most this lead,
  // reducing it if an actual ACK ever advances by only one vblank.
  const uint64_t interval = (delta * refresh + 500000000000ULL) / 1000000000000ULL;
  const uint32_t elapsed = latest_drm_sequence_ - source_drm_sequence_;
  if (elapsed > 0x7fffffffU || interval == 0) {
    *error = "source PTS pacing invalid DRM sequence interval";
    return false;
  }
  return static_cast<uint64_t>(elapsed) + pacing_advance_ >= interval;
}

bool Presenter::note_pacing_ack_locked(const PhysicalAck& ack, const FrameIdentity& id,
                                       bool repeated, std::string* error) {
  if (!source_pts_pacing_) return true;
  if (backend_->supports_physical_presentation()) {
    if (ack.observer_session == 0 || (have_pacing_ack_ && ack.observer_session != pacing_observer_session_)) {
      *error = "source PTS pacing observer session changed";
      return false;
    }
    if (have_pacing_ack_) {
      const uint32_t advance = ack.drm_sequence - latest_drm_sequence_;
      if (advance == 0 || advance > 0x7fffffffU) {
        *error = "source PTS pacing nonmonotonic DRM ACK";
        return false;
      }
      pacing_advance_ = std::min(pacing_advance_, advance);
    }
    latest_drm_sequence_ = ack.drm_sequence;
    pacing_observer_session_ = ack.observer_session;
    have_pacing_ack_ = true;
  }
  if (!repeated) {
    last_paced_source_ = id;
    source_drm_sequence_ = ack.drm_sequence;
    last_source_ack_time_ = std::chrono::steady_clock::now();
    have_source_ack_ = true;
  }
  return true;
}

void Presenter::update_watermark_locked() {
  uint32_t active = 0;
  for (const auto& slot : slots_)
    if (slot.state != SlotState::kFree) ++active;
  metrics_.queue_high_watermark = std::max(metrics_.queue_high_watermark, active);
}

void Presenter::fail_closed_locked(const std::string& reason) {
  if (!failed_closed_) {
    failed_closed_ = true;
    failure_reason_ = reason;
    ++metrics_.backend_failures;
  }
  stop_requested_ = true;
}

bool Presenter::set_proof_capture_path(const std::string& output_path, std::string* error) {
  std::lock_guard<std::mutex> lock(mutex_);
  if (started_ || output_path.empty()) {
    if (error) *error = started_ ? "proof capture must be configured before start" : "empty proof path";
    return false;
  }
  proof_path_ = output_path;
  proof_records_.clear();
  proof_records_.reserve(kMaxProofRecords);
  proof_status_ = "armed";
  proof_invalidated_ = false;
  proof_invalid_reason_.clear();
  return true;
}

void Presenter::invalidate_capture(const std::string& reason) {
  std::lock_guard<std::mutex> lock(mutex_);
  if (proof_path_.empty() || proof_invalidated_) return;
  proof_invalidated_ = true;
  proof_invalid_reason_ = reason.empty() ? "presenter input or drain failed" : reason;
  ++metrics_.proof_failures;
}

bool Presenter::append_proof_locked(const PhysicalAck& acknowledgement, const Slot& slot) {
  if (proof_path_.empty()) return true;
  if (proof_records_.size() == kMaxProofRecords) {
    ++metrics_.proof_failures;
    proof_status_ = "capacity exceeded; no record overwritten";
    return false;
  }
  proof_records_.push_back({slot.identity, acknowledgement});
  ++metrics_.proof_records;
  return true;
}

void Presenter::write_proof_capture() {
  std::string path;
  std::vector<ProofRecord> records;
  Metrics metrics;
  bool physical = false;
  bool failed_closed = false;
  bool proof_invalidated = false;
  std::string failure_reason;
  std::string status;
  uint64_t refresh_millihz = 0;
  {
    std::lock_guard<std::mutex> lock(mutex_);
    if (proof_path_.empty() || proof_status_ == "written") return;
    path = proof_path_;
    records = proof_records_;
    metrics = metrics_;
    physical = backend_->supports_physical_presentation();
    failed_closed = failed_closed_;
    proof_invalidated = proof_invalidated_;
    failure_reason = failed_closed_ ? failure_reason_ : proof_invalid_reason_;
    status = proof_status_;
    refresh_millihz = backend_->mode_refresh_millihz();
  }
  const bool acceptance_valid = physical && !failed_closed && !proof_invalidated &&
                                metrics.proof_failures == 0 && metrics.backend_failures == 0;
  const bool capacity_exceeded = status == "capacity exceeded; no record overwritten";
  std::ostringstream json;
  json << "{\"schema\":\"hdmi-presenter-proof-v1\",\"physical\":"
       << (physical ? "true" : "false")
       << ",\"capture_status\":\"" << (proof_invalidated || failed_closed || capacity_exceeded ? "invalid" : "complete") << '\"'
       << ",\"acceptance_valid\":" << (acceptance_valid ? "true" : "false")
       << ",\"capacity_exceeded\":" << (capacity_exceeded ? "true" : "false")
       << ",\"proof_failures\":" << metrics.proof_failures
       << ",\"backend_failures\":" << metrics.backend_failures
       << ",\"failed_closed\":" << (failed_closed ? "true" : "false")
       << ",\"capture_invalidated\":" << (proof_invalidated ? "true" : "false")
       << ",\"failure_reason\":\"" << json_escape(failure_reason) << '"'
       << ",\"mode_refresh_millihz\":" << refresh_millihz
       << ",\"presented_at_hdmi_boundary\":" << metrics.presented_at_hdmi_boundary
       << ",\"source_pts_pacing_enabled\":" << (source_pts_pacing_ ? "true" : "false")
       << ",\"scanout_repeats\":" << metrics.scanout_repeats
       << ",\"note\":\"drm timestamp is board DRM event time; do not subtract source pts\",\"records\":[";
  for (size_t i = 0; i < records.size(); ++i) {
    const ProofRecord& record = records[i];
    if (i) json << ',';
    json << "{\"source_session_id\":" << record.identity.session_id
         << ",\"source_frame_id\":" << record.identity.frame_id
         << ",\"source_pts_ns\":" << record.identity.pts_ns
         << ",\"payload_crc32\":" << record.identity.payload_crc32
         << ",\"token\":" << record.acknowledgement.token
         << ",\"drm_sequence\":" << record.acknowledgement.drm_sequence
         << ",\"drm_tv_sec\":" << record.acknowledgement.drm_tv_sec
         << ",\"drm_tv_usec\":" << record.acknowledgement.drm_tv_usec
         << ",\"observer_session\":" << record.acknowledgement.observer_session << '}';
  }
  json << "]}\n";
  const std::string bytes = json.str();
  const int fd = open(path.c_str(), O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC, 0644);
  bool written = fd >= 0;
  size_t offset = 0;
  while (written && offset < bytes.size()) {
    const ssize_t count = write(fd, bytes.data() + offset, bytes.size() - offset);
    if (count > 0) offset += static_cast<size_t>(count);
    else if (count < 0 && errno == EINTR) continue;
    else written = false;
  }
  if (fd >= 0 && close(fd) != 0) written = false;
  std::lock_guard<std::mutex> lock(mutex_);
  if (written) proof_status_ = "written";
  else { ++metrics_.proof_failures; proof_status_ = "proof write failed or destination already exists"; }
}

SubmitResult Presenter::submit_rgb565(const FrameIdentity& id, const uint8_t* payload,
                                      size_t bytes, int wait_ms, std::string* error) {
  if (!payload || bytes != kRgb565Bytes) {
    if (error) *error = "invalid RGB565 payload size";
    return SubmitResult::kInvalid;
  }
  FillSlot fill;
  const SubmitResult begin = begin_fill(wait_ms, &fill, error);
  if (begin != SubmitResult::kQueued) return begin;
  std::memcpy(fill.rgb565, payload, bytes);
  return finish_fill(fill, id, error);
}

SubmitResult Presenter::begin_fill(int wait_ms, FillSlot* fill, std::string* error) {
  if (!fill || wait_ms < 0) return SubmitResult::kInvalid;
  std::unique_lock<std::mutex> lock(mutex_);
  if (!started_ || stop_requested_) {
    if (error) *error = failed_closed_ ? failure_reason_ : "presenter stopped";
    return SubmitResult::kStopped;
  }
  int index = reserve_free_slot_locked();
  if (index < 0) {
    const auto until = std::chrono::steady_clock::now() + std::chrono::milliseconds(wait_ms);
    while (index < 0 && !stop_requested_) {
      if (state_changed_.wait_until(lock, until) == std::cv_status::timeout) break;
      index = reserve_free_slot_locked();
    }
  }
  if (index < 0) {
    ++metrics_.queue_full;
    if (error) *error = stop_requested_ ? (failed_closed_ ? failure_reason_ : "presenter stopped")
                                        : "all presentation slots are retained; bounded backpressure";
    return stop_requested_ ? SubmitResult::kStopped : SubmitResult::kQueueFull;
  }
  update_watermark_locked();
  fill->index = static_cast<unsigned>(index);
  fill->rgb565 = slots_[static_cast<size_t>(index)].rgb565.data();
  return SubmitResult::kQueued;
}

SubmitResult Presenter::finish_fill(const FillSlot& fill, const FrameIdentity& id,
                                    std::string* error) {
  if (fill.index >= slots_.size() || !fill.rgb565) return SubmitResult::kInvalid;
  Slot& slot = slots_[fill.index];
  {
    std::lock_guard<std::mutex> lock(mutex_);
    if (stop_requested_ || slot.state != SlotState::kFilling || slot.rgb565.data() != fill.rgb565) {
      if (error) *error = failed_closed_ ? failure_reason_ : "fill slot is no longer owned";
      return SubmitResult::kStopped;
    }
  }
  const bool timing = stage_timing_enabled();
  const TimingSnapshot convert_begin = timing ? timing_snapshot() : TimingSnapshot{};
  RgbExpandMode expand_mode = RgbExpandMode::kScalar;
  { std::lock_guard<std::mutex> lock(mutex_); expand_mode = rgb_expand_mode_; }
  rgb565_to_xrgb8888_expand(slot.rgb565.data(), slot.xrgb_ptr, kRgb565Bytes / 2, expand_mode);
  if (timing) add_stage_timing(TimingStage::kConvert, convert_begin, timing_snapshot());
  {
    std::lock_guard<std::mutex> lock(mutex_);
    if (stop_requested_ || slot.state != SlotState::kFilling) return SubmitResult::kStopped;
    if (source_pts_pacing_ && next_ready_order_ == UINT64_MAX) {
      fail_closed_locked("source PTS pacing ready-order counter overflow");
      if (error) *error = failure_reason_;
      return SubmitResult::kStopped;
    }
    slot.ready_order = ++next_ready_order_;
    slot.identity = id;
    publish_slot_state_locked(fill.index, SlotState::kReady);
    ++metrics_.queued;
  }
  state_changed_.notify_all();
  return SubmitResult::kQueued;
}

void Presenter::abandon_fill(const FillSlot& fill) {
  if (fill.index >= slots_.size()) return;
  std::lock_guard<std::mutex> lock(mutex_);
  Slot& slot = slots_[fill.index];
  if (slot.state == SlotState::kFilling && slot.rgb565.data() == fill.rgb565) {
    publish_slot_state_locked(fill.index, SlotState::kFree);
    state_changed_.notify_all();
  }
}

void Presenter::worker() {
  while (true) {
    SlotView view{};
    uint64_t token = 0;
    bool do_submit = false;
    bool do_poll = false;
    {
      std::unique_lock<std::mutex> lock(mutex_);
      if (stop_requested_) return;
      int ready = (!backend_busy_ && token_to_slot_.size() < kMaxInFlight)
                      ? next_ready_slot_locked() : -1;
      bool pacing_wait = false;
      auto due = std::chrono::steady_clock::time_point::max();
      if (ready >= 0 && source_pts_pacing_) {
        std::string error;
        if (!source_pts_ready_locked(static_cast<unsigned>(ready), &due, &error)) {
          if (!error.empty()) { fail_closed_locked(error); state_changed_.notify_all(); return; }
          ready = -1;
          pacing_wait = true;
        }
      }
      if (ready >= 0) {
        Slot& slot = slots_[static_cast<unsigned>(ready)];
        token = next_token_++;
        if (token == 0) token = next_token_++;
        slot.token = token;
        publish_slot_state_locked(static_cast<unsigned>(ready), SlotState::kSubmitting);
        token_to_slot_.emplace(token, static_cast<unsigned>(ready));
        submit_order_.push_back(token);
        view = {slot.xrgb_ptr, kXrgb8888Bytes, slot.identity, static_cast<unsigned>(ready)};
        do_submit = true;
      } else if (!backend_busy_ && token_to_slot_.empty() &&
                 repeat_displayed_on_idle_ &&
                 (!repeat_paused_ || (source_pts_pacing_ && next_ready_slot_locked() >= 0)) &&
                 metrics_.presented_at_hdmi_boundary != 0 &&
                 backend_->supports_physical_presentation() && displayed_slot_ >= 0) {
        /* Keep the same already-acknowledged buffer physically refreshing
         * while the producer is idle.  This is not a new source frame: the
         * token is tracked separately and increments scanout_repeats only. */
        Slot& slot = slots_[static_cast<unsigned>(displayed_slot_)];
        token = next_token_++;
        if (token == 0) token = next_token_++;
        token_to_slot_.emplace(token, static_cast<unsigned>(displayed_slot_));
        repeat_tokens_.insert(token);
        submit_order_.push_back(token);
        view = {slot.xrgb_ptr, kXrgb8888Bytes, slot.identity,
                static_cast<unsigned>(displayed_slot_)};
        do_submit = true;
      } else if (!token_to_slot_.empty() && backend_->supports_physical_presentation()) {
        do_poll = true;
      } else if (!token_to_slot_.empty()) {
        /* Null backend has no scanout ownership, so immediate completion is
         * safe and intentionally never counted as physical presentation. */
        token = submit_order_.front();
        do_poll = false;
      } else {
        if (pacing_wait) {
          state_changed_.wait_until(lock, due);
          continue;
        }
        state_changed_.wait(lock, [&] {
          return stop_requested_ || next_ready_slot_locked() >= 0 ||
                 (!token_to_slot_.empty() && backend_->supports_physical_presentation()) ||
                 (repeat_displayed_on_idle_ && !repeat_paused_ &&
                  metrics_.presented_at_hdmi_boundary != 0 && displayed_slot_ >= 0 &&
                  backend_->supports_physical_presentation());
        });
        continue;
      }
    }

    if (do_submit) {
      std::string error;
      const bool timing = stage_timing_enabled();
      const TimingSnapshot submit_begin = timing ? timing_snapshot() : TimingSnapshot{};
      const BackendSubmitResult result = backend_->submit(view, token, &error);
      if (timing) add_stage_timing(TimingStage::kSubmit, submit_begin, timing_snapshot());
      std::lock_guard<std::mutex> lock(mutex_);
      const auto found = token_to_slot_.find(token);
      if (found == token_to_slot_.end()) {
        fail_closed_locked("backend submit raced with an invalid token state");
      } else if (result == BackendSubmitResult::kAccepted) {
        if (repeat_tokens_.find(token) == repeat_tokens_.end())
          publish_slot_state_locked(found->second, SlotState::kInFlight);
        ++metrics_.kms_submitted;
      } else if (result == BackendSubmitResult::kBusy) {
        /* This presenter owns the only standard DRM pending flip.  An EBUSY
         * therefore means a completion event exists which cannot be mapped to
         * one of our tokens (for example another KMS client raced us).  It is
         * unsafe to put this buffer back into READY and retry: doing so loses
         * the ownership proof for whichever buffer KMS is still scanning.
         * Fail closed rather than spinning or treating an unrelated event as
         * our acknowledgement. */
        ++metrics_.rejected;
        token_to_slot_.erase(found);
        if (!submit_order_.empty() && submit_order_.back() == token) submit_order_.pop_back();
        else {
          fail_closed_locked("backend busy response broke submission ordering");
          state_changed_.notify_all();
          return;
        }
        fail_closed_locked(error.empty() ? "DRM already has an unmapped pending page flip" : error);
      } else {
        /* A generic submission failure has no trustworthy ownership result:
         * it must not silently free the candidate last frame and let drain
         * report success. */
        ++metrics_.rejected;
        fail_closed_locked(error.empty() ? "backend rejected page-flip submission" : error);
        state_changed_.notify_all();
        return;
      }
      state_changed_.notify_all();
      continue;
    }

    std::string error;
    BackendAckResult ack_result = BackendAckResult::kAck;
    PhysicalAck acknowledgement{};
    acknowledgement.token = token;
    if (do_poll) {
      const bool timing = stage_timing_enabled();
      const TimingSnapshot ack_begin = timing ? timing_snapshot() : TimingSnapshot{};
      ack_result = backend_->wait_for_physical_ack(&acknowledgement, kAckPollMs, &error);
      if (timing) add_stage_timing(TimingStage::kAckWait, ack_begin, timing_snapshot());
    }
    if (ack_result == BackendAckResult::kTimeout) continue;
    std::lock_guard<std::mutex> lock(mutex_);
    if (ack_result == BackendAckResult::kFailure) {
      ++metrics_.rejected;
      fail_closed_locked(error.empty() ? "backend acknowledgement failure" : error);
      state_changed_.notify_all();
      return;
    }
    const auto found = token_to_slot_.find(acknowledgement.token);
    const bool repeated = repeat_tokens_.find(acknowledgement.token) != repeat_tokens_.end();
    if (acknowledgement.token == 0 || found == token_to_slot_.end() || submit_order_.empty() ||
        submit_order_.front() != acknowledgement.token ||
        (repeated ? slots_[found->second].state != SlotState::kDisplayed
                  : slots_[found->second].state != SlotState::kInFlight)) {
      ++metrics_.rejected;
      fail_closed_locked("late, duplicate, unknown, or out-of-order presentation acknowledgement");
      state_changed_.notify_all();
      return;
    }
    if (!note_pacing_ack_locked(acknowledgement, slots_[found->second].identity, repeated, &error)) {
      ++metrics_.rejected;
      fail_closed_locked(error);
      state_changed_.notify_all();
      return;
    }
    const unsigned new_displayed = found->second;
    const int old_displayed = displayed_slot_;
    token_to_slot_.erase(found);
    submit_order_.pop_front();
    backend_busy_ = false;
    if (repeated) {
      repeat_tokens_.erase(acknowledgement.token);
      ++metrics_.scanout_repeats;
      state_changed_.notify_all();
      continue;
    }
    if (backend_->supports_physical_presentation() &&
        !append_proof_locked(acknowledgement, slots_[new_displayed])) {
      fail_closed_locked("proof ledger capacity exceeded");
      state_changed_.notify_all();
      return;
    }
    if (old_displayed >= 0 && old_displayed != static_cast<int>(new_displayed)) {
      /* Hardware page-flip event for new_displayed is the only proof that the
       * old scanout buffer (including initial black) is no longer read. */
      publish_slot_state_locked(static_cast<unsigned>(old_displayed), SlotState::kFree);
    }
    displayed_slot_ = static_cast<int>(new_displayed);
    publish_slot_state_locked(new_displayed, SlotState::kDisplayed);
    if (backend_->supports_physical_presentation()) {
      ++metrics_.eof_completed;
      ++metrics_.presented_at_hdmi_boundary;
    }
    state_changed_.notify_all();
  }
}

Metrics Presenter::metrics() const { std::lock_guard<std::mutex> lock(mutex_); return metrics_; }
void Presenter::note_input_record() { std::lock_guard<std::mutex> lock(mutex_); ++metrics_.input_records; }
void Presenter::note_crc_verified() { std::lock_guard<std::mutex> lock(mutex_); ++metrics_.crc_verified; }
std::string Presenter::backend_status() const {
  std::lock_guard<std::mutex> lock(mutex_);
  if (failed_closed_) return failure_reason_;
  return backend_->supports_physical_presentation() ? "physical presentation enabled"
                                                    : backend_->unavailable_reason();
}
std::string Presenter::proof_status() const { std::lock_guard<std::mutex> lock(mutex_); return proof_status_; }
}  // namespace hdmi_presenter
