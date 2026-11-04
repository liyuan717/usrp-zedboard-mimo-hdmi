// Deterministic process-local scheduling tests for mrf1_native_rx_backend.
//
// This file intentionally includes the runtime implementation so it can test
// its anonymous-namespace queues/stages.  Every device, frontend and IMBC
// symbol below is a FAKE test double: this does not exercise RF, DMA or PL.

#include <algorithm>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdarg>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <deque>
#include <iostream>
#include <mutex>
#include <sstream>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>
#include <filesystem>

#include <fcntl.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>

#include "mrf1_native_frontend.h"
#include "rf_native_transport.h"
#include "zedboard_interleaved_candidate.h"
#include "zedboard_rx_transport.h"

extern "C" int __real_close(int);
extern "C" int __real_open(const char*,int,...);
extern "C" ssize_t __real_write(int,const void*,size_t);

#ifndef MRF1_RUNTIME_TEST_TIMEOUT_SCALE
#define MRF1_RUNTIME_TEST_TIMEOUT_SCALE 1
#endif
#if MRF1_RUNTIME_TEST_TIMEOUT_SCALE < 1
#error "MRF1_RUNTIME_TEST_TIMEOUT_SCALE must be positive"
#endif

#define main native_runtime_program_main_for_test
#include "../src/mrf1_native_rx_backend.cpp"
#undef main

namespace {
constexpr int kFakeRxFd = 71;
constexpr int kFakeOutputFd = 72;
constexpr uint32_t kBuildId = 0x7a31b442U;
constexpr uint32_t kTestTimeoutScale = MRF1_RUNTIME_TEST_TIMEOUT_SCALE;

std::chrono::milliseconds test_wait_ms(uint32_t base_ms) {
  return std::chrono::milliseconds(static_cast<uint64_t>(base_ms) * kTestTimeoutScale);
}

uint32_t test_run_deadline_ms(uint32_t base_ms) {
  return static_cast<uint32_t>(static_cast<uint64_t>(base_ms) * kTestTimeoutScale);
}

struct FakeState {
  std::mutex mu;
  std::condition_variable rx_cv;
  std::vector<mrf1_nf_status> frontend_steps;
  std::vector<rnt_code> submit_steps;
  std::vector<uint32_t> submitted_frame_ids;
  std::vector<uint8_t> output;
  uint32_t available_blocks{0};
  uint32_t read_block{0};
  std::size_t read_offset{0};
  unsigned read_chunk{997};
  bool eof{false};
  bool block_read{false};
  bool rx_aborted{false};
  bool fail_delivery_write{false};
  bool throw_frontend{false};
  bool fail_frontend_create{false};
  bool fail_rnt_open{false};
  bool block_first_submit{false};
  bool first_submit_entered{false};
  bool overlap_observed{false};
  unsigned wire_corruption{0};
  unsigned rx_header_case{0};
  bool coherent_adc_tone{false};
  unsigned tone_start_block{0};
  double created_coarse_prior{0};
  unsigned wrong_owner_close{0};
  bool require_closed_before_frontend{false};
  bool frontend_before_capture_close{false};
  std::vector<uint32_t> fed_sequences;
  std::thread::id capture_open_thread{};
  std::thread::id rnt_open_thread{};
  std::condition_variable submit_cv;
  uint32_t rx_info_flags{ZED_RXTH_INFO_DMAENGINE_OWNER |
                         ZED_RXTH_INFO_EXCLUSIVE_OPEN |
                         ZED_RXTH_INFO_FIXED_BLOCK |
                         ZED_RXTH_INFO_NO_MMAP |
                         ZED_RXTH_INFO_STICKY_IDLE_ABORT};
  uint32_t imbc_info_flags{ZED_INTERLEAVED_CANDIDATE_INFO_BATCH_ENVELOPE |
                           ZED_INTERLEAVED_CANDIDATE_INFO_STICKY_IDLE_ABORT};
  unsigned open_rx{0};
  unsigned open_output{0};
  unsigned close_rx{0};
  unsigned close_output{0};
  unsigned rx_abort_calls{0};
  unsigned frontend_create{0};
  unsigned frontend_destroy{0};
  unsigned frontend_feed{0};
  unsigned rnt_open{0};
  unsigned rnt_abort{0};
  unsigned rnt_destroy{0};
  unsigned rnt_submit{0};
  unsigned output_writes{0};

