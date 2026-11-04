/* See h264_annexb_validator.h. Kept separate so the production parser is
 * sanitizer-testable without a libavcodec installation. */
#include "h264_annexb_validator.h"

#include <limits.h>
#include <stdlib.h>

struct bit_reader {
    const uint8_t *data;
    size_t bytes;
    size_t bit;
};

static bool bit_read(struct bit_reader *reader, unsigned count, uint32_t *value) {
    uint32_t output = 0;
    unsigned i;
    if (count > 32U || reader->bit > reader->bytes * 8U ||
        count > reader->bytes * 8U - reader->bit)
        return false;
    for (i = 0; i < count; ++i) {
        output = (output << 1U) |
                 ((reader->data[reader->bit >> 3U] >> (7U - (reader->bit & 7U))) & 1U);
        ++reader->bit;
    }
    *value = output;
    return true;
}

static bool bit_read_ue(struct bit_reader *reader, uint32_t *value) {
    unsigned zeros = 0;
    uint32_t bit;
    uint32_t suffix = 0;
    while (true) {
        if (!bit_read(reader, 1U, &bit))
            return false;
        if (bit != 0U)
            break;
        if (++zeros > 31U)
            return false;
    }
    if (!bit_read(reader, zeros, &suffix))
        return false;
    if (zeros == 31U && suffix == UINT32_MAX)
        return false;
    *value = ((UINT32_C(1) << zeros) - 1U) + suffix;
    return true;
}

static bool bit_read_se(struct bit_reader *reader, int32_t *value) {
    uint32_t code_num;
    if (!bit_read_ue(reader, &code_num) || code_num == UINT32_MAX)
        return false;
    if ((code_num & 1U) != 0U)
        *value = (int32_t)((code_num >> 1U) + 1U);
    else
        *value = -(int32_t)(code_num >> 1U);
    return true;
}

static bool rbsp_copy(const uint8_t *nal, size_t nal_size, uint8_t **out, size_t *out_size) {
    uint8_t *buffer;
    size_t read_at;
    size_t write_at = 0;
    unsigned zeros = 0;
    if (nal_size < 2U)
        return false;
    buffer = malloc(nal_size - 1U);
    if (buffer == NULL)
        return false;
    for (read_at = 1U; read_at < nal_size; ++read_at) {
        uint8_t value = nal[read_at];
        if (zeros >= 2U && value == 3U) {
            if (read_at + 1U >= nal_size || nal[read_at + 1U] > 3U) {
                free(buffer);
                return false;
            }
            zeros = 0;
            continue;
        }
        buffer[write_at++] = value;
        if (value == 0U)
            ++zeros;
        else
            zeros = 0;
    }
    if (write_at == 0U) {
        free(buffer);
        return false;
    }
    *out = buffer;
    *out_size = write_at;
    return true;
}

static bool validate_sps(const uint8_t *nal, size_t nal_size) {
    struct bit_reader reader;
    uint8_t *rbsp = NULL;
    size_t rbsp_size = 0;
    uint32_t profile;
    uint32_t constraints;
    uint32_t ignored;
    uint32_t poc_type;
    uint32_t index;
    bool ok = false;
    if (!rbsp_copy(nal, nal_size, &rbsp, &rbsp_size))
        return false;
    reader.data = rbsp;
    reader.bytes = rbsp_size;
    reader.bit = 0;
    if (!bit_read(&reader, 8U, &profile) || !bit_read(&reader, 8U, &constraints) ||
        !bit_read(&reader, 8U, &ignored) || profile != 66U ||
        (constraints & UINT32_C(0x40)) == 0U || !bit_read_ue(&reader, &ignored) ||
        !bit_read_ue(&reader, &ignored) || !bit_read_ue(&reader, &poc_type))
        goto done;
    if (poc_type == 0U) {
        if (!bit_read_ue(&reader, &ignored))
            goto done;
    } else if (poc_type == 1U) {
        uint32_t cycles;
        if (!bit_read(&reader, 1U, &ignored) || !bit_read_ue(&reader, &ignored) ||
            !bit_read_ue(&reader, &ignored) || !bit_read_ue(&reader, &cycles) || cycles > 255U)
            goto done;
        for (index = 0; index < cycles; ++index) {
            int32_t signed_ignored;
            if (!bit_read_se(&reader, &signed_ignored))
                goto done;
        }
    } else if (poc_type > 2U) {
        goto done;
    }
    if (!bit_read_ue(&reader, &ignored) || !bit_read(&reader, 1U, &ignored) ||
        !bit_read_ue(&reader, &ignored) || !bit_read_ue(&reader, &ignored) ||
        !bit_read(&reader, 1U, &ignored) || ignored != 1U)
        goto done;
    ok = true;
done:
    free(rbsp);
    return ok;
}

