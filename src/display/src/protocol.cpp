#include "hdmi_presenter/protocol.h"
#include "crc32_slice8_unroll_candidate.h"

#include <cstring>
#include <limits>

namespace hdmi_presenter {
uint16_t load_le16(const uint8_t* p) { return static_cast<uint16_t>(p[0]) | (static_cast<uint16_t>(p[1]) << 8); }
uint32_t load_le32(const uint8_t* p) { return static_cast<uint32_t>(p[0]) | (static_cast<uint32_t>(p[1]) << 8) | (static_cast<uint32_t>(p[2]) << 16) | (static_cast<uint32_t>(p[3]) << 24); }
uint64_t load_le64(const uint8_t* p) { return static_cast<uint64_t>(load_le32(p)) | (static_cast<uint64_t>(load_le32(p + 4)) << 32); }
void store_le16(uint8_t* p, uint16_t v) { p[0] = static_cast<uint8_t>(v); p[1] = static_cast<uint8_t>(v >> 8); }
void store_le32(uint8_t* p, uint32_t v) { for (unsigned i=0;i<4;++i) p[i]=static_cast<uint8_t>(v>>(8*i)); }
void store_le64(uint8_t* p, uint64_t v) { store_le32(p, static_cast<uint32_t>(v)); store_le32(p+4, static_cast<uint32_t>(v>>32)); }
uint32_t ieee_crc32(const uint8_t* data, size_t size) {
  /* Function-local static initialization is serialized by C++11; after that
   * the 8x256 table is immutable and shared safely by all receiver threads.
   * This imports the reviewed helper candidate unchanged (SHA-256
   * 789b053994cffe3d23143e97d8adfc2a7f38394982bb896413e6f749c59e51f5). */
  static const crc32_slice8_context context = [] {
    crc32_slice8_context initialized{};
    crc32_slice8_init(&initialized);
    return initialized;
  }();
  return crc32_slice8_unroll4(&context, data, size);
}
ParseError parse_hdv1_header(const std::array<uint8_t, kHdv1HeaderBytes>& raw, FrameIdentity* id) {
  if (std::memcmp(raw.data(), "HDV1", 4) != 0) return ParseError::kBadMagic;
  if (load_le16(raw.data()+4) != kHdv1Version) return ParseError::kBadVersion;
  if (load_le16(raw.data()+6) != kHdv1HeaderBytes) return ParseError::kBadHeaderBytes;
  if (load_le32(raw.data()+8) != kRgb565Le) return ParseError::kBadPixelFormat;
  if (load_le32(raw.data()+12) != kWidth || load_le32(raw.data()+16) != kHeight) return ParseError::kBadGeometry;
  if (load_le32(raw.data()+20) != kRgb565Bytes) return ParseError::kBadPayloadSize;
  if (load_le32(raw.data()+52) != 0 || load_le32(raw.data()+56) != 0) return ParseError::kBadFlags;
  if (ieee_crc32(raw.data(), 60) != load_le32(raw.data()+60)) return ParseError::kBadHeaderCrc;
  id->session_id=load_le64(raw.data()+24); id->frame_id=load_le64(raw.data()+32); id->pts_ns=load_le64(raw.data()+40); id->payload_crc32=load_le32(raw.data()+48);
  return ParseError::kOk;
}
namespace {
bool vcdo_zero_payload(const std::array<uint8_t, kVcodecHeaderBytes>& raw) {
  return load_le32(raw.data() + 48) == 0 && load_le32(raw.data() + 52) == 0 &&
         load_le32(raw.data() + 56) == 0 && load_le32(raw.data() + 60) == 0 &&
         load_le32(raw.data() + 64) == 0;
}

bool vcdo_zero_identity(const std::array<uint8_t, kVcodecHeaderBytes>& raw) {
  return load_le64(raw.data() + 16) == 0 && load_le64(raw.data() + 24) == 0 &&
         load_le64(raw.data() + 32) == 0 && load_le64(raw.data() + 40) == 0;
}

ParseError vcdo_identity(const std::array<uint8_t, kVcodecHeaderBytes>& raw,
                         FrameIdentity* id, bool convert_pts_ms) {
  if (!id) return ParseError::kBadFlags;
  constexpr uint64_t kNsPerMs = 1000000U;
  const uint64_t source_pts = load_le64(raw.data() + 40);
  if (convert_pts_ms && source_pts > std::numeric_limits<uint64_t>::max() / kNsPerMs)
    return ParseError::kBadTimestamp;
  id->session_id = load_le64(raw.data() + 16);
  id->frame_id = load_le64(raw.data() + 24);
  id->pts_ns = convert_pts_ms ? source_pts * kNsPerMs : source_pts;
  id->payload_crc32 = load_le32(raw.data() + 64);
  return ParseError::kOk;
}
}  // namespace

ParseError parse_vcdo_record_header(const std::array<uint8_t, kVcodecHeaderBytes>& raw,
                                    VcdoRecord* record) {
  if (!record) return ParseError::kBadFlags;
  if (std::memcmp(raw.data(), "VCDO", 4) != 0) return ParseError::kBadMagic;
  if (load_le16(raw.data()+4) != 1) return ParseError::kBadVersion;
  if (load_le16(raw.data()+6) != kVcodecHeaderBytes) return ParseError::kBadHeaderBytes;
  if (load_le32(raw.data()+68) != 0) return ParseError::kBadFlags;
  const uint32_t raw_type = load_le32(raw.data() + 8);
  const uint32_t flags = load_le32(raw.data() + 12);
  if (raw_type < static_cast<uint32_t>(VcdoRecordType::kFrame) ||
      raw_type > static_cast<uint32_t>(VcdoRecordType::kStopAck))
    return ParseError::kNotFrame;
  record->type = static_cast<VcdoRecordType>(raw_type);
  record->identity = {};
  if (record->type == VcdoRecordType::kFrame) {
    if (flags != 0) return ParseError::kBadFlags;
    if (load_le32(raw.data()+48) != kWidth || load_le32(raw.data()+52) != kHeight) return ParseError::kBadGeometry;
    if (load_le32(raw.data()+56) != kRgb565Le) return ParseError::kBadPixelFormat;
    if (load_le32(raw.data()+60) != kRgb565Bytes) return ParseError::kBadPayloadSize;
    if (load_le64(raw.data() + 32) == 0) return ParseError::kBadFlags;
    /* VCDO preserves production source_timestamp_ms in source_pts. */
    return vcdo_identity(raw, &record->identity, true);
  }
  if (!vcdo_zero_payload(raw)) return ParseError::kBadPayloadSize;
  if (record->type == VcdoRecordType::kInputError ||
      record->type == VcdoRecordType::kDecodeError ||
      record->type == VcdoRecordType::kFlushed) {
    if (flags != kVcodecNeedIdr || load_le64(raw.data() + 32) == 0) return ParseError::kBadFlags;
    return vcdo_identity(raw, &record->identity, false);
  }
  if (flags != kVcodecNeedIdr || !vcdo_zero_identity(raw)) return ParseError::kBadFlags;
  return ParseError::kOk;
}

ParseError parse_vcdo_frame_header(const std::array<uint8_t, kVcodecHeaderBytes>& raw, FrameIdentity* id) {
  VcdoRecord record{};
  const ParseError result = parse_vcdo_record_header(raw, &record);
  if (result != ParseError::kOk) return result;
  if (record.type != VcdoRecordType::kFrame) return ParseError::kNotFrame;
  if (!id) return ParseError::kBadFlags;
  *id = record.identity;
  return ParseError::kOk;
}
const char* parse_error_name(ParseError e) { static const char* n[]={"ok","short_header","bad_magic","bad_version","bad_header_bytes","bad_geometry","bad_payload_size","bad_pixel_format","bad_flags","bad_header_crc","bad_payload_crc","bad_timestamp","nonmonotonic_session","nonmonotonic_frame","nonmonotonic_pts","not_frame"}; return n[static_cast<unsigned>(e)]; }
}  // namespace hdmi_presenter
