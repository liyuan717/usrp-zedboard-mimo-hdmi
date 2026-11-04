/*
 * Persistent constrained-baseline H.264 -> RGB565LE decoder boundary.
 * See README.md and video_codec_protocol.h before changing this file: a
 * FIFO association is intentionally forbidden by the protocol.
 */
#include "video_codec_protocol.h"
#include "h264_annexb_validator.h"
#include "codec_crc32.h"
#include "codec_converter_policy.h"
#include "codec_profile.h"
#include "diagnostics/crc32_slice8_candidate.h"
#include "diagnostics/crc32_slice8_unroll_candidate.h"

#ifndef MIMO_CODEC_ENABLE_NEON601
#define MIMO_CODEC_ENABLE_NEON601 0
#endif

#if MIMO_CODEC_ENABLE_NEON601
#include "diagnostics/neon_converter/yuv420p_rgb565.h"
#endif

#include <errno.h>
#include <inttypes.h>
#include <limits.h>
#include <signal.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include <libavcodec/avcodec.h>
#include <libavutil/error.h>
#include <libavutil/imgutils.h>
#include <libavutil/pixfmt.h>
#include <libswscale/swscale.h>

#define VCODEC_DIAGNOSTIC_LIMIT 256U

struct identity {
    uint64_t session_id;
    uint64_t source_frame_id;
    uint64_t decode_token;
    int64_t source_pts;
};

struct pending_token {
    struct identity id;
    bool used;
};

struct helper {
    const AVCodec *codec;
    AVCodecContext *codec_ctx;
    AVPacket *packet;
    AVFrame *frame;
    struct SwsContext *sws;
    uint8_t *rgb565;
    size_t rgb565_capacity;
    struct pending_token pending[VCODEC_MAX_PENDING_TOKENS];
    uint32_t pending_count;
    uint64_t active_session;
    uint64_t last_token;
    bool have_session;
    bool need_idr;
    unsigned diagnostics;
    struct codec_profile profile;
    struct crc32_slice8_context crc_context;
    bool crc_slice8;
    enum vcodec_converter converter;
};

static uint32_t helper_crc32(struct helper *helper, const uint8_t *data, size_t bytes) {
    return helper->crc_slice8 ? crc32_slice8_unroll4(&helper->crc_context, data, bytes) :
                               crc32_ieee(data, bytes);
}

static void report_profile(const struct helper *helper) {
    static const char *names[CODEC_PROFILE_STAGE_COUNT] = {
        "validate", "decode", "convert", "rgb_crc", "pipe_write" };
    unsigned i;
    if (!helper->profile.enabled)
        return;
    fprintf(stderr, "CODEC_PROFILE_V1 {\"frames\":%" PRIu64 ",\"clock_error\":%s,\"crc_impl\":\"%s\",\"crc_kernel\":\"%s\",\"converter_impl\":\"%s\",\"stages\":{",
            helper->profile.frames, helper->profile.clock_error ? "true" : "false",
            helper->crc_slice8 ? "slice8" : "zlib", helper->crc_slice8 ? "unroll4" : "zlib",
            vcodec_converter_name(helper->converter));
    for (i = 0; i < CODEC_PROFILE_STAGE_COUNT; ++i)
        fprintf(stderr, "%s\"%s\":{\"calls\":%" PRIu64 ",\"wall_ns\":%" PRIu64
                ",\"thread_cpu_ns\":%" PRIu64 "}", i ? "," : "", names[i],
                helper->profile.calls[i], helper->profile.wall_ns[i], helper->profile.cpu_ns[i]);
    fputs("}}\n", stderr);
}

static void diagnostic(struct helper *helper, const char *format, ...) {
    va_list args;
    if (helper->diagnostics++ >= VCODEC_DIAGNOSTIC_LIMIT)
        return;
    fputs("mimo-video-codec-helper: ", stderr);
    va_start(args, format);
    vfprintf(stderr, format, args);
    va_end(args);
    fputc('\n', stderr);
    fflush(stderr);
}