static bool validate_slice(const uint8_t *nal, size_t nal_size, bool expect_idr) {
    struct bit_reader reader;
    uint8_t *rbsp = NULL;
    size_t rbsp_size = 0;
    uint32_t first_mb;
    uint32_t slice_type;
    const unsigned nal_type = nal_size == 0U ? 0U : (nal[0] & 0x1fU);
    bool ok = false;
    if ((expect_idr && nal_type != 5U) || (!expect_idr && nal_type != 1U))
        return false;
    if (!rbsp_copy(nal, nal_size, &rbsp, &rbsp_size))
        return false;
    reader.data = rbsp;
    reader.bytes = rbsp_size;
    reader.bit = 0;
    if (!bit_read_ue(&reader, &first_mb) || !bit_read_ue(&reader, &slice_type))
        goto done;
    if (first_mb != 0U || slice_type > 9U ||
        (expect_idr && slice_type != 2U && slice_type != 7U) ||
        (!expect_idr && slice_type != 0U && slice_type != 5U))
        goto done;
    ok = true;
done:
    free(rbsp);
    return ok;
}

static bool find_annexb_start_code(const uint8_t *bytes, size_t size, size_t cursor,
                                   size_t *offset, size_t *prefix_bytes) {
    size_t pos = cursor;
    while (pos + 3U <= size) {
        if (bytes[pos] == 0U && bytes[pos + 1U] == 0U && bytes[pos + 2U] == 1U) {
            *offset = pos;
            *prefix_bytes = 3U;
            return true;
        }
        if (pos + 4U <= size && bytes[pos] == 0U && bytes[pos + 1U] == 0U &&
            bytes[pos + 2U] == 0U && bytes[pos + 3U] == 1U) {
            *offset = pos;
            *prefix_bytes = 4U;
            return true;
        }
        ++pos;
    }
    return false;
}

bool vcodec_validate_h264_au(const uint8_t *bytes, size_t size, bool self_contained_idr) {
    size_t start;
    size_t prefix;
    size_t cursor;
    const uint8_t *nal;
    size_t nal_size;
    unsigned sps_count = 0;
    unsigned pps_count = 0;
    unsigned slices = 0;
    unsigned aud_count = 0;
    bool saw_vcl = false;
    if (bytes == NULL || !find_annexb_start_code(bytes, size, 0U, &start, &prefix) || start != 0U)
        return false;
    cursor = 0U;
    while (find_annexb_start_code(bytes, size, cursor, &start, &prefix)) {
        size_t next_start;
        size_t next_prefix;
        size_t payload_start = start + prefix;
        size_t payload_end = size;
        if (find_annexb_start_code(bytes, size, payload_start, &next_start, &next_prefix))
            payload_end = next_start;
        (void)next_prefix;
        if (payload_start >= payload_end)
            return false;
        nal = bytes + payload_start;
        nal_size = payload_end - payload_start;
        {
            const unsigned type = nal[0] & 0x1fU;
            if ((nal[0] & 0x80U) != 0U || type == 0U || type > 23U)
                return false;
            if (aud_count == 0U) {
                if (type != 9U)
                    return false;
                ++aud_count;
            } else if (type == 9U || saw_vcl ||
                       (type != 1U && type != 5U && type != 6U && type != 7U && type != 8U)) {
                return false;
            } else if (type == 7U) {
                if (++sps_count != 1U || !validate_sps(nal, nal_size))
                    return false;
            } else if (type == 8U) {
                if (++pps_count != 1U)
                    return false;
            } else if (type == 1U || type == 5U) {
                if (++slices != 1U || !validate_slice(nal, nal_size, self_contained_idr))
                    return false;
                saw_vcl = true;
            }
        }
        cursor = payload_end;
        if (cursor >= size)
            break;
    }
    if (aud_count != 1U || slices != 1U)
        return false;
    if (self_contained_idr)
        return sps_count == 1U && pps_count == 1U;
    return sps_count == 0U && pps_count == 0U;
}
