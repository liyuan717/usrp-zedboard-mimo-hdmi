#include "hdmi_presenter/rgb_expand.h"

#include <cstring>

#if defined(__ARM_NEON)
#include <arm_neon.h>
#endif

namespace hdmi_presenter {
namespace {
void scalar(const uint8_t* input, uint8_t* output, size_t pixels) {
  for (size_t i = 0; i < pixels; ++i) {
    const uint16_t p = static_cast<uint16_t>(input[2 * i]) |
                       (static_cast<uint16_t>(input[2 * i + 1]) << 8);
    const uint8_t r5 = static_cast<uint8_t>(p >> 11);
    const uint8_t g6 = static_cast<uint8_t>((p >> 5) & 0x3fU);
    const uint8_t b5 = static_cast<uint8_t>(p & 0x1fU);
    output[4 * i] = static_cast<uint8_t>((b5 << 3) | (b5 >> 2));
    output[4 * i + 1] = static_cast<uint8_t>((g6 << 2) | (g6 >> 4));
    output[4 * i + 2] = static_cast<uint8_t>((r5 << 3) | (r5 >> 2));
    output[4 * i + 3] = 0;
  }
}
#if defined(__ARM_NEON)
void neon(const uint8_t* input, uint8_t* output, size_t pixels) {
  size_t i = 0;
  const uint16x8_t mask_g = vdupq_n_u16(0x07e0U);
  const uint16x8_t mask_b = vdupq_n_u16(0x001fU);
  for (; i + 8 <= pixels; i += 8) {
    const uint16x8_t p = vreinterpretq_u16_u8(vld1q_u8(input + 2 * i));
    const uint8x8_t r5 = vmovn_u16(vshrq_n_u16(p, 11));
    const uint8x8_t g6 = vmovn_u16(vshrq_n_u16(vandq_u16(p, mask_g), 5));
    const uint8x8_t b5 = vmovn_u16(vandq_u16(p, mask_b));
    const uint8x8_t r = vorr_u8(vshl_n_u8(r5, 3), vshr_n_u8(r5, 2));
    const uint8x8_t g = vorr_u8(vshl_n_u8(g6, 2), vshr_n_u8(g6, 4));
    const uint8x8_t b = vorr_u8(vshl_n_u8(b5, 3), vshr_n_u8(b5, 2));
    const uint8x8_t z = vdup_n_u8(0);
    const uint8x8x2_t bg = vzip_u8(b, g);
    const uint8x8x2_t rz = vzip_u8(r, z);
    const uint16x4x2_t lo = vzip_u16(vreinterpret_u16_u8(bg.val[0]), vreinterpret_u16_u8(rz.val[0]));
    const uint16x4x2_t hi = vzip_u16(vreinterpret_u16_u8(bg.val[1]), vreinterpret_u16_u8(rz.val[1]));
    uint8_t* dst = output + 4 * i;
    vst1_u8(dst, vreinterpret_u8_u16(lo.val[0]));
    vst1_u8(dst + 8, vreinterpret_u8_u16(lo.val[1]));
    vst1_u8(dst + 16, vreinterpret_u8_u16(hi.val[0]));
    vst1_u8(dst + 24, vreinterpret_u8_u16(hi.val[1]));
  }
  scalar(input + 2 * i, output + 4 * i, pixels - i);
}
#endif
}  // namespace
const char* rgb_expand_mode_name(RgbExpandMode mode) { return mode == RgbExpandMode::kNeon ? "neon" : "scalar"; }
bool parse_rgb_expand_mode(const char* text, RgbExpandMode* mode) {
  if (!text || !mode) return false;
  if (std::strcmp(text, "scalar") == 0) { *mode = RgbExpandMode::kScalar; return true; }
  if (std::strcmp(text, "neon") == 0) { *mode = RgbExpandMode::kNeon; return true; }
  return false;
}
bool rgb_expand_neon_compiled() {
#if defined(__ARM_NEON)
  return true;
#else
  return false;
#endif
}
void rgb565_to_xrgb8888_expand(const uint8_t* input, uint8_t* output, size_t pixels, RgbExpandMode mode) {
#if defined(__ARM_NEON)
  if (mode == RgbExpandMode::kNeon) { neon(input, output, pixels); return; }
#else
  (void)mode;
#endif
  scalar(input, output, pixels);
}
}  // namespace hdmi_presenter
