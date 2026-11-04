/* SPDX-License-Identifier: MIT
 * Diagnostic candidate, not the production CRC selection. IEEE reflected
 * CRC-32 (0xedb88320), init/final xor ffffffff. No payload or polynomial change.
 * Context is initialized before use and then read-only; no global init race.
 */
#ifndef MIMO_CRC32_SLICE8_CANDIDATE_H
#define MIMO_CRC32_SLICE8_CANDIDATE_H
#include <stddef.h>
#include <stdint.h>
#include <string.h>
struct crc32_slice8_context { uint32_t table[8][256]; };
static inline void crc32_slice8_init(struct crc32_slice8_context *ctx) {
    unsigned i, bit, slice;
    for (i = 0; i < 256; ++i) {
        uint32_t v = i;
        for (bit = 0; bit < 8; ++bit)
            v = (v >> 1) ^ (UINT32_C(0xedb88320) & (0U - (v & 1U)));
        ctx->table[0][i] = v;
    }
    for (slice = 1; slice < 8; ++slice)
        for (i = 0; i < 256; ++i) {
            uint32_t v = ctx->table[slice-1][i];
            ctx->table[slice][i] = (v >> 8) ^ ctx->table[0][v & 255U];
        }
}
static inline uint32_t crc32_candidate_le32(const uint8_t *p) {
#if defined(__BYTE_ORDER__) && __BYTE_ORDER__ == __ORDER_LITTLE_ENDIAN__
    uint32_t word;
    memcpy(&word, p, sizeof(word)); /* valid for every byte alignment */
    return word;
#else
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) |
           ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
#endif
}
static inline uint32_t crc32_slice8(const struct crc32_slice8_context *ctx,
                                  const uint8_t *p, size_t bytes) {
    uint32_t crc = UINT32_C(0xffffffff);
    while (bytes >= 8) {
        uint32_t a = crc32_candidate_le32(p) ^ crc;
        uint32_t b = crc32_candidate_le32(p+4);
        crc = ctx->table[7][a & 255U] ^ ctx->table[6][(a >> 8) & 255U] ^
              ctx->table[5][(a >> 16) & 255U] ^ ctx->table[4][a >> 24] ^
              ctx->table[3][b & 255U] ^ ctx->table[2][(b >> 8) & 255U] ^
              ctx->table[1][(b >> 16) & 255U] ^ ctx->table[0][b >> 24];
        p += 8;
        bytes -= 8;
    }
    while (bytes--) crc = (crc >> 8) ^ ctx->table[0][(crc ^ *p++) & 255U];
    return crc ^ UINT32_C(0xffffffff);
}
#endif
