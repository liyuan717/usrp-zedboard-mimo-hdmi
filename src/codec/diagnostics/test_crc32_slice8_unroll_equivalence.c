/* SPDX-License-Identifier: MIT */
#include "crc32_slice8_candidate.h"
#include "crc32_slice8_unroll_candidate.h"

#include <stdio.h>
#include <stdlib.h>
#include <zlib.h>

#define BUFFER_BYTES (4194304U + 32U)
#define IMAGE_BYTES 1228800U

static uint32_t xorshift32(uint32_t *state) {
    uint32_t value = *state;
    value ^= value << 13;
    value ^= value >> 17;
    value ^= value << 5;
    *state = value;
    return value;
}

int main(void) {
    static const size_t boundary_lengths[] = {
        0U, 1U, 7U, 8U, 9U, 15U, 16U, 31U, 32U, 33U,
        255U, 256U, 257U, 4096U, IMAGE_BYTES, 4194304U
    };
    struct crc32_slice8_context context;
    uint8_t *data = malloc(BUFFER_BYTES);
    uint32_t state = UINT32_C(0x19ab82d7);
    unsigned offset;
    unsigned bytes;
    size_t index;
    if (data == NULL)
        return 2;
    for (index = 0U; index < BUFFER_BYTES; ++index)
        data[index] = (uint8_t)xorshift32(&state);
    crc32_slice8_init(&context);
    if (crc32_slice8_unroll4(&context, (const uint8_t *)"123456789", 9U) != UINT32_C(0xcbf43926)) {
        free(data);
        return 3;
    }
    for (offset = 0U; offset < 32U; ++offset) {
        for (bytes = 0U; bytes <= 257U; ++bytes) {
            const uint32_t reference = (uint32_t)crc32(0L, data + offset, bytes);
            if (crc32_slice8(&context, data + offset, bytes) != reference ||
                crc32_slice8_unroll4(&context, data + offset, bytes) != reference) {
                free(data);
                return 4;
            }
        }
        for (index = 0U; index < sizeof(boundary_lengths) / sizeof(boundary_lengths[0]); ++index) {
            const size_t length = boundary_lengths[index];
            const uint32_t reference = (uint32_t)crc32(0L, data + offset, (uInt)length);
            if (crc32_slice8(&context, data + offset, length) != reference ||
                crc32_slice8_unroll4(&context, data + offset, length) != reference) {
                free(data);
                return 5;
            }
        }
    }
    free(data);
    puts("CRC32_SLICE8_UNROLL4_EQUIVALENCE_PASS ieee=0xedb88320 offsets=32 small_lengths=258 boundary_lengths=16 seed=0x19ab82d7");
    return 0;
}
