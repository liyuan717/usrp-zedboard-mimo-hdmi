#include "codec_converter_policy.h"

#include <string.h>

bool vcodec_converter_select(const char *value, bool neon601_compiled,
                             enum vcodec_converter *out) {
    if (out == NULL)
        return false;
    if (value == NULL || strcmp(value, "sws") == 0) {
        *out = VCODEC_CONVERTER_SWS;
        return true;
    }
    if (strcmp(value, "neon601") == 0 && neon601_compiled) {
        *out = VCODEC_CONVERTER_NEON601;
        return true;
    }
    return false;
}

const char *vcodec_converter_name(enum vcodec_converter converter) {
    switch (converter) {
    case VCODEC_CONVERTER_SWS:
        return "sws";
    case VCODEC_CONVERTER_NEON601:
        return "neon601";
    default:
        return "invalid";
    }
}
