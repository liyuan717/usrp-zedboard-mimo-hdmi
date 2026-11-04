/* Strict, libav-independent H.264 Annex-B admission checks for vcodec v1. */
#ifndef MIMO_H264_ANNEXB_VALIDATOR_H
#define MIMO_H264_ANNEXB_VALIDATOR_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/*
 * Validate the exact narrow input contract used by video_codec_helper.c:
 * leading/unique AUD, optional SEI, constrained-baseline SPS where required,
 * a single progressive P or IDR slice, and no NAL after VCL.  This performs
 * no decode; libavcodec remains the final syntax and picture validator.
 */
bool vcodec_validate_h264_au(const uint8_t *bytes, size_t size, bool self_contained_idr);

#endif