static bool read_exact(void *buffer, size_t size, bool *eof_before_any) {
    uint8_t *out = buffer;
    size_t done = 0;
    *eof_before_any = false;
    while (done < size) {
        ssize_t got = read(STDIN_FILENO, out + done, size - done);
        if (got == 0) {
            if (done == 0)
                *eof_before_any = true;
            return false;
        }
        if (got < 0) {
            if (errno == EINTR)
                continue;
            return false;
        }
        done += (size_t)got;
    }
    return true;
}

static bool write_exact(const void *buffer, size_t size) {
    const uint8_t *in = buffer;
    size_t done = 0;
    while (done < size) {
        ssize_t put = write(STDOUT_FILENO, in + done, size - done);
        if (put < 0) {
            if (errno == EINTR)
                continue;
            return false;
        }
        if (put == 0)
            return false;
        done += (size_t)put;
    }
    return true;
}

static bool host_is_little_endian(void) {
    const uint16_t sample = UINT16_C(1);
    return *((const uint8_t *)&sample) == 1U;
}

static void header_init(struct vcodec_output_header *out, uint32_t status,
                        uint32_t flags, const struct identity *id) {
    memset(out, 0, sizeof(*out));
    out->magic = VCODEC_OUTPUT_MAGIC;
    out->version = VCODEC_PROTOCOL_VERSION;
    out->header_bytes = (uint16_t)sizeof(*out);
    out->status = status;
    out->flags = flags;
    if (id != NULL) {
        out->session_id = id->session_id;
        out->source_frame_id = id->source_frame_id;
        out->decode_token = id->decode_token;
        out->source_pts = id->source_pts;
    }
}

static bool write_status(uint32_t status, uint32_t flags, const struct identity *id) {
    struct vcodec_output_header out;
    header_init(&out, status, flags, id);
    return write_exact(&out, sizeof(out));
}

static bool write_fatal(void) {
    return write_status(VCODEC_STATUS_FATAL, VCODEC_OUTPUT_NEED_IDR, NULL);
}

static bool write_rgb565_frame(struct helper *helper, const struct identity *id) {
    struct vcodec_output_header out;
    struct codec_profile_stamp stamp;
    bool written;
    header_init(&out, VCODEC_STATUS_FRAME, 0, id);
    out.width = VCODEC_WIDTH;
    out.height = VCODEC_HEIGHT;
    out.pixel_format = VCODEC_PIXEL_RGB565LE;
    out.payload_bytes = VCODEC_RGB565_BYTES;
    stamp = codec_profile_begin(&helper->profile);
    out.payload_crc32 = helper_crc32(helper, helper->rgb565, VCODEC_RGB565_BYTES);
    codec_profile_end(&helper->profile, CODEC_PROFILE_CRC, stamp);
    stamp = codec_profile_begin(&helper->profile);
    written = write_exact(&out, sizeof(out)) && write_exact(helper->rgb565, VCODEC_RGB565_BYTES);
    codec_profile_end(&helper->profile, CODEC_PROFILE_WRITE, stamp);
    if (written) ++helper->profile.frames;
    return written;
}

static struct pending_token *find_pending(struct helper *helper, uint64_t token) {
    unsigned index;
    for (index = 0; index < VCODEC_MAX_PENDING_TOKENS; ++index)
        if (helper->pending[index].used && helper->pending[index].id.decode_token == token)
            return &helper->pending[index];
    return NULL;
}

static struct pending_token *insert_pending(struct helper *helper, const struct identity *id) {
    unsigned index;
    if (helper->pending_count >= VCODEC_MAX_PENDING_TOKENS || find_pending(helper, id->decode_token) != NULL)
        return NULL;
    for (index = 0; index < VCODEC_MAX_PENDING_TOKENS; ++index) {
        if (!helper->pending[index].used) {
            helper->pending[index].id = *id;
            helper->pending[index].used = true;
            ++helper->pending_count;
            return &helper->pending[index];
        }
    }
    return NULL;
}

static void erase_pending(struct helper *helper, struct pending_token *token) {
    token->used = false;
    if (helper->pending_count != 0U)
        --helper->pending_count;
}

static bool invalidate_pending(struct helper *helper, uint32_t status) {
    unsigned index;
    for (index = 0; index < VCODEC_MAX_PENDING_TOKENS; ++index) {
        if (helper->pending[index].used) {
            if (!write_status(status, VCODEC_OUTPUT_NEED_IDR, &helper->pending[index].id))
                return false;
            erase_pending(helper, &helper->pending[index]);
        }
    }
    return true;
}

