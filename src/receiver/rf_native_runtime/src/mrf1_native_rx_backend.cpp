#include "current_window_cfo.h"
#include "finite_capture_cfo.h"
#include "mrf1_native_rx_runtime.h"
#include "mrf1_native_frontend.h"
#include "rf_native_transport.h"
#include "zedboard_rx_transport.h"

#include <array>
#include <atomic>
#include <chrono>
#include <cerrno>
#include <condition_variable>
#include <csignal>
#include <cstdint>
#include <cstddef>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fcntl.h>
#include <fstream>
#include <iostream>
#include <memory>
#include <mutex>
#include <string>
#include <stdexcept>
#include <thread>
#include <unistd.h>
#include <vector>
#include <sys/ioctl.h>
#include <sys/random.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>

namespace {
constexpr char kRxth[] = "/dev/zed-rxth";
constexpr char kImbc[] = "/dev/zed-mimo-interleaved-candidate";
// At most 5120 raw blocks (~40.2 MiB), preallocated before any ADC read.
// Finite capture closes DMA before DSP; this is not continuous streaming.
constexpr std::size_t kCaptureSlots = kMaximumFiniteCaptureBlocks;
constexpr uint32_t kMinimumFiniteBlocks = 128;
constexpr std::size_t kFrameSlots = 2;
constexpr std::size_t kDeliverySlots = 2;
constexpr uint32_t kDefaultDeadlineMs = 60000;
constexpr uint32_t kMaximumDeadlineMs = 120000;
volatile sig_atomic_t g_signal_stop = 0;

void on_signal(int) { g_signal_stop = 1; }

struct StageTiming {
  uint64_t calls{0}, total_ns{0}, maximum_ns{0};
};

struct MeasureStage {
  StageTiming& timing;
  std::chrono::steady_clock::time_point start{std::chrono::steady_clock::now()};
  ~MeasureStage() noexcept {
    const auto ns = static_cast<uint64_t>(std::chrono::duration_cast<std::chrono::nanoseconds>(
        std::chrono::steady_clock::now() - start).count());
    ++timing.calls;
    timing.total_ns += ns;
    if (ns > timing.maximum_ns) timing.maximum_ns = ns;
  }
};

template <typename T> class BoundedQueue {
 public:
  explicit BoundedQueue(std::size_t capacity) : capacity_(capacity), values_(capacity) {}
  bool push(T value, std::chrono::steady_clock::time_point deadline, const std::atomic_bool& stop) {
    std::unique_lock<std::mutex> lock(mu_);
    while (!closed_ && size_ == capacity_ && !stop.load()) {
      if (cv_not_full_.wait_until(lock, deadline) == std::cv_status::timeout) return false;
    }
    if (closed_ || stop.load()) return false;
    values_[(head_ + size_) % capacity_] = std::move(value);
    ++size_;
    if (size_ > high_watermark_) high_watermark_ = size_;
    cv_not_empty_.notify_one(); return true;
  }
  bool pop(T* out, std::chrono::steady_clock::time_point deadline, const std::atomic_bool& stop) {
    std::unique_lock<std::mutex> lock(mu_);
    while (!closed_ && size_ == 0 && !stop.load()) {
      if (cv_not_empty_.wait_until(lock, deadline) == std::cv_status::timeout) return false;
    }
    if (stop.load() || size_ == 0) return false;
    *out = std::move(values_[head_]);
    values_[head_] = T{};
    head_ = (head_ + 1) % capacity_;
    --size_;
    cv_not_full_.notify_one(); return true;
  }
  void close() { std::lock_guard<std::mutex> lock(mu_); closed_ = true; cv_not_empty_.notify_all(); cv_not_full_.notify_all(); }
  std::size_t high_watermark() { std::lock_guard<std::mutex> lock(mu_); return high_watermark_; }
 private:
  // Fixed storage allocated before any worker starts; unlike deque push/pop,
  // wraparound never allocates or releases queue nodes on the receive path.
  std::size_t capacity_, high_watermark_{0}, head_{0}, size_{0};
  std::vector<T> values_; bool closed_{false}; std::mutex mu_; std::condition_variable cv_not_empty_, cv_not_full_;
};

struct Block { std::vector<uint8_t> bytes = std::vector<uint8_t>(MRF1_RXTH_BLOCK_BYTES); };
struct Frame {
  mrf1_nf_provenance provenance{};
  std::vector<uint8_t> headers = std::vector<uint8_t>(MRF1_MRF_HEADER_BYTES * 2);
  std::vector<uint8_t> tasks = std::vector<uint8_t>(MRF1_BATCH_TASK_BYTES);
};
struct Delivery {
  rnt_frame_result result{};
  Frame* slot{nullptr};
  uint32_t build_id{};
  bool rejected{false};
};
enum class DeliveryMode { kFile, kSocket };

struct DeliveryOutput {
  DeliveryMode mode{DeliveryMode::kFile};
  int fd{-1};
  sockaddr_un destination{};
  socklen_t destination_length{0};
  uint32_t socket_send_buffer_bytes{0};
  // Per-process random epoch bound to the AF_UNIX source address. It is not
  // part of RFD1 and never changes the over-air/local delivery record ABI.
  std::array<uint8_t, 16> producer_epoch{};
};
// Local storage records, NOT over-air MRF1. Explicit LE encoding avoids
// native struct padding/endianness. RFD1 carries two packets; RFE1 marks a
// payload-CRC rejection and invalidates the downstream video assembly.
constexpr std::size_t kDeliveryHeaderBytes = 20;
static uint32_t crc32_update(uint32_t c, const uint8_t* p, std::size_t n) {
  while (n--) { c ^= *p++; for (unsigned i=0;i<8;++i) c = (c>>1) ^ ((c&1U)?0xedb88320U:0U); }
  return c;
}
static void store_le32(uint8_t* p, uint32_t value) {
  for (unsigned i = 0; i != 4; ++i) p[i] = static_cast<uint8_t>(value >> (8U * i));
}

struct Shared {
  std::atomic_bool stop{false}; std::atomic_bool delivery_done{false}; std::atomic_int error{0};
  std::atomic_uint64_t detected{0}, delivered{0}, crc_rejected{0};
  std::atomic_uint64_t ipc_enqueued{0}, ipc_dropped{0}, ipc_drop_consumer_absent{0};
  std::atomic_uint64_t ipc_drop_congested{0}, ipc_drop_interrupted{0};
  DeliveryOutput output{};
  std::mutex capture_mu; int rx_fd{-1}; std::mutex transport_mu; rnt_transport* transport{nullptr};
  BoundedQueue<Block*> free_blocks; BoundedQueue<Block*> capture;
  const std::size_t capture_slots;
  BoundedQueue<Frame*> free_frames{kFrameSlots}; BoundedQueue<Frame*> frames{kFrameSlots}; BoundedQueue<Delivery> delivery{kDeliverySlots};
  std::vector<std::unique_ptr<Block>> block_storage; std::vector<std::unique_ptr<Frame>> frame_storage;
  // One writer per stage; the supervisor reads these only after joining all
  // workers. Fixed-size aggregates, never an unbounded per-frame trace.
  StageTiming capture_read_timing, frontend_timing, batch_timing, delivery_timing;
  uint64_t captured_blocks{0};
  uint32_t frame_budget{0};
  uint32_t last_rf_frame_id{0};
  bool current_window_cfo{false};
  bool finite_capture_cfo{false};
  std::vector<Block*> startup_blocks;
  std::atomic<mrf1_nf_frontend*> prepared_frontend{nullptr};
  mrf1_nf_stats frontend_stats{};
  ~Shared() { if (auto* f = prepared_frontend.exchange(nullptr)) mrf1_nf_destroy(f); }
  explicit Shared(std::size_t slots = 1024) : free_blocks(slots), capture(slots), capture_slots(slots) {
    if (slots == 0 || slots > kCaptureSlots) throw std::invalid_argument("capture pool bound invalid");
    if (const char* flag = std::getenv("LINK_CURRENT_WINDOW_CFO")) {
      if (std::string(flag) != "1") throw std::invalid_argument("current CFO flag must be1");
      current_window_cfo = true; startup_blocks.reserve(65);
    }
    if (const char* flag = std::getenv("LINK_FINITE_CAPTURE_CFO")) {
      if (std::string(flag) != "1" || current_window_cfo)
        throw std::invalid_argument("finite CFO requires1 and forbids first64 mode");
      finite_capture_cfo=true;startup_blocks.reserve(capture_slots);
    }
    if (const char* text = std::getenv("LINK_DIAGNOSTIC_RF_FRAME_BUDGET")) {
      char* end = nullptr;
      errno = 0;
      const unsigned long value = std::strtoul(text, &end, 10);
      if (errno || end == text || *end || value == 0 || value > 1024)
        throw std::invalid_argument("frame budget must be decimal 1..1024");
      frame_budget = static_cast<uint32_t>(value);
    }
    if (const char* text = std::getenv("LINK_DIAGNOSTIC_LAST_RF_FRAME_ID")) {
      char* end = nullptr; errno = 0;
      const unsigned long value = std::strtoul(text, &end, 10);
      if (!finite_capture_cfo || errno || end == text || *end || value == 0 || value > 1048576)
        throw std::invalid_argument("finite last RF frame ID must be decimal 1..1048576");
      last_rf_frame_id = static_cast<uint32_t>(value);
    }
    const auto deadline = std::chrono::steady_clock::time_point::max();
    for (std::size_t index = 0; index < capture_slots; ++index) { block_storage.emplace_back(std::make_unique<Block>()); (void)free_blocks.push(block_storage.back().get(), deadline, stop); }
    for (std::size_t index = 0; index < kFrameSlots; ++index) { frame_storage.emplace_back(std::make_unique<Frame>()); (void)free_frames.push(frame_storage.back().get(), deadline, stop); }
  }
  void request_stop(int code) {
    if (code != 0) { int expected = 0; (void)error.compare_exchange_strong(expected, code); }
    stop.store(true); free_blocks.close(); capture.close(); free_frames.close(); frames.close(); delivery.close();
    { std::lock_guard<std::mutex> lock(capture_mu); if (rx_fd >= 0) (void)::ioctl(rx_fd, ZED_RXTH_IOC_ABORT); }
    std::lock_guard<std::mutex> lock(transport_mu); if (transport != nullptr) (void)rnt_abort(transport);
  }
};

// These guards belong to the stage that opened the resource. Deregistration
// and close share the cancellation lease mutex, including exception unwind.
struct CaptureOwner {
  Shared* shared;
  int fd;
  bool release() noexcept {
    std::lock_guard<std::mutex> lock(shared->capture_mu);
    if(fd<0)return true;
    if (shared->rx_fd == fd) shared->rx_fd = -1;
    const int aborted=::ioctl(fd,ZED_RXTH_IOC_ABORT);
    const int closed=::close(fd);fd=-1;
    return aborted==0 && closed==0;
  }
  ~CaptureOwner() noexcept { (void)release(); }
};

struct BatchOwner {
  Shared* shared;
  rnt_transport* transport;
  ~BatchOwner() noexcept {
    std::lock_guard<std::mutex> lock(shared->transport_mu);
    if (shared->transport == transport) shared->transport = nullptr;
    (void)rnt_abort(transport);
    rnt_destroy(transport);
  }
};

struct OutputOwner {
  int fd;
  ~OutputOwner() noexcept { if (fd >= 0) (void)::close(fd); }
};

bool write_complete(int fd,const uint8_t* bytes,std::size_t size) noexcept {
  std::size_t at=0;
  while(at<size){const ssize_t n=::write(fd,bytes+at,size-at);
    if(n<0&&errno==EINTR)continue;
    if(n<=0)return false;
    at+=static_cast<std::size_t>(n);
  }
  return true;
}
// In finite mode DMA ownership is retired before evidence I/O or DSP. This
// guard also preserves the first complete rejected raw block on failure.
struct FiniteCaptureFinalizer {
  Shared* shared;CaptureOwner& owner;int evidence_fd;const char* path;
  const uint8_t* bad_raw{nullptr};
  ~FiniteCaptureFinalizer() noexcept {
    if(!shared->finite_capture_cfo)return;
    if(!owner.release())shared->request_stop(16);
    if(evidence_fd<0)return;
    for(const auto* block:shared->startup_blocks)
      if(!write_complete(evidence_fd,block->bytes.data(),block->bytes.size())) {shared->request_stop(15);break;}
    if(bad_raw && path) {
      try {
        const std::string failure=std::string(path)+".failure.rxth";
        const int fd=::open(failure.c_str(),O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW|O_CLOEXEC,0600);
        if(fd<0){shared->request_stop(15);return;}
        const bool written=write_complete(fd,bad_raw,MRF1_RXTH_BLOCK_BYTES);
        const int closed=::close(fd);
        if(!written||closed!=0)shared->request_stop(15);
      }catch(...){shared->request_stop(15);}
    }
  }
};

bool valid_pathname_socket_path(const std::string& path) {
  return !path.empty() && path[0] == '/' && path.find('\0') == std::string::npos &&
         path.size() < sizeof(sockaddr_un::sun_path);
}

bool fill_random_epoch(std::array<uint8_t, 16>* epoch) {
  std::size_t done = 0;
  while (done != epoch->size()) {
    const ssize_t got = ::getrandom(epoch->data() + done, epoch->size() - done, 0);
    if (got > 0) { done += static_cast<std::size_t>(got); continue; }
    if (got < 0 && errno == EINTR) continue;
    return false;
  }
  return true;
}

bool bind_random_abstract_sender(int fd, std::array<uint8_t, 16>* epoch) {
  // The receiver observes this address with recvmsg. A new backend process
  // gets a fresh 128-bit epoch without changing RFD1/RFE1 bytes. Bounded
  // retries handle the practically impossible abstract-name collision.
  constexpr char kPrefix[] = "mrf1-";
  constexpr char kHex[] = "0123456789abcdef";
  for (unsigned attempt = 0; attempt != 8; ++attempt) {
    if (!fill_random_epoch(epoch)) return false;
    sockaddr_un source{};
    source.sun_family = AF_UNIX;
    source.sun_path[0] = '\0';
    std::memcpy(source.sun_path + 1, kPrefix, sizeof(kPrefix) - 1);
    for (std::size_t index = 0; index != epoch->size(); ++index) {
      source.sun_path[1 + sizeof(kPrefix) - 1 + 2 * index] = kHex[((*epoch)[index] >> 4) & 0x0fU];
      source.sun_path[1 + sizeof(kPrefix) - 1 + 2 * index + 1] = kHex[(*epoch)[index] & 0x0fU];
    }
    const socklen_t length = static_cast<socklen_t>(offsetof(sockaddr_un, sun_path) + 1 +
        (sizeof(kPrefix) - 1) + 2 * epoch->size());
    if (::bind(fd, reinterpret_cast<const sockaddr*>(&source), length) == 0) return true;
    if (errno != EADDRINUSE) return false;
  }
  return false;
}

bool prepare_file_output(Shared* shared, const std::string& path) {
  if (path.empty() || path.find('\0') != std::string::npos) return false;
  const int fd = ::open(path.c_str(), O_CREAT | O_EXCL | O_WRONLY | O_NOFOLLOW | O_CLOEXEC, 0600);
  if (fd < 0) return false;
  struct stat delivery_stat{};
  if (::fstat(fd, &delivery_stat) != 0 || !S_ISREG(delivery_stat.st_mode)) {
    (void)::close(fd);
    return false;
  }
  shared->output.mode = DeliveryMode::kFile;
  shared->output.fd = fd;
  return true;
}

bool prepare_socket_output(Shared* shared, const std::string& path) {
  if (!valid_pathname_socket_path(path)) return false;
  const int fd = ::socket(AF_UNIX, SOCK_DGRAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
  if (fd < 0) return false;
  if (!bind_random_abstract_sender(fd, &shared->output.producer_epoch)) {
    (void)::close(fd);
    return false;
  }
  sockaddr_un destination{};
  destination.sun_family = AF_UNIX;
  std::memcpy(destination.sun_path, path.data(), path.size());
  destination.sun_path[path.size()] = '\0';
  int send_buffer_bytes = 0;
  socklen_t send_buffer_bytes_len = sizeof(send_buffer_bytes);
  if (::getsockopt(fd, SOL_SOCKET, SO_SNDBUF, &send_buffer_bytes, &send_buffer_bytes_len) != 0 ||
      send_buffer_bytes < 0) {
    (void)::close(fd);
    return false;
  }
  shared->output.mode = DeliveryMode::kSocket;
  shared->output.fd = fd;
  shared->output.destination = destination;
  shared->output.destination_length = static_cast<socklen_t>(offsetof(sockaddr_un, sun_path) + path.size() + 1U);
  shared->output.socket_send_buffer_bytes = static_cast<uint32_t>(send_buffer_bytes);
  return true;
}

bool check_rxth(int fd) {
  zed_rxth_info info{};
  if (::ioctl(fd, ZED_RXTH_IOC_GET_INFO, &info) != 0) return false;
  const uint32_t needed = ZED_RXTH_INFO_DMAENGINE_OWNER | ZED_RXTH_INFO_EXCLUSIVE_OPEN | ZED_RXTH_INFO_FIXED_BLOCK | ZED_RXTH_INFO_NO_MMAP | ZED_RXTH_INFO_STICKY_IDLE_ABORT;
  if (info.abi_version != 1 || info.format_version != 1 || info.block_bytes != MRF1_RXTH_BLOCK_BYTES ||
      info.header_bytes != 32 || info.samples_per_block != 1024 || info.bytes_per_sample != 8 || info.reserved != 0 || (info.flags & needed) != needed) return false;
  zed_rxth_timeout timeout{1000, 0}; return ::ioctl(fd, ZED_RXTH_IOC_SET_TIMEOUT, &timeout) == 0;
}

void capture_stage_impl(Shared* shared, uint32_t blocks, std::chrono::steady_clock::time_point deadline) {
  const int fd = ::open(kRxth, O_RDWR | O_CLOEXEC); if (fd < 0 || !check_rxth(fd)) { if (fd >= 0) ::close(fd); shared->request_stop(10); return; }
  CaptureOwner owner{shared, fd};
  { std::lock_guard<std::mutex> lock(shared->capture_mu); shared->rx_fd = fd; }
  // Optional OUTPUT evidence of newly read blocks, never an IQ replay input.
  // This also preserves the received samples if a later DSP stage is stopped.
  const char* evidence_path = std::getenv("LINK_RXTH_EVIDENCE_OUT");
  int evidence_fd = -1;
  if (evidence_path) {
    if (std::string(evidence_path).rfind("/tmp/link-share/", 0) != 0) {
      shared->request_stop(14); return;
    }
    evidence_fd = ::open(evidence_path, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0600);
    if (evidence_fd < 0) { shared->request_stop(14); return; }
  }
  OutputOwner evidence_owner{evidence_fd};
  FiniteCaptureFinalizer finalizer{shared,owner,evidence_fd,evidence_path};
  uint32_t prior_epoch = 0, prior_sequence = 0;
  uint64_t prior_sample = 0;
  bool have_prior = false;
  for (uint32_t index = 0; index < blocks && !shared->stop.load() && !g_signal_stop; ++index) {
    Block* block = nullptr;
    if (!shared->free_blocks.pop(&block, deadline, shared->stop)) { if (!shared->stop.load()) shared->request_stop(12); break; }
    std::size_t done = 0;
    {
      MeasureStage measurement{shared->capture_read_timing};
      while (done < block->bytes.size()) {
      if(std::chrono::steady_clock::now()>=deadline){shared->request_stop(40);return;}
      const ssize_t got = ::read(fd, block->bytes.data() + done, block->bytes.size() - done);
      if (got < 0 && errno == EINTR) continue;
      if (got <= 0) { (void)shared->free_blocks.push(block, deadline, shared->stop); shared->request_stop(11); return; }
      done += static_cast<std::size_t>(got);
      }
    }
    const auto* raw = block->bytes.data();
    const auto le32 = [](const uint8_t* q) {
      return uint32_t(q[0]) | (uint32_t(q[1]) << 8) |
          (uint32_t(q[2]) << 16) | (uint32_t(q[3]) << 24);
    };
    const uint32_t flags = uint32_t(raw[6]) | (uint32_t(raw[7]) << 8);
    const uint32_t epoch = le32(raw + 8), sequence = le32(raw + 12);
    const uint64_t sample = uint64_t(le32(raw + 16)) | (uint64_t(le32(raw + 20)) << 32);
    const bool startup = index == 0 && sequence == 0 && epoch != 0 && flags == 0x19;
    const bool initial = index == 0 && flags == 0x11;
    bool valid = std::memcmp(raw, "RXTH", 4) == 0 && raw[4] == 1 && raw[5] == 32 &&
        le32(raw + 24) == 1024 && ~crc32_update(0xffffffffU, raw, 28) == le32(raw + 28) &&
        (flags == 1 || initial || startup);
    if (have_prior) valid = valid && epoch == prior_epoch && sequence == prior_sequence + 1U &&
        sample == prior_sample + 1024U && flags == 1;
    for (std::size_t at = 32; at < block->bytes.size(); at += 2)
      valid = valid && (raw[at] & 15U) == 0;
    if (!valid) {
      std::fprintf(stderr, "LIVE_RXTH_HEADER_FAULT block=%u flags=%u epoch=%u sequence=%u first=%llu header_crc_valid=%u\n", index, flags,epoch,sequence,static_cast<unsigned long long>(sample),unsigned(~crc32_update(0xffffffffU,raw,28)==le32(raw+28)));
      finalizer.bad_raw=raw;
      (void)shared->free_blocks.push(block, deadline, shared->stop);
      shared->request_stop(13); return;
    }
    if (evidence_fd >= 0 && !shared->finite_capture_cfo) {
      std::size_t written = 0;
      while (written < block->bytes.size()) {
        const ssize_t n = ::write(evidence_fd, raw + written, block->bytes.size() - written);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) { shared->request_stop(15); return; }
        written += static_cast<std::size_t>(n);
      }
    }
    prior_epoch = epoch; prior_sequence = sequence; prior_sample = sample; have_prior = true;
    if (index == 0) std::fprintf(stderr, "LIVE_RXTH_FIRST_BLOCK monotonic_ns=%lld\n",
        static_cast<long long>(std::chrono::duration_cast<std::chrono::nanoseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count()));
    ++shared->captured_blocks;
    if(shared->finite_capture_cfo) shared->startup_blocks.push_back(block);
    else if (!shared->capture.push(block, deadline, shared->stop)) { (void)shared->free_blocks.push(block, deadline, shared->stop); if (!shared->stop.load()) shared->request_stop(12); break; }
  }
  shared->capture.close();
}

void frontend_stage_impl(Shared* shared, std::chrono::steady_clock::time_point deadline) {
  mrf1_nf_config config{}; config.sample_rate_hz = 4000000; config.task_iterations = 512; config.config_id = 2; config.proposal_step_raw = 32768;
  mrf1_nf_frontend* frontend = shared->prepared_frontend.exchange(nullptr);
  if (!frontend && mrf1_nf_create(&config, &frontend) != MRF1_NF_OK) { shared->request_stop(20); return; }
  std::unique_ptr<mrf1_nf_frontend, decltype(&mrf1_nf_destroy)> owner(frontend, &mrf1_nf_destroy);
  Block* block = nullptr;
  uint32_t formed_frames = 0;
  std::size_t startup_index = 0;
  const auto next_block = [&]() {
    if (startup_index < shared->startup_blocks.size()) {
      block = shared->startup_blocks[startup_index++]; return true;
    }
    return shared->capture.pop(&block, deadline, shared->stop);
  };
  while (next_block() && !shared->stop.load() && !g_signal_stop) {
    Frame* frame = nullptr;
    if (!shared->free_frames.pop(&frame, deadline, shared->stop)) { (void)shared->free_blocks.push(block, deadline, shared->stop); if (!shared->stop.load()) shared->request_stop(22); break; }
    mrf1_nf_output_slot slot{frame->headers.data(), frame->headers.size(), frame->tasks.data(), frame->tasks.size()};
    mrf1_nf_batch_view view{};
    mrf1_nf_status status;
    { MeasureStage measurement{shared->frontend_timing};
      status = mrf1_nf_feed_into(frontend, block->bytes.data(), block->bytes.size(), &slot, &view);
    }
    (void)shared->free_blocks.push(block, deadline, shared->stop);
    if (status == MRF1_NF_NEED_MORE || status == MRF1_NF_REACQUIRING || status == MRF1_NF_NO_FRAME) { (void)shared->free_frames.push(frame, deadline, shared->stop); continue; }
    if (status != MRF1_NF_OK || view.header_bytes != 40 || view.task_bytes_len != MRF1_BATCH_TASK_BYTES || view.headers != frame->headers.data() || view.task_bytes != frame->tasks.data()) { (void)shared->free_frames.push(frame, deadline, shared->stop); shared->request_stop(21); break; }
    frame->provenance = view.provenance;
    if (!shared->frames.push(frame, deadline, shared->stop)) { (void)shared->free_frames.push(frame, deadline, shared->stop); if (!shared->stop.load()) shared->request_stop(22); break; }
    // End the finite DSP lane after all budgeted frames are formed. The
    // capture lane still validates/drains its bounded new ADC window and the
    // real detector/delivery lanes must still finish every frame and CRC.
    ++formed_frames;
    // Optional finite boundary from the CRC-validated public MRF1 header.
    // It permits a lost prefix duplicate without processing the quiet tail.
    // PL/CRC and the independent complete-AU/unique-frame gates still apply.
    if ((shared->frame_budget && formed_frames == shared->frame_budget) ||
        (shared->last_rf_frame_id && view.provenance.frame_id == shared->last_rf_frame_id)) break;
  }
  mrf1_nf_get_stats(frontend, &shared->frontend_stats);
  shared->frames.close();
}

void batch_stage_impl(Shared* shared, uint32_t expected_build_id, std::chrono::steady_clock::time_point deadline) {
  rnt_transport* transport = nullptr;
  if (rnt_open(kImbc, nullptr, &transport) != RNT_OK) { shared->request_stop(30); return; }
  BatchOwner owner{shared, transport};
  { std::lock_guard<std::mutex> lock(shared->transport_mu); shared->transport = transport; }
  Frame* frame = nullptr;
  while (shared->frames.pop(&frame, deadline, shared->stop) && !shared->stop.load() && !g_signal_stop) {
    rnt_submit_config config{}; config.rf_frame_id = frame->provenance.frame_id; config.expected_build_id = expected_build_id; config.timeout_ms = 1000; config.required_config_id = 2; config.required_iterations = 512; config.required_proposal_step_raw = 32768;
    Delivery delivery{}; delivery.slot = frame; delivery.build_id = expected_build_id;
    rnt_code code;
    { MeasureStage measurement{shared->batch_timing};
      code = rnt_submit_mrf_frame(transport, frame->tasks.data(), frame->tasks.size(), frame->headers.data(), frame->headers.size(), &config, &delivery.result);
    }
    if (code == RNT_E_CRC_REJECTED) {
      shared->crc_rejected.fetch_add(1);
      // This bench's hard stop policy prohibits recovery after a CRC fault.
      std::fprintf(stderr, "LIVE_CRC_REJECT frame=%u\n", frame->provenance.frame_id);
      (void)shared->free_frames.push(frame, deadline, shared->stop);
      shared->request_stop(35); break;
    } else if (code != RNT_OK) {
      (void)shared->free_frames.push(frame, deadline, shared->stop); shared->request_stop(31); break;
    } else {
      shared->detected.fetch_add(1);
      std::fprintf(stderr, "LIVE_PL_CRC_OK frame=%u monotonic_ns=%lld\n",
          frame->provenance.frame_id, static_cast<long long>(
          std::chrono::duration_cast<std::chrono::nanoseconds>(
          std::chrono::steady_clock::now().time_since_epoch()).count()));
    }
    if (!shared->delivery.push(std::move(delivery), deadline, shared->stop)) { (void)shared->free_frames.push(frame, deadline, shared->stop); if (!shared->stop.load()) shared->request_stop(32); break; }
  }
  shared->delivery.close();
}

void delivery_stage_impl(Shared* shared, std::chrono::steady_clock::time_point deadline) {
  // One preallocated envelope is reused after each frame slot is returned.
  // Datagram output sends the exact record in one operation; it never waits
  // for a video consumer, ACK, decoder, or display pipeline.
  std::array<uint8_t, kDeliveryHeaderBytes + RNT_PACKETS_BYTES> record{};
  Delivery value;
  while (shared->delivery.pop(&value, deadline, shared->stop) && !g_signal_stop) {
    MeasureStage measurement{shared->delivery_timing};
    // rnt_submit_mrf_frame has already validated complete two-stream packets
    // and CRC policy.  The display/recorder consumer is intentionally outside
    // this RF-only executable; count only complete delivered envelopes.
    uint8_t* const header = record.data();
    std::memset(header, 0, kDeliveryHeaderBytes);
    std::memcpy(header, value.rejected ? "RFE1" : "RFD1", 4);
    store_le32(header + 4, value.slot->provenance.frame_id);
    store_le32(header + 8, value.build_id);
    const std::size_t payload_bytes = value.rejected ? 0 : RNT_PACKETS_BYTES;
    store_le32(header + 12, static_cast<uint32_t>(payload_bytes));
    uint32_t crc = crc32_update(0xffffffffU, header, 16);
    crc = crc32_update(crc, value.result.packets, payload_bytes);
    store_le32(header + 16, ~crc);
    if (payload_bytes != 0) std::memcpy(record.data() + kDeliveryHeaderBytes, value.result.packets, payload_bytes);
    const std::size_t record_bytes = kDeliveryHeaderBytes + payload_bytes;
    bool fatal_output_error = false;
    if (shared->output.mode == DeliveryMode::kFile) {
      const uint8_t* bytes = record.data();
      std::size_t remaining = record_bytes;
      while (remaining != 0) {
        const ssize_t wrote = ::write(shared->output.fd, bytes, remaining);
        if (wrote < 0 && errno == EINTR) continue;
        if (wrote <= 0) { fatal_output_error = true; break; }
        bytes += wrote;
        remaining -= static_cast<std::size_t>(wrote);
      }
      if (!fatal_output_error && !value.rejected) shared->delivered.fetch_add(1);
    } else {
      const ssize_t sent = ::sendto(shared->output.fd, record.data(), record_bytes,
                                    MSG_DONTWAIT | MSG_NOSIGNAL,
                                    reinterpret_cast<const sockaddr*>(&shared->output.destination),
                                    shared->output.destination_length);
      if (sent == static_cast<ssize_t>(record_bytes)) {
        shared->ipc_enqueued.fetch_add(1);
      } else if (sent >= 0) {
        // SOCK_DGRAM must preserve record atomicity; a short result is not a
        // valid delivery and is an implementation/configuration failure.
        fatal_output_error = true;
      } else {
        const int send_error = errno;
        shared->ipc_dropped.fetch_add(1);
        if (send_error == ENOENT || send_error == ECONNREFUSED || send_error == ECONNRESET) {
          shared->ipc_drop_consumer_absent.fetch_add(1);
        } else if (send_error == EAGAIN || send_error == EWOULDBLOCK || send_error == ENOBUFS) {
          shared->ipc_drop_congested.fetch_add(1);
        } else if (send_error == EINTR) {
          // Do not retry: preserving at-most-once record identity matters more
          // than opportunistically recovering an interrupted datagram send.
          shared->ipc_drop_interrupted.fetch_add(1);
        } else {
          fatal_output_error = true;
        }
      }
    }
    if (fatal_output_error) {
      (void)shared->free_frames.push(value.slot, deadline, shared->stop);
      shared->request_stop(50);
      shared->delivery_done.store(true);
      return;
    }
    (void)shared->free_frames.push(value.slot, deadline, shared->stop);
  }
  shared->delivery_done.store(true);
}

void capture_stage(Shared* s, uint32_t n, std::chrono::steady_clock::time_point d) { try { capture_stage_impl(s, n, d); } catch (...) { s->request_stop(60); } }
void frontend_stage(Shared* s, std::chrono::steady_clock::time_point d) { try { frontend_stage_impl(s, d); } catch (...) { s->request_stop(61); } }
void batch_stage(Shared* s, uint32_t id, std::chrono::steady_clock::time_point d) { try { batch_stage_impl(s, id, d); } catch (...) { s->request_stop(62); } }
void delivery_stage(Shared* s, std::chrono::steady_clock::time_point d) { try { delivery_stage_impl(s, d); } catch (...) { s->request_stop(63); } }

bool argument_value_once(int argc, char** argv, const std::string& name, std::string* out) {
  bool found = false;
  for (int i = 2; i < argc; ++i) {
    if (name != argv[i]) continue;
    if (found || i + 1 >= argc) return false;
    *out = argv[++i];
    found = true;
  }
  return found;
}

unsigned argument_count(int argc, char** argv, const std::string& name) {
  unsigned count = 0;
  for (int i = 2; i < argc; ++i) if (name == argv[i]) ++count;
  return count;
}
uint32_t u32_or_zero(const std::string& text) {
  // Strict, allocation/locale-free numeric parsing. In particular do not
  // pull glibc 2.38 __isoc23_strtoul into an otherwise older-compatible ARM
  // executable just to accept a CLI integer, or silently accept "12junk".
  unsigned base = 10;
  std::size_t position = 0;
  if (text.size() > 2 && text[0] == '0' && (text[1] == 'x' || text[1] == 'X')) {
    base = 16;
    position = 2;
  }
  if (position == text.size()) return 0;
  uint32_t result = 0;
  for (; position < text.size(); ++position) {
    const char ch = text[position];
    unsigned digit = 16;
    if (ch >= '0' && ch <= '9') digit = static_cast<unsigned>(ch - '0');
    else if (ch >= 'a' && ch <= 'f') digit = static_cast<unsigned>(ch - 'a') + 10U;
    else if (ch >= 'A' && ch <= 'F') digit = static_cast<unsigned>(ch - 'A') + 10U;
    if (digit >= base || result > (0xffffffffU - digit) / base) return 0;
    result = result * base + digit;
  }
  return result;
}

void print_stage_timing(const char* name, const StageTiming& timing) {
  std::cout << '"' << name << "\":{\"calls\":" << timing.calls
            << ",\"total_ns\":" << timing.total_ns
            << ",\"maximum_ns\":" << timing.maximum_ns << '}';
}
void print_identity() { std::cout << "{\"schema\":\"" << MRF1_NATIVE_RUNTIME_SCHEMA << "\",\"mode\":\"rxonly\",\"ready\":false,\"run_ready\":false,\"native_stage\":\"build_unverified\",\"slow_python_path\":false,\"build_id\":\"mrf1-native-runtime-v1\",\"protocol_commit\":\"" << MRF1_NATIVE_RUNTIME_PROTOCOL_COMMIT << "\",\"pipeline\":{\"rxth_block_bytes\":8224,\"input_task_bytes\":120,\"output_result_bytes\":64,\"tasks_per_frame\":528}}\n"; }

int run(uint32_t blocks, uint32_t expected_build_id, uint32_t deadline_ms,
        const std::string& delivery_path, const std::string& delivery_socket = "") {
  if (blocks == 0 || blocks > kCaptureSlots || expected_build_id == 0 || deadline_ms == 0 || deadline_ms > kMaximumDeadlineMs) return 2;
  if ((delivery_path.empty() && delivery_socket.empty()) || (!delivery_path.empty() && !delivery_socket.empty())) return 2;
  g_signal_stop = 0;
  std::signal(SIGINT, on_signal); std::signal(SIGTERM, on_signal);
  Shared shared(blocks);
  if(shared.finite_capture_cfo && blocks<kMinimumFiniteBlocks)return 2;
  if ((!delivery_path.empty() && !prepare_file_output(&shared, delivery_path)) ||
      (!delivery_socket.empty() && !prepare_socket_output(&shared, delivery_socket))) return 2;
  OutputOwner output_owner{shared.output.fd};
  const auto deadline = std::chrono::steady_clock::now() + std::chrono::milliseconds(deadline_ms);
  const auto run_started = std::chrono::steady_clock::now();
  std::thread capture, frontend, batch, delivery;
  try {
    capture = std::thread(capture_stage, &shared, blocks, shared.finite_capture_cfo ? std::min(deadline,std::chrono::steady_clock::now()+std::chrono::seconds(5)) : deadline);
    if(shared.finite_capture_cfo) {
      capture.join();
      if(shared.stop.load() || g_signal_stop || shared.captured_blocks!=blocks)
        throw std::runtime_error("finite new ADC capture did not complete cleanly");
      std::fprintf(stderr,"LIVE_FINITE_ADC_CLOSED blocks=%llu before_evidence_DSP_PL=1\n",static_cast<unsigned long long>(shared.captured_blocks));
    }
    if (shared.current_window_cfo || shared.finite_capture_cfo) {
      // Capture owns all device reads and validates every raw header first.
      // Retain the first blocks in original order. The same frontend will
      // subsequently receive every block, including the initial reset marker.
      std::vector<std::array<std::complex<double>, 2>> samples;
      if (!shared.finite_capture_cfo) samples.reserve(65536);
      const auto append_samples=[&](Block* block) {
        const auto* raw=block->bytes.data();
        if(raw[6]==0x19)return;
        for(unsigned n=0;n<1024;++n) {
          std::array<std::complex<double>,2> value{};
          for(unsigned lane=0;lane<2;++lane) {
            const auto* p=raw+32+n*8+lane*4;
            const auto word=[](const uint8_t* v){return static_cast<int16_t>(uint16_t(v[0])|(uint16_t(v[1])<<8));};
            value[lane]={word(p)/32768.0,word(p+2)/32768.0};
          }
          samples.push_back(value);
        }
      };
      if(!shared.finite_capture_cfo) {
        while(samples.size()<65536) {
          Block* block=nullptr;
          if(!shared.capture.pop(&block,deadline,shared.stop)||g_signal_stop)
            throw std::runtime_error("current CFO startup capture ended early");
          shared.startup_blocks.push_back(block);
          if(shared.startup_blocks.size()>65)throw std::runtime_error("current CFO startup bound exceeded");
          append_samples(block);
        }
      }
      CurrentCfoEstimate estimate;
      if(shared.finite_capture_cfo) {
        // Decode only one 65536-sample CFO window (~2 MiB), rather than
        // materializing 64 MiB of complex<double> for a 2048-block capture.
        const std::size_t skip = shared.startup_blocks.front()->bytes[6] == 0x19 ? 1 : 0;
        const std::size_t sample_count = (shared.startup_blocks.size() - skip) * 1024;
        const auto selected=select_finite_capture_cfo_windows(sample_count,
          [&](std::size_t at, auto& window) {
            if(g_signal_stop || std::chrono::steady_clock::now() >= deadline)
              throw std::runtime_error("finite CFO interrupted or deadline expired");
            for(std::size_t n=0;n<window.size();++n) {
              const std::size_t index=at+n;
              const auto* p=shared.startup_blocks[skip+index/1024]->bytes.data()+32+(index%1024)*8;
              const auto word=[](const uint8_t* v){return static_cast<int16_t>(uint16_t(v[0])|(uint16_t(v[1])<<8));};
              for(unsigned lane=0;lane<2;++lane)
                window[n][lane]={word(p+lane*4)/32768.0,word(p+lane*4+2)/32768.0};
            }
          });
        estimate=selected.estimate;
        std::fprintf(stderr,"LIVE_FINITE_CFO window_start=%zu coherent_windows=%u examined_windows=%u samples=%zu min_coherence=%.9f capture_closed=1\n",selected.window_start,selected.coherent_windows,selected.examined_windows,sample_count,selected.score);
      } else estimate=estimate_current_cfo(samples);
      // Capture's single getenv is complete before it publishes any block.
      // Create the unchanged frontend here before launching its worker or the
      // detector/delivery workers. Restore the environment before launching.
      const char* original = std::getenv("LINK_DIAGNOSTIC_COARSE_CFO_HZ");
      const bool had_prior = original != nullptr;
      const std::string saved_prior = had_prior ? original : "";
      char text[64]; std::snprintf(text, sizeof(text), "%.17g", estimate.common_hz);
      if (::setenv("LINK_DIAGNOSTIC_COARSE_CFO_HZ", text, 1) != 0) throw std::runtime_error("CFO prior environment failed");
      mrf1_nf_config config{}; config.sample_rate_hz = 4000000; config.task_iterations = 512;
      config.config_id = 2; config.proposal_step_raw = 32768;
      mrf1_nf_frontend* f = nullptr;
      const auto status = mrf1_nf_create(&config, &f);
      if (had_prior) (void)::setenv("LINK_DIAGNOSTIC_COARSE_CFO_HZ", saved_prior.c_str(), 1);
      else (void)::unsetenv("LINK_DIAGNOSTIC_COARSE_CFO_HZ");
      if (status != MRF1_NF_OK) throw std::runtime_error("current CFO frontend creation failed");
      shared.prepared_frontend.store(f);
      std::fprintf(stderr, "LIVE_CURRENT_CFO hz=%.9f rx0=%.9f rx1=%.9f coh0=%.6f coh1=%.6f retained_blocks=%zu\n",
          estimate.common_hz, estimate.hz[0], estimate.hz[1], estimate.coherence[0], estimate.coherence[1], shared.startup_blocks.size());
    }
    frontend = std::thread(frontend_stage, &shared, deadline);
    batch = std::thread(batch_stage, &shared, expected_build_id, deadline);
    delivery = std::thread(delivery_stage, &shared, deadline);
  } catch (const std::exception& error) {
    std::fprintf(stderr, "LIVE_STARTUP_FAILED %s\n", error.what());
    shared.request_stop(64);
    if (capture.joinable()) capture.join();
    if (frontend.joinable()) frontend.join();
    if (batch.joinable()) batch.join();
    if (delivery.joinable()) delivery.join();
    return 3;
  }
  while (!shared.stop.load() && !shared.delivery_done.load() && !g_signal_stop && std::chrono::steady_clock::now() < deadline) std::this_thread::sleep_for(std::chrono::milliseconds(10));
  if (g_signal_stop || (!shared.delivery_done.load() && std::chrono::steady_clock::now() >= deadline)) shared.request_stop(40);
  if(capture.joinable())capture.join();
  frontend.join(); batch.join(); delivery.join();
  const int error = shared.error.load();
  const auto elapsed_ns = std::chrono::duration_cast<std::chrono::nanoseconds>(
      std::chrono::steady_clock::now() - run_started).count();
  std::cout << "{\"schema\":\"" << MRF1_NATIVE_RUNTIME_SCHEMA
            << "\",\"mode\":\"rxonly\",\"started\":true,\"blocks\":" << blocks
            << ",\"capture_pool_blocks\":" << shared.capture_slots
            << ",\"capture_pool_bytes\":" << shared.capture_slots * MRF1_RXTH_BLOCK_BYTES
            << ",\"captured_blocks\":" << shared.captured_blocks
            << ",\"detected_frames\":" << shared.detected.load()
            << ",\"delivered_frames\":" << shared.delivered.load()
            << ",\"crc_rejected_frames\":" << shared.crc_rejected.load()
            << ",\"delivery\":{\"mode\":\""
            << (shared.output.mode == DeliveryMode::kFile ? "file" : "unix_dgram")
            << "\",\"ipc_enqueued_records\":" << shared.ipc_enqueued.load()
            << ",\"ipc_dropped_records\":" << shared.ipc_dropped.load()
            << ",\"ipc_drop_consumer_absent\":" << shared.ipc_drop_consumer_absent.load()
            << ",\"ipc_drop_congested\":" << shared.ipc_drop_congested.load()
            << ",\"ipc_drop_interrupted\":" << shared.ipc_drop_interrupted.load()
            << ",\"ipc_sender_queue_high_watermark_available\":false"
            << ",\"ipc_socket_send_buffer_bytes\":" << shared.output.socket_send_buffer_bytes << '}'
            << ",\"elapsed_ns\":" << elapsed_ns
            << ",\"queue_high_watermarks\":{\"capture\":" << shared.capture.high_watermark()
            << ",\"frames\":" << shared.frames.high_watermark()
            << ",\"delivery\":" << shared.delivery.high_watermark() << '}'
            << ",\"frontend\":{\"rejected_blocks\":" << shared.frontend_stats.rejected_blocks
            << ",\"continuity_resets\":" << shared.frontend_stats.continuity_resets
            << ",\"acquisition_attempts\":" << shared.frontend_stats.acquisition_attempts
            << ",\"acquisition_failures\":" << shared.frontend_stats.acquisition_failures << '}'
            << ",\"timings\":{";
  print_stage_timing("capture_read", shared.capture_read_timing); std::cout << ',';
  print_stage_timing("frontend_feed", shared.frontend_timing); std::cout << ',';
  print_stage_timing("detector_batch", shared.batch_timing); std::cout << ',';
  print_stage_timing("delivery_write", shared.delivery_timing);
  std::cout << "},\"video_frame_rate_verified\":false}\n";
  if (error != 0) { std::cerr << "RX-only runtime stopped with code " << error << "\n"; return 3; }
  if (shared.detected.load() == 0) return 4;
  return 0;
}

}  // namespace

