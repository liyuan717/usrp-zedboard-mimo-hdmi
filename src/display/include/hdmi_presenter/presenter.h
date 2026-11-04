#pragma once

#include "hdmi_presenter/protocol.h"
#include "hdmi_presenter/rgb_expand.h"

#include <array>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <unordered_map>
#include <unordered_set>
#include <deque>
#include <vector>

namespace hdmi_presenter {

enum class SlotState { kFree, kFilling, kReady, kSubmitting, kInFlight, kDisplayed };
enum class SubmitResult { kQueued, kQueueFull, kStopped, kInvalid }; 
enum class BackendSubmitResult { kAccepted, kBusy, kFailure };
enum class BackendAckResult { kAck, kTimeout, kFailure };
struct FillSlot { unsigned index{}; uint8_t* rgb565{}; };

/* Timing is opt-in and cumulative. Wall time is CLOCK_MONOTONIC; CPU time is
 * CLOCK_THREAD_CPUTIME_ID sampled in the thread which executed that stage.
 * Thus reader and display-worker CPU counters are intentionally additive, not
 * one process-wide CPU clock. */
struct TimingSnapshot { uint64_t monotonic_ns{}; uint64_t thread_cpu_ns{}; };
enum class TimingStage { kHeaderRead, kPayloadRead, kCrc, kQueueWait, kConvert, kSubmit, kAckWait };
struct TimingCounter { uint64_t count{}; uint64_t wall_ns{}; uint64_t thread_cpu_ns{}; };
struct StageTimingTotals {
  TimingCounter header_read, payload_read, crc, queue_wait, convert, submit, ack_wait;
};
TimingSnapshot timing_snapshot();
uint64_t timing_delta(uint64_t begin, uint64_t end);

struct Metrics {
  uint64_t input_records{};
  uint64_t crc_verified{};
  uint64_t queued{};
  uint64_t kms_submitted{};
  uint64_t eof_completed{};
  /* HDMI transmitter frame-boundary acknowledgement only.  It deliberately
   * says nothing about a panel/camera/human visual acceptance test. */
  uint64_t presented_at_hdmi_boundary{};
  uint64_t rejected{};
  uint64_t queue_full{};
  uint64_t scanout_repeats{};
  uint64_t backend_failures{};
  uint64_t proof_records{};
  uint64_t proof_failures{};
  uint32_t queue_high_watermark{};
  std::array<SlotState, 3> slots{};
};

struct PhysicalAck {
  uint64_t token{};
  uint32_t drm_sequence{};
  uint32_t drm_tv_sec{};
  uint32_t drm_tv_usec{};
  uint64_t observer_session{};
};

struct SlotView {
  const uint8_t* xrgb{};
  size_t xrgb_bytes{};
  FrameIdentity identity{};
  unsigned slot_index{};
};

/* PresentBackend is called only by the display worker. A successful submit
 * must not imply presentation: the nonzero application token completes only
 * on the paired driver's production DMA/VSYNC page-flip event. Standard DRM
 * admits one unfinished flip per CRTC; token bookkeeping remains strict so a
 * future explicit queue ABI cannot weaken buffer ownership. */
class PresentBackend {
 public:
  virtual ~PresentBackend() = default;
  virtual bool supports_physical_presentation() const = 0;
  virtual std::string unavailable_reason() const = 0;
  virtual int initial_displayed_slot() const = 0;
  virtual uint64_t mode_refresh_millihz() const = 0;
  virtual BackendSubmitResult submit(const SlotView& view, uint64_t token,
                                     std::string* error) = 0;
  virtual uint8_t* xrgb_slot(unsigned slot_index, size_t bytes) = 0;
  virtual BackendAckResult wait_for_physical_ack(PhysicalAck* acknowledgement, int timeout_ms,
                                                  std::string* error) = 0;
  virtual void shutdown() = 0;
};

std::unique_ptr<PresentBackend> make_null_backend();
std::unique_ptr<PresentBackend> make_drm_backend(const std::string& card_path);

class Presenter {
 public:
  explicit Presenter(std::unique_ptr<PresentBackend> backend);
  ~Presenter();
  Presenter(const Presenter&) = delete;
  Presenter& operator=(const Presenter&) = delete;
  void start();
  /* Waits for every queued/submitted frame to reach the backend's completion
   * boundary. The displayed slot intentionally remains retained. */
  bool drain(int timeout_ms, std::string* error);
  void stop();
  SubmitResult submit_rgb565(const FrameIdentity& identity, const uint8_t* payload,
                             size_t bytes, int queue_wait_ms, std::string* error);
  SubmitResult begin_fill(int queue_wait_ms, FillSlot* fill, std::string* error);
  SubmitResult finish_fill(const FillSlot& fill, const FrameIdentity& identity, std::string* error);
  void abandon_fill(const FillSlot& fill);
  void note_input_record();
  void note_crc_verified();
  void set_stage_timing_enabled(bool enabled);
  void set_repeat_displayed_on_idle(bool enabled);
  void set_rgb_expand_mode(RgbExpandMode mode);
  void set_source_pts_pacing(bool enabled);
  bool stage_timing_enabled() const;
  void add_stage_timing(TimingStage stage, const TimingSnapshot& begin, const TimingSnapshot& end);
  StageTimingTotals stage_timing_totals() const;
  std::string stage_timing_summary_json() const;
  /* Marks a configured proof invalid because an input/protocol/drain failure
   * happened outside the backend worker.  It never rewrites prior records. */
  void invalidate_capture(const std::string& reason);
  bool set_proof_capture_path(const std::string& output_path, std::string* error);
  Metrics metrics() const;
  std::string backend_status() const;
  std::string proof_status() const;

