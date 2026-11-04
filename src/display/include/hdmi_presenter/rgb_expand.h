#pragma once

#include <cstddef>
#include <cstdint>

namespace hdmi_presenter {
enum class RgbExpandMode { kScalar, kNeon };
const char* rgb_expand_mode_name(RgbExpandMode mode);
bool parse_rgb_expand_mode(const char* text, RgbExpandMode* mode);
bool rgb_expand_neon_compiled();
/* Always bit-exact to scalar: RGB565LE -> B,G,R,0 XRGB8888 bytes. */
void rgb565_to_xrgb8888_expand(const uint8_t* input, uint8_t* output, size_t pixels,
                               RgbExpandMode mode);
}  // namespace hdmi_presenter
