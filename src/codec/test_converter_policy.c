#include "codec_converter_policy.h"

#include <assert.h>
#include <stddef.h>

int main(void) {
    enum vcodec_converter selected = VCODEC_CONVERTER_NEON601;

    assert(vcodec_converter_select(NULL, false, &selected));
    assert(selected == VCODEC_CONVERTER_SWS);
    assert(vcodec_converter_select("sws", false, &selected));
    assert(selected == VCODEC_CONVERTER_SWS);
    assert(!vcodec_converter_select("neon601", false, &selected));
    assert(vcodec_converter_select("neon601", true, &selected));
    assert(selected == VCODEC_CONVERTER_NEON601);
    assert(!vcodec_converter_select("NEON601", true, &selected));
    assert(!vcodec_converter_select("", true, &selected));
    assert(!vcodec_converter_select("sws ", true, &selected));
    assert(!vcodec_converter_select("sws", true, NULL));
    assert(vcodec_converter_name(VCODEC_CONVERTER_SWS)[0] == 's');
    assert(vcodec_converter_name(VCODEC_CONVERTER_NEON601)[0] == 'n');
    assert(vcodec_converter_name((enum vcodec_converter)42)[0] == 'i');
    return 0;
}