static void reset_decoder_to_idr(struct helper *helper) {
    avcodec_flush_buffers(helper->codec_ctx);
    helper->need_idr = true;
}

#if MIMO_CODEC_ENABLE_NEON601
static bool frame_is_neon601_compatible(const AVFrame *frame) {
    /* The direct candidate does not resample chroma.  It deliberately accepts
     * only the left-sited progressive 4:2:0 contract, rather than guessing
     * about an unspecified or JPEG/centered chroma phase. */
    return frame->color_range == AVCOL_RANGE_MPEG &&
           (frame->colorspace == AVCOL_SPC_SMPTE170M || frame->colorspace == AVCOL_SPC_BT470BG) &&
           frame->chroma_location == AVCHROMA_LOC_LEFT &&
           frame->linesize[0] > 0 && frame->linesize[1] > 0 && frame->linesize[2] > 0;
}
#endif

static bool render_frame(struct helper *helper, const struct identity *id) {
    uint8_t *planes[4] = { helper->rgb565, NULL, NULL, NULL };
    int strides[4] = { (int)(VCODEC_WIDTH * 2U), 0, 0, 0 };
    int converted;
    struct codec_profile_stamp stamp;
    if (helper->frame->width != (int)VCODEC_WIDTH || helper->frame->height != (int)VCODEC_HEIGHT ||
        helper->frame->format != AV_PIX_FMT_YUV420P ||
        (helper->frame->flags & AV_FRAME_FLAG_INTERLACED) != 0) {
        diagnostic(helper, "decoded frame property mismatch: %dx%d fmt=%d interlaced=%d",
                   helper->frame->width, helper->frame->height, helper->frame->format,
                   (helper->frame->flags & AV_FRAME_FLAG_INTERLACED) != 0);
        return false;
    }
    stamp = codec_profile_begin(&helper->profile);
    if (helper->converter == VCODEC_CONVERTER_SWS) {
        helper->sws = sws_getCachedContext(helper->sws, helper->frame->width, helper->frame->height,
                                           (enum AVPixelFormat)helper->frame->format, (int)VCODEC_WIDTH,
                                           (int)VCODEC_HEIGHT, AV_PIX_FMT_RGB565LE, SWS_BILINEAR, NULL, NULL, NULL);
        if (helper->sws == NULL) {
            codec_profile_end(&helper->profile, CODEC_PROFILE_CONVERT, stamp);
            return false;
        }
        converted = sws_scale(helper->sws, (const uint8_t * const *)helper->frame->data,
                              helper->frame->linesize, 0, helper->frame->height, planes, strides);
        codec_profile_end(&helper->profile, CODEC_PROFILE_CONVERT, stamp);
        if (converted != (int)VCODEC_HEIGHT)
            return false;
    } else if (helper->converter == VCODEC_CONVERTER_NEON601) {
#if MIMO_CODEC_ENABLE_NEON601
        struct yuv420p_rgb565_image source;
        int convert_status;
        if (!frame_is_neon601_compatible(helper->frame)) {
            codec_profile_end(&helper->profile, CODEC_PROFILE_CONVERT, stamp);
            diagnostic(helper, "neon601 rejects color metadata: range=%d colorspace=%d chroma_location=%d strides=%d/%d/%d",
                       helper->frame->color_range, helper->frame->colorspace,
                       helper->frame->chroma_location, helper->frame->linesize[0],
                       helper->frame->linesize[1], helper->frame->linesize[2]);
            return false;
        }
        source.y = helper->frame->data[0];
        source.u = helper->frame->data[1];
        source.v = helper->frame->data[2];
        source.y_stride = (size_t)helper->frame->linesize[0];
        source.u_stride = (size_t)helper->frame->linesize[1];
        source.v_stride = (size_t)helper->frame->linesize[2];
        source.width = VCODEC_WIDTH;
        source.height = VCODEC_HEIGHT;
        convert_status = yuv420p_to_rgb565_neon(&source, helper->rgb565, (size_t)strides[0]);
        codec_profile_end(&helper->profile, CODEC_PROFILE_CONVERT, stamp);
        if (convert_status != YUV420P_RGB565_OK) {
            diagnostic(helper, "neon601 conversion failed: %d", convert_status);
            return false;
        }
#else
        codec_profile_end(&helper->profile, CODEC_PROFILE_CONVERT, stamp);
        diagnostic(helper, "neon601 selected in a helper built without neon601 support");
        return false;
#endif
    } else {
        codec_profile_end(&helper->profile, CODEC_PROFILE_CONVERT, stamp);
        diagnostic(helper, "invalid converter selection");
        return false;
    }
    return write_rgb565_frame(helper, id);
}