 private:
  struct Slot {
    SlotState state{SlotState::kFree};
    FrameIdentity identity{};
    uint64_t token{};
    uint64_t ready_order{};
    std::vector<uint8_t> rgb565 = std::vector<uint8_t>(kRgb565Bytes);
    std::vector<uint8_t> xrgb;
    uint8_t* xrgb_ptr{};
  };
  void worker();
  int reserve_free_slot_locked();
  int next_ready_slot_locked() const;
  void update_watermark_locked();
  bool source_pts_ready_locked(unsigned index, std::chrono::steady_clock::time_point* due, std::string* error) const;
  bool note_pacing_ack_locked(const PhysicalAck& ack, const FrameIdentity& id, bool repeated, std::string* error);
  void fail_closed_locked(const std::string& reason);
  void publish_slot_state_locked(unsigned index, SlotState state);
  bool append_proof_locked(const PhysicalAck& acknowledgement, const Slot& slot);
  void write_proof_capture();

  std::unique_ptr<PresentBackend> backend_;
  mutable std::mutex mutex_;
  std::condition_variable state_changed_;
  std::array<Slot, 3> slots_;
  std::unordered_map<uint64_t, unsigned> token_to_slot_;
  std::unordered_set<uint64_t> repeat_tokens_;
  std::deque<uint64_t> submit_order_;
  struct ProofRecord { FrameIdentity identity{}; PhysicalAck acknowledgement{}; };
  std::vector<ProofRecord> proof_records_;
  Metrics metrics_;
  StageTimingTotals stage_timing_;
  bool stage_timing_enabled_{};
  RgbExpandMode rgb_expand_mode_{RgbExpandMode::kScalar};
  int displayed_slot_{-1};
  uint64_t next_token_{1};
  bool started_{false};
  bool stop_requested_{false};
  bool backend_busy_{false};
  bool repeat_displayed_on_idle_{false};
  bool repeat_paused_{false};
  bool source_pts_pacing_{};
  bool have_source_ack_{};
  bool have_pacing_ack_{};
  FrameIdentity last_paced_source_{};
  uint32_t source_drm_sequence_{};
  uint32_t latest_drm_sequence_{};
  uint32_t pacing_advance_{2};
  uint64_t pacing_observer_session_{};
  uint64_t next_ready_order_{};
  std::chrono::steady_clock::time_point last_source_ack_time_{};
  bool failed_closed_{false};
  std::string failure_reason_;
  std::string proof_path_;
  std::string proof_status_{"disabled"};
  bool proof_invalidated_{};
  std::string proof_invalid_reason_;
  std::thread worker_;
};

}  // namespace hdmi_presenter
