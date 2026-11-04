#include "codec_crc32.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static uint32_t previous_bitwise_crc(const uint8_t *data, size_t count)
{
    uint32_t value = UINT32_C(0xffffffff);
    size_t index;
    for (index = 0; index < count; ++index) {
        unsigned bit;
        value ^= data[index];
        for (bit = 0; bit < 8; ++bit)
            value = (value >> 1) ^ ((value & 1U) ? UINT32_C(0xedb88320) : 0U);
    }
    return value ^ UINT32_C(0xffffffff);
}

int main(void)
{
    const size_t sizes[] = {0, 1, 2, 7, 8, 15, 16, 31, 32, 255, 256, 4096,
                            1228800, 4194304};
    uint8_t *data = malloc(4194304);
    size_t index;
    if (!data)
        return 2;
    if (crc32_ieee(NULL, 0) != 0U ||
        crc32_ieee((const uint8_t *)"123456789", 9) != UINT32_C(0xcbf43926)) {
        free(data);
        return 3;
    }
    for (index = 0; index < 4194304; ++index)
        data[index] = (uint8_t)((index * 73U) ^ (index >> 5));
    for (index = 0; index < sizeof(sizes) / sizeof(sizes[0]); ++index) {
        const uint32_t expected = previous_bitwise_crc(data, sizes[index]);
        if (crc32_ieee(data, sizes[index]) != expected) {
            fprintf(stderr, "CRC mismatch at %zu bytes\n", sizes[index]);
            free(data);
            return 4;
        }
    }
    memset(data, 0, 1228800);
    if (crc32_ieee(data, 1228800) != UINT32_C(0x7fd9f67a)) {
        free(data);
        return 5;
    }
    free(data);
    puts("CODEC_IEEE_CRC_EQUIVALENCE_PASS sizes=14 known_vectors=3");
    return 0;
}
