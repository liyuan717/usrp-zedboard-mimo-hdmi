#include "hdmi_presenter/receiver.h"

#include <array>
#include <cerrno>
#include <chrono>
#include <cstring>
#include <fcntl.h>
#include <netinet/in.h>
#include <poll.h>
#include <sys/socket.h>
#include <unistd.h>

namespace hdmi_presenter {
namespace {
enum class ReadResult { kOk, kEof, kTimeout, kError };

ReadResult read_exact(int fd, uint8_t* out, size_t bytes, int timeout_ms, std::string* error) {
  size_t offset = 0;
  while (offset < bytes) {
    pollfd pfd{fd, static_cast<short>(POLLIN | POLLHUP), 0};
    const int ready = poll(&pfd, 1, timeout_ms);
    if (ready == 0) { if (error) *error = "input timeout during framed record"; return ReadResult::kTimeout; }
    if (ready < 0) { if (errno == EINTR) continue; if (error) *error = std::string("poll: ") + std::strerror(errno); return ReadResult::kError; }
    const ssize_t count = read(fd, out + offset, bytes - offset);
    if (count > 0) { offset += static_cast<size_t>(count); continue; }
    if (count == 0) { if (offset == 0) return ReadResult::kEof; if (error) *error = "EOF in partial frame"; return ReadResult::kError; }
    if (errno == EINTR) continue;
    if (error) *error = std::string("read: ") + std::strerror(errno);
    return ReadResult::kError;
  }
  return ReadResult::kOk;
}

ReadResult timed_read_exact(int fd, uint8_t* out, size_t bytes, int timeout_ms,
                            Presenter* presenter, bool timing, TimingStage stage,
                            std::string* error) {
  const TimingSnapshot begin = timing ? timing_snapshot() : TimingSnapshot{};
  const ReadResult result = read_exact(fd, out, bytes, timeout_ms, error);
  if (timing) presenter->add_stage_timing(stage, begin, timing_snapshot());
  return result;
}

bool monotonic(const FrameIdentity& id, bool* have, FrameIdentity* last, std::string* error) {
  if (!*have) { *last = id; *have = true; return true; }
  if (id.session_id < last->session_id) { if (error) *error = "session_id moved backwards"; return false; }
  if (id.session_id > last->session_id) { *last = id; return true; }
  if (id.frame_id <= last->frame_id) { if (error) *error = "frame_id is not strictly increasing within session"; return false; }
  if (id.pts_ns <= last->pts_ns) { if (error) *error = "pts_ns is not strictly increasing within session"; return false; }
  *last = id;
  return true;
}

bool write_feedback_header(int fd, const std::array<uint8_t, kVcodecHeaderBytes>& header,
                           int timeout_ms, std::string* error) {
  if (fd < 0) return true;
  const int original_flags = fcntl(fd, F_GETFL);
  if (original_flags < 0) { if (error) *error = std::string("feedback F_GETFL: ") + std::strerror(errno); return false; }
  const bool restore_flags = (original_flags & O_NONBLOCK) == 0;
  if (restore_flags && fcntl(fd, F_SETFL, original_flags | O_NONBLOCK) != 0) {
    if (error) *error = std::string("feedback F_SETFL: ") + std::strerror(errno);
    return false;
  }
  bool written = true;
  size_t offset = 0;
  const auto deadline = std::chrono::steady_clock::now() + std::chrono::milliseconds(timeout_ms);
  while (offset < header.size()) {
    const auto now = std::chrono::steady_clock::now();
    if (now >= deadline) { written = false; if (error) *error = "codec feedback deadline exceeded"; break; }
    const auto remaining = std::chrono::duration_cast<std::chrono::milliseconds>(deadline - now).count();
    pollfd pfd{fd, POLLOUT, 0};
    const int ready = poll(&pfd, 1, static_cast<int>(remaining > 0 ? remaining : 0));
    if (ready == 0) { written = false; if (error) *error = "codec feedback deadline exceeded"; break; }
    if (ready < 0) { if (errno == EINTR) continue; written = false; if (error) *error = std::string("codec feedback poll: ") + std::strerror(errno); break; }
    if (pfd.revents & (POLLERR | POLLHUP | POLLNVAL)) { written = false; if (error) *error = "codec feedback peer unavailable"; break; }
    const ssize_t count = write(fd, header.data() + offset, header.size() - offset);
    if (count > 0) { offset += static_cast<size_t>(count); continue; }
    if (count < 0 && (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK)) continue;
    written = false;
    if (error) *error = count == 0 ? "codec feedback short write" : std::string("codec feedback write: ") + std::strerror(errno);
    break;
  }
  if (restore_flags && fcntl(fd, F_SETFL, original_flags) != 0) {
    if (written) {
      written = false;
      if (error) *error = std::string("feedback flag restore: ") + std::strerror(errno);
    }
  }
  return written;
}

bool feedback(const ReceiverConfig& config, const std::array<uint8_t, kVcodecHeaderBytes>& header,
              ReceiverResult* result, std::string* error) {
  if (!write_feedback_header(config.codec_feedback_fd, header, config.feedback_timeout_ms, error)) return false;
  if (config.codec_feedback_fd >= 0) ++result->feedback_records;
  return true;
}

ReceiverResult receive_hdv1_records(int fd, Presenter* presenter, const ReceiverConfig& config) {
  ReceiverResult result;
  const bool timing = config.stage_timing;
  bool have = false;
  FrameIdentity last{};
  while (config.max_records == 0 || result.accepted < config.max_records) {
    std::array<uint8_t, kHdv1HeaderBytes> header{};
    std::string error;
    const ReadResult header_result = timed_read_exact(fd, header.data(), header.size(), config.read_timeout_ms,
                                                      presenter, timing, TimingStage::kHeaderRead, &error);
    if (header_result == ReadResult::kEof) break;
    if (header_result != ReadResult::kOk) { result.error = error; break; }
    presenter->note_input_record();
    FrameIdentity identity{};
    const ParseError parsed = parse_hdv1_header(header, &identity);
    if (parsed != ParseError::kOk) { result.error = std::string("bad input header: ") + parse_error_name(parsed); break; }
    if (!monotonic(identity, &have, &last, &error)) { result.error = error; break; }
    FillSlot fill;
    const TimingSnapshot queue_begin = timing ? timing_snapshot() : TimingSnapshot{};
    const SubmitResult queued = presenter->begin_fill(config.queue_wait_ms, &fill, &error);
    if (timing) presenter->add_stage_timing(TimingStage::kQueueWait, queue_begin, timing_snapshot());
    if (queued != SubmitResult::kQueued) { result.error = error.empty() ? "presentation queue unavailable" : error; break; }
    const ReadResult payload_result = timed_read_exact(fd, fill.rgb565, kRgb565Bytes, config.read_timeout_ms,
                                                       presenter, timing, TimingStage::kPayloadRead, &error);
    if (payload_result != ReadResult::kOk) { presenter->abandon_fill(fill); result.error = payload_result == ReadResult::kEof ? "EOF before frame payload" : error; break; }
    const TimingSnapshot crc_begin = timing ? timing_snapshot() : TimingSnapshot{};
    const uint32_t crc = ieee_crc32(fill.rgb565, kRgb565Bytes);
    if (timing) presenter->add_stage_timing(TimingStage::kCrc, crc_begin, timing_snapshot());
    if (crc != identity.payload_crc32) { presenter->abandon_fill(fill); result.error = "payload CRC32 mismatch"; break; }
    presenter->note_crc_verified();
    if (presenter->finish_fill(fill, identity, &error) != SubmitResult::kQueued) { presenter->abandon_fill(fill); result.error = error.empty() ? "presenter stopped" : error; break; }
    ++result.accepted;
  }
  return result;
}

ReceiverResult receive_codec_records(int fd, Presenter* presenter, const ReceiverConfig& config) {
  ReceiverResult result;
  const bool timing = config.stage_timing;
  bool have_frame = false;
  FrameIdentity last_frame{};
  while (true) {
    std::array<uint8_t, kVcodecHeaderBytes> header{};
    std::string error;
    const ReadResult header_result = timed_read_exact(fd, header.data(), header.size(), config.read_timeout_ms,
                                                      presenter, timing, TimingStage::kHeaderRead, &error);
    if (header_result == ReadResult::kEof) { result.error = "codec stdout EOF before STOP_ACK"; return result; }
    if (header_result != ReadResult::kOk) { result.error = error; return result; }
    presenter->note_input_record();
    VcdoRecord record{};
    const ParseError parsed = parse_vcdo_record_header(header, &record);
    if (parsed != ParseError::kOk) { result.error = std::string("bad codec output header: ") + parse_error_name(parsed); return result; }
    if (record.type == VcdoRecordType::kFrame) {
      if (!monotonic(record.identity, &have_frame, &last_frame, &error)) { result.error = error; return result; }
      FillSlot fill;
      const TimingSnapshot queue_begin = timing ? timing_snapshot() : TimingSnapshot{};
      const SubmitResult queued = presenter->begin_fill(config.queue_wait_ms, &fill, &error);
      if (timing) presenter->add_stage_timing(TimingStage::kQueueWait, queue_begin, timing_snapshot());
      if (queued != SubmitResult::kQueued) { result.error = error.empty() ? "presentation queue unavailable" : error; return result; }
      const ReadResult payload_result = timed_read_exact(fd, fill.rgb565, kRgb565Bytes, config.read_timeout_ms,
                                                         presenter, timing, TimingStage::kPayloadRead, &error);
      if (payload_result != ReadResult::kOk) { presenter->abandon_fill(fill); result.error = payload_result == ReadResult::kEof ? "EOF before codec frame payload" : error; return result; }
      const TimingSnapshot crc_begin = timing ? timing_snapshot() : TimingSnapshot{};
      const uint32_t crc = ieee_crc32(fill.rgb565, kRgb565Bytes);
      if (timing) presenter->add_stage_timing(TimingStage::kCrc, crc_begin, timing_snapshot());
      if (crc != record.identity.payload_crc32) { presenter->abandon_fill(fill); result.error = "codec frame payload CRC32 mismatch"; return result; }
      presenter->note_crc_verified();
      if (presenter->finish_fill(fill, record.identity, &error) != SubmitResult::kQueued) { presenter->abandon_fill(fill); result.error = error.empty() ? "presenter stopped" : error; return result; }
      if (!feedback(config, header, &result, &error)) { result.error = error; return result; }
      ++result.accepted;
      continue;
    }
    ++result.control_records;
    if (!feedback(config, header, &result, &error)) { result.error = error; return result; }
    if (record.type == VcdoRecordType::kStopAck) {
      /* STOP_ACK is the logical completion marker, but successful helper
       * termination must also close stdout.  Do not accept trailing FATAL or
       * garbage records merely because they arrive after a valid STOP_ACK. */
      uint8_t trailing = 0;
      const ReadResult close_result = read_exact(fd, &trailing, 1, config.read_timeout_ms, &error);
      if (close_result == ReadResult::kEof) { result.saw_stop_ack = true; return result; }
      result.error = close_result == ReadResult::kTimeout
                         ? "codec stdout did not close after STOP_ACK"
                         : close_result == ReadResult::kOk
                               ? "codec stdout contained trailing bytes after STOP_ACK"
                               : error;
      return result;
    }
    if (record.type == VcdoRecordType::kFatal) { result.error = "codec helper reported FATAL"; return result; }
    /* INPUT_ERROR, DECODE_ERROR, FLUSHED and FLUSH_ACK are feedback/recovery
     * records, never video submissions. A later self-contained IDR may run. */
  }
}
}  // namespace

ReceiverResult receive_records(int fd, InputKind kind, Presenter* presenter, const ReceiverConfig& config) {
  ReceiverResult result;
  if (!presenter) { result.error = "presenter is null"; return result; }
  if (fd < 0) { result.error = "invalid input fd"; return result; }
  if (kind == InputKind::kCodecOutputFd) {
    if (config.max_records != 0) { result.error = "codec output forbids max_records truncation; require STOP_ACK"; return result; }
    if (config.codec_feedback_fd == fd) { result.error = "codec feedback fd must differ from codec output fd"; return result; }
    return receive_codec_records(fd, presenter, config);
  }
  if (config.codec_feedback_fd >= 0) { result.error = "codec feedback fd is valid only with codec output"; return result; }
  return receive_hdv1_records(fd, presenter, config);
}

int serve_tcp(uint16_t port, Presenter* presenter, const ReceiverConfig& config, std::string* error) {
  const int listener = socket(AF_INET, SOCK_STREAM, 0);
  if (listener < 0) { if (error) *error = std::string("socket: ") + std::strerror(errno); return -1; }
  int enable = 1;
  (void)setsockopt(listener, SOL_SOCKET, SO_REUSEADDR, &enable, sizeof(enable));
  sockaddr_in address{};
  address.sin_family = AF_INET;
  address.sin_addr.s_addr = htonl(INADDR_ANY);
  address.sin_port = htons(port);
  if (bind(listener, reinterpret_cast<sockaddr*>(&address), sizeof(address)) != 0 || listen(listener, 1) != 0) {
    if (error) *error = std::string("listen: ") + std::strerror(errno);
    close(listener);
    return -1;
  }
  int overall = 0;
  while (true) {
    const int client = accept(listener, nullptr, nullptr);
    if (client < 0) { if (errno == EINTR) continue; if (error) *error = std::string("accept: ") + std::strerror(errno); overall = -1; break; }
    ReceiverResult received = receive_records(client, InputKind::kHdv1Tcp, presenter, config);
    close(client);
    if (!received.error.empty()) { if (error) *error = received.error; overall = -1; break; }
    if (config.max_records != 0) break;
  }
  close(listener);
  return overall;
}
}  // namespace hdmi_presenter
