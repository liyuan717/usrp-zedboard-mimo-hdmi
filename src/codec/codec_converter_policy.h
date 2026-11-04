/* Runtime converter selection policy; independent of FFmpeg and wire ABI. */
#ifndef MIMO_CODEC_CONVERTER_POLICY_H
#define MIMO_CODEC_CONVERTER_POLICY_H

#include <stdbool.h>

enum vcodec_converter {
    VCODEC_CONVERTER_SWS = 0,
    VCODEC_CONVERTER_NEON601 = 1
};

/*
 * ``value`` is the literal MIMO_CODEC_CONVERTER environment value, or NULL
 * when it is unset.  The default is the historical libswscale path.  Unknown
 * values and neon601 without a binary compiled for it are errors; callers
 * must not quietly select a different color conversion path.
 */
bool vcodec_converter_select(const char *value, bool neon601_compiled,
                             enum vcodec_converter *out);
const char *vcodec_converter_name(enum vcodec_converter converter);

#endif
