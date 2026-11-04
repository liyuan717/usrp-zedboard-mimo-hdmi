#include "rf_native_transport.h"

#include <array>
#include <atomic>
#include <cerrno>
#include <condition_variable>
#include <cstdint>
#include <cstring>
#include <limits>
#include <mutex>
#include <new>
#include <vector>

#if defined(__linux__)
#include <fcntl.h>
#include <sys/ioctl.h>
#include <unistd.h>
#endif

namespace {

constexpr uint32_t kAbiVersion = 1;
constexpr uint32_t kInputBytes = 120;
constexpr uint32_t kOutputBytes = 64;
constexpr uint32_t kTaskCount = 528;
constexpr uint32_t kTimeoutMaxMs = 60000;
constexpr uint32_t kMopMagic = 0x30504f4dU;  // MOP0, little endian.
constexpr uint32_t kMipMagic = 0x3050494dU;  // MIP0, little endian.
constexpr uint32_t kStreamVersion = 1;
constexpr uint32_t kMopType = 2;
constexpr uint32_t kMipType = 1;
constexpr uint32_t kInputBeats = 15;
constexpr uint32_t kOutputBeats = 8;
constexpr uint32_t kSuccess = 0;
constexpr uint32_t kProfileZeroWarning = 0x8000;
constexpr uint32_t kInfoEnvelope = 1u << 0;
constexpr uint32_t kInfoTagReorder = 1u << 1;
constexpr uint32_t kInfoStickyIdleAbort = 1u << 3;
constexpr uint32_t kCoreId = 0x494d4243U;      // IMBC
constexpr uint32_t kCoreVersion = 0x00010000U;
constexpr uint32_t kCoreBusy = 1u << 0;
constexpr uint32_t kCoreError = 1u << 2;
constexpr uint32_t kDmaErrorMask = 0x70;

struct InfoWire {
    uint32_t abi_version;
    uint32_t input_bytes;
    uint32_t output_bytes;
    uint32_t max_tasks;
    uint32_t build_id;
    uint32_t flags;
};

struct BatchWire {
    uint32_t count;
    uint32_t timeout_ms;
    uint32_t flags;
    uint32_t reserved;
    uint64_t input_user_ptr;
    uint64_t output_user_ptr;
};

struct StatusWire {
    uint32_t core_id;
    uint32_t core_version;
    uint32_t build_id;
    uint32_t candidate_status;
    uint32_t candidate_error;
    uint32_t mm2s_dmasr;
    uint32_t s2mm_dmasr;
    int32_t last_error;
    uint32_t recovery_state;
};

static_assert(sizeof(InfoWire) == 24, "IMBC info ABI drift");
static_assert(sizeof(BatchWire) == 32, "IMBC batch ABI drift");
static_assert(sizeof(StatusWire) == 36, "IMBC status ABI drift");

constexpr unsigned long Ioc(unsigned long direction, unsigned long number, unsigned long size) {
    return (direction << 30u) | (static_cast<unsigned long>('I') << 8u) |
           number | (size << 16u);
}
constexpr unsigned long kIocGetInfo = Ioc(2, 0x00, sizeof(InfoWire));
constexpr unsigned long kIocSubmitBatch = Ioc(1, 0x01, sizeof(BatchWire));
constexpr unsigned long kIocAbort = Ioc(0, 0x02, 0);
constexpr unsigned long kIocGetStatus = Ioc(2, 0x04, sizeof(StatusWire));

uint32_t LoadLe32(const uint8_t *p) {
    return static_cast<uint32_t>(p[0]) |
           (static_cast<uint32_t>(p[1]) << 8u) |
           (static_cast<uint32_t>(p[2]) << 16u) |
           (static_cast<uint32_t>(p[3]) << 24u);
}

uint64_t LoadLe64(const uint8_t *p) {
    return static_cast<uint64_t>(LoadLe32(p)) |
           (static_cast<uint64_t>(LoadLe32(p + 4)) << 32u);
}

int32_t Signed32(uint32_t value) {
    int32_t result = 0;
    std::memcpy(&result, &value, sizeof(result));
    return result;
}

uint32_t Crc32(const uint8_t *bytes, size_t length) {
    uint32_t crc = 0xffffffffU;
    for (size_t i = 0; i < length; ++i) {
        crc ^= bytes[i];
        for (unsigned bit = 0; bit < 8; ++bit) {
            crc = (crc >> 1u) ^ ((crc & 1u) ? 0xedb88320U : 0U);
        }
    }
    return ~crc;
}

rnt_code FromIoResult(int result, bool abort_requested) {
    if (result == 0) {
        return abort_requested ? RNT_E_ABORTED : RNT_OK;
    }
    const int error = result < 0 ? -result : result;
    if (abort_requested || error == ECANCELED || error == EINTR) {
        return RNT_E_ABORTED;
    }
    return RNT_E_IO;
}

#if defined(__linux__)
int LinuxOpen(void *, const char *path, int *out_fd) {
    const int fd = ::open(path, O_RDWR | O_CLOEXEC);
    if (fd < 0) {
        return -errno;
    }
    *out_fd = fd;
    return 0;
}

int LinuxClose(void *, int fd) {
    return ::close(fd) == 0 ? 0 : -errno;
}

int LinuxIoctl(void *, int fd, unsigned long request, void *arg, size_t) {
    return ::ioctl(fd, request, arg) == 0 ? 0 : -errno;
}
#endif

rnt_io_vtable DefaultIo() {
#if defined(__linux__)
    return rnt_io_vtable{nullptr, LinuxOpen, LinuxClose, LinuxIoctl};
#else
    return rnt_io_vtable{nullptr, nullptr, nullptr, nullptr};
#endif
}

rnt_code ValidateMrfHeaders(const uint8_t *headers, uint32_t rf_frame_id) {
    for (uint8_t stream = 0; stream < 2; ++stream) {
        const uint8_t *header = headers + static_cast<size_t>(stream) * RNT_PUBLIC_HEADER_BYTES;
        if (std::memcmp(header, "MRF1", 4) != 0 || header[4] != 1 ||
            header[5] != stream || header[6] != 1 || header[7] != 0 ||
            LoadLe32(header + 8) != rf_frame_id || LoadLe32(header + 12) != 128) {
            return RNT_E_PROTOCOL;
        }
        if (LoadLe32(header + 16) != Crc32(header, 16)) {
            // A public MRF header is a pre-submit contract, not received
            // payload data.  Python treats this as a fatal framing error too.
            return RNT_E_PROTOCOL;
        }
    }
    return RNT_OK;
}

rnt_code ValidateTasks(const uint8_t *tasks, const rnt_submit_config &config) {
    std::array<bool, kTaskCount> observed_frame_tags{};
    for (uint32_t index = 0; index < kTaskCount; ++index) {
        const uint8_t *task = tasks + static_cast<size_t>(index) * kInputBytes;
        const uint64_t word0 = LoadLe64(task);
        const uint64_t word1 = LoadLe64(task + 8);
        const uint64_t word2 = LoadLe64(task + 16);
        const uint64_t word3 = LoadLe64(task + 24);
        const uint64_t word4 = LoadLe64(task + 32);
        const uint64_t word5 = LoadLe64(task + 40);
        const uint64_t word6 = LoadLe64(task + 48);
        const uint64_t word7 = LoadLe64(task + 56);
        if (static_cast<uint32_t>(word0) != kMipMagic ||
            ((word0 >> 32u) & 0xffffU) != kStreamVersion ||
            ((word0 >> 48u) & 0xffU) != kMipType || ((word0 >> 56u) & 0xffU) != 8 ||
            (word2 & 0xffU) != 2 || ((word2 >> 8u) & 0xffU) != 2 ||
            ((word2 >> 16u) & 0xffU) != 1 || ((word2 >> 24u) & 0xffU) != 3 ||
            ((word2 >> 48u) & 0xffffU) != 0 ||
            (word3 & 0xffffffffU) == 0 ||
            ((word3 >> 32u) & 0xffffffffU) != 0 ||
            (word4 & 0xffffU) != kInputBeats || ((word4 >> 16u) & 0xffffU) != kOutputBeats ||
            ((word5 >> 32u) != 0) || word7 != 0) {
            return RNT_E_PROTOCOL;
        }
        const uint32_t task_frame = static_cast<uint32_t>(word1);
        const uint32_t task_config = static_cast<uint32_t>(word1 >> 32u);
        const uint32_t ns = static_cast<uint32_t>(word2 >> 32u) & 0xffffU;
        const uint32_t minimum = static_cast<uint32_t>(word4 >> 32u) & 0xffffU;
        const uint32_t maximum = static_cast<uint32_t>(word4 >> 48u) & 0xffffU;
        const int32_t proposal = Signed32(static_cast<uint32_t>(word6 >> 32u));
        if (task_frame == 0 || task_frame > kTaskCount || task_config != config.required_config_id ||
            ns < config.required_iterations || minimum != config.required_iterations ||
            maximum != config.required_iterations || proposal != config.required_proposal_step_raw) {
            return RNT_E_PROTOCOL;
        }
        if (observed_frame_tags[task_frame - 1]) {
            return RNT_E_PROTOCOL;
        }
        observed_frame_tags[task_frame - 1] = true;
    }
    for (bool present : observed_frame_tags) {
        if (!present) {
            return RNT_E_PROTOCOL;
        }
    }
    return RNT_OK;
}

struct ParsedOutput {
    uint32_t frame_tag;
    uint32_t config_tag;
    uint8_t x0;
    uint8_t x1;
    uint32_t actual_iterations;
    uint32_t build_id;
    uint64_t pl_cycles;
    uint32_t overflow_count;
    uint32_t saturation_count;
};

rnt_code ParseOutput(const uint8_t *raw, const rnt_submit_config &config, ParsedOutput *out) {
    const uint64_t word0 = LoadLe64(raw);
    const uint64_t word1 = LoadLe64(raw + 8);
    const uint64_t word2 = LoadLe64(raw + 16);
    const uint64_t word3 = LoadLe64(raw + 24);
    const uint64_t word5 = LoadLe64(raw + 40);
    const uint64_t word6 = LoadLe64(raw + 48);
    const uint64_t word7 = LoadLe64(raw + 56);
    if (static_cast<uint32_t>(word0) != kMopMagic ||
        ((word0 >> 32u) & 0xffffU) != kStreamVersion ||
        ((word0 >> 48u) & 0xffU) != kMopType || ((word0 >> 56u) & 0xffU) != kOutputBeats ||
        (word2 & 0xffffU) != kSuccess || ((word2 >> 16u) & 0xffffU) != kProfileZeroWarning ||
        (word3 >> 16u & 0xffffU) != 0 || static_cast<uint32_t>(word7) != 0) {
        return RNT_E_PROTOCOL;
    }
    const uint32_t actual_iterations = static_cast<uint32_t>(word2 >> 32u);
    const uint8_t x0 = static_cast<uint8_t>(word3);
    const uint8_t x1 = static_cast<uint8_t>(word3 >> 8u);
    const uint32_t build_id = static_cast<uint32_t>(word7 >> 32u);
    if (actual_iterations != config.required_iterations || x0 >= 4 || x1 >= 4 ||
        build_id != config.expected_build_id) {
        return RNT_E_PROTOCOL;
    }
    *out = ParsedOutput{static_cast<uint32_t>(word1), static_cast<uint32_t>(word1 >> 32u),
                        x0, x1, actual_iterations, build_id, word5,
                        static_cast<uint32_t>(word6), static_cast<uint32_t>(word6 >> 32u)};
    return RNT_OK;
}

rnt_code BuildAndVerifyPackets(const uint8_t *headers,
                               const std::array<ParsedOutput, kTaskCount> &ordered,
                               rnt_frame_result *result) {
    std::array<uint8_t, RNT_PACKETS_BYTES> packets{};
    std::memcpy(packets.data(), headers, RNT_PUBLIC_HEADER_BYTES);
    std::memcpy(packets.data() + RNT_PACKET_BYTES,
                headers + RNT_PUBLIC_HEADER_BYTES, RNT_PUBLIC_HEADER_BYTES);
    for (uint32_t index = 0; index < kTaskCount; ++index) {
        const size_t byte_offset = RNT_PUBLIC_HEADER_BYTES + index / 4u;
        const unsigned shift = 6u - 2u * (index % 4u);
        packets[byte_offset] |= static_cast<uint8_t>(ordered[index].x0 << shift);
        packets[RNT_PACKET_BYTES + byte_offset] |= static_cast<uint8_t>(ordered[index].x1 << shift);
    }
    for (uint32_t stream = 0; stream < 2; ++stream) {
        const uint8_t *packet = packets.data() + static_cast<size_t>(stream) * RNT_PACKET_BYTES;
        if (LoadLe32(packet + 148) != Crc32(packet + 20, 128)) {
            return RNT_E_CRC_REJECTED;
        }
    }
    std::memcpy(result->packets, packets.data(), packets.size());
    return RNT_OK;
}

}  // namespace

