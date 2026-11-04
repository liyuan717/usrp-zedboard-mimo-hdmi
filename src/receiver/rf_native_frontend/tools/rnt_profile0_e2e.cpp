#include "rf_native_transport.h"

#include <array>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <iostream>
#include <iterator>
#include <vector>

namespace {
constexpr uint32_t kBuildId = 0x7a31c204U;
constexpr uint32_t kTaskCount = 528U;
constexpr size_t kMopFrameBytes = kTaskCount * RNT_RESULT_BYTES;
constexpr size_t kMipFrameBytes = 40U + kTaskCount * RNT_TASK_BYTES;
struct InfoWire { uint32_t abi_version, input_bytes, output_bytes, max_tasks, build_id, flags; };
struct BatchWire { uint32_t count, timeout_ms, flags, reserved; uint64_t input_user_ptr, output_user_ptr; };
struct StatusWire { uint32_t core_id, core_version, build_id, candidate_status, candidate_error, mm2s_dmasr, s2mm_dmasr; int32_t last_error; uint32_t recovery_state; };
constexpr unsigned long ioc(unsigned long direction, unsigned long number, unsigned long size) {
  return (direction << 30U) | (static_cast<unsigned long>('I') << 8U) | number | (size << 16U);
}
constexpr unsigned long kInfo = ioc(2, 0, sizeof(InfoWire));
constexpr unsigned long kSubmit = ioc(1, 1, sizeof(BatchWire));
constexpr unsigned long kStatus = ioc(2, 4, sizeof(StatusWire));
uint32_t crc32(const uint8_t *data, size_t count) {
  uint32_t crc = 0xffffffffU;
  for (size_t i=0;i<count;++i) { crc ^= data[i]; for(unsigned b=0;b<8;++b) crc=(crc>>1U)^((crc&1U)?0xedb88320U:0U); }
  return ~crc;
}
struct FakeImbc {
  const uint8_t *mop = nullptr;
  static int Open(void *, const char *, int *fd) { *fd = 9; return 0; }
  static int Close(void *, int) { return 0; }
  static int Ioctl(void *context, int, unsigned long request, void *arg, size_t) {
    return static_cast<FakeImbc *>(context)->call(request, arg);
  }
  int call(unsigned long request, void *arg) const {
    if (request == kInfo) { *static_cast<InfoWire *>(arg) = {1U, RNT_TASK_BYTES, RNT_RESULT_BYTES, kTaskCount, kBuildId, 0x0bU}; return 0; }
    if (request == kStatus) { *static_cast<StatusWire *>(arg) = {0x494d4243U, 0x00010000U, kBuildId, 0, 0, 0, 0, 0, 0}; return 0; }
    if (request != kSubmit || mop == nullptr) return -22;
    const auto *batch = static_cast<const BatchWire *>(arg);
    if (batch->count != kTaskCount || batch->flags || batch->reserved) return -22;
    std::memcpy(reinterpret_cast<void *>(static_cast<uintptr_t>(batch->output_user_ptr)), mop, kMopFrameBytes);
    return 0;
  }
};
}  // namespace

int main(int argc, char **argv) {
  if (argc != 3) return 2;
  std::ifstream mip_in(argv[1], std::ios::binary), mop_in(argv[2], std::ios::binary);
  std::vector<uint8_t> mip((std::istreambuf_iterator<char>(mip_in)), {}), mop((std::istreambuf_iterator<char>(mop_in)), {});
  if (mip.empty() || mip.size() % kMipFrameBytes || mop.size() != (mip.size()/kMipFrameBytes)*kMopFrameBytes) return 2;
  FakeImbc fake{};
  const rnt_io_vtable io{&fake, FakeImbc::Open, FakeImbc::Close, FakeImbc::Ioctl};
  rnt_transport *transport = nullptr;
  if (rnt_open("profile0-fake", &io, &transport) != RNT_OK) return 3;
  const size_t frames = mip.size()/kMipFrameBytes;
  size_t valid = 0, rejected = 0;
  for (size_t i=0;i<frames;++i) {
    const uint8_t *record = mip.data() + i*kMipFrameBytes;
    fake.mop = mop.data() + i*kMopFrameBytes;
    const uint32_t frame_id = uint32_t(record[8]) | (uint32_t(record[9])<<8U) | (uint32_t(record[10])<<16U) | (uint32_t(record[11])<<24U);
    const rnt_submit_config config{frame_id, kBuildId, 1000U, 2U, 64U, 32768};
    rnt_frame_result result{};
    const rnt_code submit = rnt_submit_mrf_frame(transport, record+40U, kTaskCount*RNT_TASK_BYTES,
                                                  record, 40U, &config, &result);
    if (submit != RNT_OK) {
      std::cerr << "rnt_submit frame_index=" << i << " rf_frame_id=" << frame_id
                << " code=" << rnt_code_string(submit) << '\n';
      ++rejected;
      continue;
    }
    const uint32_t crc0 = uint32_t(result.packets[148]) | (uint32_t(result.packets[149])<<8U) |
                          (uint32_t(result.packets[150])<<16U) | (uint32_t(result.packets[151])<<24U);
    if (crc32(result.packets + 20U, 128U) != crc0) { ++rejected; continue; }
    const uint8_t *stream1 = result.packets + 152U;
    const uint32_t crc1 = uint32_t(stream1[148]) | (uint32_t(stream1[149])<<8U) |
                          (uint32_t(stream1[150])<<16U) | (uint32_t(stream1[151])<<24U);
    if (crc32(stream1 + 20U, 128U) != crc1) { ++rejected; continue; }
    ++valid;
  }
  rnt_destroy(transport);
  std::cout << "{\"status\":\"rnt_profile0_e2e_ok\",\"frames\":" << frames
            << ",\"dual_payload_crc_valid\":" << valid
            << ",\"rejected\":" << rejected << "}\n";
  return valid == frames && rejected == 0U ? 0 : 6;
}
