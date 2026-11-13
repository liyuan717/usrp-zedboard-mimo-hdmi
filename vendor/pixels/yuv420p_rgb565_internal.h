#ifndef MIMO_YUV420P_RGB565_INTERNAL_H
#define MIMO_YUV420P_RGB565_INTERNAL_H

#include "yuv420p_rgb565.h"

#include <limits.h>

static inline int yuv420p_rgb565_validate(const struct yuv420p_rgb565_image *src,
                                          const uint8_t *dst, size_t dst_stride) {
    size_t chroma_width;
    if (src == NULL || dst == NULL || src->y == NULL || src->u == NULL || src->v == NULL ||
        src->width == 0U || src->height == 0U || (src->width & 1U) != 0U ||
        (src->height & 1U) != 0U || src->width > (uint32_t)(SIZE_MAX / 2U))
        return YUV420P_RGB565_EINVAL;
    chroma_width = (size_t)src->width / 2U;
    if (src->y_stride < src->width || src->u_stride < chroma_width ||
        src->v_stride < chroma_width || dst_stride < (size_t)src->width * 2U)
        return YUV420P_RGB565_EINVAL;
    if (src->y_stride > SIZE_MAX / src->height || src->u_stride > SIZE_MAX / (src->height / 2U) ||
        src->v_stride > SIZE_MAX / (src->height / 2U) || dst_stride > SIZE_MAX / src->height)
        return YUV420P_RGB565_EINVAL;
    return YUV420P_RGB565_OK;
}

/* C does not define right-shift behaviour for negative signed values.  This
 * explicitly implements the arithmetic-floor shift used by vshrq_n_s16. */
static inline int yuv420p_rgb565_floor_div_256(int value) {
    return value >= 0 ? value / 256 : -(((-value) + 255) / 256);
}

static inline uint8_t yuv420p_rgb565_clip_u8(int value) {
    return value <= 0 ? 0U : value >= 255 ? 255U : (uint8_t)value;
}

static inline void yuv420p_rgb565_pixel(uint8_t y, uint8_t u, uint8_t v, uint8_t *out) {
    const int c = (int)y - 16;
    const int d = (int)u - 128;
    const int e = (int)v - 128;
    const uint8_t r = yuv420p_rgb565_clip_u8(yuv420p_rgb565_floor_div_256(298 * c + 409 * e + 128));
    const uint8_t g = yuv420p_rgb565_clip_u8(yuv420p_rgb565_floor_div_256(298 * c - 100 * d - 208 * e + 128));
    const uint8_t b = yuv420p_rgb565_clip_u8(yuv420p_rgb565_floor_div_256(298 * c + 516 * d + 128));
    const uint16_t packed = (uint16_t)(((uint16_t)(r >> 3) << 11) |
                                       ((uint16_t)(g >> 2) << 5) | (uint16_t)(b >> 3));
    out[0] = (uint8_t)packed;
    out[1] = (uint8_t)(packed >> 8);
}

#endif
