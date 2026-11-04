#include "rf_native_transport.h"

#include <array>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <mutex>
#include <thread>
#include <vector>

namespace {

constexpr uint32_t kBuildId = 0x7a31c204U;
constexpr uint32_t kConfig = 2;
constexpr uint32_t kIterations = 64;
constexpr int32_t kProposal = 32768;
constexpr uint32_t kMopMagic = 0x30504f4dU;
constexpr uint32_t kMipMagic = 0x3050494dU;
constexpr uint32_t kInfoRequiredFlags = 0x0bU; // envelope, tag reorder, sticky idle abort
using TaskBuffer = std::array<uint8_t, RNT_TASK_BUFFER_BYTES>;

struct InfoWire { uint32_t abi_version, input_bytes, output_bytes, max_tasks, build_id, flags; };
struct BatchWire { uint32_t count, timeout_ms, flags, reserved; uint64_t input_user_ptr, output_user_ptr; };
struct StatusWire { uint32_t core_id, core_version, build_id, candidate_status, candidate_error, mm2s_dmasr, s2mm_dmasr; int32_t last_error; uint32_t recovery_state; };
static_assert(sizeof(BatchWire) == 32, "test UAPI layout");

constexpr unsigned long Ioc(unsigned long direction, unsigned long number, unsigned long size) {
    return (direction << 30u) | (static_cast<unsigned long>('I') << 8u) | number | (size << 16u);
}
constexpr unsigned long kIocInfo = Ioc(2, 0, sizeof(InfoWire));
constexpr unsigned long kIocSubmit = Ioc(1, 1, sizeof(BatchWire));
constexpr unsigned long kIocAbort = Ioc(0, 2, 0);
constexpr unsigned long kIocStatus = Ioc(2, 4, sizeof(StatusWire));

void Store32(uint8_t *p, uint32_t v) {
    p[0] = static_cast<uint8_t>(v); p[1] = static_cast<uint8_t>(v >> 8u);
    p[2] = static_cast<uint8_t>(v >> 16u); p[3] = static_cast<uint8_t>(v >> 24u);
}
void Store64(uint8_t *p, uint64_t v) { Store32(p, static_cast<uint32_t>(v)); Store32(p + 4, static_cast<uint32_t>(v >> 32u)); }
uint32_t Crc32(const uint8_t *bytes, size_t size) {
    uint32_t crc = 0xffffffffU;
    for (size_t i = 0; i < size; ++i) {
        crc ^= bytes[i];
        for (unsigned bit = 0; bit < 8; ++bit) crc = (crc >> 1u) ^ ((crc & 1u) ? 0xedb88320U : 0U);
    }
    return ~crc;
}

TaskBuffer MakeTasks(const std::array<uint32_t, RNT_TASK_COUNT> *physical_order = nullptr) {
    TaskBuffer tasks{};
    for (uint32_t i = 0; i < RNT_TASK_COUNT; ++i) {
        const uint32_t tag = physical_order == nullptr ? i + 1u : (*physical_order)[i];
        uint8_t *p = tasks.data() + static_cast<size_t>(i) * RNT_TASK_BYTES;
        Store64(p, kMipMagic | (uint64_t{1} << 32u) | (uint64_t{1} << 48u) | (uint64_t{8} << 56u));
        Store64(p + 8, static_cast<uint64_t>(tag) | (uint64_t{kConfig} << 32u));
        Store64(p + 16, uint64_t{2} | (uint64_t{2} << 8u) | (uint64_t{1} << 16u) |
                           (uint64_t{3} << 24u) | (uint64_t{kIterations} << 32u));
        Store64(p + 24, tag | 1u); // nonzero deterministic seed
        Store64(p + 32, uint64_t{15} | (uint64_t{8} << 16u) |
                           (uint64_t{kIterations} << 32u) | (uint64_t{kIterations} << 48u));
        Store64(p + 48, uint64_t{static_cast<uint32_t>(kProposal)} << 32u);
    }
    return tasks;
}

std::array<uint8_t, RNT_PUBLIC_HEADERS_BYTES> MakeHeaders(uint32_t rf_frame_id) {
    std::array<uint8_t, RNT_PUBLIC_HEADERS_BYTES> headers{};
    for (uint8_t stream = 0; stream < 2; ++stream) {
        uint8_t *h = headers.data() + stream * RNT_PUBLIC_HEADER_BYTES;
        std::memcpy(h, "MRF1", 4); h[4] = 1; h[5] = stream; h[6] = 1;
        Store32(h + 8, rf_frame_id); Store32(h + 12, 128); Store32(h + 16, Crc32(h, 16));
    }
    return headers;
}

std::array<uint8_t, RNT_PACKETS_BYTES> MakeExpectedPackets(const std::array<uint8_t, RNT_PUBLIC_HEADERS_BYTES> &headers) {
    std::array<uint8_t, RNT_PACKETS_BYTES> packets{};
    std::memcpy(packets.data(), headers.data(), RNT_PUBLIC_HEADER_BYTES);
    std::memcpy(packets.data() + RNT_PACKET_BYTES, headers.data() + RNT_PUBLIC_HEADER_BYTES, RNT_PUBLIC_HEADER_BYTES);
    for (uint32_t stream = 0; stream < 2; ++stream) {
        uint8_t *p = packets.data() + stream * RNT_PACKET_BYTES;
        for (uint32_t i = 0; i < 128; ++i) p[20 + i] = static_cast<uint8_t>((i * 13u + stream * 31u) & 0xffu);
        Store32(p + 148, Crc32(p + 20, 128));
    }
    return packets;
}

uint8_t Symbol(const std::array<uint8_t, RNT_PACKETS_BYTES> &packets, uint32_t stream, uint32_t index) {
    const uint8_t value = packets[stream * RNT_PACKET_BYTES + 20 + index / 4u];
    return static_cast<uint8_t>((value >> (6u - 2u * (index % 4u))) & 3u);
}

void MakeOutput(uint8_t *p, uint32_t tag, uint8_t x0, uint8_t x1) {
    std::memset(p, 0, RNT_RESULT_BYTES);
    Store64(p, kMopMagic | (uint64_t{1} << 32u) | (uint64_t{2} << 48u) | (uint64_t{8} << 56u));
    Store64(p + 8, static_cast<uint64_t>(tag) | (uint64_t{kConfig} << 32u));
    Store64(p + 16, uint64_t{0} | (uint64_t{0x8000} << 16u) | (uint64_t{kIterations} << 32u));
    Store64(p + 24, static_cast<uint64_t>(x0) | (uint64_t{x1} << 8u));
    Store64(p + 40, tag); // exact total is checked in success test.
    Store64(p + 48, uint64_t{3} | (uint64_t{5} << 32u)); // nonfatal diagnostics
    Store64(p + 56, uint64_t{kBuildId} << 32u);
}

enum class Mode { kSuccess, kShortOutput, kWrongTag, kDuplicateTag, kInvalidMop, kCorruptDecision, kBlocking };

struct FakeDevice {
    Mode mode = Mode::kSuccess;
    std::array<uint8_t, RNT_PACKETS_BYTES> expected{};
    std::mutex mutex;
    std::condition_variable cv;
    bool abort_seen = false;
    bool missing_sticky_cap = false;
    bool block_next_status = false;
    bool status_blocked = false;
    bool release_status = false;
    std::atomic<bool> sticky_abort{false};
    std::atomic<unsigned> submits{0};

