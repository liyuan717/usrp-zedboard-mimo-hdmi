#include "hdmi_presenter/receiver.h"

#include <array>
#include <cassert>
#include <cerrno>
#include <chrono>
#include <cstring>
#include <thread>
#include <unistd.h>
#include <vector>

using namespace hdmi_presenter;

namespace {
bool write_all(int fd, const uint8_t* bytes, size_t size) {
  size_t offset = 0;
  while (offset != size) {
    const ssize_t written = write(fd, bytes + offset, size - offset);
    if (written > 0) { offset += static_cast<size_t>(written); continue; }
    if (written < 0 && errno == EINTR) continue;
    return false;
  }
  return true;
}

bool read_all(int fd, uint8_t* bytes, size_t size) {
  size_t offset = 0;
  while (offset != size) {
    const ssize_t read_count = read(fd, bytes + offset, size - offset);
    if (read_count > 0) { offset += static_cast<size_t>(read_count); continue; }
    if (read_count < 0 && errno == EINTR) continue;
    return false;
  }
  return true;
}

std::array<uint8_t, kVcodecHeaderBytes> header(VcdoRecordType type,
                                                const FrameIdentity& id,
                                                uint64_t decode_token,
                                                uint32_t payload_crc = 0) {
  std::array<uint8_t, kVcodecHeaderBytes> raw{};
  std::memcpy(raw.data(), "VCDO", 4);
  store_le16(raw.data() + 4, 1);
  store_le16(raw.data() + 6, kVcodecHeaderBytes);
  store_le32(raw.data() + 8, static_cast<uint32_t>(type));
  if (type == VcdoRecordType::kFrame) {
    store_le64(raw.data() + 16, id.session_id);
    store_le64(raw.data() + 24, id.frame_id);
    store_le64(raw.data() + 32, decode_token);
    /* Frame VCDO source_pts is production millisecond input. */
    store_le64(raw.data() + 40, id.pts_ns / 1000000U);
    store_le32(raw.data() + 48, kWidth);
    store_le32(raw.data() + 52, kHeight);
    store_le32(raw.data() + 56, kRgb565Le);
    store_le32(raw.data() + 60, kRgb565Bytes);
    store_le32(raw.data() + 64, payload_crc);
  } else if (type == VcdoRecordType::kInputError ||
             type == VcdoRecordType::kDecodeError ||
             type == VcdoRecordType::kFlushed) {
    store_le32(raw.data() + 12, kVcodecNeedIdr);
    store_le64(raw.data() + 16, id.session_id);
    store_le64(raw.data() + 24, id.frame_id);
    store_le64(raw.data() + 32, decode_token);
    store_le64(raw.data() + 40, id.pts_ns);
  } else {
    store_le32(raw.data() + 12, kVcodecNeedIdr);
  }
  return raw;
}

void test_frame_controls_and_feedback() {
  int output[2]{};
  int feedback_pipe[2]{};
  assert(pipe(output) == 0);
  assert(pipe(feedback_pipe) == 0);
  std::vector<uint8_t> pixels(kRgb565Bytes, 0x5a);
  const FrameIdentity id{17, 31, 1234000000U, ieee_crc32(pixels.data(), pixels.size())};
  const auto frame = header(VcdoRecordType::kFrame, id, 101, id.payload_crc32);
  const auto input_error = header(VcdoRecordType::kInputError, {17, 32, 1235, 0}, 102);
  const auto flushed = header(VcdoRecordType::kFlushed, {17, 33, 1236, 0}, 103);
  const auto flush_ack = header(VcdoRecordType::kFlushAck, {}, 0);
  const auto stop_ack = header(VcdoRecordType::kStopAck, {}, 0);
  const std::array<std::array<uint8_t, kVcodecHeaderBytes>, 5> expected{
      frame, input_error, flushed, flush_ack, stop_ack};
  std::thread fake_helper([&] {
    assert(write_all(output[1], frame.data(), frame.size()));
    assert(write_all(output[1], pixels.data(), pixels.size()));
    assert(write_all(output[1], input_error.data(), input_error.size()));
    assert(write_all(output[1], flushed.data(), flushed.size()));
    assert(write_all(output[1], flush_ack.data(), flush_ack.size()));
    assert(write_all(output[1], stop_ack.data(), stop_ack.size()));
    close(output[1]);
  });

  Presenter presenter(make_null_backend());
  presenter.start();
  ReceiverConfig config{};
  config.codec_feedback_fd = feedback_pipe[1];
  config.feedback_timeout_ms = 1000;
  config.read_timeout_ms = 1000;
  config.queue_wait_ms = 1000;
  const ReceiverResult result = receive_records(output[0], InputKind::kCodecOutputFd,
                                                &presenter, config);
  close(output[0]);
  fake_helper.join();
  assert(result.error.empty());
  assert(result.accepted == 1 && result.control_records == 4);
  assert(result.feedback_records == expected.size() && result.saw_stop_ack);
  assert(presenter.drain(1000, nullptr));
  presenter.stop();

  close(feedback_pipe[1]);
  for (const auto& expected_header : expected) {
    std::array<uint8_t, kVcodecHeaderBytes> observed{};
    assert(read_all(feedback_pipe[0], observed.data(), observed.size()));
    assert(observed == expected_header);
  }
  close(feedback_pipe[0]);
}

void test_eof_before_stop_is_error() {
  int output[2]{};
  assert(pipe(output) == 0);
  close(output[1]);
  Presenter presenter(make_null_backend());
  presenter.start();
  const ReceiverResult result = receive_records(output[0], InputKind::kCodecOutputFd,
                                                &presenter, ReceiverConfig{});
  close(output[0]);
  assert(result.error == "codec stdout EOF before STOP_ACK");
  presenter.stop();
}

void test_codec_max_records_is_rejected() {
  int output[2]{};
  assert(pipe(output) == 0);
  ReceiverConfig config{};
  config.max_records = 1;
  Presenter presenter(make_null_backend());
  const ReceiverResult result = receive_records(output[0], InputKind::kCodecOutputFd,
                                                &presenter, config);
  close(output[0]);
  close(output[1]);
  assert(result.error.find("forbids max_records") != std::string::npos);
}

void test_fatal_after_frame_is_not_presented_as_success() {
  int output[2]{};
  int feedback_pipe[2]{};
  assert(pipe(output) == 0 && pipe(feedback_pipe) == 0);
  std::vector<uint8_t> pixels(kRgb565Bytes, 0x3c);
  const FrameIdentity id{22, 41, 2000000000U, ieee_crc32(pixels.data(), pixels.size())};
  const auto frame = header(VcdoRecordType::kFrame, id, 201, id.payload_crc32);
  const auto fatal = header(VcdoRecordType::kFatal, {}, 0);
  std::thread fake_helper([&] {
    assert(write_all(output[1], frame.data(), frame.size()));
    assert(write_all(output[1], pixels.data(), pixels.size()));
    assert(write_all(output[1], fatal.data(), fatal.size()));
    close(output[1]);
  });
  Presenter presenter(make_null_backend());
  presenter.start();
  ReceiverConfig config{};
  config.codec_feedback_fd = feedback_pipe[1];
  config.read_timeout_ms = 1000;
  const ReceiverResult result = receive_records(output[0], InputKind::kCodecOutputFd,
                                                &presenter, config);
  close(output[0]);
  fake_helper.join();
  assert(result.accepted == 1 && result.control_records == 1);
  assert(result.error == "codec helper reported FATAL" && !result.saw_stop_ack);
  close(feedback_pipe[1]);
  std::array<uint8_t, kVcodecHeaderBytes> received{};
  assert(read_all(feedback_pipe[0], received.data(), received.size()) && received == frame);
  assert(read_all(feedback_pipe[0], received.data(), received.size()) && received == fatal);
  close(feedback_pipe[0]);
  presenter.stop();
}

void test_stop_ack_requires_immediate_clean_eof() {
  int output[2]{};
  assert(pipe(output) == 0);
  const auto stop_ack = header(VcdoRecordType::kStopAck, {}, 0);
  assert(write_all(output[1], stop_ack.data(), stop_ack.size()));
  const uint8_t garbage = 0xa5;
  assert(write_all(output[1], &garbage, 1));
  close(output[1]);
  Presenter presenter(make_null_backend());
  presenter.start();
  ReceiverConfig config{};
  config.read_timeout_ms = 1000;
  const ReceiverResult result = receive_records(output[0], InputKind::kCodecOutputFd,
                                                &presenter, config);
  close(output[0]);
  assert(result.error == "codec stdout contained trailing bytes after STOP_ACK");
  assert(!result.saw_stop_ack);
  presenter.stop();
}
}  // namespace

int main() {
  test_frame_controls_and_feedback();
  test_eof_before_stop_is_error();
  test_codec_max_records_is_rejected();
  test_fatal_after_frame_is_not_presented_as_success();
  test_stop_ack_requires_immediate_clean_eof();
  return 0;
}
