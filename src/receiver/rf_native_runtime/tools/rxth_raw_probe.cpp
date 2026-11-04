// SPDX-License-Identifier: MIT
// Bounded RX8 RXTH long-run diagnostic.  This program only opens /dev/zed-rxth.
#include "zedboard_rx_transport.h"

#include <cerrno>
#include <charconv>
#include <chrono>
#include <csignal>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <fcntl.h>
#include <limits>
#include <string>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <sys/time.h>
#include <unistd.h>

namespace {
constexpr const char* kDevice = "/dev/zed-rxth";
constexpr uint32_t kMaxBlocks = 250000;
constexpr uint32_t kSampleRateHz = 4000000;
constexpr uint16_t kKnownFlags = 0x01ff;
constexpr uint16_t kValid = 1;
constexpr uint16_t kFlush = 1u << 3;
constexpr uint16_t kResetEpoch = 1u << 4;
constexpr uint16_t kInitialResetFlags = kValid | kResetEpoch;
constexpr uint16_t kStartupPrerollFlags = kValid | kFlush | kResetEpoch;
constexpr uint32_t kDeadlineMaxMs = 120000;
static_assert(ZED_RXTH_FORMAT_VERSION == 1, "probe contract is RXTH ABI v1");
static_assert(ZED_RXTH_HEADER_BYTES == 32, "probe contract is a 32-byte RXTH header");
static_assert(ZED_RXTH_SAMPLES_PER_BLOCK == 1024, "probe contract is 1024 samples/block");
static_assert(ZED_RXTH_BLOCK_BYTES == 8224, "probe contract is an 8224-byte RXTH block");

volatile sig_atomic_t g_deadline_expired = 0;

extern "C" void deadline_handler(int) { g_deadline_expired = 1; }

struct RingSnapshot {
  bool available = false;
  uint64_t capacity = 0;
  uint64_t halts = 0;
  uint64_t ready = 0;
  std::string reason;
};

struct Header {
  uint16_t flags;
  uint32_t epoch;
  uint32_t sequence;
  uint64_t first_sample;
  uint32_t sample_count;
};

struct Run {
  uint32_t requested = 0;
  uint32_t read_blocks = 0;
  uint32_t accepted_blocks = 0;
  uint64_t read_total_ns = 0;
  uint64_t read_max_ns = 0;
  uint64_t elapsed_ns = 0;
  uint32_t deadline_ms = 0;
  bool deadline_expired = false;
  bool have_first = false;
  bool have_startup = false;
  Header first{};
  Header startup{};
  Header last{};
  std::string error;
};

uint16_t le16(const uint8_t* p) { return static_cast<uint16_t>(p[0]) | (static_cast<uint16_t>(p[1]) << 8); }
uint32_t le32(const uint8_t* p) { return static_cast<uint32_t>(le16(p)) | (static_cast<uint32_t>(le16(p + 2)) << 16); }
uint64_t le64(const uint8_t* p) { return static_cast<uint64_t>(le32(p)) | (static_cast<uint64_t>(le32(p + 4)) << 32); }

uint32_t crc32(const uint8_t* bytes, size_t size) {
  uint32_t crc = 0xffffffffu;
  for (size_t i = 0; i < size; ++i) {
    crc ^= bytes[i];
    for (unsigned bit = 0; bit != 8; ++bit) crc = (crc >> 1) ^ (0xedb88320u & -(crc & 1u));
  }
  return crc ^ 0xffffffffu;
}

bool parse_header(const uint8_t* raw, Header* out, std::string* error) {
  if (std::memcmp(raw, "RXTH", 4) != 0 || raw[4] != ZED_RXTH_FORMAT_VERSION || raw[5] != ZED_RXTH_HEADER_BYTES) {
    *error = "RXTH header identity/version/size mismatch";
    return false;
  }
  if (crc32(raw, 28) != le32(raw + 28)) { *error = "RXTH header CRC mismatch"; return false; }
  out->flags = le16(raw + 6);
  out->epoch = le32(raw + 8);
  out->sequence = le32(raw + 12);
  out->first_sample = le64(raw + 16);
  out->sample_count = le32(raw + 24);
  if ((out->flags & ~kKnownFlags) || !(out->flags & kValid) ||
      out->sample_count != ZED_RXTH_SAMPLES_PER_BLOCK ||
      out->first_sample > std::numeric_limits<uint64_t>::max() - out->sample_count) {
    *error = "RXTH header flags or sample interval invalid";
    return false;
  }
  for (size_t offset = ZED_RXTH_HEADER_BYTES; offset < ZED_RXTH_BLOCK_BYTES; offset += 2) {
    if (raw[offset] & 0x0f) { *error = "RXTH payload violates signed12-MSB alignment"; return false; }
  }
  return true;
}

enum class BlockClass { kClean, kStartupPreroll, kInvalid };

// The one allowed non-clean block is a deterministic reset boundary emitted
// before the first real capture block after the transport was disabled.  This
// is deliberately not a generic "ignore fault flags" switch: it cannot be
// used after an accepted block or more than once.
BlockClass classify_block(const Header& value, uint32_t startup_preroll_limit,
                          uint32_t observed_startup_preroll, uint32_t accepted_blocks) {
  if (value.flags == kValid) return BlockClass::kClean;
  // Preserve the v1 probe's existing first-block contract: reset-only is a
  // valid initial clean header. It must never reappear after a startup
  // preroll or accepted data, where it would denote an unexpected epoch cut.
  if (value.flags == kInitialResetFlags && observed_startup_preroll == 0 && accepted_blocks == 0)
    return BlockClass::kClean;
  if (startup_preroll_limit == 1 && observed_startup_preroll == 0 && accepted_blocks == 0 &&
      value.flags == kStartupPrerollFlags && value.sequence == 0 && value.epoch != 0)
    return BlockClass::kStartupPreroll;
  return BlockClass::kInvalid;
}

bool check_continuity(const Header& prior, const Header& current, std::string* error) {
  if (current.epoch != prior.epoch || (current.flags & kResetEpoch) ||
      current.sequence != prior.sequence + 1u || current.first_sample != prior.first_sample + ZED_RXTH_SAMPLES_PER_BLOCK) {
    *error = "RXTH epoch/sequence/first-sample continuity mismatch";
    return false;
  }
  return true;
}

bool read_text(const std::string& path, std::string* out) {
  FILE* stream = std::fopen(path.c_str(), "re");
  if (!stream) return false;
  char buffer[256]{};
  const bool got = std::fgets(buffer, sizeof(buffer), stream) != nullptr;
  const int close_rc = std::fclose(stream);
  const bool ok = got && close_rc == 0;
  if (!ok) return false;
  *out = buffer;
  while (!out->empty() && (out->back() == '\n' || out->back() == '\r')) out->pop_back();
  return true;
}

bool parse_u64(const std::string& text, uint64_t* value) {
  const char* begin = text.data();
  const char* end = begin + text.size();
  const auto parsed = std::from_chars(begin, end, *value, 10);
  return parsed.ec == std::errc{} && parsed.ptr == end;
}

RingSnapshot ring_snapshot() {
  RingSnapshot result;
  char target[4096]{};
  const char* link = "/sys/class/misc/zed-rxth/device";
  if (!realpath(link, target)) { result.reason = "RXTH ring sysfs unavailable"; return result; }
  const std::string base(target);
  std::string compatible;
  if (!read_text(base + "/of_node/compatible", &compatible) || compatible != "mimo,rf-rx-transport-v1") {
    result.reason = "RXTH provider compatible mismatch";
    return result;
  }
  std::string capacity, halts, ready;
  if (!read_text(base + "/ring_capacity", &capacity) || !read_text(base + "/ring_halts", &halts) ||
      !read_text(base + "/ready_blocks", &ready) || !parse_u64(capacity, &result.capacity) ||
      !parse_u64(halts, &result.halts) || !parse_u64(ready, &result.ready) || result.capacity == 0) {
    result.reason = "RXTH ring counters unavailable or invalid";
    return result;
  }
  result.available = true;
  return result;
}

bool write_fresh(const std::string& directory, const char* name, const uint8_t* bytes, size_t length, std::string* error) {
  const std::string path = directory + "/" + name;
  const int fd = open(path.c_str(), O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW, 0600);
  if (fd < 0) { *error = "sample output create failed"; return false; }
  const ssize_t wrote = write(fd, bytes, length);
  const int sync_rc = fsync(fd);
  const int close_rc = close(fd);
  const bool ok = wrote == static_cast<ssize_t>(length) && sync_rc == 0 && close_rc == 0;
  if (!ok) { *error = "sample output write failed"; return false; }
  return true;
}

void json_header(FILE* out, const Header& value) {
  std::fprintf(out, "{\"epoch\":%u,\"sequence\":%u,\"first_adc_sample\":%llu,\"sample_count\":%u,\"flags\":%u}",
              value.epoch, value.sequence, static_cast<unsigned long long>(value.first_sample), value.sample_count, value.flags);
}

void json_status(FILE* out, const zed_rxth_status& value) {
  std::fprintf(out, "{\"submitted_blocks\":%llu,\"completed_blocks\":%llu,\"aborted_blocks\":%llu,\"timeout_blocks\":%llu,\"delivery_failures\":%llu,\"last_error\":%d,\"state\":%u}",
              static_cast<unsigned long long>(value.submitted_blocks), static_cast<unsigned long long>(value.completed_blocks),
              static_cast<unsigned long long>(value.aborted_blocks), static_cast<unsigned long long>(value.timeout_blocks),
              static_cast<unsigned long long>(value.delivery_failures), value.last_error, value.state);
}

void json_ring(FILE* out, const RingSnapshot& value) {
  if (!value.available) { std::fprintf(out, "{\"available\":false,\"reason\":\"%s\"}", value.reason.c_str()); return; }
  std::fprintf(out, "{\"available\":true,\"ring_capacity\":%llu,\"ring_halts\":%llu,\"ready_blocks\":%llu}",
              static_cast<unsigned long long>(value.capacity), static_cast<unsigned long long>(value.halts), static_cast<unsigned long long>(value.ready));
}

bool read_status(int fd, zed_rxth_status* status) { return ioctl(fd, ZED_RXTH_IOC_GET_STATUS, status) == 0 && status->reserved == 0; }

bool status_accepts_capture(const zed_rxth_status& before, const zed_rxth_status& after, uint32_t blocks, std::string* note, std::string* error) {
  if (after.completed_blocks < before.completed_blocks || after.completed_blocks - before.completed_blocks < blocks || after.aborted_blocks != before.aborted_blocks ||
      after.timeout_blocks != before.timeout_blocks || after.delivery_failures != before.delivery_failures ||
      (after.state != ZED_RXTH_STATE_IDLE && after.state != ZED_RXTH_STATE_ACTIVE)) {
    *error = "RXTH status counter/state fault during capture";
    return false;
  }
  if (after.last_error == 0) { *note = "no_error"; return true; }
  if (before.last_error == -ECANCELED && after.last_error == -ECANCELED) {
    *note = "historical_clean_close_ecanceled";
    return true;
  }
  *error = "RXTH unexpected last_error";
  return false;
}

bool parse_number(const char* text, uint32_t* out) {
  const char* end = text + std::strlen(text);
  const auto parsed = std::from_chars(text, end, *out, 10);
  return parsed.ec == std::errc{} && parsed.ptr == end;
}

void usage() { std::fprintf(stderr, "usage: rxth_raw_probe --blocks N --timeout-ms N --deadline-ms N --report /tmp/file.json [--startup-preroll-blocks 0|1] [--sample-dir /tmp/newdir]\n"); }
}  // namespace