    static int Open(void *, const char *, int *fd) { *fd = 7; return 0; }
    static int Close(void *, int) { return 0; }
    static int Ioctl(void *context, int, unsigned long request, void *arg, size_t) {
        return static_cast<FakeDevice *>(context)->IoctlImpl(request, arg);
    }

    int IoctlImpl(unsigned long request, void *arg) {
        if (request == kIocInfo) {
            const uint32_t flags = missing_sticky_cap ? 3U : kInfoRequiredFlags;
            *static_cast<InfoWire *>(arg) = InfoWire{1, RNT_TASK_BYTES, RNT_RESULT_BYTES, RNT_TASK_COUNT, kBuildId, flags};
            return 0;
        }
        if (request == kIocStatus) {
            std::unique_lock<std::mutex> lock(mutex);
            if (block_next_status) {
                block_next_status = false;
                status_blocked = true;
                cv.notify_all();
                cv.wait(lock, [this] { return release_status; });
            }
            *static_cast<StatusWire *>(arg) = StatusWire{0x494d4243U, 0x00010000U, kBuildId, 0, 0, 0, 0, 0, 0};
            return 0;
        }
        if (request == kIocAbort) {
            sticky_abort.store(true, std::memory_order_release);
            std::lock_guard<std::mutex> lock(mutex);
            abort_seen = true;
            cv.notify_all();
            return 0;
        }
        if (request != kIocSubmit) return -22;
        submits.fetch_add(1, std::memory_order_release);
        if (sticky_abort.load(std::memory_order_acquire)) return -125; // driver preflight gate
        auto *batch = static_cast<BatchWire *>(arg);
        if (batch->count != RNT_TASK_COUNT || batch->flags != 0 || batch->reserved != 0) return -22;
        if (mode == Mode::kBlocking) {
            std::unique_lock<std::mutex> lock(mutex);
            cv.wait(lock, [this] { return abort_seen; });
            return -125; // ECANCELED
        }
        auto *output = reinterpret_cast<uint8_t *>(static_cast<uintptr_t>(batch->output_user_ptr));
        const uint32_t output_count = mode == Mode::kShortOutput
                                          ? static_cast<uint32_t>(RNT_TASK_COUNT - 1u)
                                          : static_cast<uint32_t>(RNT_TASK_COUNT);
        for (uint32_t slot = 0; slot < output_count; ++slot) {
            const uint32_t index = RNT_TASK_COUNT - 1u - slot; // prove output-tag reorder.
            uint32_t tag = index + 1u;
            if (mode == Mode::kWrongTag && slot == 0) tag = RNT_TASK_COUNT + 1u;
            if (mode == Mode::kDuplicateTag && slot == 0) tag = 2;
            uint8_t x0 = Symbol(expected, 0, index);
            uint8_t x1 = Symbol(expected, 1, index);
            if (mode == Mode::kCorruptDecision && index == 0) x0 ^= 1u;
            MakeOutput(output + static_cast<size_t>(slot) * RNT_RESULT_BYTES, tag, x0, x1);
            if (mode == Mode::kInvalidMop && slot == 0) output[0] = 0;
        }
        return 0;
    }
};

rnt_io_vtable Vtable(FakeDevice *fake) { return rnt_io_vtable{fake, FakeDevice::Open, FakeDevice::Close, FakeDevice::Ioctl}; }

rnt_submit_config Config() { return rnt_submit_config{0x10203040U, kBuildId, 1000, kConfig, kIterations, kProposal}; }

bool Check(bool condition, const char *message) {
    if (!condition) std::fprintf(stderr, "FAIL: %s\n", message);
    return condition;
}

bool RunMode(Mode mode, rnt_code expected_code, const TaskBuffer *task_override = nullptr) {
    const TaskBuffer default_tasks = MakeTasks();
    const TaskBuffer &tasks = task_override == nullptr ? default_tasks : *task_override;
    const rnt_submit_config config = Config();
    const auto headers = MakeHeaders(config.rf_frame_id);
    FakeDevice fake{};
    fake.mode = mode;
    fake.expected = MakeExpectedPackets(headers);
    rnt_transport *transport = nullptr;
    const rnt_io_vtable io = Vtable(&fake);
    if (!Check(rnt_open("fake", &io, &transport) == RNT_OK, "open fake")) return false;
    rnt_frame_result result{};
    const rnt_code code = rnt_submit_mrf_frame(transport, tasks.data(), tasks.size(), headers.data(), headers.size(), &config, &result);
    bool ok = Check(code == expected_code, "expected submit outcome");
    if (code == RNT_OK) {
        ok &= Check(std::memcmp(result.packets, fake.expected.data(), fake.expected.size()) == 0, "reordered packets exact");
        ok &= Check(result.metadata.result_count == RNT_TASK_COUNT && result.metadata.actual_iterations == kIterations, "metadata");
        ok &= Check(result.metadata.total_pl_cycles == (static_cast<uint64_t>(RNT_TASK_COUNT) * (RNT_TASK_COUNT + 1u)) / 2u, "cycle aggregation");
        ok &= Check(result.metadata.total_overflow_count == static_cast<uint64_t>(RNT_TASK_COUNT) * 3u &&
                    result.metadata.total_saturation_count == static_cast<uint64_t>(RNT_TASK_COUNT) * 5u,
                    "nonfatal MOP0 diagnostic aggregation");
    } else {
        std::array<uint8_t, sizeof(result)> zeros{};
        ok &= Check(std::memcmp(&result, zeros.data(), sizeof(result)) == 0, "failure returns no packet prefix");
    }
    rnt_destroy(transport);
    return ok;
}

bool TestHeaderCrc() {
    const auto tasks = MakeTasks();
    const rnt_submit_config config = Config();
    auto headers = MakeHeaders(config.rf_frame_id);
    headers[16] ^= 1;
    FakeDevice fake{};
    rnt_transport *transport = nullptr;
    const rnt_io_vtable io = Vtable(&fake);
    if (!Check(rnt_open("fake", &io, &transport) == RNT_OK, "open for header CRC")) return false;
    rnt_frame_result result{};
    const rnt_code code = rnt_submit_mrf_frame(transport, tasks.data(), tasks.size(), headers.data(), headers.size(), &config, &result);
    rnt_destroy(transport);
    return Check(code == RNT_E_PROTOCOL && fake.submits.load(std::memory_order_acquire) == 0, "header contract rejects before DMA");
}

bool TestMissingStickyAbortCapability() {
    FakeDevice fake{};
    fake.missing_sticky_cap = true;
    rnt_transport *transport = nullptr;
    const rnt_io_vtable io = Vtable(&fake);
    const rnt_code code = rnt_open("fake", &io, &transport);
    if (transport != nullptr) rnt_destroy(transport);
    return Check(code == RNT_E_DEVICE, "open rejects driver without sticky idle-abort capability");
}

bool TestIdleAbortIsTerminal() {
    const auto tasks = MakeTasks();
    const rnt_submit_config config = Config();
    const auto headers = MakeHeaders(config.rf_frame_id);
    FakeDevice fake{};
    rnt_transport *transport = nullptr;
    const rnt_io_vtable io = Vtable(&fake);
    if (!Check(rnt_open("fake", &io, &transport) == RNT_OK, "open for idle abort")) return false;
    rnt_frame_result result{};
    bool ok = Check(rnt_abort(transport) == RNT_OK, "idle abort reaches sticky driver gate");
    ok &= Check(rnt_submit_mrf_frame(transport, tasks.data(), tasks.size(), headers.data(), headers.size(), &config, &result) == RNT_E_ABORTED,
                "cancelled transport never starts a later submission");
    ok &= Check(fake.submits.load(std::memory_order_acquire) == 0, "idle cancel refused before submit ioctl");
    rnt_destroy(transport);
    return ok;
}

bool TestDriverStickyPreSubmitGate() {
    const auto tasks = MakeTasks();
    const rnt_submit_config config = Config();
    const auto headers = MakeHeaders(config.rf_frame_id);
    FakeDevice fake{};
    // Model a sticky owner cancel that was linearized in the driver before
    // SUBMIT's event-lock preflight.  No userspace atomic can close this gap.
    fake.sticky_abort.store(true, std::memory_order_release);
    rnt_transport *transport = nullptr;
    const rnt_io_vtable io = Vtable(&fake);
    if (!Check(rnt_open("fake", &io, &transport) == RNT_OK, "open for driver pre-submit gate")) return false;
    rnt_frame_result result{};
    const rnt_code code = rnt_submit_mrf_frame(transport, tasks.data(), tasks.size(), headers.data(), headers.size(), &config, &result);
    const bool ok = Check(code == RNT_E_ABORTED && fake.submits.load(std::memory_order_acquire) == 1,
                          "sticky driver gate cancels a submit entering the kernel");
    rnt_destroy(transport);
    return ok;
}

bool TestPreSubmitCancel() {
    const auto tasks = MakeTasks();
    const rnt_submit_config config = Config();
    const auto headers = MakeHeaders(config.rf_frame_id);
    FakeDevice fake{};
    rnt_transport *transport = nullptr;
    const rnt_io_vtable io = Vtable(&fake);
    if (!Check(rnt_open("fake", &io, &transport) == RNT_OK, "open for pre-submit cancel")) return false;
    { std::lock_guard<std::mutex> lock(fake.mutex); fake.block_next_status = true; }
    rnt_frame_result result{};
    rnt_code submit_code = RNT_OK;
    std::thread worker([&] {
        submit_code = rnt_submit_mrf_frame(transport, tasks.data(), tasks.size(), headers.data(), headers.size(), &config, &result);
    });
    bool blocked = false;
    for (unsigned i = 0; i < 100 && !blocked; ++i) {
        { std::lock_guard<std::mutex> lock(fake.mutex); blocked = fake.status_blocked; }
        if (!blocked) std::this_thread::sleep_for(std::chrono::milliseconds(2));
    }
    bool ok = Check(blocked, "preflight status reached");
    ok &= Check(rnt_abort(transport) == RNT_OK, "pre-submit abort accepted");
    { std::lock_guard<std::mutex> lock(fake.mutex); fake.release_status = true; fake.cv.notify_all(); }
    worker.join();
    ok &= Check(submit_code == RNT_E_ABORTED, "cancel after status prevents user submit");
    ok &= Check(fake.submits.load(std::memory_order_acquire) == 0, "no submit ioctl after pre-submit cancel");
    rnt_destroy(transport);
    return ok;
}

bool TestArbitraryTaskAndOutputPermutation() {
    std::array<uint32_t, RNT_TASK_COUNT> input_order{};
    for (uint32_t index = 0; index < RNT_TASK_COUNT; ++index) {
        input_order[index] = (index * 73u) % RNT_TASK_COUNT + 1u;
    }
    const TaskBuffer tasks = MakeTasks(&input_order);
    return RunMode(Mode::kSuccess, RNT_OK, &tasks);
}

bool TestConcurrentAbort() {
    const auto tasks = MakeTasks();
    const rnt_submit_config config = Config();
    const auto headers = MakeHeaders(config.rf_frame_id);
    FakeDevice fake{};
    fake.mode = Mode::kBlocking;
    rnt_transport *transport = nullptr;
    const rnt_io_vtable io = Vtable(&fake);
    if (!Check(rnt_open("fake", &io, &transport) == RNT_OK, "open for abort")) return false;
    rnt_frame_result result{};
    rnt_code submit_code = RNT_OK;
    std::thread worker([&] {
        submit_code = rnt_submit_mrf_frame(transport, tasks.data(), tasks.size(), headers.data(), headers.size(), &config, &result);
    });
    bool submitted = false;
    for (unsigned i = 0; i < 100 && !submitted; ++i) {
        submitted = fake.submits.load(std::memory_order_acquire) != 0;
        if (!submitted) std::this_thread::sleep_for(std::chrono::milliseconds(2));
    }
    bool ok = Check(submitted, "blocking submit entered ioctl");
    ok &= Check(rnt_abort(transport) == RNT_OK, "abort does not wait for submit lock");
    worker.join();
    ok &= Check(submit_code == RNT_E_ABORTED, "submit wakes as aborted");
    rnt_destroy(transport);
    return ok;
}

}  // namespace

int main() {
    bool ok = true;
    ok &= RunMode(Mode::kSuccess, RNT_OK);
    ok &= RunMode(Mode::kShortOutput, RNT_E_PROTOCOL);
    ok &= RunMode(Mode::kWrongTag, RNT_E_PROTOCOL);
    ok &= RunMode(Mode::kDuplicateTag, RNT_E_PROTOCOL);
    ok &= RunMode(Mode::kInvalidMop, RNT_E_PROTOCOL);
    ok &= RunMode(Mode::kCorruptDecision, RNT_E_CRC_REJECTED);
    ok &= TestHeaderCrc();
    ok &= TestMissingStickyAbortCapability();
    ok &= TestIdleAbortIsTerminal();
    ok &= TestDriverStickyPreSubmitGate();
    ok &= TestPreSubmitCancel();
    ok &= TestArbitraryTaskAndOutputPermutation();
    ok &= TestConcurrentAbort();
    std::fprintf(stdout, ok ? "rf_native_transport tests: PASS\n" : "rf_native_transport tests: FAIL\n");
    return ok ? 0 : 1;
}