struct rnt_transport {
    rnt_io_vtable io{};
    int fd = -1;
    InfoWire info{};
    std::mutex lifecycle_mutex;
    std::condition_variable lifecycle_cv;
    uint32_t inflight_ioctls = 0;
    bool closing = false;
    std::mutex submit_mutex;
    std::atomic<bool> abort_requested{false};
    std::vector<uint8_t> output_buffer;
};

namespace {

class IoLease {
  public:
    explicit IoLease(rnt_transport *transport) : transport_(transport) {
        std::lock_guard<std::mutex> lock(transport_->lifecycle_mutex);
        if (!transport_->closing && transport_->fd >= 0) {
            fd_ = transport_->fd;
            ++transport_->inflight_ioctls;
        }
    }

    ~IoLease() {
        if (fd_ >= 0) {
            std::lock_guard<std::mutex> lock(transport_->lifecycle_mutex);
            --transport_->inflight_ioctls;
            transport_->lifecycle_cv.notify_all();
        }
    }

    bool valid() const { return fd_ >= 0; }
    int Call(unsigned long command, void *arg, size_t arg_size) const {
        return transport_->io.ioctl(transport_->io.context, fd_, command, arg, arg_size);
    }

  private:
    rnt_transport *transport_;
    int fd_ = -1;
};

rnt_code ValidateInfo(const InfoWire &info) {
    if (info.abi_version != kAbiVersion || info.input_bytes != kInputBytes ||
        info.output_bytes != kOutputBytes || info.max_tasks != kTaskCount ||
        (info.flags & (kInfoEnvelope | kInfoTagReorder | kInfoStickyIdleAbort)) !=
            (kInfoEnvelope | kInfoTagReorder | kInfoStickyIdleAbort)) {
        return RNT_E_DEVICE;
    }
    return RNT_OK;
}

rnt_code CheckStatusWire(const StatusWire &status, uint32_t build_id) {
    const bool healthy = status.core_id == kCoreId && status.core_version == kCoreVersion &&
                         status.build_id == build_id &&
                         (status.candidate_status & (kCoreBusy | kCoreError)) == 0 &&
                         status.candidate_error == 0 &&
                         ((status.mm2s_dmasr | status.s2mm_dmasr) & kDmaErrorMask) == 0 &&
                         status.last_error == 0 && status.recovery_state != 2;
    return healthy ? RNT_OK : RNT_E_DEVICE;
}

rnt_code GetStatus(rnt_transport *transport) {
    IoLease lease(transport);
    if (!lease.valid()) {
        return RNT_E_STATE;
    }
    StatusWire status{};
    const int io_result = lease.Call(kIocGetStatus, &status, sizeof(status));
    const rnt_code io_code = FromIoResult(io_result, false);
    if (io_code != RNT_OK) {
        return io_code;
    }
    return CheckStatusWire(status, transport->info.build_id);
}

}  // namespace