#ifndef RXTH_RAW_PROBE_UNIT_TEST
int main(int argc, char** argv) {
  uint32_t blocks = 0, timeout_ms = 0, deadline_ms = 0, startup_preroll_blocks = 0;
  std::string report_path, sample_dir;
  for (int i = 1; i < argc; ++i) {
    if (std::strcmp(argv[i], "--blocks") == 0 && ++i < argc) { if (!parse_number(argv[i], &blocks)) { usage(); return 2; } }
    else if (std::strcmp(argv[i], "--timeout-ms") == 0 && ++i < argc) { if (!parse_number(argv[i], &timeout_ms)) { usage(); return 2; } }
    else if (std::strcmp(argv[i], "--deadline-ms") == 0 && ++i < argc) { if (!parse_number(argv[i], &deadline_ms)) { usage(); return 2; } }
    else if (std::strcmp(argv[i], "--startup-preroll-blocks") == 0 && ++i < argc) { if (!parse_number(argv[i], &startup_preroll_blocks)) { usage(); return 2; } }
    else if (std::strcmp(argv[i], "--report") == 0 && ++i < argc) report_path = argv[i];
    else if (std::strcmp(argv[i], "--sample-dir") == 0 && ++i < argc) sample_dir = argv[i];
    else { usage(); return 2; }
  }
  if (blocks == 0 || blocks > kMaxBlocks || timeout_ms == 0 || timeout_ms > ZED_RXTH_TIMEOUT_MAX_MS || deadline_ms == 0 || deadline_ms > kDeadlineMaxMs || startup_preroll_blocks > 1 || report_path.rfind("/tmp/", 0) != 0 ||
      (!sample_dir.empty() && sample_dir.rfind("/tmp/", 0) != 0)) { usage(); return 2; }
  if (!sample_dir.empty() && mkdir(sample_dir.c_str(), 0700) != 0) { std::fprintf(stderr, "sample directory must be fresh\n"); return 2; }

  Run run; run.requested = blocks; run.deadline_ms = deadline_ms;
  zed_rxth_status before{}, after{}, before_close{}, after_abort{};
  RingSnapshot ring_before, ring_after;
  uint8_t raw[ZED_RXTH_BLOCK_BYTES]{};
  uint8_t startup_raw[ZED_RXTH_BLOCK_BYTES]{};
  uint8_t first_raw[ZED_RXTH_BLOCK_BYTES]{};
  uint8_t last_raw[ZED_RXTH_BLOCK_BYTES]{};
  bool have_failure_raw = false, have_startup_raw = false, have_first_raw = false, have_last_raw = false, cleanup_abort_ok = false, close_ok = false;
  bool before_status_ok = false, after_capture_status_ok = false, after_abort_status_ok = false, before_close_status_ok = false;
  bool deadline_timer_ok = false, deadline_handler_installed = false;
  std::string interpretation = "not_started";
  int fd = open(kDevice, O_RDWR | O_CLOEXEC);
  if (fd < 0) run.error = "open /dev/zed-rxth failed";
  zed_rxth_timeout timeout{timeout_ms, 0};
  if (fd >= 0 && ioctl(fd, ZED_RXTH_IOC_SET_TIMEOUT, &timeout) != 0) run.error = "RXTH set timeout failed";
  if (fd >= 0 && run.error.empty()) before_status_ok = read_status(fd, &before);
  if (fd >= 0 && run.error.empty() && !before_status_ok) run.error = "RXTH GET_STATUS before failed";
  if (fd >= 0 && run.error.empty()) ring_before = ring_snapshot();
  struct sigaction new_action{};
  struct sigaction old_action{};
  new_action.sa_handler = deadline_handler;
  sigemptyset(&new_action.sa_mask);
  if (fd >= 0 && run.error.empty()) {
    if (sigaction(SIGALRM, &new_action, &old_action) != 0) run.error = "deadline signal setup failed";
    else deadline_handler_installed = true;
  }
  if (fd >= 0 && run.error.empty()) {
    const itimerval timer{{0, 0}, {static_cast<time_t>(deadline_ms / 1000), static_cast<suseconds_t>((deadline_ms % 1000) * 1000)}};
    g_deadline_expired = 0;
    if (setitimer(ITIMER_REAL, &timer, nullptr) != 0) run.error = "deadline timer setup failed";
    else deadline_timer_ok = true;
  }
  const auto started = std::chrono::steady_clock::now();
  Header previous{};
  bool have_previous = false;
  const uint32_t max_read_blocks = blocks + startup_preroll_blocks;
  for (uint32_t index = 0; fd >= 0 && run.error.empty() && run.accepted_blocks < blocks && index < max_read_blocks; ++index) {
    const auto read_started = std::chrono::steady_clock::now();
    const ssize_t got = read(fd, raw, sizeof(raw));
    const uint64_t read_ns = static_cast<uint64_t>(std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now() - read_started).count());
    ++run.read_blocks; run.read_total_ns += read_ns; if (read_ns > run.read_max_ns) run.read_max_ns = read_ns;
    if (g_deadline_expired) { run.deadline_expired = true; run.error = "RXTH wall deadline expired"; break; }
    if (got != static_cast<ssize_t>(sizeof(raw))) { run.error = got < 0 ? "RXTH read failed" : "RXTH short read"; break; }
    have_failure_raw = true;
    Header parsed{};
    if (!parse_header(raw, &parsed, &run.error)) break;
    const BlockClass classification = classify_block(parsed, startup_preroll_blocks,
                                                     run.have_startup ? 1u : 0u, run.accepted_blocks);
    if (classification == BlockClass::kInvalid) { run.error = "RXTH non-clean block rejected"; break; }
    if (have_previous && !check_continuity(previous, parsed, &run.error)) break;
    if (classification == BlockClass::kStartupPreroll) {
      std::memcpy(startup_raw, raw, sizeof(raw)); have_startup_raw = true;
      run.startup = parsed; run.have_startup = true;
      previous = parsed; have_previous = true;
      continue;
    }
    if (!have_first_raw) { std::memcpy(first_raw, raw, sizeof(raw)); have_first_raw = true; run.first = parsed; run.have_first = true; }
    std::memcpy(last_raw, raw, sizeof(raw)); have_last_raw = true; run.last = parsed;
    previous = parsed; have_previous = true; ++run.accepted_blocks;
  }
  run.elapsed_ns = static_cast<uint64_t>(std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now() - started).count());
  if (g_deadline_expired && run.error.empty()) { run.deadline_expired = true; run.error = "RXTH wall deadline expired"; }
  if (deadline_timer_ok) {
    const itimerval disabled{};
    setitimer(ITIMER_REAL, &disabled, nullptr);
    sigaction(SIGALRM, &old_action, nullptr);
    deadline_timer_ok = false;
    deadline_handler_installed = false;
  } else if (deadline_handler_installed) {
    sigaction(SIGALRM, &old_action, nullptr);
    deadline_handler_installed = false;
  }
  if (fd >= 0) after_capture_status_ok = read_status(fd, &after);
  if (fd >= 0 && run.error.empty() && !after_capture_status_ok) run.error = "RXTH GET_STATUS after failed";
  if (fd >= 0) {
    ring_after = ring_snapshot();
    if (run.error.empty() && ring_before.available && ring_after.available && ring_after.halts != ring_before.halts) run.error = "RXTH ring_halts changed during capture";
    if (run.error.empty() && after_capture_status_ok && !status_accepts_capture(before, after, run.read_blocks, &interpretation, &run.error)) {}
  }
  // Stop the real RXTH DMA owner before any optional filesystem work.  A
  // slow /tmp write must never extend a live capture or delay cancellation.
  if (fd >= 0) {
    cleanup_abort_ok = ioctl(fd, ZED_RXTH_IOC_ABORT) == 0;
    after_abort_status_ok = read_status(fd, &after_abort);
    before_close_status_ok = read_status(fd, &before_close);
    close_ok = close(fd) == 0;
  }
  const bool cleanup_ok = cleanup_abort_ok && after_abort_status_ok && before_close_status_ok && close_ok;
  // Do not replace a capture failure with a later artifact failure, and do
  // not make a failed ABORT/close look like a sample-output error.  The
  // report carries both independent result channels.
  std::string artifact_error;
  if (!sample_dir.empty()) {
    std::string detail;
    if (have_startup_raw && !write_fresh(sample_dir, "startup.rxth", startup_raw, sizeof(startup_raw), &detail)) artifact_error = detail;
    if (have_first_raw && !write_fresh(sample_dir, "first.rxth", first_raw, sizeof(first_raw), &detail)) artifact_error = detail;
    if (have_last_raw && !write_fresh(sample_dir, "last.rxth", last_raw, sizeof(last_raw), &detail) && artifact_error.empty()) artifact_error = detail;
    if (!run.error.empty() && have_failure_raw && !write_fresh(sample_dir, "failure.rxth", raw, sizeof(raw), &detail) && artifact_error.empty()) artifact_error = detail;
  }
  const bool ok = run.error.empty() && artifact_error.empty() && run.accepted_blocks == blocks && after_capture_status_ok && cleanup_ok;
  const int report_fd = open(report_path.c_str(), O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW, 0600);
  if (report_fd < 0) { std::fprintf(stderr, "report path must be fresh under /tmp\n"); return 2; }
  FILE* report = fdopen(report_fd, "w");
  if (!report) { close(report_fd); return 2; }
  std::fprintf(report, "{\"schema\":\"rx8-rxth-raw-probe-cpp-v1\",\"ok\":%s,\"error\":\"%s\",\"requested_blocks\":%u,\"read_blocks\":%u,\"accepted_blocks\":%u,\"startup_preroll\":{\"requested_blocks\":%u,\"observed_blocks\":%u},\"payload_hash\":\"not_computed; header CRC and signed12 alignment only\",\"timing\":{\"capture_elapsed_ns\":%llu,\"wall_deadline_ms\":%u,\"deadline_expired\":%s,\"read_calls\":%u,\"read_total_ns\":%llu,\"read_max_ns\":%llu,\"sample_rate_hz_assumed\":%u,\"expected_sample_duration_ns\":%llu,\"interpretation\":\"wall-clock includes driver wait and block boundaries; not standalone 4MSPS proof\"},\"status_read_ok\":",
               ok ? "true" : "false", run.error.c_str(), run.requested, run.read_blocks, run.accepted_blocks, startup_preroll_blocks, run.have_startup ? 1u : 0u,
               static_cast<unsigned long long>(run.elapsed_ns), run.deadline_ms, run.deadline_expired ? "true" : "false", run.read_blocks, static_cast<unsigned long long>(run.read_total_ns), static_cast<unsigned long long>(run.read_max_ns), kSampleRateHz,
               static_cast<unsigned long long>(static_cast<uint64_t>(blocks) * ZED_RXTH_SAMPLES_PER_BLOCK * 1000000000ull / kSampleRateHz));
  std::fputs("{\"before\":", report); std::fputs(before_status_ok ? "true" : "false", report); std::fputs(",\"after_capture\":", report); std::fputs(after_capture_status_ok ? "true" : "false", report);
  std::fputs(",\"after_abort\":", report); std::fputs(after_abort_status_ok ? "true" : "false", report); std::fputs(",\"before_close\":", report); std::fputs(before_close_status_ok ? "true" : "false", report);
  std::fputs("},\"status_before\":", report); json_status(report, before); std::fputs(",\"status_after_capture\":", report); json_status(report, after);
  std::fputs(",\"status_after_abort\":", report); json_status(report, after_abort);
  std::fputs(",\"status_before_close\":", report); json_status(report, before_close);
  std::fputs(",\"ring_before\":", report); json_ring(report, ring_before);
  std::fputs(",\"ring_after_capture\":", report); json_ring(report, ring_after);
  std::fprintf(report, ",\"last_error_interpretation\":\"%s\",\"cleanup_ok\":%s,\"cleanup_abort_ok\":%s,\"close_ok\":%s,\"artifact_error\":\"%s\"",
               interpretation.c_str(), cleanup_ok ? "true" : "false", cleanup_abort_ok ? "true" : "false", close_ok ? "true" : "false", artifact_error.c_str());
  if (run.have_first) { std::fputs(",\"first\":", report); json_header(report, run.first); }
  if (run.have_startup) { std::fputs(",\"startup\":", report); json_header(report, run.startup); }
  if (run.accepted_blocks) { std::fputs(",\"last\":", report); json_header(report, run.last); }
  std::fputs("}\n", report); const bool report_ok = std::fflush(report) == 0 && fsync(report_fd) == 0 && std::fclose(report) == 0;
  return ok && report_ok ? 0 : 1;
}
#endif  // RXTH_RAW_PROBE_UNIT_TEST
