#include "hdmi_presenter/protocol.h"
#include <array>
#include <cassert>
#include <cstring>
#include <limits>
#include <vector>

using namespace hdmi_presenter;
static uint32_t reference_crc32(const uint8_t* bytes, size_t size) {
  uint32_t crc = 0xffffffffU;
  for (size_t i = 0; i < size; ++i) {
    crc ^= bytes[i];
    for (unsigned bit = 0; bit < 8; ++bit)
      crc = (crc >> 1) ^ (0xedb88320U & (0U - (crc & 1U)));
  }
  return crc ^ 0xffffffffU;
}
int main() {
  /* Golden vector emitted by host/src/mimo_host/hdmi_debug_source.py. */
  const uint8_t golden[64]={0x48,0x44,0x56,0x31,0x01,0x00,0x40,0x00,0x01,0x00,0x00,0x00,0x00,0x04,0x00,0x00,0x58,0x02,0x00,0x00,0x00,0xc0,0x12,0x00,0x34,0x12,0x00,0x00,0x00,0x00,0x00,0x00,0xef,0xcd,0xab,0x89,0x00,0x00,0x00,0x00,0x55,0xa0,0xfc,0x01,0x00,0x00,0x00,0x00,0x7a,0xf6,0xd9,0x7f,0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x41,0x6b,0x07,0x2f};
  std::array<uint8_t,64> header{}; std::memcpy(header.data(),golden,64); FrameIdentity id{};
  assert(parse_hdv1_header(header,&id)==ParseError::kOk);
  assert(id.session_id==0x1234U && id.frame_id==0x89abcdefU && id.pts_ns==33333333U && id.payload_crc32==0x7fd9f67aU);
  assert(ieee_crc32(nullptr,0)==0U);
  header[32]^=1; assert(parse_hdv1_header(header,&id)==ParseError::kBadHeaderCrc);
  /* VCDO source_pts is the helper's source_timestamp_ms, not HDV1 ns. */
  std::array<uint8_t,kVcodecHeaderBytes> vcdo{};
  std::memcpy(vcdo.data(), "VCDO", 4); store_le16(vcdo.data()+4,1); store_le16(vcdo.data()+6,kVcodecHeaderBytes);
  store_le32(vcdo.data()+8,1); store_le64(vcdo.data()+16,9); store_le64(vcdo.data()+24,11);
  store_le64(vcdo.data()+32,12); store_le64(vcdo.data()+40,1234);
  store_le32(vcdo.data()+48,kWidth); store_le32(vcdo.data()+52,kHeight); store_le32(vcdo.data()+56,kRgb565Le);
  store_le32(vcdo.data()+60,kRgb565Bytes); store_le32(vcdo.data()+64,0x12345678);
  assert(parse_vcdo_frame_header(vcdo,&id)==ParseError::kOk);
  assert(id.session_id==9 && id.frame_id==11 && id.pts_ns==1234000000ULL);
  /* Control records are wire-valid but cannot be mistaken for RGB frames.
   * Their source PTS is opaque helper identity data, not the production ms
   * timestamp conversion applied to FRAME. */
  std::array<uint8_t,kVcodecHeaderBytes> input_error{};
  std::memcpy(input_error.data(), "VCDO", 4); store_le16(input_error.data()+4,1); store_le16(input_error.data()+6,kVcodecHeaderBytes);
  store_le32(input_error.data()+8,static_cast<uint32_t>(VcdoRecordType::kInputError));
  store_le32(input_error.data()+12,kVcodecNeedIdr); store_le64(input_error.data()+16,9); store_le64(input_error.data()+24,12);
  store_le64(input_error.data()+32,13); store_le64(input_error.data()+40,33333);
  VcdoRecord record{};
  assert(parse_vcdo_record_header(input_error,&record)==ParseError::kOk);
  assert(record.type==VcdoRecordType::kInputError && record.identity.pts_ns==33333);
  assert(parse_vcdo_frame_header(input_error,&id)==ParseError::kNotFrame);
  std::array<uint8_t,kVcodecHeaderBytes> stop_ack{};
  std::memcpy(stop_ack.data(), "VCDO", 4); store_le16(stop_ack.data()+4,1); store_le16(stop_ack.data()+6,kVcodecHeaderBytes);
  store_le32(stop_ack.data()+8,static_cast<uint32_t>(VcdoRecordType::kStopAck));
  store_le32(stop_ack.data()+12,kVcodecNeedIdr);
  assert(parse_vcdo_record_header(stop_ack,&record)==ParseError::kOk);
  assert(record.type==VcdoRecordType::kStopAck && record.identity.session_id==0);
  store_le32(stop_ack.data()+12,0);
  assert(parse_vcdo_record_header(stop_ack,&record)==ParseError::kBadFlags);
  store_le64(vcdo.data()+40,std::numeric_limits<uint64_t>::max() / 1000000U + 1U);
  assert(parse_vcdo_frame_header(vcdo,&id)==ParseError::kBadTimestamp);
  /* Slice-by-8 must preserve the existing IEEE result for unaligned short,
   * medium and representative full-frame inputs; this is test-only slow code. */
  std::vector<uint8_t> corpus(kRgb565Bytes + 32);
  for (size_t i = 0; i < corpus.size(); ++i) corpus[i] = static_cast<uint8_t>((i * 37U + 11U) & 0xffU);
  for (size_t offset = 0; offset < 32; ++offset)
    for (size_t length : {size_t{0}, size_t{1}, size_t{7}, size_t{8}, size_t{255}, size_t{257}, size_t{4097}})
      assert(ieee_crc32(corpus.data() + offset, length) == reference_crc32(corpus.data() + offset, length));
  assert(ieee_crc32(corpus.data() + 1, kRgb565Bytes) == reference_crc32(corpus.data() + 1, kRgb565Bytes));
  return 0;
}
