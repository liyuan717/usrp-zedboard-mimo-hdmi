/* SPDX-License-Identifier: MIT
 *
 * Reviewed opt-in CRC kernel for the existing slice8 table context. The
 * legacy crc32_slice8_candidate.h remains unchanged as a reproducible
 * baseline. This header adds only the independently A9-benchmarked four-block
 * loop shape; it does not change the IEEE CRC-32 definition or context/init.
 *
 * Provenance: crc_stream_opt/r3_slice8_unroll, A9 run 379. The benchmark
 * verified zlib/baseline/unroll4 equality and measured unroll4 faster than
 * baseline. No prefetch is used; only complete blocks and the exact tail read.
 */
#ifndef MIMO_CRC32_SLICE8_UNROLL_CANDIDATE_H
#define MIMO_CRC32_SLICE8_UNROLL_CANDIDATE_H

#include "crc32_slice8_candidate.h"

static inline uint32_t crc32_slice8_unroll_load_le32(const uint8_t *p) {
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) |
           ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

static inline uint32_t crc32_slice8_unroll_block(const struct crc32_slice8_context *ctx,
                                                  uint32_t crc,
                                                  const uint8_t *p) {
    const uint32_t a = crc32_slice8_unroll_load_le32(p) ^ crc;
    const uint32_t b = crc32_slice8_unroll_load_le32(p + 4U);
    return ctx->table[7][a & UINT32_C(0xff)] ^
           ctx->table[6][(a >> 8) & UINT32_C(0xff)] ^
           ctx->table[5][(a >> 16) & UINT32_C(0xff)] ^
           ctx->table[4][a >> 24] ^
           ctx->table[3][b & UINT32_C(0xff)] ^
           ctx->table[2][(b >> 8) & UINT32_C(0xff)] ^
           ctx->table[1][(b >> 16) & UINT32_C(0xff)] ^
           ctx->table[0][b >> 24];
}

static inline uint32_t crc32_slice8_unroll4(const struct crc32_slice8_context *ctx,
                                             const uint8_t *p,
                                             size_t bytes) {
    uint32_t crc = UINT32_C(0xffffffff);
    while (bytes >= 32U) {
        crc = crc32_slice8_unroll_block(ctx, crc, p);
        crc = crc32_slice8_unroll_block(ctx, crc, p + 8U);
        crc = crc32_slice8_unroll_block(ctx, crc, p + 16U);
        crc = crc32_slice8_unroll_block(ctx, crc, p + 24U);
        p += 32U;
        bytes -= 32U;
    }
    while (bytes >= 8U) {
        crc = crc32_slice8_unroll_block(ctx, crc, p);
        p += 8U;
        bytes -= 8U;
    }
    while (bytes != 0U) {
        crc = (crc >> 8) ^ ctx->table[0][(crc ^ *p) & UINT32_C(0xff)];
        ++p;
        --bytes;
    }
    return crc ^ UINT32_C(0xffffffff);
}

#endif
