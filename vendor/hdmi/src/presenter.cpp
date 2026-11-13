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
/* Proof capture is a diagnosis artifact, not a bounded-duration gate.  Keep
 * enough recent identity/DRM pairs to diagnose a failure while keeping the
 * cumulative acknowledgement count for the complete run. */
constexpr size_t kProofTailRecords = 64;
constexpr auto kSignalLossDelay = std::chrono::seconds(1);

void draw_signal_loss_notice(uint8_t* pixels) {
  // "SIGNAL LOST", 5x7 glyphs, high-contrast on a black background.
  constexpr uint8_t glyphs[][7] = {
    {15,16,16,14,1,1,30}, {31,4,4,4,4,4,31}, {14,17,16,23,17,17,15},
    {17,25,21,19,17,17,17}, {14,17,17,31,17,17,17}, {16,16,16,16,16,16,31},
    {0,0,0,0,0,0,0}, {16,16,16,16,16,16,31}, {14,17,17,17,17,17,14},
    {15,16,16,14,1,1,30}, {31,4,4,4,4,4,4},
  };
  constexpr unsigned scale = 8;
  constexpr unsigned left = (kWidth - (sizeof(glyphs) / sizeof(glyphs[0]) * 6 - 1) * scale) / 2;
  constexpr unsigned top = (kHeight - 7 * scale) / 2;
  std::memset(pixels, 0, kXrgb8888Bytes);
  for (unsigned letter = 0; letter < sizeof(glyphs) / sizeof(glyphs[0]); ++letter)
    for (unsigned row = 0; row < 7; ++row)
      for (unsigned col = 0; col < 5; ++col)
        if (glyphs[letter][row] & (16U >> col))
          for (unsigned line = 0; line < scale; ++line) {
            const unsigned y = top + row * scale + line;
            const unsigned x = left + (letter * 6 + col) * scale;
            std::memset(pixels + (y * kWidth + x) * 4, 255, scale * 4);
          }
}

uint64_t physical_ack_timestamp_ns(const PhysicalAck& acknowledgement) {
  return static_cast<uint64_t>(acknowledgement.drm_tv_sec) * 1000000000ULL +
         static_cast<uint64_t>(acknowledgement.drm_tv_usec) * 1000ULL;
}

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
Presenter::~Presenter() { (void)stop(nullptr); }

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
    slots_[i].signal_loss_notice = false;
    publish_slot_state_locked(i, SlotState::kFree);
  }
  displayed_slot_ = backend_->initial_displayed_slot();
  if (displayed_slot_ < -1 || displayed_slot_ >= static_cast<int>(slots_.size())) {
    fail_closed_locked("backend reported an invalid initial displayed slot");
  } else if (displayed_slot_ >= 0) {
    publish_slot_state_locked(static_cast<unsigned>(displayed_slot_), SlotState::kDisplayed);
  }
  token_to_slot_.clear();
  repeat_tokens_.clear();
  submit_order_.clear();
  next_token_ = 1;
  next_ready_order_ = 0;
  backend_busy_ = false;
  stop_requested_ = false;
  repeat_paused_ = false;
  signal_loss_deadline_ = std::chrono::steady_clock::now() + kSignalLossDelay;
  last_physical_ack_ = {};
  last_physical_ack_valid_ = false;
  backend_was_physical_ = backend_->supports_physical_presentation();
  backend_mode_refresh_millihz_ = backend_->mode_refresh_millihz();
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

