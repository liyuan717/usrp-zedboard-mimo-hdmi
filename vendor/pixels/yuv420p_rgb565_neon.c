#include "yuv420p_rgb565_internal.h"

#if defined(__ARM_NEON)
#include <arm_neon.h>
#include <string.h>

#if defined(__GNUC__) || defined(__clang__)
#define MIMO_ALWAYS_INLINE static inline __attribute__((always_inline))
#else
#define MIMO_ALWAYS_INLINE static inline
#endif

/* ARMv7 little-endian only: memcpy performs an unaligned exact four-byte read
 * without a C alignment violation, then vcreate maps word bit 0 to byte lane
 * 0.  No padding beyond the active chroma row is read. */
MIMO_ALWAYS_INLINE uint8x8_t expand_four_chroma(const uint8_t *input) {
    uint32_t word;
    memcpy(&word, input, sizeof(word));
    return vzip_u8(vcreate_u8((uint64_t)word), vcreate_u8((uint64_t)word)).val[0];
}

/* Intermediate BT.601 sums reach about +/-124k, so this must widen before
 * multiplication; a 16-bit vmlaq would wrap and is not bit-true. */
MIMO_ALWAYS_INLINE void convert_four_channels(int16x4_t c, int16x4_t d, int16x4_t e,
                                               uint16x4_t *r, uint16x4_t *g, uint16x4_t *b) {
    const int32x4_t y_base = vaddq_s32(vmull_n_s16(c, 298), vdupq_n_s32(128));
    const int32x4_t red = vmlal_n_s16(y_base, e, 409);
    const int32x4_t green = vmlal_n_s16(vmlal_n_s16(y_base, d, -100), e, -208);
    const int32x4_t blue = vmlal_n_s16(y_base, d, 516);
    *r = vqshrun_n_s32(red, 8);
    *g = vqshrun_n_s32(green, 8);
    *b = vqshrun_n_s32(blue, 8);
}

MIMO_ALWAYS_INLINE void convert_eight(const uint8_t *y, const uint8_t *u, const uint8_t *v, uint8_t *out) {
    const int16x8_t c = vsubq_s16(vreinterpretq_s16_u16(vmovl_u8(vld1_u8(y))), vdupq_n_s16(16));
    const int16x8_t d = vsubq_s16(vreinterpretq_s16_u16(vmovl_u8(expand_four_chroma(u))), vdupq_n_s16(128));
    const int16x8_t e = vsubq_s16(vreinterpretq_s16_u16(vmovl_u8(expand_four_chroma(v))), vdupq_n_s16(128));
    uint16x4_t r_lo, r_hi, g_lo, g_hi, b_lo, b_hi;
    uint8x8_t r, g, b;
    uint16x8_t packed;
    convert_four_channels(vget_low_s16(c), vget_low_s16(d), vget_low_s16(e), &r_lo, &g_lo, &b_lo);
    convert_four_channels(vget_high_s16(c), vget_high_s16(d), vget_high_s16(e), &r_hi, &g_hi, &b_hi);
    r = vqmovn_u16(vcombine_u16(r_lo, r_hi));
    g = vqmovn_u16(vcombine_u16(g_lo, g_hi));
    b = vqmovn_u16(vcombine_u16(b_lo, b_hi));
    packed = vshlq_n_u16(vmovl_u8(vshr_n_u8(r, 3)), 11);
    packed = vorrq_u16(packed, vshlq_n_u16(vmovl_u8(vshr_n_u8(g, 2)), 5));
    packed = vorrq_u16(packed, vmovl_u8(vshr_n_u8(b, 3)));
    /* A byte store accepts unaligned output; ARM little-endian emits RGB565LE. */
    vst1q_u8(out, vreinterpretq_u8_u16(packed));
}

int yuv420p_to_rgb565_neon(const struct yuv420p_rgb565_image *src,
                           uint8_t *dst, size_t dst_stride) {
    uint32_t row;
    int status = yuv420p_rgb565_validate(src, dst, dst_stride);
    if (status != YUV420P_RGB565_OK)
        return status;
    for (row = 0U; row < src->height; ++row) {
        const uint8_t *y_row = src->y + (size_t)row * src->y_stride;
        const uint8_t *u_row = src->u + (size_t)(row / 2U) * src->u_stride;
        const uint8_t *v_row = src->v + (size_t)(row / 2U) * src->v_stride;
        uint8_t *out = dst + (size_t)row * dst_stride;
        uint32_t col = 0U;
        for (; col + 8U <= src->width; col += 8U)
            convert_eight(y_row + col, u_row + col / 2U, v_row + col / 2U, out + (size_t)col * 2U);
        for (; col < src->width; ++col)
            yuv420p_rgb565_pixel(y_row[col], u_row[col / 2U], v_row[col / 2U], out + (size_t)col * 2U);
    }
    return YUV420P_RGB565_OK;
}

#else
int yuv420p_to_rgb565_neon(const struct yuv420p_rgb565_image *src,
                           uint8_t *dst, size_t dst_stride) {
    (void)src;
    (void)dst;
    (void)dst_stride;
    return YUV420P_RGB565_ENEON_UNAVAILABLE;
}
#endif