static bool drain_frames(struct helper *helper) {
    while (true) {
        struct codec_profile_stamp stamp = codec_profile_begin(&helper->profile);
        int ret = avcodec_receive_frame(helper->codec_ctx, helper->frame);
        codec_profile_end(&helper->profile, CODEC_PROFILE_DECODE, stamp);
        struct pending_token *pending;
        if (ret == AVERROR(EAGAIN) || ret == AVERROR_EOF)
            return true;
        if (ret < 0) {
            char message[AV_ERROR_MAX_STRING_SIZE];
            av_strerror(ret, message, sizeof(message));
            diagnostic(helper, "avcodec_receive_frame failed: %s", message);
            return false;
        }
        if (helper->frame->pts == AV_NOPTS_VALUE || helper->frame->pts < 1) {
            diagnostic(helper, "decoder emitted an AVFrame without usable token PTS");
            return false;
        }
        pending = find_pending(helper, (uint64_t)helper->frame->pts);
        if (pending == NULL) {
            diagnostic(helper, "decoder emitted unknown or duplicate token %" PRId64, helper->frame->pts);
            return false;
        }
        if (!render_frame(helper, &pending->id))
            return false;
        erase_pending(helper, pending);
        av_frame_unref(helper->frame);
    }
}

static bool validate_decode_header(const struct vcodec_input_header *in, struct identity *id) {
    if (in->magic != VCODEC_INPUT_MAGIC || in->version != VCODEC_PROTOCOL_VERSION ||
        in->header_bytes != sizeof(*in) || in->command != VCODEC_COMMAND_DECODE ||
        (in->flags & ~VCODEC_INPUT_SELF_CONTAINED_IDR) != 0U || in->decode_token == 0U ||
        in->decode_token > INT64_MAX ||
        in->width != VCODEC_WIDTH || in->height != VCODEC_HEIGHT ||
        in->codec != VCODEC_CODEC_H264_ANNEX_B || in->access_unit_bytes == 0U ||
        in->access_unit_bytes > VCODEC_MAX_ACCESS_UNIT_BYTES || in->reserved != 0U)
        return false;
    id->session_id = in->session_id;
    id->source_frame_id = in->source_frame_id;
    id->decode_token = in->decode_token;
    id->source_pts = in->source_pts;
    return true;
}

static bool validate_control_header(const struct vcodec_input_header *in) {
    return in->magic == VCODEC_INPUT_MAGIC && in->version == VCODEC_PROTOCOL_VERSION &&
           in->header_bytes == sizeof(*in) && (in->command == VCODEC_COMMAND_FLUSH ||
           in->command == VCODEC_COMMAND_STOP) && in->flags == 0U && in->session_id == 0U &&
           in->source_frame_id == 0U && in->decode_token == 0U && in->source_pts == 0 &&
           in->width == 0U && in->height == 0U && in->codec == 0U && in->access_unit_bytes == 0U &&
           in->access_unit_crc32 == 0U && in->reserved == 0U;
}

static bool recover_after_input_error(struct helper *helper, const struct identity *id, uint32_t status) {
    struct pending_token *current;
    if (!write_status(status, VCODEC_OUTPUT_NEED_IDR, id))
        return false;
    /* A packet can have been reserved before av_new_packet/send fails. Its
     * terminal record above must be the only terminal record for that token. */
    current = find_pending(helper, id->decode_token);
    if (current != NULL)
        erase_pending(helper, current);
    if (!invalidate_pending(helper, VCODEC_STATUS_FLUSHED))
        return false;
    reset_decoder_to_idr(helper);
    return true;
}

