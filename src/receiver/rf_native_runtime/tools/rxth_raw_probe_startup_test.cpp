// Executable host-only parser/policy regression for rxth_raw_probe.cpp.
// It does not open a device, issue ioctls, or emulate DMA.
#define RXTH_RAW_PROBE_UNIT_TEST
#include "rxth_raw_probe.cpp"

#include <array>
#include <cstdlib>

namespace {
void put16(uint8_t* p, uint16_t value) { p[0] = static_cast<uint8_t>(value); p[1] = static_cast<uint8_t>(value >> 8); }
void put32(uint8_t* p, uint32_t value) { put16(p, static_cast<uint16_t>(value)); put16(p + 2, static_cast<uint16_t>(value >> 16)); }
void put64(uint8_t* p, uint64_t value) { put32(p, static_cast<uint32_t>(value)); put32(p + 4, static_cast<uint32_t>(value >> 32)); }

std::array<uint8_t, ZED_RXTH_BLOCK_BYTES> block(uint16_t flags, uint32_t epoch, uint32_t sequence, uint64_t first) {
  std::array<uint8_t, ZED_RXTH_BLOCK_BYTES> raw{};
  std::memcpy(raw.data(), "RXTH", 4);
  raw[4] = ZED_RXTH_FORMAT_VERSION; raw[5] = ZED_RXTH_HEADER_BYTES;
  put16(raw.data() + 6, flags); put32(raw.data() + 8, epoch); put32(raw.data() + 12, sequence);
  put64(raw.data() + 16, first); put32(raw.data() + 24, ZED_RXTH_SAMPLES_PER_BLOCK);
  put32(raw.data() + 28, crc32(raw.data(), 28));
  return raw;
}

void require(bool value, const char* text) { if (!value) { std::fprintf(stderr, "CHECK_FAIL %s\n", text); std::exit(1); } }
}

int main() {
  std::string error;
  Header startup{}, clean{};
  auto startup_raw = block(kStartupPrerollFlags, 2, 0, 100);
  require(parse_header(startup_raw.data(), &startup, &error), "startup structural parse");
  require(classify_block(startup, 0, 0, 0) == BlockClass::kInvalid, "default rejects startup");
  require(classify_block(startup, 1, 0, 0) == BlockClass::kStartupPreroll, "explicit first startup accepts");
  require(classify_block(startup, 1, 1, 0) == BlockClass::kInvalid, "second startup rejects");
  require(classify_block(startup, 1, 0, 1) == BlockClass::kInvalid, "startup after clean rejects");

  auto reset_only_raw = block(kInitialResetFlags, 7, 19, 200);
  Header reset_only{};
  require(parse_header(reset_only_raw.data(), &reset_only, &error), "reset-only structural parse");
  require(classify_block(reset_only, 0, 0, 0) == BlockClass::kClean, "default first reset-only accepts");
  require(classify_block(reset_only, 1, 0, 0) == BlockClass::kClean, "explicit mode first reset-only accepts");
  require(classify_block(reset_only, 1, 1, 0) == BlockClass::kInvalid, "reset-only after startup rejects");
  require(classify_block(reset_only, 0, 0, 1) == BlockClass::kInvalid, "later reset-only rejects");

  auto clean_raw = block(kValid, 2, 1, 1124);
  require(parse_header(clean_raw.data(), &clean, &error), "clean structural parse");
  require(classify_block(clean, 0, 0, 0) == BlockClass::kClean, "clean accepts");
  require(check_continuity(startup, clean, &error), "startup to first clean continuity");
  auto gap_raw = block(kValid, 2, 2, 1124); Header gap{};
  require(parse_header(gap_raw.data(), &gap, &error), "gap parse");
  require(!check_continuity(startup, gap, &error), "sequence gap rejects");

  auto adc_raw = block(kValid | 0x0002u, 2, 0, 100); Header adc{};
  require(parse_header(adc_raw.data(), &adc, &error), "known ADC flag structural parse");
  require(classify_block(adc, 1, 0, 0) == BlockClass::kInvalid, "ADC flag rejects");
  auto unknown_raw = block(kValid | 0x0200u, 2, 0, 100); Header ignored{};
  require(!parse_header(unknown_raw.data(), &ignored, &error), "unknown flags reject");
  auto crc_raw = startup_raw; crc_raw[28] ^= 1;
  require(!parse_header(crc_raw.data(), &ignored, &error), "CRC rejects");
  auto alignment_raw = startup_raw; alignment_raw[ZED_RXTH_HEADER_BYTES] = 1;
  require(!parse_header(alignment_raw.data(), &ignored, &error), "MSB12 alignment rejects");
  std::puts("RXTH_RAW_PROBE_STARTUP_POLICY_PASS checks=17");
  return 0;
}
