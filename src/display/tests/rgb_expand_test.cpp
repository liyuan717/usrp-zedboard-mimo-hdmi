#include "hdmi_presenter/rgb_expand.h"

#include <array>
#include <cassert>
#include <cstdint>
#include <cstring>
#include <vector>

using namespace hdmi_presenter;

namespace {
void reference(const uint8_t* input, uint8_t* output, size_t count) {
  for (size_t i = 0; i < count; ++i) {
    const uint16_t p = static_cast<uint16_t>(input[2 * i]) | (static_cast<uint16_t>(input[2 * i + 1]) << 8);
    const uint8_t r = static_cast<uint8_t>(p >> 11);
    const uint8_t g = static_cast<uint8_t>((p >> 5) & 0x3fU);
    const uint8_t b = static_cast<uint8_t>(p & 0x1fU);
    output[4 * i] = static_cast<uint8_t>((b << 3) | (b >> 2));
    output[4 * i + 1] = static_cast<uint8_t>((g << 2) | (g >> 4));
    output[4 * i + 2] = static_cast<uint8_t>((r << 3) | (r >> 2));
    output[4 * i + 3] = 0;
  }
}
}  // namespace

int main() {
  constexpr size_t kAll = 65536;
  std::vector<uint8_t> canonical(2 * kAll);
  for (size_t i = 0; i < kAll; ++i) {
    canonical[2 * i] = static_cast<uint8_t>(i);
    canonical[2 * i + 1] = static_cast<uint8_t>(i >> 8);
  }
  for (size_t input_offset = 0; input_offset < 16; ++input_offset) {
    std::vector<uint8_t> source(2 * kAll + 32, 0xa5);
    std::memcpy(source.data() + input_offset, canonical.data(), canonical.size());
    for (size_t output_offset = 0; output_offset < 16; ++output_offset) {
      std::vector<uint8_t> expected(4 * kAll + 32, 0x7c);
      std::vector<uint8_t> actual(4 * kAll + 32, 0x7c);
      reference(source.data() + input_offset, expected.data() + output_offset, kAll);
      rgb565_to_xrgb8888_expand(source.data() + input_offset, actual.data() + output_offset, kAll,
                                 RgbExpandMode::kNeon);
      assert(expected == actual);
    }
  }
  std::array<uint8_t, 64> input{};
  for (size_t i = 0; i < input.size(); ++i) input[i] = static_cast<uint8_t>(i * 29U);
  for (size_t count = 0; count < 16; ++count) {
    std::array<uint8_t, 128> expected{};
    std::array<uint8_t, 128> actual{};
    reference(input.data() + 1, expected.data() + 3, count);
    rgb565_to_xrgb8888_expand(input.data() + 1, actual.data() + 3, count, RgbExpandMode::kNeon);
    assert(expected == actual);
  }
  assert(parse_rgb_expand_mode("scalar", nullptr) == false);
  RgbExpandMode mode{};
  assert(parse_rgb_expand_mode("scalar", &mode) && mode == RgbExpandMode::kScalar);
  assert(parse_rgb_expand_mode("neon", &mode) && mode == RgbExpandMode::kNeon);
  assert(!parse_rgb_expand_mode("invalid", &mode));
  return 0;
}