static bool process_decode(struct helper *helper, const struct vcodec_input_header *in) {
    struct identity id;
    uint8_t *access_unit = NULL;
    bool eof = false;
    bool self_contained;
    int ret;
    bool au_valid;
    struct codec_profile_stamp stamp;
    char error_text[AV_ERROR_MAX_STRING_SIZE];
    if (!validate_decode_header(in, &id)) {
        diagnostic(helper, "invalid DECODE header");
        return false; /* stream cannot be trusted enough to resynchronize */
    }
    access_unit = malloc(in->access_unit_bytes);
    if (access_unit == NULL)
        return recover_after_input_error(helper, &id, VCODEC_STATUS_INPUT_ERROR);
    if (!read_exact(access_unit, in->access_unit_bytes, &eof)) {
        free(access_unit);
        diagnostic(helper, "short EOF/read inside AU");
        return false;
    }
    self_contained = (in->flags & VCODEC_INPUT_SELF_CONTAINED_IDR) != 0U;
    stamp = codec_profile_begin(&helper->profile);
    au_valid = !(helper_crc32(helper, access_unit, in->access_unit_bytes) != in->access_unit_crc32 ||
        (helper->need_idr && !self_contained) ||
        !vcodec_validate_h264_au(access_unit, in->access_unit_bytes, self_contained));
    codec_profile_end(&helper->profile, CODEC_PROFILE_VALIDATE, stamp);
    if (!au_valid) {
        free(access_unit);
        diagnostic(helper, "AU rejected by CRC, recovery state, or constrained-baseline contract");
        return recover_after_input_error(helper, &id, VCODEC_STATUS_INPUT_ERROR);
    }
    if (!helper->have_session) {
        if (!self_contained) {
            free(access_unit);
            return recover_after_input_error(helper, &id, VCODEC_STATUS_INPUT_ERROR);
        }
        helper->active_session = id.session_id;
        helper->have_session = true;
    } else if (id.session_id != helper->active_session) {
        free(access_unit);
        diagnostic(helper, "session switch requires a new helper process");
        return recover_after_input_error(helper, &id, VCODEC_STATUS_INPUT_ERROR);
    }
    if (id.decode_token <= helper->last_token || find_pending(helper, id.decode_token) != NULL) {
        free(access_unit);
        diagnostic(helper, "duplicate or non-monotonic decode token is protocol-fatal");
        return false;
    }
    if (insert_pending(helper, &id) == NULL) {
        free(access_unit);
        diagnostic(helper, "pending-token capacity exhausted");
        return recover_after_input_error(helper, &id, VCODEC_STATUS_INPUT_ERROR);
    }
    helper->last_token = id.decode_token;
    av_packet_unref(helper->packet);
    ret = av_new_packet(helper->packet, (int)in->access_unit_bytes);
    if (ret < 0) {
        free(access_unit);
        return recover_after_input_error(helper, &id, VCODEC_STATUS_DECODE_ERROR);
    }
    memcpy(helper->packet->data, access_unit, in->access_unit_bytes);
    free(access_unit);
    helper->packet->pts = (int64_t)id.decode_token;
    helper->packet->dts = (int64_t)id.decode_token;
    stamp = codec_profile_begin(&helper->profile);
    ret = avcodec_send_packet(helper->codec_ctx, helper->packet);
    codec_profile_end(&helper->profile, CODEC_PROFILE_DECODE, stamp);
    if (ret == AVERROR(EAGAIN)) {
        /* A legal persistent decoder may still hold output from earlier
         * packets. Drain it, then retry this same immutable packet once. */
        if (!drain_frames(helper))
            return false;
        stamp = codec_profile_begin(&helper->profile);
        ret = avcodec_send_packet(helper->codec_ctx, helper->packet);
        codec_profile_end(&helper->profile, CODEC_PROFILE_DECODE, stamp);
    }
    if (ret < 0) {
        av_strerror(ret, error_text, sizeof(error_text));
        diagnostic(helper, "avcodec_send_packet failed: %s", error_text);
        return recover_after_input_error(helper, &id, VCODEC_STATUS_DECODE_ERROR);
    }
    if (!drain_frames(helper))
        return false;
    helper->need_idr = false;
    return true;
}