  void reset() {
    std::lock_guard<std::mutex> lock(mu);
    frontend_steps.clear(); submit_steps.clear(); submitted_frame_ids.clear(); output.clear();
    available_blocks = 0; read_block = 0; read_offset = 0; read_chunk = 997;
    eof = false; block_read = false; rx_aborted = false; fail_delivery_write = false;
    throw_frontend = false; fail_frontend_create = false; fail_rnt_open = false;
    block_first_submit = false; first_submit_entered = false; overlap_observed = false;
    wire_corruption = 0; wrong_owner_close = 0; capture_open_thread = {}; rnt_open_thread = {};
    tone_start_block = 0; rx_header_case = 0; coherent_adc_tone = false; created_coarse_prior = 0;
    require_closed_before_frontend=false;frontend_before_capture_close=false;fed_sequences.clear();
    rx_info_flags = ZED_RXTH_INFO_DMAENGINE_OWNER | ZED_RXTH_INFO_EXCLUSIVE_OPEN |
                    ZED_RXTH_INFO_FIXED_BLOCK | ZED_RXTH_INFO_NO_MMAP |
                    ZED_RXTH_INFO_STICKY_IDLE_ABORT;
    imbc_info_flags = ZED_INTERLEAVED_CANDIDATE_INFO_BATCH_ENVELOPE |
                      ZED_INTERLEAVED_CANDIDATE_INFO_STICKY_IDLE_ABORT;
    open_rx = open_output = close_rx = close_output = rx_abort_calls = 0;
    frontend_create = frontend_destroy = frontend_feed = 0;
    rnt_open = rnt_abort = rnt_destroy = rnt_submit = output_writes = 0;
  }
};

FakeState g_fake;

uint32_t read_le32(const uint8_t *p) {
  return static_cast<uint32_t>(p[0]) | (static_cast<uint32_t>(p[1]) << 8U) |
         (static_cast<uint32_t>(p[2]) << 16U) | (static_cast<uint32_t>(p[3]) << 24U);
}

uint32_t record_crc(const uint8_t *header, const uint8_t *payload, std::size_t n) {
  uint32_t crc = crc32_update(0xffffffffU, header, 16);
  return ~crc32_update(crc, payload, n);
}

bool expect(bool value, const char *message) {
  if (!value) std::cerr << "FAIL: " << message << "\n";
  return value;
}

bool resources_closed(const char *what) {
  std::lock_guard<std::mutex> lock(g_fake.mu);
  return expect(g_fake.open_rx == 1 && g_fake.close_rx == 1, what) &&
         expect(g_fake.open_output == 1 && g_fake.close_output == 1, "output owner closed once") &&
         expect(g_fake.frontend_create == 1 && g_fake.frontend_destroy == 1, "frontend lifetime balanced") &&
         expect(g_fake.rnt_open == 1 && g_fake.rnt_destroy == 1, "transport lifetime balanced") &&
         expect(g_fake.wrong_owner_close == 0, "capture close and transport destroy stay on owner threads");
}

bool socket_resources_closed(const char *what) {
  std::lock_guard<std::mutex> lock(g_fake.mu);
  return expect(g_fake.open_rx == 1 && g_fake.close_rx == 1, what) &&
         expect(g_fake.open_output == 0 && g_fake.close_output == 0, "socket mode does not open a diagnostic file") &&
         expect(g_fake.frontend_create == 1 && g_fake.frontend_destroy == 1, "socket frontend lifetime balanced") &&
         expect(g_fake.rnt_open == 1 && g_fake.rnt_destroy == 1, "socket transport lifetime balanced");
}

bool test_bounded_queue_deadline_and_close() {
  std::atomic_bool stop{false};
  BoundedQueue<int> queue(1);
  const auto now = std::chrono::steady_clock::now();
  if (!expect(queue.push(1, now + test_wait_ms(30), stop), "seed bounded queue")) return false;
  const auto started = std::chrono::steady_clock::now();
  if (!expect(!queue.push(2, started + test_wait_ms(35), stop), "full queue has deadline")) return false;
  if (!expect(std::chrono::steady_clock::now() - started >= test_wait_ms(20), "full queue actually waited")) return false;
  std::atomic_bool returned{false};
  std::thread waiter([&] { returned.store(!queue.push(3, std::chrono::steady_clock::now() + test_wait_ms(1000), stop)); });
  std::this_thread::sleep_for(test_wait_ms(10));
  queue.close();
  waiter.join();
  if (!expect(returned.load(), "close wakes backpressured producer")) return false;

  // Fixed ring: fill, wrap the producer index, close, then drain only the
  // already-enqueued values in FIFO order. No deque node allocation is used.
  BoundedQueue<int> ring(2);
  const auto deadline = std::chrono::steady_clock::now() + test_wait_ms(100);
  if (!expect(ring.push(1, deadline, stop) && ring.push(2, deadline, stop), "fill fixed ring")) return false;
  if (!expect(ring.high_watermark() == 2, "fixed ring records full high watermark")) return false;
  int value = 0;
  if (!expect(ring.pop(&value, deadline, stop) && value == 1, "fixed ring first FIFO value")) return false;
  if (!expect(ring.push(3, deadline, stop), "fixed ring wraps producer index")) return false;
  ring.close();
  if (!expect(ring.pop(&value, deadline, stop) && value == 2, "closed ring drains pre-close second value")) return false;
  if (!expect(ring.pop(&value, deadline, stop) && value == 3, "closed ring drains wrapped value")) return false;
  if (!expect(!ring.pop(&value, deadline, stop), "closed drained ring has no extra value")) return false;

  BoundedQueue<int> stopped(1);
  if (!expect(stopped.push(7, deadline, stop), "seed stop queue")) return false;
  stop.store(true); value = -1;
  return expect(!stopped.pop(&value, deadline, stop) && value == -1, "stop prevents dequeue of pending ring value");
}

bool test_normal_partial_rx_to_complete_delivery() {
  g_fake.reset();
  { std::lock_guard<std::mutex> lock(g_fake.mu);
    g_fake.available_blocks = 3;
    g_fake.frontend_steps = {MRF1_NF_NO_FRAME, MRF1_NF_OK, MRF1_NF_NO_FRAME};
    g_fake.submit_steps = {RNT_OK};
  }
  const int rc = run(3, kBuildId, test_run_deadline_ms(500), "fake-normal");
  if (!expect(rc == 0, "valid frame run succeeds")) return false;
  bool records_ok = true;
  {
    std::lock_guard<std::mutex> lock(g_fake.mu);
    records_ok = expect(g_fake.rnt_submit == 1 && g_fake.submitted_frame_ids.size() == 1, "one non-duplicated submit") &&
                 expect(g_fake.output.size() == kDeliveryHeaderBytes + RNT_PACKETS_BYTES, "complete record written despite partial writes");
    if (records_ok) {
      const uint8_t *h = g_fake.output.data();
      records_ok = expect(std::memcmp(h, "RFD1", 4) == 0 && read_le32(h + 4) == 1001U &&
                          read_le32(h + 8) == kBuildId && read_le32(h + 12) == RNT_PACKETS_BYTES, "valid delivery header") &&
                   expect(read_le32(h + 16) == record_crc(h, h + kDeliveryHeaderBytes, RNT_PACKETS_BYTES), "valid delivery record CRC");
    }
  }
  return records_ok && resources_closed("capture owner closed after normal run");
}

bool bind_unix_dgram(const std::string& path, int* fd) {
  *fd = ::socket(AF_UNIX, SOCK_DGRAM | SOCK_CLOEXEC, 0);
  if (*fd < 0) return false;
  sockaddr_un address{};
  address.sun_family = AF_UNIX;
  if (path.size() >= sizeof(address.sun_path)) { (void)__real_close(*fd); return false; }
  std::memcpy(address.sun_path, path.data(), path.size());
  address.sun_path[path.size()] = '\0';
  const socklen_t len = static_cast<socklen_t>(offsetof(sockaddr_un, sun_path) + path.size() + 1U);
  if (::bind(*fd, reinterpret_cast<const sockaddr*>(&address), len) != 0) { (void)__real_close(*fd); return false; }
  return true;
}

bool is_random_epoch_sender(const sockaddr_un& peer, socklen_t length) {
  constexpr char kPrefix[] = "mrf1-";
  constexpr char kHex[] = "0123456789abcdef";
  const std::size_t expected = 1U + sizeof(kPrefix) - 1U + 32U;
  if (length != static_cast<socklen_t>(offsetof(sockaddr_un, sun_path) + expected) ||
      peer.sun_path[0] != '\0' || std::memcmp(peer.sun_path + 1, kPrefix, sizeof(kPrefix) - 1U) != 0) return false;
  for (std::size_t index = 1U + sizeof(kPrefix) - 1U; index != expected; ++index) {
    if (std::strchr(kHex, peer.sun_path[index]) == nullptr) return false;
  }
  return true;
}

bool fill_unix_dgram_receiver_until_eagain(const std::string& path, std::vector<int>* filler_fds,
                                           unsigned* records_sent, int* socket_send_buffer_bytes) {
  constexpr unsigned kMaximumFillers = 32;
  constexpr unsigned kMaximumRecords = 8192;
  filler_fds->clear(); *records_sent = 0; *socket_send_buffer_bytes = 0;
  const auto close_fillers = [&]() {
    for (std::size_t index = 0; index < filler_fds->size(); ++index) {
      (void)__real_close((*filler_fds)[index]);
      (void)::unlink((path + ".filler." + std::to_string(index)).c_str());
    }
    filler_fds->clear();
  };
  sockaddr_un destination{};
  destination.sun_family = AF_UNIX;
  if (path.size() >= sizeof(destination.sun_path)) return false;
  std::memcpy(destination.sun_path, path.data(), path.size());
  destination.sun_path[path.size()] = '\0';
  const socklen_t length = static_cast<socklen_t>(offsetof(sockaddr_un, sun_path) + path.size() + 1U);
  const uint8_t filler_byte = 0xa5U;
  for (unsigned source_index = 0; source_index != kMaximumFillers && *records_sent < kMaximumRecords; ++source_index) {
    const int filler = ::socket(AF_UNIX, SOCK_DGRAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
    if (filler < 0) { close_fillers(); return false; }
    const std::string filler_path = path + ".filler." + std::to_string(source_index);
    if (filler_path.size() >= sizeof(destination.sun_path)) { (void)__real_close(filler); close_fillers(); return false; }
    sockaddr_un source{}; source.sun_family = AF_UNIX;
    std::memcpy(source.sun_path, filler_path.data(), filler_path.size()); source.sun_path[filler_path.size()] = '\0';
    (void)::unlink(filler_path.c_str());
    const socklen_t source_length = static_cast<socklen_t>(offsetof(sockaddr_un, sun_path) + filler_path.size() + 1U);
    if (::bind(filler, reinterpret_cast<const sockaddr*>(&source), source_length) != 0 ||
        ::connect(filler, reinterpret_cast<const sockaddr*>(&destination), length) != 0) {
      (void)__real_close(filler); (void)::unlink(filler_path.c_str()); close_fillers(); return false;
    }
    int send_buffer = 0; socklen_t send_buffer_length = sizeof(send_buffer);
    if (::getsockopt(filler, SOL_SOCKET, SO_SNDBUF, &send_buffer, &send_buffer_length) != 0) {
      (void)__real_close(filler); (void)::unlink(filler_path.c_str()); close_fillers(); return false;
    }
    *socket_send_buffer_bytes = send_buffer;
    const ssize_t first = ::send(filler, &filler_byte, sizeof(filler_byte), MSG_DONTWAIT | MSG_NOSIGNAL);
    if (first < 0 && (errno == EAGAIN || errno == EWOULDBLOCK || errno == ENOBUFS)) {
      (void)__real_close(filler); (void)::unlink(filler_path.c_str()); return true;
    }
    if (first != static_cast<ssize_t>(sizeof(filler_byte))) { (void)__real_close(filler); (void)::unlink(filler_path.c_str()); close_fillers(); return false; }
    ++*records_sent;
    while (*records_sent < kMaximumRecords) {
      const ssize_t sent = ::send(filler, &filler_byte, sizeof(filler_byte), MSG_DONTWAIT | MSG_NOSIGNAL);
      if (sent == static_cast<ssize_t>(sizeof(filler_byte))) { ++*records_sent; continue; }
      if (sent < 0 && (errno == EAGAIN || errno == EWOULDBLOCK || errno == ENOBUFS)) break;
      (void)__real_close(filler); (void)::unlink(filler_path.c_str()); close_fillers(); return false;
    }
    filler_fds->push_back(filler);
  }
  close_fillers();
  return false;
}

bool json_u64_field(const std::string& receipt, const std::string& key, uint64_t* value) {
  const std::string prefix = "\"" + key + "\":";
  const std::size_t start = receipt.rfind(prefix);
  if (start == std::string::npos) return false;
  std::size_t position = start + prefix.size();
  if (position == receipt.size() || receipt[position] < '0' || receipt[position] > '9') return false;
  uint64_t parsed = 0;
  while (position < receipt.size() && receipt[position] >= '0' && receipt[position] <= '9') {
    parsed = parsed * 10U + static_cast<uint64_t>(receipt[position] - '0');
    ++position;
  }
  *value = parsed;
  return true;
}

bool test_unix_dgram_consumer_absent_and_record_order() {
  const std::string absent = "/tmp/mrf1-runtime-absent-" + std::to_string(static_cast<unsigned long>(::getpid())) + ".sock";
  (void)::unlink(absent.c_str());
  g_fake.reset();
  { std::lock_guard<std::mutex> lock(g_fake.mu);
    g_fake.available_blocks = 2;
    g_fake.frontend_steps = {MRF1_NF_OK, MRF1_NF_OK};
    g_fake.submit_steps = {RNT_OK, RNT_OK};
  }
  if (!expect(run(2, kBuildId, test_run_deadline_ms(500), "", absent) == 0,
              "absent AF_UNIX consumer drops without stopping RF stream")) return false;
  { std::lock_guard<std::mutex> lock(g_fake.mu);
    if (!expect(g_fake.rnt_submit == 2, "absent consumer preserves all RF submissions")) return false;
  }
  if (!socket_resources_closed("absent socket consumer closes RF resources")) return false;

  const std::string bound = "/tmp/mrf1-runtime-bound-" + std::to_string(static_cast<unsigned long>(::getpid())) + ".sock";
  (void)::unlink(bound.c_str());
  int consumer = -1;
  if (!expect(bind_unix_dgram(bound, &consumer), "bind real AF_UNIX dgram consumer")) return false;
  g_fake.reset();
  { std::lock_guard<std::mutex> lock(g_fake.mu);
    g_fake.available_blocks = 3;
    g_fake.frontend_steps = {MRF1_NF_OK, MRF1_NF_OK, MRF1_NF_OK};
    g_fake.submit_steps = {RNT_OK, RNT_OK, RNT_OK};
  }
  const int rc = run(3, kBuildId, test_run_deadline_ms(700), "", bound);
  std::vector<std::vector<uint8_t>> records;
  bool sender_epoch_ok = true;
  for (unsigned index = 0; index < 3; ++index) {
    std::vector<uint8_t> record(kDeliveryHeaderBytes + RNT_PACKETS_BYTES);
    sockaddr_un peer{}; socklen_t peer_length = sizeof(peer);
    const ssize_t got = ::recvfrom(consumer, record.data(), record.size(), 0,
                                   reinterpret_cast<sockaddr*>(&peer), &peer_length);
    if (got > 0) {
      sender_epoch_ok = sender_epoch_ok && is_random_epoch_sender(peer, peer_length);
      record.resize(static_cast<std::size_t>(got)); records.push_back(std::move(record));
    }
  }
  (void)__real_close(consumer);
  (void)::unlink(bound.c_str());
  if (!expect(rc == 0 && records.size() == 3, "real AF_UNIX receives one datagram per RFD1/RFE1 record")) return false;
  if (!expect(sender_epoch_ok, "socket producer binds one random abstract sender epoch")) return false;
  for (std::size_t index = 0; index < records.size(); ++index) {
    const auto& record = records[index];
    if (!expect(record.size() >= kDeliveryHeaderBytes, "IPC record contains complete header")) return false;
    const uint8_t* header = record.data();
    const uint32_t payload = read_le32(header + 12);
    if (!expect(read_le32(header + 4) == 1000U + index &&
                record.size() == kDeliveryHeaderBytes + payload &&
                read_le32(header + 16) == record_crc(header, header + kDeliveryHeaderBytes, payload),
                "IPC record preserves frame order and record CRC")) return false;
  }
  return expect(std::memcmp(records[1].data(), "RFD1", 4) == 0, "IPC preserves valid frame event") &&
         socket_resources_closed("bound socket consumer closes RF resources");
}

bool test_unix_dgram_congestion_and_consumer_restart() {
  const std::string path = "/tmp/mrf1-runtime-restart-" + std::to_string(static_cast<unsigned long>(::getpid())) + ".sock";
  (void)::unlink(path.c_str());
  int consumer = -1;
  if (!expect(bind_unix_dgram(path, &consumer), "bind AF_UNIX consumer with no receiver thread")) return false;
  int small_receive_buffer = 1024;
  if (!expect(::setsockopt(consumer, SOL_SOCKET, SO_RCVBUF, &small_receive_buffer, sizeof(small_receive_buffer)) == 0,
              "set bounded AF_UNIX receive queue")) { (void)__real_close(consumer); (void)::unlink(path.c_str()); return false; }
  std::vector<int> filler_fds;
  unsigned prefill_records = 0;
  int filler_send_buffer_bytes = 0;
  if (!expect(fill_unix_dgram_receiver_until_eagain(path, &filler_fds, &prefill_records, &filler_send_buffer_bytes),
              "prefill AF_UNIX consumer queue until a new sender gets EAGAIN")) {
    (void)__real_close(consumer); (void)::unlink(path.c_str()); return false;
  }
  std::cerr << "AF_UNIX congestion setup: max_dgram_qlen=";
  { std::ifstream qlen("/proc/sys/net/unix/max_dgram_qlen"); std::cerr << qlen.rdbuf(); }
  std::cerr << " fillers=" << filler_fds.size() << " records=" << prefill_records
            << " sndbuf=" << filler_send_buffer_bytes << "\n";
  constexpr unsigned kRecords = 96;
  g_fake.reset();
  { std::lock_guard<std::mutex> lock(g_fake.mu);
    g_fake.available_blocks = kRecords;
    g_fake.frontend_steps.assign(kRecords, MRF1_NF_OK);
    g_fake.submit_steps.assign(kRecords, RNT_OK);
  }
  const auto started = std::chrono::steady_clock::now();
  std::ostringstream receipt;
  std::streambuf* const previous_stdout = std::cout.rdbuf(receipt.rdbuf());
  const int congested_rc = run(kRecords, kBuildId, test_run_deadline_ms(1500), "", path);
  std::cout.rdbuf(previous_stdout);
  const auto elapsed = std::chrono::steady_clock::now() - started;
  int flags = ::fcntl(consumer, F_GETFL, 0);
  (void)::fcntl(consumer, F_SETFL, flags | O_NONBLOCK);
  unsigned received = 0;
  std::array<uint8_t, kDeliveryHeaderBytes + RNT_PACKETS_BYTES> record{};
  while (::recv(consumer, record.data(), record.size(), 0) > 0) ++received;
  for (std::size_t index = 0; index < filler_fds.size(); ++index) {
    (void)__real_close(filler_fds[index]);
    (void)::unlink((path + ".filler." + std::to_string(index)).c_str());
  }
  (void)__real_close(consumer);
  (void)::unlink(path.c_str());
  { std::lock_guard<std::mutex> lock(g_fake.mu);
    if (!expect(congested_rc == 0 && g_fake.rnt_submit == kRecords, "full consumer queue cannot stop RF stream")) return false;
  }
  uint64_t dropped = 0;
  if (!expect(json_u64_field(receipt.str(), "ipc_dropped_records", &dropped) && dropped > 0,
              "full AF_UNIX queue is reported as visible IPC drops")) return false;
  if (!expect(received >= 1 && elapsed < test_wait_ms(1200), "nonblocking AF_UNIX congestion does not stall the RF deadline")) return false;
  if (!socket_resources_closed("congested socket consumer closes RF resources")) return false;

  // A stopped consumer unlinks the endpoint; a later independently-bound
  // consumer on the same pathname gets a fresh record without restarting RF.
  for (unsigned round = 0; round != 2; ++round) {
    if (!expect(bind_unix_dgram(path, &consumer), "restarted consumer rebinds endpoint")) return false;
    g_fake.reset();
    { std::lock_guard<std::mutex> lock(g_fake.mu);
      g_fake.available_blocks = 1; g_fake.frontend_steps = {MRF1_NF_OK}; g_fake.submit_steps = {RNT_OK};
    }
    const int restart_rc = run(1, kBuildId, test_run_deadline_ms(400), "", path);
    const ssize_t got = ::recv(consumer, record.data(), record.size(), 0);
    (void)__real_close(consumer);
    (void)::unlink(path.c_str());
    if (!expect(restart_rc == 0 && got == static_cast<ssize_t>(kDeliveryHeaderBytes + RNT_PACKETS_BYTES),
                "consumer stop/restart receives a complete fresh record")) return false;
    if (!socket_resources_closed("restarted socket consumer closes RF resources")) return false;
  }
  return true;
}

bool test_crc_rejection_event_then_recovery() {
  g_fake.reset();
  { std::lock_guard<std::mutex> lock(g_fake.mu);
    g_fake.available_blocks = 2;
    g_fake.frontend_steps = {MRF1_NF_OK, MRF1_NF_OK};
    g_fake.submit_steps = {RNT_E_CRC_REJECTED, RNT_OK};
  }
  if (!expect(run(2, kBuildId, test_run_deadline_ms(500), "fake-crc-stop") == 3,
              "first CRC rejection is terminal")) return false;
  { std::lock_guard<std::mutex> lock(g_fake.mu);
    if (!expect(g_fake.rnt_submit == 1 && g_fake.output.empty(),
                "CRC rejection never retries or publishes a later good frame")) return false;
  }
  return resources_closed("CRC stop releases both owners");
}

bool test_pipeline_overlap_and_slot_lifetime() {
  g_fake.reset();
  { std::lock_guard<std::mutex> lock(g_fake.mu);
    g_fake.available_blocks = 3;
    g_fake.frontend_steps = {MRF1_NF_OK, MRF1_NF_OK, MRF1_NF_OK};
    g_fake.submit_steps = {RNT_OK, RNT_OK, RNT_OK};
    g_fake.block_first_submit = true;
  }
  const int rc = run(3, kBuildId, test_run_deadline_ms(800), "fake-overlap");
  if (!expect(rc == 0, "three-frame fixed-slot overlap run succeeds")) return false;
  bool slots_ok = false;
  {
    std::lock_guard<std::mutex> lock(g_fake.mu);
    slots_ok = expect(g_fake.first_submit_entered && g_fake.overlap_observed, "frontend advances while first IMBC submit is blocked") &&
               expect(g_fake.frontend_feed == 3 && g_fake.rnt_submit == 3, "all three frames drain through two frame slots") &&
               expect(g_fake.wire_corruption == 0, "task/header sentinels and frame IDs survive bounded-slot backpressure") &&
               expect(g_fake.submitted_frame_ids == std::vector<uint32_t>({1000U, 1001U, 1002U}), "no duplicate or reordered submission under overlap") &&
               expect(g_fake.output.size() == 3 * (kDeliveryHeaderBytes + RNT_PACKETS_BYTES), "three complete deliveries only");
  }
  return slots_ok && resources_closed("overlap run releases owners");
}

bool test_current_window_prior_preserves_blocks_and_owners() {
  g_fake.reset();
  { std::lock_guard<std::mutex> lock(g_fake.mu);
    g_fake.available_blocks = 66; g_fake.coherent_adc_tone = true; g_fake.rx_header_case = 1;
    g_fake.frontend_steps.assign(66, MRF1_NF_NEED_MORE); g_fake.frontend_steps.back() = MRF1_NF_OK;
    g_fake.submit_steps = {RNT_OK};
  }
  ::setenv("LINK_CURRENT_WINDOW_CFO", "1", 1);
  ::setenv("LINK_DIAGNOSTIC_COARSE_CFO_HZ", "1234", 1);
  const int good = run(66, kBuildId, test_run_deadline_ms(4000), "fake-current-cfo");
  ::unsetenv("LINK_CURRENT_WINDOW_CFO");
  const bool restored = std::string(std::getenv("LINK_DIAGNOSTIC_COARSE_CFO_HZ")) == "1234";
  ::unsetenv("LINK_DIAGNOSTIC_COARSE_CFO_HZ");
  if (!expect(good == 0 && restored, "current-window estimation runs and restores environment")) return false;
  { std::lock_guard<std::mutex> lock(g_fake.mu);
    if (!expect(g_fake.frontend_feed == 66 && g_fake.read_block == 66 && g_fake.rnt_submit == 1 &&
          std::abs(g_fake.created_coarse_prior - 9200) < 1,
          "all current ADC blocks feed in order; prepared frontend uses fresh tone, not stale1234")) return false;
  }
  if (!resources_closed("current prior success closes original owners")) return false;
  g_fake.reset();
  { std::lock_guard<std::mutex> lock(g_fake.mu); g_fake.available_blocks = 65; }
  ::setenv("LINK_CURRENT_WINDOW_CFO", "1", 1);
  const int bad = run(65, kBuildId, test_run_deadline_ms(4000), "fake-current-cfo-zero");
  ::unsetenv("LINK_CURRENT_WINDOW_CFO");
  if (!expect(bad != 0, "zero receive energy fails closed")) return false;
  { std::lock_guard<std::mutex> lock(g_fake.mu);
    if (!expect(g_fake.frontend_create == 0 && g_fake.rnt_open == 0,
                "failed fresh prior starts no frontend or detector")) return false;
  }
  { std::lock_guard<std::mutex> lock(g_fake.mu);
    return expect(g_fake.open_rx == 1 && g_fake.close_rx == 1 && g_fake.open_output == 1 &&
                  g_fake.close_output == 1 && g_fake.wrong_owner_close == 0 &&
                  g_fake.frontend_destroy == 0 && g_fake.rnt_destroy == 0,
                  "rejected current prior closes capture/output; never created frontend or detector");
  }
}

bool test_frame_budget_drains_capture_without_extra_dsp() {
  g_fake.reset();
  { std::lock_guard<std::mutex> lock(g_fake.mu);
    g_fake.available_blocks = 4;
    g_fake.frontend_steps = {MRF1_NF_OK, MRF1_NF_OK, MRF1_NF_OK, MRF1_NF_OK};
    g_fake.submit_steps = {RNT_OK, RNT_OK, RNT_OK, RNT_OK};
  }
  ::setenv("LINK_DIAGNOSTIC_RF_FRAME_BUDGET", "1", 1);
  const int rc = run(4, kBuildId, test_run_deadline_ms(500), "fake-frame-budget");
  ::unsetenv("LINK_DIAGNOSTIC_RF_FRAME_BUDGET");
  if (!expect(rc == 0, "finite frame-budget run finishes cleanly")) return false;
  { std::lock_guard<std::mutex> lock(g_fake.mu);
    if (!expect(g_fake.rnt_submit == 1 && g_fake.frontend_feed == 1 && g_fake.read_block == 4,
                "all ADC blocks drain but only budgeted frame reaches actual submission")) return false;
  }
  return resources_closed("frame-budget completion releases all owners");
}

bool test_live_header_faults_and_pool_bound() {
  for (unsigned mode = 1; mode <= 5; ++mode) {
    g_fake.reset();
    { std::lock_guard<std::mutex> lock(g_fake.mu);
      g_fake.rx_header_case = mode;
      g_fake.available_blocks = 2;
      g_fake.frontend_steps = {MRF1_NF_OK, MRF1_NF_OK};
      g_fake.submit_steps = {RNT_OK, RNT_OK};
    }
    const int rc = run(2, kBuildId, test_run_deadline_ms(500), "fake-header-boundary");
    if (!expect(rc == (mode == 1 ? 0 : 3),
                "one initial preroll allowed; CRC/gap/repeated flag/lane faults stop")) return false;
    if (!resources_closed("RXTH header boundary releases owners")) return false;
  }
  g_fake.reset();
  if (!expect(run(5121, kBuildId, 500, "fake-over-pool") == 2,
              "capture cannot exceed the bounded pool")) return false;
  std::lock_guard<std::mutex> lock(g_fake.mu);
  return expect(g_fake.open_rx == 0 && g_fake.rnt_open == 0,
                "over-pool invocation opens no device");
}

bool test_eof_without_frame_is_failure() {
  g_fake.reset();
  { std::lock_guard<std::mutex> lock(g_fake.mu); g_fake.eof = true; }
  const int rc = run(1, kBuildId, test_run_deadline_ms(300), "fake-eof");
  if (!expect(rc == 3, "EOF is terminal failure, never a zero-frame success")) return false;
  bool no_record = false;
  { std::lock_guard<std::mutex> lock(g_fake.mu); no_record = expect(g_fake.rnt_submit == 0 && g_fake.output.empty(), "EOF produces no fake DSP submission or record"); }
  return no_record && resources_closed("EOF still releases owners");
}

bool test_finite_capture_without_decoded_frame_is_failure() {
  g_fake.reset();
  { std::lock_guard<std::mutex> lock(g_fake.mu);
    g_fake.available_blocks = 2; g_fake.frontend_steps = {MRF1_NF_NO_FRAME, MRF1_NF_REACQUIRING};
  }
  const int rc = run(2, kBuildId, test_run_deadline_ms(300), "fake-no-frame");
  if (!expect(rc == 4, "finite capture with no valid frame is not a success")) return false;
  bool no_submit = false;
  { std::lock_guard<std::mutex> lock(g_fake.mu); no_submit = expect(g_fake.rnt_submit == 0 && g_fake.output.empty(), "no decoded frame has no delivery record"); }
  return no_submit && resources_closed("no-frame completion still releases owners");
}

bool test_sticky_abort_capabilities_are_required() {
  g_fake.reset();
  { std::lock_guard<std::mutex> lock(g_fake.mu); g_fake.rx_info_flags &= ~ZED_RXTH_INFO_STICKY_IDLE_ABORT; }
  if (!expect(run(1, kBuildId, test_run_deadline_ms(300), "fake-rxth-no-sticky") == 3, "RXTH INFO bit4 is required before capture")) return false;
  bool no_rx_read = false;
  { std::lock_guard<std::mutex> lock(g_fake.mu); no_rx_read = expect(g_fake.read_block == 0 && g_fake.read_offset == 0, "missing RXTH sticky capability prevents read"); }
  if (!no_rx_read) return false;

  g_fake.reset();
  // Keep capture inside fake_read until batch's fake rnt_open rejects the
  // absent capability. request_stop(30) then issues ABORT and wakes read.
  // This makes 30 the first error without timing sleeps or an EOF race.
  { std::lock_guard<std::mutex> lock(g_fake.mu);
    g_fake.imbc_info_flags &= ~ZED_INTERLEAVED_CANDIDATE_INFO_STICKY_IDLE_ABORT;
    g_fake.block_read = true;
  }
  if (!expect(run(1, kBuildId, test_run_deadline_ms(300), "fake-imbc-no-sticky") == 3, "IMBC INFO bit3 is required by fake transport contract")) return false;
  bool no_submit = false;
  { std::lock_guard<std::mutex> lock(g_fake.mu);
    no_submit = expect(g_fake.rnt_open == 1 && g_fake.rnt_submit == 0, "missing IMBC sticky capability rejects before submission") &&
                expect(g_fake.rx_abort_calls >= 1, "IMBC open failure aborts the blocked RXTH read");
  }
  return no_submit;
}

bool test_deadline_aborts_blocked_read() {
  g_fake.reset();
  { std::lock_guard<std::mutex> lock(g_fake.mu); g_fake.block_read = true; }
  const auto started = std::chrono::steady_clock::now();
  const int rc = run(1, kBuildId, test_run_deadline_ms(40), "fake-blocked-read");
  const auto elapsed = std::chrono::steady_clock::now() - started;
  if (!expect(rc == 3, "deadline stops blocked capture")) return false;
  if (!expect(elapsed < test_wait_ms(800), "blocked capture has bounded cancellation")) return false;
  bool aborted = false;
  { std::lock_guard<std::mutex> lock(g_fake.mu); aborted = expect(g_fake.rx_abort_calls >= 1 && g_fake.close_rx == 1, "deadline sends RXTH ABORT before capture close"); }
  return aborted && resources_closed("blocked read leaves no owner thread/resource");
}

bool test_frontend_exception_and_delivery_write_failure_cleanup() {
  g_fake.reset();
  { std::lock_guard<std::mutex> lock(g_fake.mu); g_fake.available_blocks = 1; g_fake.throw_frontend = true; }
  if (!expect(run(1, kBuildId, test_run_deadline_ms(300), "fake-frontend-throw") == 3, "frontend exception becomes controlled failure")) return false;
  if (!resources_closed("frontend exception unwinds owners")) return false;

  g_fake.reset();
  { std::lock_guard<std::mutex> lock(g_fake.mu);
    g_fake.available_blocks = 1; g_fake.frontend_steps = {MRF1_NF_OK};
    g_fake.submit_steps = {RNT_OK}; g_fake.fail_delivery_write = true;
  }
  if (!expect(run(1, kBuildId, test_run_deadline_ms(300), "fake-delivery-fail") == 3, "delivery write failure is terminal")) return false;
  bool no_partial = false;
  { std::lock_guard<std::mutex> lock(g_fake.mu); no_partial = expect(g_fake.rnt_submit == 1 && g_fake.output.empty(), "bad delivery path never leaves partial record"); }
  return no_partial && resources_closed("delivery failure releases owners");
}

bool test_cli_deadline_range() {
  g_fake.reset();
  char program[] = "runtime";
  char start[] = "start-rx";
  char blocks[] = "--blocks";
  char one[] = "1";
  char build[] = "--imbc-build-id";
  char build_value[] = "0x7a31b442";
  char output[] = "--delivery-path";
  char output_value[] = "fake-deadline-max";
  char deadline[] = "--deadline-ms";
  char allowed[] = "120000";
  char *allowed_argv[] = {program, start, blocks, one, build, build_value, output, output_value, deadline, allowed};
  { std::lock_guard<std::mutex> lock(g_fake.mu);
    g_fake.available_blocks = 1; g_fake.frontend_steps = {MRF1_NF_OK}; g_fake.submit_steps = {RNT_OK};
  }
  if (!expect(native_runtime_program_main_for_test(10, allowed_argv) == 0, "CLI accepts the 120000 ms runtime deadline")) return false;
  if (!resources_closed("maximum CLI deadline run releases owners")) return false;

  g_fake.reset();
  char rejected[] = "120001";
  char *rejected_argv[] = {program, start, blocks, one, build, build_value, output, output_value, deadline, rejected};
  if (!expect(native_runtime_program_main_for_test(10, rejected_argv) == 2, "CLI rejects deadline above 120000 ms")) return false;
  std::lock_guard<std::mutex> lock(g_fake.mu);
  return expect(g_fake.open_rx == 0 && g_fake.open_output == 0 && g_fake.rnt_open == 0, "rejected deadline starts no worker or device owner");
}
}  // namespace

// -------- FAKE C ABI and fake syscalls below; no board resources are touched. --------
struct mrf1_nf_frontend { unsigned unused; };
struct rnt_transport { unsigned unused; };

extern "C" mrf1_nf_status mrf1_nf_create(const mrf1_nf_config *, mrf1_nf_frontend **out) {
  std::lock_guard<std::mutex> lock(g_fake.mu); ++g_fake.frontend_create;
  if(g_fake.require_closed_before_frontend && g_fake.close_rx!=1)g_fake.frontend_before_capture_close=true;
  if (const char* value = std::getenv("LINK_DIAGNOSTIC_COARSE_CFO_HZ")) g_fake.created_coarse_prior = std::strtod(value, nullptr);
  if (g_fake.fail_frontend_create) return MRF1_NF_INTERNAL;
  *out = new mrf1_nf_frontend{}; return MRF1_NF_OK;
}
extern "C" void mrf1_nf_destroy(mrf1_nf_frontend *frontend) {
  { std::lock_guard<std::mutex> lock(g_fake.mu); ++g_fake.frontend_destroy; } delete frontend;
}
extern "C" void mrf1_nf_get_stats(const mrf1_nf_frontend *, mrf1_nf_stats *stats) {
  std::lock_guard<std::mutex> lock(g_fake.mu);
  *stats = mrf1_nf_stats{};
  stats->rxth_blocks = g_fake.frontend_feed;
}
extern "C" mrf1_nf_status mrf1_nf_feed_into(mrf1_nf_frontend *, const uint8_t *raw_block, size_t,
                                                const mrf1_nf_output_slot *slot, mrf1_nf_batch_view *out) {
  std::lock_guard<std::mutex> lock(g_fake.mu);
  const unsigned index = g_fake.frontend_feed++;
  g_fake.fed_sequences.push_back(read_le32(raw_block+12));
  if (g_fake.throw_frontend) throw std::runtime_error("FAKE frontend exception");
  const mrf1_nf_status status = index < g_fake.frontend_steps.size() ? g_fake.frontend_steps[index] : MRF1_NF_NO_FRAME;
  if (status != MRF1_NF_OK) return status;
  std::fill(slot->headers, slot->headers + MRF1_MRF_HEADER_BYTES * 2, static_cast<uint8_t>(0x30U + index));
  std::fill(slot->task_bytes, slot->task_bytes + MRF1_BATCH_TASK_BYTES, static_cast<uint8_t>(0x80U + index));
  out->headers = slot->headers; out->header_bytes = MRF1_MRF_HEADER_BYTES * 2;
  out->task_bytes = slot->task_bytes; out->task_bytes_len = MRF1_BATCH_TASK_BYTES;
  out->provenance.frame_id = 1000U + index; out->provenance.flags = 1;
  g_fake.submit_cv.notify_all();
  return MRF1_NF_OK;
}

extern "C" rnt_code rnt_open(const char *, const rnt_io_vtable *, rnt_transport **out) {
  std::lock_guard<std::mutex> lock(g_fake.mu); ++g_fake.rnt_open;
  if ((g_fake.imbc_info_flags & ZED_INTERLEAVED_CANDIDATE_INFO_STICKY_IDLE_ABORT) == 0 || g_fake.fail_rnt_open) return RNT_E_DEVICE;
  g_fake.rnt_open_thread = std::this_thread::get_id();
  *out = new rnt_transport{}; return RNT_OK;
}
extern "C" rnt_code rnt_submit_mrf_frame(rnt_transport *, const uint8_t *tasks, size_t task_bytes,
                                             const uint8_t *headers, size_t header_bytes,
                                             const rnt_submit_config *config, rnt_frame_result *result) {
  std::unique_lock<std::mutex> lock(g_fake.mu);
  if (tasks == nullptr || headers == nullptr || task_bytes != MRF1_BATCH_TASK_BYTES || header_bytes != MRF1_MRF_HEADER_BYTES * 2 ||
      config == nullptr || config->expected_build_id != kBuildId) return RNT_E_ARGUMENT;
  const unsigned index = g_fake.rnt_submit++; g_fake.submitted_frame_ids.push_back(config->rf_frame_id);
  const uint32_t frame_offset = config->rf_frame_id - 1000U;
  const bool sentinel_ok = config->rf_frame_id >= 1000U && config->required_config_id == 2 &&
                           config->required_iterations == 512 && config->required_proposal_step_raw == 32768 &&
                           tasks[0] == static_cast<uint8_t>(0x80U + frame_offset) && tasks[MRF1_BATCH_TASK_BYTES - 1] == static_cast<uint8_t>(0x80U + frame_offset) &&
                           headers[0] == static_cast<uint8_t>(0x30U + frame_offset) && headers[MRF1_MRF_HEADER_BYTES * 2 - 1] == static_cast<uint8_t>(0x30U + frame_offset);
  if (!sentinel_ok) { ++g_fake.wire_corruption; return RNT_E_PROTOCOL; }
  if (index == 0 && g_fake.block_first_submit) {
    g_fake.first_submit_entered = true; g_fake.submit_cv.notify_all();
    g_fake.overlap_observed = g_fake.submit_cv.wait_for(lock, test_wait_ms(300), [] { return g_fake.frontend_feed >= 2; });
  }
  const rnt_code code = index < g_fake.submit_steps.size() ? g_fake.submit_steps[index] : RNT_OK;
  if (code == RNT_OK) {
    for (std::size_t i = 0; i < RNT_PACKETS_BYTES; ++i) result->packets[i] = static_cast<uint8_t>((index * 37U + i) & 0xffU);
    result->metadata.build_id = kBuildId;
  }
  return code;
}
extern "C" rnt_code rnt_abort(rnt_transport *) { std::lock_guard<std::mutex> lock(g_fake.mu); ++g_fake.rnt_abort; return RNT_OK; }
extern "C" void rnt_destroy(rnt_transport *transport) { { std::lock_guard<std::mutex> lock(g_fake.mu); ++g_fake.rnt_destroy; if (std::this_thread::get_id() != g_fake.rnt_open_thread) ++g_fake.wrong_owner_close; } delete transport; }

int fake_open(const char *path, int, ...) {
  std::lock_guard<std::mutex> lock(g_fake.mu);
  if (std::strcmp(path, "/dev/zed-rxth") == 0) { ++g_fake.open_rx; g_fake.capture_open_thread = std::this_thread::get_id(); return kFakeRxFd; }
  ++g_fake.open_output; return kFakeOutputFd;
}
ssize_t fake_read(int fd, void *buffer, size_t count) {
  std::unique_lock<std::mutex> lock(g_fake.mu);
  if (fd != kFakeRxFd) { errno = EBADF; return -1; }
  if (g_fake.block_read) {
    const bool released = g_fake.rx_cv.wait_for(lock, test_wait_ms(750), [] { return g_fake.rx_aborted; });
    errno = released ? ECANCELED : ETIMEDOUT; return -1;
  }
  if (g_fake.eof || g_fake.read_block >= g_fake.available_blocks) return 0;
  const std::size_t remaining = MRF1_RXTH_BLOCK_BYTES - g_fake.read_offset;
  const std::size_t n = std::min({count, remaining, static_cast<std::size_t>(g_fake.read_chunk)});
  std::array<uint8_t, MRF1_RXTH_BLOCK_BYTES> raw{};
  std::memcpy(raw.data(), "RXTH", 4); raw[4] = 1; raw[5] = 32; raw[6] = 1;
  store_le32(raw.data() + 8, 1); store_le32(raw.data() + 12, g_fake.read_block);
  store_le32(raw.data() + 16, g_fake.read_block * 1024U);
  store_le32(raw.data() + 24, 1024);
  if (g_fake.rx_header_case == 1 && g_fake.read_block == 0) raw[6] = 0x19;
  if (g_fake.rx_header_case == 3 && g_fake.read_block == 1)
    store_le32(raw.data() + 16, 4096);
  if (g_fake.rx_header_case == 4 && g_fake.read_block == 1) raw[6] = 0x19;
  if(g_fake.rx_header_case==6 && g_fake.read_block==212)raw[6]=0x15;
  if(g_fake.rx_header_case==7 && g_fake.read_block==1500)raw[6]=0x15;
  store_le32(raw.data() + 28, ~crc32_update(0xffffffffU, raw.data(), 28));
  if (g_fake.rx_header_case == 2) raw[28] ^= 1;
  if (g_fake.rx_header_case == 5) raw[32] = 1;
  if (g_fake.coherent_adc_tone && g_fake.read_block >= g_fake.tone_start_block) {
    for (unsigned i = 0; i < 1024; ++i) {
      const double phase = 2 * 3.14159265358979323846 * 9200 * (g_fake.read_block * 1024.0 + i) / 4000000;
      for (unsigned lane = 0; lane < 2; ++lane) {
        const double amplitude = lane ? 700 : 1400;
        const int16_t re = static_cast<int16_t>(std::lround(amplitude * std::cos(phase))) * 16;
        const int16_t im = static_cast<int16_t>(std::lround(amplitude * std::sin(phase))) * 16;
        const unsigned at = 32 + i * 8 + lane * 4;
        raw[at] = uint16_t(re) & 255; raw[at + 1] = uint16_t(re) >> 8;
        raw[at + 2] = uint16_t(im) & 255; raw[at + 3] = uint16_t(im) >> 8;
      }
    }
  }
  std::memcpy(buffer, raw.data() + g_fake.read_offset, n);
  g_fake.read_offset += n;
  if (g_fake.read_offset == MRF1_RXTH_BLOCK_BYTES) { ++g_fake.read_block; g_fake.read_offset = 0; }
  return static_cast<ssize_t>(n);
}
ssize_t fake_write(int fd, const void *buffer, size_t count) {
  std::lock_guard<std::mutex> lock(g_fake.mu);
  if (fd != kFakeOutputFd || g_fake.fail_delivery_write) { errno = EIO; return -1; }
  const std::size_t n = std::min(count, static_cast<std::size_t>(7));
  const uint8_t *p = static_cast<const uint8_t *>(buffer);
  g_fake.output.insert(g_fake.output.end(), p, p + n); ++g_fake.output_writes;
  return static_cast<ssize_t>(n);
}
int fake_ioctl(int fd, unsigned long request, ...) {
  std::lock_guard<std::mutex> lock(g_fake.mu);
  if (fd != kFakeRxFd) { errno = EBADF; return -1; }
  void *arg = nullptr;
  if (request == ZED_RXTH_IOC_GET_INFO || request == ZED_RXTH_IOC_SET_TIMEOUT) {
    va_list ap; va_start(ap, request); arg = va_arg(ap, void *); va_end(ap);
  }
  if (request == ZED_RXTH_IOC_GET_INFO) {
    auto *info = static_cast<zed_rxth_info *>(arg);
    *info = zed_rxth_info{1, 1, MRF1_RXTH_BLOCK_BYTES, 32, 1024, 8, g_fake.rx_info_flags, 0}; return 0;
  }
  if (request == ZED_RXTH_IOC_SET_TIMEOUT) return 0;
  if (request == ZED_RXTH_IOC_ABORT) { ++g_fake.rx_abort_calls; g_fake.rx_aborted = true; g_fake.rx_cv.notify_all(); return 0; }
  errno = EINVAL; return -1;
}
int fake_close(int fd) {
  std::lock_guard<std::mutex> lock(g_fake.mu);
  if (fd == kFakeRxFd) { ++g_fake.close_rx; if (std::this_thread::get_id() != g_fake.capture_open_thread) ++g_fake.wrong_owner_close; }
  else if (fd == kFakeOutputFd) ++g_fake.close_output;
  else return __real_close(fd);
  return 0;
}
int fake_fstat(int fd, struct stat *st) {
  if (fd != kFakeOutputFd) { errno = EBADF; return -1; }
  std::memset(st, 0, sizeof(*st)); st->st_mode = S_IFREG | 0600; return 0;
}

// Linker wrappers preserve the production source tokens (including
// BoundedQueue::close) while routing only this executable's syscall edges.
extern "C" int __wrap_open(const char *path, int flags, ...) {
  if(std::string(path).find("/tmp/link-share/nativefinite-test-")==0) {
    mode_t mode=0;if(flags&O_CREAT){va_list ap;va_start(ap,flags);mode=va_arg(ap,int);va_end(ap);}
    return __real_open(path,flags,mode);
  }
  return fake_open(path, flags);
}
extern "C" ssize_t __wrap_read(int fd, void *buffer, size_t count) { return fake_read(fd, buffer, count); }
extern "C" ssize_t __wrap_write(int fd, const void *buffer, size_t count) { if(fd!=kFakeOutputFd)return __real_write(fd,buffer,count);return fake_write(fd, buffer, count); }
extern "C" int __wrap_close(int fd) { return fake_close(fd); }
extern "C" int __wrap_fstat(int fd, struct stat *st) { return fake_fstat(fd, st); }
extern "C" int __wrap_ioctl(int fd, unsigned long request, ...) {
  if (request == ZED_RXTH_IOC_GET_INFO || request == ZED_RXTH_IOC_SET_TIMEOUT) {
    va_list ap; va_start(ap, request); void *arg = va_arg(ap, void *); va_end(ap);
    return fake_ioctl(fd, request, arg);
  }
  return fake_ioctl(fd, request);
}


bool test_finite_capture_CFO_and_fail_closed() {
  const std::string directory="/tmp/link-share/nativefinite-test-"+std::to_string(::getpid());
  std::filesystem::create_directories(directory);
  for(unsigned scenario=0;scenario<3;++scenario) {
    g_fake.reset();
    const std::string evidence=directory+"/raw-"+std::to_string(scenario);
    setenv("LINK_FINITE_CAPTURE_CFO","1",1);
    setenv("LINK_DIAGNOSTIC_COARSE_CFO_HZ","1234",1);
    setenv("LINK_RXTH_EVIDENCE_OUT",evidence.c_str(),1);
    {
      std::lock_guard<std::mutex> lock(g_fake.mu);
      g_fake.available_blocks=1024;g_fake.read_chunk=8224;
      g_fake.coherent_adc_tone=scenario==0;
      g_fake.rx_header_case=scenario==2?6:1;
      g_fake.require_closed_before_frontend=true;
      g_fake.frontend_steps.assign(1024,MRF1_NF_NO_FRAME);
      g_fake.frontend_steps[1023]=MRF1_NF_OK;
    }
    const int rc=run(1024,kBuildId,test_run_deadline_ms(5000),"fake-finite");
    unsetenv("LINK_FINITE_CAPTURE_CFO");unsetenv("LINK_RXTH_EVIDENCE_OUT");
    if(!expect(std::string(getenv("LINK_DIAGNOSTIC_COARSE_CFO_HZ"))=="1234","finite mode restores prior environment"))return false;
    unsetenv("LINK_DIAGNOSTIC_COARSE_CFO_HZ");
    {
      std::lock_guard<std::mutex> lock(g_fake.mu);
      if(!expect(g_fake.close_rx==1 && !g_fake.frontend_before_capture_close,"capture closes before any frontend"))return false;
      if(scenario==0) {
        if(!expect(rc==0 && g_fake.frontend_feed==1024 && g_fake.rnt_submit==1,"finite success delivers all raw blocks and real-detector interface"))return false;
        for(unsigned k=0;k<1024;++k)if(!expect(g_fake.fed_sequences[k]==k,"finite raw block order preserved"))return false;
        if(!expect(std::abs(g_fake.created_coarse_prior-9200)<1,"finite receive-only CFO selected"))return false;
      } else {
        if(!expect(rc==3 && g_fake.frontend_create==0 && g_fake.rnt_open==0,"bad capture or incoherent input never opens detector"))return false;
      }
    }
    if(!expect(std::filesystem::file_size(evidence)==(scenario==2?212U:1024U)*8224U,"raw evidence saved after capture shutdown"))return false;
    if(scenario==2) {
      std::ifstream input(evidence+".failure.rxth",std::ios::binary);
      std::array<uint8_t,8224> raw{};input.read(reinterpret_cast<char*>(raw.data()),raw.size());
      if(!expect(input.gcount()==8224 && raw[6]==0x15 && read_le32(raw.data()+28)==~crc32_update(0xffffffffU,raw.data(),28),"complete rejected block preserved"))return false;
    }
  }
  std::filesystem::remove_all(directory);
  return true;
}

bool test_long_capture_bounds_late_signal_fault_eof() {
  // All failures must close capture once, preserve raw evidence, and never
  // invoke the frontend/PL. A late coherent tone forces search beyond 1024.
  const std::string directory="/tmp/link-share/nativefinite-test-"+std::to_string(::getpid())+"-long";
  std::filesystem::create_directories(directory);
  for(unsigned scenario=0;scenario<5;++scenario) {
    const unsigned blocks=scenario==4?1536:2048;
    const unsigned valid=scenario==1?1500:(scenario==2?1700:blocks);
    g_fake.reset();
    const auto evidence=directory+"/raw-"+std::to_string(scenario);
    setenv("LINK_FINITE_CAPTURE_CFO","1",1);
    setenv("LINK_RXTH_EVIDENCE_OUT",evidence.c_str(),1);
    {
      std::lock_guard<std::mutex> lock(g_fake.mu);
      g_fake.available_blocks=scenario==2?1700:blocks;
      g_fake.read_chunk=scenario==4?997:8224;
      g_fake.coherent_adc_tone=scenario!=3;
      g_fake.tone_start_block=1200;
      g_fake.rx_header_case=scenario==1?7:1;
      g_fake.require_closed_before_frontend=true;
      g_fake.frontend_steps.assign(blocks,MRF1_NF_NO_FRAME);
      g_fake.frontend_steps.back()=MRF1_NF_OK;
    }
    const int rc=run(blocks,kBuildId,test_run_deadline_ms(10000),"fake-long");
    unsetenv("LINK_FINITE_CAPTURE_CFO");unsetenv("LINK_RXTH_EVIDENCE_OUT");
    {
      std::lock_guard<std::mutex> lock(g_fake.mu);
      if(!expect(g_fake.close_rx==1 && g_fake.wrong_owner_close==0 && !g_fake.frontend_before_capture_close,"long capture closes exactly once before DSP"))return false;
      if(scenario==0 || scenario==4) {
        if(!expect(rc==0 && g_fake.frontend_feed==blocks && g_fake.rnt_submit==1,"late signal after block1024 selected and all blocks fed"))return false;
        if(!expect(std::abs(g_fake.created_coarse_prior-9200)<1,"long capture CFO recovered"))return false;
        for(unsigned n=0;n<blocks;++n)if(!expect(g_fake.fed_sequences[n]==n,"long capture preserves original block order"))return false;
      } else if(!expect(rc==3 && g_fake.frontend_create==0 && g_fake.rnt_open==0,"long fault EOF or incoherent capture fails closed"))return false;
    }
    if(!expect(std::filesystem::file_size(evidence)==valid*8224U,"long capture evidence exact size including incomplete failure denominator"))return false;
    if(scenario==1 && !expect(std::filesystem::file_size(evidence+".failure.rxth")==8224,"late fault block retained"))return false;
  }
  g_fake.reset();
  if(!expect(run(5121,kBuildId,5000,"fake-bound")==2 && run(0,kBuildId,5000,"fake-bound")==2,"out of bound rejected before I/O"))return false;
  setenv("LINK_FINITE_CAPTURE_CFO","1",1);
  const int rc=run(127,kBuildId,5000,"fake-small");unsetenv("LINK_FINITE_CAPTURE_CFO");
  if(!expect(rc==2 && g_fake.open_rx==0 && g_fake.open_output==0,"undersize finite capture rejected before devices or output"))return false;
  std::filesystem::remove_all(directory);return true;
}
bool test_capture3840_bounds_late_signal_fault_eof() {
  // All failures must close capture once, preserve raw evidence, and never
  // invoke the frontend/PL. A late coherent tone forces search beyond 1024.
  const std::string directory="/tmp/link-share/nativefinite-test-"+std::to_string(::getpid())+"-3840";
  std::filesystem::create_directories(directory);
  for(unsigned scenario=0;scenario<5;++scenario) {
    const unsigned blocks=3840;
    const unsigned valid=scenario==1?1500:(scenario==2?1700:blocks);
    g_fake.reset();
    const auto evidence=directory+"/raw-"+std::to_string(scenario);
    setenv("LINK_FINITE_CAPTURE_CFO","1",1);
    setenv("LINK_RXTH_EVIDENCE_OUT",evidence.c_str(),1);
    {
      std::lock_guard<std::mutex> lock(g_fake.mu);
      g_fake.available_blocks=scenario==2?1700:blocks;
      g_fake.read_chunk=scenario==4?997:8224;
      g_fake.coherent_adc_tone=scenario!=3;
      g_fake.tone_start_block=3000;
      g_fake.rx_header_case=scenario==1?7:1;
      g_fake.require_closed_before_frontend=true;
      g_fake.frontend_steps.assign(blocks,MRF1_NF_NO_FRAME);
      g_fake.frontend_steps.back()=MRF1_NF_OK;
    }
    const int rc=run(blocks,kBuildId,test_run_deadline_ms(10000),"fake-long");
    unsetenv("LINK_FINITE_CAPTURE_CFO");unsetenv("LINK_RXTH_EVIDENCE_OUT");
    {
      std::lock_guard<std::mutex> lock(g_fake.mu);
      if(!expect(g_fake.close_rx==1 && g_fake.wrong_owner_close==0 && !g_fake.frontend_before_capture_close,"long capture closes exactly once before DSP"))return false;
      if(scenario==0 || scenario==4) {
        if(!expect(rc==0 && g_fake.frontend_feed==blocks && g_fake.rnt_submit==1,"late signal after block1024 selected and all blocks fed"))return false;
        if(!expect(std::abs(g_fake.created_coarse_prior-9200)<1,"long capture CFO recovered"))return false;
        for(unsigned n=0;n<blocks;++n)if(!expect(g_fake.fed_sequences[n]==n,"long capture preserves original block order"))return false;
      } else if(!expect(rc==3 && g_fake.frontend_create==0 && g_fake.rnt_open==0,"long fault EOF or incoherent capture fails closed"))return false;
    }
    if(!expect(std::filesystem::file_size(evidence)==valid*8224U,"long capture evidence exact size including incomplete failure denominator"))return false;
    if(scenario==1 && !expect(std::filesystem::file_size(evidence+".failure.rxth")==8224,"late fault block retained"))return false;
  }
  g_fake.reset();
  if(!expect(run(5121,kBuildId,5000,"fake-bound")==2 && run(0,kBuildId,5000,"fake-bound")==2,"out of bound rejected before I/O"))return false;
  setenv("LINK_FINITE_CAPTURE_CFO","1",1);
  const int rc=run(127,kBuildId,5000,"fake-small");unsetenv("LINK_FINITE_CAPTURE_CFO");
  if(!expect(rc==2 && g_fake.open_rx==0 && g_fake.open_output==0,"undersize finite capture rejected before devices or output"))return false;
  std::filesystem::remove_all(directory);return true;
}
bool test_capture5120_bounds_late_signal_fault_eof() {
  // All failures must close capture once, preserve raw evidence, and never
  // invoke the frontend/PL. A late coherent tone forces search beyond 1024.
  const std::string directory="/tmp/link-share/nativefinite-test-"+std::to_string(::getpid())+"-5120";
  std::filesystem::create_directories(directory);
  for(unsigned scenario=0;scenario<5;++scenario) {
    const unsigned blocks=5120;
    const unsigned valid=scenario==1?1500:(scenario==2?1700:blocks);
    g_fake.reset();
    const auto evidence=directory+"/raw-"+std::to_string(scenario);
    setenv("LINK_FINITE_CAPTURE_CFO","1",1);
    setenv("LINK_RXTH_EVIDENCE_OUT",evidence.c_str(),1);
    {
      std::lock_guard<std::mutex> lock(g_fake.mu);
      g_fake.available_blocks=scenario==2?1700:blocks;
      g_fake.read_chunk=scenario==4?997:8224;
      g_fake.coherent_adc_tone=scenario!=3;
      g_fake.tone_start_block=4200;
      g_fake.rx_header_case=scenario==1?7:1;
      g_fake.require_closed_before_frontend=true;
      g_fake.frontend_steps.assign(blocks,MRF1_NF_NO_FRAME);
      g_fake.frontend_steps.back()=MRF1_NF_OK;
    }
    const int rc=run(blocks,kBuildId,test_run_deadline_ms(10000),"fake-long");
    unsetenv("LINK_FINITE_CAPTURE_CFO");unsetenv("LINK_RXTH_EVIDENCE_OUT");
    {
      std::lock_guard<std::mutex> lock(g_fake.mu);
      if(!expect(g_fake.close_rx==1 && g_fake.wrong_owner_close==0 && !g_fake.frontend_before_capture_close,"long capture closes exactly once before DSP"))return false;
      if(scenario==0 || scenario==4) {
        if(!expect(rc==0 && g_fake.frontend_feed==blocks && g_fake.rnt_submit==1,"late signal after block1024 selected and all blocks fed"))return false;
        if(!expect(std::abs(g_fake.created_coarse_prior-9200)<1,"long capture CFO recovered"))return false;
        for(unsigned n=0;n<blocks;++n)if(!expect(g_fake.fed_sequences[n]==n,"long capture preserves original block order"))return false;
      } else if(!expect(rc==3 && g_fake.frontend_create==0 && g_fake.rnt_open==0,"long fault EOF or incoherent capture fails closed"))return false;
    }
    if(!expect(std::filesystem::file_size(evidence)==valid*8224U,"long capture evidence exact size including incomplete failure denominator"))return false;
    if(scenario==1 && !expect(std::filesystem::file_size(evidence+".failure.rxth")==8224,"late fault block retained"))return false;
  }
  g_fake.reset();
  if(!expect(run(5121,kBuildId,5000,"fake-bound")==2 && run(0,kBuildId,5000,"fake-bound")==2,"out of bound rejected before I/O"))return false;
  setenv("LINK_FINITE_CAPTURE_CFO","1",1);
  const int rc=run(127,kBuildId,5000,"fake-small");unsetenv("LINK_FINITE_CAPTURE_CFO");
  if(!expect(rc==2 && g_fake.open_rx==0 && g_fake.open_output==0,"undersize finite capture rejected before devices or output"))return false;
  std::filesystem::remove_all(directory);return true;
}

bool test_finite_public_last_frame_boundary() {
  for(unsigned scenario=0;scenario<2;++scenario) {
    g_fake.reset();setenv("LINK_FINITE_CAPTURE_CFO","1",1);
    setenv("LINK_DIAGNOSTIC_LAST_RF_FRAME_ID","1001",1);
    setenv("LINK_DIAGNOSTIC_RF_FRAME_BUDGET","3",1);
    {
      std::lock_guard<std::mutex> lock(g_fake.mu);
      g_fake.available_blocks=2048;g_fake.read_chunk=8224;
      g_fake.coherent_adc_tone=true;g_fake.rx_header_case=1;
      g_fake.frontend_steps={MRF1_NF_OK,MRF1_NF_OK,MRF1_NF_OK};
      if(scenario==1)g_fake.submit_steps={RNT_OK,RNT_E_CRC_REJECTED};
      g_fake.require_closed_before_frontend=true;
    }
    const int rc=run(2048,kBuildId,test_run_deadline_ms(10000),"fake-last-frame");
    unsetenv("LINK_FINITE_CAPTURE_CFO");unsetenv("LINK_DIAGNOSTIC_LAST_RF_FRAME_ID");unsetenv("LINK_DIAGNOSTIC_RF_FRAME_BUDGET");
    if(!expect(rc==(scenario==0?0:3),"finite boundary completes cleanly only when last-frame PL CRC passes"))return false;
    {
      std::lock_guard<std::mutex> lock(g_fake.mu);
      if(!expect(g_fake.read_block==2048 && g_fake.frontend_feed==2 && g_fake.rnt_submit==2,"all raw blocks captured, process through public final frame only"))return false;
      if(scenario==1 && !expect(g_fake.output.size()<=324,"last-frame PL CRC rejection stops without delivering its payload"))return false;
    }
    if(!resources_closed("finite last-frame boundary releases owners"))return false;
  }
  return true;
}

int main() {
  const bool bits = expect((ZED_RXTH_INFO_STICKY_IDLE_ABORT & (1U << 4)) != 0, "RXTH sticky-ABORT INFO bit available") &&
                    expect((ZED_INTERLEAVED_CANDIDATE_INFO_STICKY_IDLE_ABORT & (1U << 3)) != 0, "IMBC sticky-ABORT INFO bit available");
  const bool ok = bits && test_bounded_queue_deadline_and_close() && test_normal_partial_rx_to_complete_delivery() &&
                  test_unix_dgram_consumer_absent_and_record_order() &&
                  test_unix_dgram_congestion_and_consumer_restart() &&
                  test_crc_rejection_event_then_recovery() && test_pipeline_overlap_and_slot_lifetime() && test_eof_without_frame_is_failure() && test_finite_capture_without_decoded_frame_is_failure() &&
                  test_sticky_abort_capabilities_are_required() &&
                  test_deadline_aborts_blocked_read() && test_frontend_exception_and_delivery_write_failure_cleanup() &&
                  test_cli_deadline_range() && test_live_header_faults_and_pool_bound() &&
                  test_frame_budget_drains_capture_without_extra_dsp() && test_current_window_prior_preserves_blocks_and_owners() && test_finite_capture_CFO_and_fail_closed() && test_long_capture_bounds_late_signal_fault_eof() && test_capture3840_bounds_late_signal_fault_eof() && test_capture5120_bounds_late_signal_fault_eof() && test_finite_public_last_frame_boundary();
  std::cout << (ok ? "runtime_lifecycle_test: PASS\n" : "runtime_lifecycle_test: FAIL\n");
  return ok ? 0 : 1;
}