extern "C" rnt_code rnt_open(const char *device_path, const rnt_io_vtable *injected_io,
                              rnt_transport **out_transport) {
    if (device_path == nullptr || out_transport == nullptr) {
        return RNT_E_ARGUMENT;
    }
    *out_transport = nullptr;
    rnt_io_vtable io = injected_io != nullptr ? *injected_io : DefaultIo();
    if (io.open == nullptr || io.close == nullptr || io.ioctl == nullptr) {
        return RNT_E_IO;
    }
    rnt_transport *transport = new (std::nothrow) rnt_transport();
    if (transport == nullptr) {
        return RNT_E_INTERNAL;
    }
    transport->io = io;
    int fd = -1;
    if (io.open(io.context, device_path, &fd) != 0 || fd < 0) {
        delete transport;
        return RNT_E_IO;
    }
    transport->fd = fd;
    rnt_code open_code = RNT_OK;
    {
        IoLease lease(transport);
        if (!lease.valid()) {
            open_code = RNT_E_STATE;
        } else {
            open_code = FromIoResult(lease.Call(kIocGetInfo, &transport->info, sizeof(transport->info)), false);
            if (open_code == RNT_OK) {
                open_code = ValidateInfo(transport->info);
            }
        }
    }
    if (open_code != RNT_OK) {
        io.close(io.context, fd);
        delete transport;
        return open_code;
    }
    const rnt_code status_code = GetStatus(transport);
    if (status_code != RNT_OK) {
        io.close(io.context, fd);
        delete transport;
        return status_code;
    }
    try {
        transport->output_buffer.assign(static_cast<size_t>(kTaskCount) * kOutputBytes, 0);
    } catch (const std::bad_alloc &) {
        io.close(io.context, fd);
        delete transport;
        return RNT_E_INTERNAL;
    }
    *out_transport = transport;
    return RNT_OK;
}

