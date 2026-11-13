#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <string>

namespace hdmi_presenter {

constexpr uint32_t kWidth = 1024;
constexpr uint32_t kHeight = 600;
constexpr uint32_t kRgb565Bytes = kWidth * kHeight * 2;
constexpr uint32_t kXrgb8888Bytes = kWidth * kHeight * 4;
constexpr uint32_t kRgb565Le = 1;
constexpr uint16_t kHdv1Version = 1;
constexpr size_t kHdv1HeaderBytes = 64;
constexpr size_t kVcodecHeaderBytes = 72;
constexpr uint32_t kVcodecNeedIdr = 1U;

/* Wire representation is deliberately byte-addressed: it is independent of
 * host alignment and all multi-byte fields are unsigned little endian. */
struct FrameIdentity {
  uint64_t session_id{};
  uint64_t frame_id{};
  uint64_t pts_ns{};
  uint32_t payload_crc32{};
  // Internal presentation metadata; existing HDV1/VCDO wire formats unchanged.
  uint32_t capture_uncertainty_us{};
};

enum class VcdoRecordType : uint32_t {
  kFrame = 1,
  kInputError = 2,
  kDecodeError = 3,
  kFlushed = 4,
  kFlushAck = 5,
  kFatal = 6,
  kStopAck = 7,
};

struct VcdoRecord {
  VcdoRecordType type{};
  FrameIdentity identity{};
};

enum class ParseError {
  kOk, kShortHeader, kBadMagic, kBadVersion, kBadHeaderBytes, kBadGeometry,
  kBadPayloadSize, kBadPixelFormat, kBadFlags, kBadHeaderCrc, kBadPayloadCrc,
  kBadTimestamp, kNonMonotonicSession, kNonMonotonicFrame, kNonMonotonicPts, kNotFrame,
};

uint32_t ieee_crc32(const uint8_t* bytes, size_t size);
uint16_t load_le16(const uint8_t* p);
uint32_t load_le32(const uint8_t* p);
uint64_t load_le64(const uint8_t* p);
void store_le16(uint8_t* p, uint16_t value);
void store_le32(uint8_t* p, uint32_t value);
void store_le64(uint8_t* p, uint64_t value);

ParseError parse_hdv1_header(const std::array<uint8_t, kHdv1HeaderBytes>& raw,
                             FrameIdentity* identity);
/* VCDO has already had its identity/payload CRC assured by the codec helper.
 * Presenter VCDO input is the production contract: source_pts carries the
 * originating ZVID2 source_timestamp_ms and is converted to pts_ns here.
 * Synthetic helper fixtures whose opaque source_pts is in microseconds are
 * not valid presenter VCDO input without an explicit adapter. */
ParseError parse_vcdo_frame_header(const std::array<uint8_t, kVcodecHeaderBytes>& raw,
                                   FrameIdentity* identity);
/* Parses every helper stdout record, including zero-payload recovery and stop
 * controls. The raw header remains the feedback payload; no RGB is copied. */
ParseError parse_vcdo_record_header(const std::array<uint8_t, kVcodecHeaderBytes>& raw,
                                    VcdoRecord* record);
const char* parse_error_name(ParseError error);

}  // namespace hdmi_presenter