int main(int argc, char** argv) try {
  if (argc < 2) return 2;
  const std::string command = argv[1];
  if (command == "identity") { print_identity(); return 0; }
  // A separately invoked process cannot safely stop this foreground owner.
  // Do not advertise a process-local flag as deployment control.
  if (command == "stop-rx") return 2;
  if (command != "start-rx") return 2;
  std::string blocks, build, deadline, delivery_path, delivery_socket;
  if (!argument_value_once(argc, argv, "--blocks", &blocks) ||
      !argument_value_once(argc, argv, "--imbc-build-id", &build)) return 2;
  const unsigned path_count = argument_count(argc, argv, "--delivery-path");
  const unsigned socket_count = argument_count(argc, argv, "--delivery-socket");
  const unsigned deadline_count = argument_count(argc, argv, "--deadline-ms");
  if (path_count > 1 || socket_count > 1 || deadline_count > 1 || path_count == socket_count) return 2;
  if ((path_count == 1 && !argument_value_once(argc, argv, "--delivery-path", &delivery_path)) ||
      (socket_count == 1 && !argument_value_once(argc, argv, "--delivery-socket", &delivery_socket)) ||
      (deadline_count == 1 && !argument_value_once(argc, argv, "--deadline-ms", &deadline))) return 2;
  const uint32_t ms = deadline_count == 1 ? u32_or_zero(deadline) : kDefaultDeadlineMs;
  return run(u32_or_zero(blocks), u32_or_zero(build), ms, delivery_path, delivery_socket);
} catch (...) {
  std::cerr << "RX-only runtime failed before/after supervised worker execution\n";
  return 3;
}
