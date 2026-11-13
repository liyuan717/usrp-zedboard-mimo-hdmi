#pragma once
#include "hdmi_presenter/presenter.h"
#include <cstdint>
#include <string>

namespace hdmi_presenter {
enum class InputKind { kHdv1Tcp, kCodecOutputFd };
struct ReceiverConfig {
  int read_timeout_ms{3000};
  int queue_wait_ms{1000};
  /* Codec feedback is header-only, optional, and must never block stdout
   * consumption indefinitely. It is not a presentation acknowledgement. */
  int codec_feedback_fd{-1};
  int feedback_timeout_ms{50};
  int drain_timeout_ms{3000};
  bool repeat_displayed_on_idle{};
  bool stage_timing{};
  RgbExpandMode rgb_expand{RgbExpandMode::kScalar};
  uint64_t max_records{};
};
struct ReceiverResult {
  uint64_t accepted{};
  uint64_t control_records{};
  uint64_t feedback_records{};
  bool saw_stop_ack{};
  std::string error;
};
/* Reads exactly framed records from fd. It tolerates partial reads and EINTR;
 * a framing, CRC, or full-queue error is explicit in error. */
ReceiverResult receive_records(int fd, InputKind kind, Presenter* presenter, const ReceiverConfig& config);
int serve_tcp(uint16_t port, Presenter* presenter, const ReceiverConfig& config, std::string* error);
}  // namespace hdmi_presenter