static bool init_helper(struct helper *helper) {
    int ret;
    memset(helper, 0, sizeof(*helper));
    helper->profile.enabled = getenv("MIMO_CODEC_PROFILE") != NULL &&
        strcmp(getenv("MIMO_CODEC_PROFILE"), "1") == 0;
    /* Opt-in only until the complete helper A/B is measured on the target.
     * Read-only per-instance tables avoid a process-global lazy-init race. */
    helper->crc_slice8 = getenv("MIMO_CODEC_CRC") != NULL &&
        strcmp(getenv("MIMO_CODEC_CRC"), "slice8") == 0;
    if (helper->crc_slice8)
        crc32_slice8_init(&helper->crc_context);
    if (!vcodec_converter_select(getenv("MIMO_CODEC_CONVERTER"),
                                 MIMO_CODEC_ENABLE_NEON601 != 0, &helper->converter)) {
        diagnostic(helper, "invalid MIMO_CODEC_CONVERTER (valid: sws%s)",
                   MIMO_CODEC_ENABLE_NEON601 ? ", neon601" : "");
        return false;
    }
    helper->need_idr = true;
    helper->codec = avcodec_find_decoder(AV_CODEC_ID_H264);
    helper->codec_ctx = helper->codec == NULL ? NULL : avcodec_alloc_context3(helper->codec);
    helper->packet = av_packet_alloc();
    helper->frame = av_frame_alloc();
    helper->rgb565_capacity = VCODEC_RGB565_BYTES;
    helper->rgb565 = malloc(helper->rgb565_capacity);
    if (helper->codec_ctx == NULL || helper->packet == NULL || helper->frame == NULL || helper->rgb565 == NULL)
        return false;
    helper->codec_ctx->thread_count = 1;
    helper->codec_ctx->thread_type = FF_THREAD_SLICE;
    helper->codec_ctx->flags |= AV_CODEC_FLAG_LOW_DELAY;
    helper->codec_ctx->pkt_timebase = (AVRational){1, 1};
    ret = avcodec_open2(helper->codec_ctx, helper->codec, NULL);
    if (ret < 0) {
        char message[AV_ERROR_MAX_STRING_SIZE];
        av_strerror(ret, message, sizeof(message));
        diagnostic(helper, "avcodec_open2 failed: %s", message);
        return false;
    }
    return true;
}

static void destroy_helper(struct helper *helper) {
    sws_freeContext(helper->sws);
    av_frame_free(&helper->frame);
    av_packet_free(&helper->packet);
    avcodec_free_context(&helper->codec_ctx);
    free(helper->rgb565);
}

int main(void) {
    struct helper helper;
    struct vcodec_input_header in;
    bool eof;
    int exit_code = 1;
    if (!host_is_little_endian()) {
        fputs("mimo-video-codec-helper: protocol requires a little-endian host\n", stderr);
        return 2;
    }
    signal(SIGPIPE, SIG_IGN);
    if (!init_helper(&helper)) {
        diagnostic(&helper, "decoder initialization failed");
        destroy_helper(&helper);
        return 2;
    }
    while (true) {
        if (!read_exact(&in, sizeof(in), &eof)) {
            if (!eof) {
                diagnostic(&helper, "short header or stdin read error");
                (void)write_fatal();
            }
            break;
        }
        if (in.command == VCODEC_COMMAND_DECODE) {
            if (!process_decode(&helper, &in)) {
                (void)write_fatal();
                break;
            }
        } else if (!validate_control_header(&in)) {
            diagnostic(&helper, "invalid control header");
            (void)write_fatal();
            break;
        } else if (in.command == VCODEC_COMMAND_FLUSH) {
            if (!invalidate_pending(&helper, VCODEC_STATUS_FLUSHED))
                break;
            reset_decoder_to_idr(&helper);
            if (!write_status(VCODEC_STATUS_FLUSH_ACK, VCODEC_OUTPUT_NEED_IDR, NULL))
                break;
        } else { /* STOP */
            if (!invalidate_pending(&helper, VCODEC_STATUS_FLUSHED))
                break;
            reset_decoder_to_idr(&helper);
            if (!write_status(VCODEC_STATUS_STOP_ACK, VCODEC_OUTPUT_NEED_IDR, NULL))
                break;
            exit_code = 0;
            break;
        }
    }
    report_profile(&helper);
    destroy_helper(&helper);
    return exit_code;
}