extern "C" rnt_code rnt_status(rnt_transport *transport) {
    return transport == nullptr ? RNT_E_ARGUMENT : GetStatus(transport);
}

extern "C" rnt_code rnt_submit_mrf_frame(rnt_transport *transport,
                                           const uint8_t *task_bytes, size_t task_bytes_len,
                                           const uint8_t *public_headers, size_t public_headers_len,
                                           const rnt_submit_config *config,
                                           rnt_frame_result *out_result) {
    if (transport == nullptr || task_bytes == nullptr || public_headers == nullptr ||
        config == nullptr || out_result == nullptr || task_bytes_len != RNT_TASK_BUFFER_BYTES ||
        public_headers_len != RNT_PUBLIC_HEADERS_BYTES || config->timeout_ms == 0 ||
        config->timeout_ms > kTimeoutMaxMs || config->required_config_id == 0 ||
        config->required_iterations == 0 || config->expected_build_id == 0) {
        return RNT_E_ARGUMENT;
    }
    std::memset(out_result, 0, sizeof(*out_result));
    // This is only a fast user-space refusal.  The required sticky-idle-ABORT
    // capability is what closes the check-to-SUBMIT ioctl race in the driver.
    if (transport->abort_requested.load(std::memory_order_acquire)) {
        return RNT_E_ABORTED;
    }
    if (config->expected_build_id != transport->info.build_id) {
        return RNT_E_DEVICE;
    }
    const rnt_code header_code = ValidateMrfHeaders(public_headers, config->rf_frame_id);
    if (header_code != RNT_OK) {
        return header_code;
    }
    const rnt_code task_code = ValidateTasks(task_bytes, *config);
    if (task_code != RNT_OK) {
        return task_code;
    }
    std::unique_lock<std::mutex> submit_lock(transport->submit_mutex, std::try_to_lock);
    if (!submit_lock.owns_lock()) {
        return RNT_E_BUSY;
    }
    if (transport->abort_requested.load(std::memory_order_acquire)) {
        return RNT_E_ABORTED;
    }
    const rnt_code before = GetStatus(transport);
    if (before != RNT_OK) {
        return before;
    }
    if (transport->abort_requested.load(std::memory_order_acquire)) {
        return RNT_E_ABORTED;
    }
    BatchWire batch{};
    batch.count = kTaskCount;
    batch.timeout_ms = config->timeout_ms;
    batch.input_user_ptr = static_cast<uint64_t>(reinterpret_cast<uintptr_t>(task_bytes));
    std::memset(transport->output_buffer.data(), 0, transport->output_buffer.size());
    batch.output_user_ptr = static_cast<uint64_t>(reinterpret_cast<uintptr_t>(transport->output_buffer.data()));
    {
        IoLease lease(transport);
        if (!lease.valid()) {
            return RNT_E_STATE;
        }
        const rnt_code code = FromIoResult(lease.Call(kIocSubmitBatch, &batch, sizeof(batch)),
                                           transport->abort_requested.load(std::memory_order_acquire));
        if (code != RNT_OK) {
            return code;
        }
    }
    if (transport->abort_requested.load(std::memory_order_acquire)) {
        return RNT_E_ABORTED;
    }
    std::array<ParsedOutput, kTaskCount> ordered{};
    std::array<bool, kTaskCount> observed{};
    uint64_t total_cycles = 0;
    uint64_t total_overflow = 0;
    uint64_t total_saturation = 0;
    for (uint32_t index = 0; index < kTaskCount; ++index) {
        ParsedOutput parsed{};
        const rnt_code code = ParseOutput(transport->output_buffer.data() + static_cast<size_t>(index) * kOutputBytes,
                                          *config, &parsed);
        if (code != RNT_OK) {
            return code;
        }
        if (parsed.frame_tag == 0 || parsed.frame_tag > kTaskCount ||
            parsed.config_tag != config->required_config_id) {
            return RNT_E_PROTOCOL;
        }
        // Per-symbol MIP0 frame_tag is the canonical 1..528 QPSK position.
        // Therefore arbitrary input and arbitrary driver output permutations
        // reconstruct exactly the same MRF payload ordering.
        const uint32_t symbol_position = parsed.frame_tag - 1;
        if (observed[symbol_position]) {
            return RNT_E_PROTOCOL;
        }
        observed[symbol_position] = true;
        ordered[symbol_position] = parsed;
        if (std::numeric_limits<uint64_t>::max() - total_cycles < parsed.pl_cycles ||
            std::numeric_limits<uint64_t>::max() - total_overflow < parsed.overflow_count ||
            std::numeric_limits<uint64_t>::max() - total_saturation < parsed.saturation_count) {
            return RNT_E_PROTOCOL;
        }
        total_cycles += parsed.pl_cycles;
        total_overflow += parsed.overflow_count;
        total_saturation += parsed.saturation_count;
    }
    for (bool seen : observed) {
        if (!seen) {
            return RNT_E_PROTOCOL;
        }
    }
    const rnt_code after = GetStatus(transport);
    if (after != RNT_OK) {
        return after;
    }
    if (transport->abort_requested.load(std::memory_order_acquire)) {
        return RNT_E_ABORTED;
    }
    const rnt_code packet_code = BuildAndVerifyPackets(public_headers, ordered, out_result);
    if (packet_code != RNT_OK) {
        std::memset(out_result, 0, sizeof(*out_result));
        return packet_code;
    }
    out_result->metadata.build_id = config->expected_build_id;
    out_result->metadata.actual_iterations = config->required_iterations;
    out_result->metadata.result_count = kTaskCount;
    out_result->metadata.total_pl_cycles = total_cycles;
    out_result->metadata.total_overflow_count = total_overflow;
    out_result->metadata.total_saturation_count = total_saturation;
    return RNT_OK;
}