void Presenter::set_signal_loss_notice(bool enabled) {
  std::lock_guard<std::mutex> lock(mutex_);
  if (!started_) signal_loss_notice_enabled_ = enabled;
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
  /* Draining is the quiescence boundary: an in-flight repeat may retire, but
   * it cannot start another repeat and keep the drain permanently busy. */
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

bool Presenter::stop(std::string* error) {
  {
    std::lock_guard<std::mutex> lock(mutex_);
    if (!started_) return true;
    stop_requested_ = true;
    repeat_paused_ = true;
  }
  state_changed_.notify_all();
  if (worker_.joinable()) worker_.join();
  /* backend shutdown replaces/disables CRTC before dumb buffers are removed;
   * we never mark an unacknowledged in-flight slot reusable during teardown. */
  std::string shutdown_error;
  const bool clean_shutdown = backend_->shutdown(&shutdown_error);
  {
    std::lock_guard<std::mutex> lock(mutex_);
    if (!clean_shutdown) {
      ++metrics_.backend_failures;
      proof_invalidated_ = true;
      proof_invalid_reason_ = "backend teardown failed: " + shutdown_error;
      if (error) *error = proof_invalid_reason_;
    }
    started_ = false;
  }
  write_proof_capture();
  return clean_shutdown;
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
  int oldest = -1;
  for (unsigned i = 0; i < slots_.size(); ++i)
    if (slots_[i].state == SlotState::kReady &&
        (oldest < 0 || slots_[i].ready_order < slots_[static_cast<unsigned>(oldest)].ready_order))
      oldest = static_cast<int>(i);
  return oldest;
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
  proof_tail_.clear();
  first_physical_identity_ = {};
  last_physical_identity_ = {};
  metrics_.physical_ack_window_valid = false;
  metrics_.first_physical_ack_ns = 0;
  metrics_.last_physical_ack_ns = 0;
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

void Presenter::set_new_frame_ack(NewFrameAck callback, void* owner) {
  std::lock_guard<std::mutex> lock(mutex_);
  new_frame_ack_ = callback;
  new_frame_ack_owner_ = owner;
}

void Presenter::set_ready_frame_expired(ReadyFrameExpired callback, void* owner) {
  std::lock_guard<std::mutex> lock(mutex_);
  ready_frame_expired_ = callback;
  ready_frame_expired_owner_ = owner;
}

bool Presenter::append_proof_locked(const PhysicalAck& acknowledgement, const Slot& slot) {
  const uint64_t timestamp_ns = physical_ack_timestamp_ns(acknowledgement);
  if (new_frame_ack_) new_frame_ack_(new_frame_ack_owner_, slot.identity, timestamp_ns);
  if (proof_path_.empty()) return true;
  if (proof_tail_.size() == kProofTailRecords) proof_tail_.pop_front();
  proof_tail_.push_back({slot.identity, acknowledgement});
  if (!metrics_.physical_ack_window_valid) {
    metrics_.physical_ack_window_valid = true;
    metrics_.first_physical_ack_ns = timestamp_ns;
    first_physical_identity_ = slot.identity;
  } else if (timestamp_ns >= metrics_.last_physical_ack_ns) {
    const uint64_t gap = timestamp_ns - metrics_.last_physical_ack_ns;
    metrics_.max_new_frame_gap_ns = std::max(metrics_.max_new_frame_gap_ns, gap);
    metrics_.new_frame_gaps_over_1s += gap > 1000000000ULL;
  }
  metrics_.last_physical_ack_ns = timestamp_ns;
  last_physical_identity_ = slot.identity;
  ++metrics_.proof_records;
  return true;
}

bool Presenter::write_physical_snapshot(const std::string& output_path, std::string* error) {
  if (output_path.empty()) {
    if (error) *error = "empty physical snapshot path";
    return false;
  }
  std::lock_guard<std::mutex> lock(mutex_);
  if (!started_ || failed_closed_ || !backend_->supports_physical_presentation() ||
      !last_physical_ack_valid_ || displayed_slot_ < 0 ||
      displayed_slot_ >= static_cast<int>(slots_.size()) ||
      slots_[static_cast<unsigned>(displayed_slot_)].state != SlotState::kDisplayed) {
    if (error) *error = "physical snapshot requires a normally drained, physically acknowledged displayed slot";
    return false;
  }
  const Slot& slot = slots_[static_cast<unsigned>(displayed_slot_)];
  if (!slot.xrgb_ptr) {
    if (error) *error = "physical snapshot displayed slot is unmapped";
    return false;
  }
  const std::string metadata_path = output_path + ".json";
  const int image_fd = open(output_path.c_str(), O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC, 0644);
  if (image_fd < 0) {
    if (error) *error = std::string("physical snapshot image create: ") + std::strerror(errno);
    return false;
  }
  const int metadata_fd = open(metadata_path.c_str(), O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC, 0644);
  if (metadata_fd < 0) {
    const int saved = errno;
    close(image_fd);
    unlink(output_path.c_str());
    if (error) *error = std::string("physical snapshot metadata create: ") + std::strerror(saved);
    return false;
  }
  std::ostringstream metadata;
  metadata << "{\"schema\":\"hdmi-presenter-snapshot-v1\",\"physical\":true"
           << ",\"format\":\"XRGB8888\",\"width\":" << kWidth
           << ",\"height\":" << kHeight << ",\"stride_bytes\":" << kWidth * 4
           << ",\"image_bytes\":" << kXrgb8888Bytes
           << ",\"source_session_id\":" << slot.identity.session_id
           << ",\"source_frame_id\":" << slot.identity.frame_id
           << ",\"source_pts_ns\":" << slot.identity.pts_ns
           << ",\"capture_uncertainty_us\":" << slot.identity.capture_uncertainty_us
           << ",\"payload_crc32\":" << slot.identity.payload_crc32
           << ",\"drm_sequence\":" << last_physical_ack_.drm_sequence
           << ",\"drm_tv_sec\":" << last_physical_ack_.drm_tv_sec
           << ",\"drm_tv_usec\":" << last_physical_ack_.drm_tv_usec
           << ",\"observer_session\":" << last_physical_ack_.observer_session << "}\n";
  const std::string metadata_bytes = metadata.str();
  const auto write_all = [](int fd, const uint8_t* data, size_t bytes) {
    size_t offset = 0;
    while (offset < bytes) {
      const ssize_t count = write(fd, data + offset, bytes - offset);
      if (count > 0) offset += static_cast<size_t>(count);
      else if (count < 0 && errno == EINTR) continue;
      else return false;
    }
    return true;
  };
  const bool image_written = write_all(image_fd, slot.xrgb_ptr, kXrgb8888Bytes);
  const bool metadata_written = image_written && write_all(
      metadata_fd, reinterpret_cast<const uint8_t*>(metadata_bytes.data()), metadata_bytes.size());
  const int image_close = close(image_fd);
  const int metadata_close = close(metadata_fd);
  const bool closed = image_close == 0 && metadata_close == 0;
  if (image_written && metadata_written && closed) return true;
  unlink(output_path.c_str());
  unlink(metadata_path.c_str());
  if (error) *error = "physical snapshot write failed";
  return false;
}

void Presenter::write_proof_capture() {
  std::string path;
  std::deque<ProofRecord> records;
  Metrics metrics;
  FrameIdentity first_identity{};
  FrameIdentity last_identity{};
  bool physical = false;
  bool failed_closed = false;
  bool proof_invalidated = false;
  std::string failure_reason;
  uint64_t refresh_millihz = 0;
  {
    std::lock_guard<std::mutex> lock(mutex_);
    if (proof_path_.empty() || proof_status_ == "written") return;
    path = proof_path_;
    records = proof_tail_;
    metrics = metrics_;
    first_identity = first_physical_identity_;
    last_identity = last_physical_identity_;
    physical = backend_was_physical_;
    failed_closed = failed_closed_;
    proof_invalidated = proof_invalidated_;
    failure_reason = failed_closed_ ? failure_reason_ : proof_invalid_reason_;
    refresh_millihz = backend_mode_refresh_millihz_;
  }
  const bool acceptance_valid = physical && !failed_closed && !proof_invalidated &&
                                metrics.proof_failures == 0 && metrics.backend_failures == 0;
  const bool physical_window = physical && metrics.physical_ack_window_valid;
  const bool timestamp_order_valid = physical_window &&
      metrics.last_physical_ack_ns >= metrics.first_physical_ack_ns;
  const uint64_t span_ns = timestamp_order_valid
      ? metrics.last_physical_ack_ns - metrics.first_physical_ack_ns : 0;
  const uint64_t new_frames = physical ? metrics.presented_at_hdmi_boundary : 0;
  const double new_frame_fps = physical_window && new_frames > 1 && span_ns != 0
      ? static_cast<double>(new_frames - 1) * 1000000000.0 / static_cast<double>(span_ns) : 0.0;
  const char* fps_scope = !physical ? "unavailable" :
      (physical_window ? "physical_drm_ack" : "no_physical_ack");
  std::ostringstream json;
  json << "{\"schema\":\"hdmi-presenter-proof-v1\",\"physical\":"
       << (physical ? "true" : "false")
       << ",\"capture_status\":\"" << (proof_invalidated || failed_closed ? "invalid" : "complete") << '\"'
       << ",\"acceptance_valid\":" << (acceptance_valid ? "true" : "false")
       << ",\"capacity_exceeded\":false"
       << ",\"proof_failures\":" << metrics.proof_failures
       << ",\"backend_failures\":" << metrics.backend_failures
       << ",\"failed_closed\":" << (failed_closed ? "true" : "false")
       << ",\"capture_invalidated\":" << (proof_invalidated ? "true" : "false")
       << ",\"failure_reason\":\"" << json_escape(failure_reason) << '"'
       << ",\"mode_refresh_millihz\":" << refresh_millihz
       << ",\"presented_at_hdmi_boundary\":" << metrics.presented_at_hdmi_boundary
       << ",\"new_frames\":" << new_frames
       << ",\"signal_loss_notices\":" << metrics.signal_loss_notices
       << ",\"first_signal_loss_ack_ns\":" << metrics.first_signal_loss_ack_ns
       << ",\"last_signal_loss_ack_ns\":" << metrics.last_signal_loss_ack_ns
       << ",\"scanout_repeats\":" << metrics.scanout_repeats
       << ",\"physical_ack_window_valid\":" << (physical_window ? "true" : "false")
       << ",\"first_physical_ack_ns\":" << (physical_window ? metrics.first_physical_ack_ns : 0)
       << ",\"last_physical_ack_ns\":" << (physical_window ? metrics.last_physical_ack_ns : 0)
       << ",\"timestamp_order_valid\":" << (timestamp_order_valid ? "true" : "false")
       << ",\"span_ns\":" << span_ns
       << ",\"derived_new_frame_fps\":" << new_frame_fps
       << ",\"new_frame_fps_scope\":\"" << fps_scope << '\"'
       << ",\"first_source_session_id\":" << (physical_window ? first_identity.session_id : 0)
       << ",\"first_source_frame_id\":" << (physical_window ? first_identity.frame_id : 0)
       << ",\"last_source_session_id\":" << (physical_window ? last_identity.session_id : 0)
       << ",\"last_source_frame_id\":" << (physical_window ? last_identity.frame_id : 0)
       << ",\"proof_records_total\":" << metrics.proof_records
       << ",\"proof_records_retained\":" << records.size()
       << ",\"records_are_recent_tail\":true"
       << ",\"note\":\"drm timestamp is board DRM event time; do not subtract source pts; records are bounded diagnostic tail\",\"records\":[";
  for (size_t i = 0; i < records.size(); ++i) {
    const ProofRecord& record = records[i];
    if (i) json << ',';
    json << "{\"source_session_id\":" << record.identity.session_id
         << ",\"source_frame_id\":" << record.identity.frame_id
         << ",\"source_pts_ns\":" << record.identity.pts_ns
         << ",\"capture_uncertainty_us\":" << record.identity.capture_uncertainty_us
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
    slot.identity = id;
    slot.signal_loss_notice = false;
    slot.ready_order = next_ready_order_++;
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
      if (ready >= 0 && ready_frame_expired_ &&
          ready_frame_expired_(ready_frame_expired_owner_, slots_[ready].identity)) {
        publish_slot_state_locked(static_cast<unsigned>(ready), SlotState::kFree);
        ++metrics_.queued_expired;
        state_changed_.notify_all();
        continue;
      }
      const bool notice_displayed = displayed_slot_ >= 0 &&
          slots_[static_cast<unsigned>(displayed_slot_)].signal_loss_notice;
      const bool notice_idle = signal_loss_notice_enabled_ && !notice_displayed &&
          !repeat_paused_ && !backend_busy_ && token_to_slot_.empty() &&
          backend_->supports_physical_presentation() &&
          std::none_of(slots_.begin(), slots_.end(),
                       [](const Slot& slot) { return slot.state == SlotState::kFilling; });
      if (ready < 0 && notice_idle && std::chrono::steady_clock::now() >= signal_loss_deadline_) {
        ready = reserve_free_slot_locked();
        if (ready >= 0) {
          Slot& slot = slots_[static_cast<unsigned>(ready)];
          slot.signal_loss_notice = true;
          slot.identity = {};
          draw_signal_loss_notice(slot.xrgb_ptr);
          update_watermark_locked();
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
                 repeat_displayed_on_idle_ && !repeat_paused_ && !notice_displayed &&
                 metrics_.presented_at_hdmi_boundary != 0 &&
                 backend_->supports_physical_presentation() && displayed_slot_ >= 0) {
        /* Repeating an acknowledged framebuffer keeps scanout fed without
         * changing source-frame accounting or buffer ownership. */
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
        if (notice_idle && std::chrono::steady_clock::now() < signal_loss_deadline_)
          state_changed_.wait_until(lock, signal_loss_deadline_);
        else
          state_changed_.wait(lock);
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
    const bool signal_loss_notice = slots_[new_displayed].signal_loss_notice;
    if (backend_->supports_physical_presentation() && !signal_loss_notice &&
        !append_proof_locked(acknowledgement, slots_[new_displayed])) {
      fail_closed_locked("failed to retain HDMI presentation proof");
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
      last_physical_ack_ = acknowledgement;
      last_physical_ack_valid_ = !signal_loss_notice;
      if (signal_loss_notice) {
        metrics_.last_signal_loss_ack_ns = physical_ack_timestamp_ns(acknowledgement);
        if (metrics_.signal_loss_notices == 0)
          metrics_.first_signal_loss_ack_ns = metrics_.last_signal_loss_ack_ns;
        ++metrics_.signal_loss_notices;
      } else {
        signal_loss_deadline_ = std::chrono::steady_clock::now() + kSignalLossDelay;
        ++metrics_.eof_completed;
        ++metrics_.presented_at_hdmi_boundary;
      }
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
