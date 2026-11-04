#ifndef MIMO_CODEC_CRC32_H
#define MIMO_CODEC_CRC32_H

#include <limits.h>
#include <stddef.h>
#include <stdint.h>
#include <zlib.h>

/* Same reflected IEEE CRC-32 wire contract as the former bit-at-a-time loop.
 * zlib is already linked by the helper. Do not remove payload integrity to
 * improve throughput: use its optimized implementation instead. Chunking keeps
 * size_t -> uInt correct even outside the current <=4 MiB protocol bounds. */
static inline uint32_t crc32_ieee(const uint8_t *data, size_t size)
{
    uLong value = crc32(0L, Z_NULL, 0);
    while (size != 0U) {
        const uInt chunk = size > UINT_MAX ? UINT_MAX : (uInt)size;
        value = crc32(value, data, chunk);
        data += chunk;
        size -= chunk;
    }
    return (uint32_t)value;
}

#endif