extern "C" rnt_code rnt_abort(rnt_transport *transport) {
    if (transport == nullptr) {
        return RNT_E_ARGUMENT;
    }
    transport->abort_requested.store(true, std::memory_order_release);
    IoLease lease(transport);
    if (!lease.valid()) {
        return RNT_E_STATE;
    }
    const int io_result = lease.Call(kIocAbort, nullptr, 0);
    if (io_result == 0) {
        return RNT_OK;
    }
    const int error = io_result < 0 ? -io_result : io_result;
    return error == ECANCELED || error == EINTR ? RNT_E_ABORTED : RNT_E_IO;
}

extern "C" rnt_code rnt_close(rnt_transport *transport) {
    if (transport == nullptr) {
        return RNT_E_ARGUMENT;
    }
    int fd = -1;
    {
        std::unique_lock<std::mutex> lock(transport->lifecycle_mutex);
        if (transport->fd < 0) {
            return RNT_OK;
        }
        transport->closing = true;
        transport->lifecycle_cv.wait(lock, [transport] { return transport->inflight_ioctls == 0; });
        fd = transport->fd;
        transport->fd = -1;
    }
    return transport->io.close(transport->io.context, fd) == 0 ? RNT_OK : RNT_E_IO;
}

extern "C" void rnt_destroy(rnt_transport *transport) {
    if (transport != nullptr) {
        (void)rnt_close(transport);
        delete transport;
    }
}

extern "C" const char *rnt_code_string(rnt_code code) {
    switch (code) {
        case RNT_OK: return "ok";
        case RNT_E_ARGUMENT: return "argument";
        case RNT_E_STATE: return "state";
        case RNT_E_BUSY: return "busy";
        case RNT_E_ABORTED: return "aborted";
        case RNT_E_IO: return "io";
        case RNT_E_PROTOCOL: return "protocol";
        case RNT_E_DEVICE: return "device";
        case RNT_E_CRC_REJECTED: return "crc-rejected";
        case RNT_E_INTERNAL: return "internal";
        default: return "unknown";
    }
}
