/*
 * Optional diagnostic YUV420P -> RGB565LE converter.
 *
 * This is deliberately not included by video_codec_helper.c.  A caller must
 * explicitly select it after validating the decoded frame's color metadata.
 */
#ifndef MIMO_YUV420P_RGB565_H
#define MIMO_YUV420P_RGB565_H

#include <stddef.h>
#include <stdint.h>

enum yuv420p_rgb565_status {
    YUV420P_RGB565_OK = 0,
    YUV420P_RGB565_EINVAL = -1,
    YUV420P_RGB565_ENEON_UNAVAILABLE = -2
};

/*
 * Planar, progressive, 4:2:0 image.  width and height must both be nonzero
 * and even.  Chroma sample (x / 2, y / 2) is used for luma pixel (x, y).
 * Each supplied stride may be wider than the active row, but no byte outside
 * the active row is read by either implementation.
 */
struct yuv420p_rgb565_image {
    const uint8_t *y;
    const uint8_t *u;
    const uint8_t *v;
    size_t y_stride;
    size_t u_stride;
    size_t v_stride;
    uint32_t width;
    uint32_t height;
};

/*
 * Output is RGB565 little-endian bytes, with dst_stride >= width * 2.
 * Conversion is BT.601 limited-range integer arithmetic:
 *  C=Y-16, D=U-128, E=V-128; (298*C + {409*E,-100*D-208*E,516*D}
 *  + 128) arithmetic-floor-divided by 256, saturated to [0,255].
 * RGB565 is truncation-quantized (R>>3,G>>2,B>>3), with no dither.
 */
int yuv420p_to_rgb565_reference(const struct yuv420p_rgb565_image *src,
                                uint8_t *dst, size_t dst_stride);

/* ARMv7 NEON implementation.  It is an actual intrinsic implementation when
 * built with NEON; builds without __ARM_NEON return ENEON_UNAVAILABLE. */
int yuv420p_to_rgb565_neon(const struct yuv420p_rgb565_image *src,
                           uint8_t *dst, size_t dst_stride);

#endif
