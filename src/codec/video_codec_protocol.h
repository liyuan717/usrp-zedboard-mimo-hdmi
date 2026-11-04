/*
 * Framed, little-endian stdin/stdout protocol for mimo-video-codec-helper.
 *
 * This file deliberately has no FFmpeg dependency so a supervisor can share
 * the constants and verify the wire ABI independently.  The target is an
 * ARMv7 little-endian Zynq Linux image; the helper rejects a non-little-endian
 * host at start-up rather than silently decoding a different byte order.
 */
#ifndef MIMO_VIDEO_CODEC_PROTOCOL_H
#define MIMO_VIDEO_CODEC_PROTOCOL_H

#include <stdint.h>

#define VCODEC_INPUT_MAGIC  UINT32_C(0x49444356) /* "VCDI" little endian */
#define VCODEC_OUTPUT_MAGIC UINT32_C(0x4f444356) /* "VCDO" little endian */
#define VCODEC_PROTOCOL_VERSION UINT16_C(1)

#define VCODEC_WIDTH UINT32_C(1024)
#define VCODEC_HEIGHT UINT32_C(600)
#define VCODEC_RGB565_BYTES (VCODEC_WIDTH * VCODEC_HEIGHT * UINT32_C(2))
/* Must match the maximum AU accepted by the RF-video dispatcher.  Four MiB
 * bounds memory allocation while leaving room for a self-contained IDR. */
#define VCODEC_MAX_ACCESS_UNIT_BYTES (UINT32_C(4) * UINT32_C(1024) * UINT32_C(1024))
#define VCODEC_MAX_PENDING_TOKENS UINT32_C(16)

enum vcodec_command {
    VCODEC_COMMAND_DECODE = 1,
    VCODEC_COMMAND_FLUSH = 2,
    VCODEC_COMMAND_STOP = 3,
};

enum vcodec_input_flags {
    /* The AU contains SPS/PPS and exactly one IDR picture. Required after
     * start, FLUSH, decode recovery, or a session change. */
    VCODEC_INPUT_SELF_CONTAINED_IDR = UINT32_C(1) << 0,
};

enum vcodec_codec {
    VCODEC_CODEC_H264_ANNEX_B = 1,
};

enum vcodec_status {
    VCODEC_STATUS_FRAME = 1,
    VCODEC_STATUS_INPUT_ERROR = 2,
    VCODEC_STATUS_DECODE_ERROR = 3,
    VCODEC_STATUS_FLUSHED = 4,
    VCODEC_STATUS_FLUSH_ACK = 5,
    VCODEC_STATUS_FATAL = 6,
    VCODEC_STATUS_STOP_ACK = 7,
};

enum vcodec_output_flags {
    /* The decoder is now intentionally waiting for an AU marked
     * SELF_CONTAINED_IDR. */
    VCODEC_OUTPUT_NEED_IDR = UINT32_C(1) << 0,
};

enum vcodec_pixel_format {
    VCODEC_PIXEL_RGB565LE = 1,
};

#pragma pack(push, 1)
struct vcodec_input_header {
    uint32_t magic;
    uint16_t version;
    uint16_t header_bytes;
    uint32_t command;
    uint32_t flags;
    uint64_t session_id;
    uint64_t source_frame_id;
    uint64_t decode_token;
    int64_t source_pts;
    uint32_t width;
    uint32_t height;
    uint32_t codec;
    uint32_t access_unit_bytes;
    uint32_t access_unit_crc32;
    uint32_t reserved;
};

struct vcodec_output_header {
    uint32_t magic;
    uint16_t version;
    uint16_t header_bytes;
    uint32_t status;
    uint32_t flags;
    uint64_t session_id;
    uint64_t source_frame_id;
    uint64_t decode_token;
    int64_t source_pts;
    uint32_t width;
    uint32_t height;
    uint32_t pixel_format;
    uint32_t payload_bytes;
    uint32_t payload_crc32;
    uint32_t reserved;
};
#pragma pack(pop)

#if defined(__STDC_VERSION__) && __STDC_VERSION__ >= 201112L
_Static_assert(sizeof(struct vcodec_input_header) == 72, "vcodec input ABI drift");
_Static_assert(sizeof(struct vcodec_output_header) == 72, "vcodec output ABI drift");
#endif

#endif
