#!/usr/bin/env sh
# Pure C policy test: no FFmpeg, board, RF, DMA, HDMI, or wire input.
set -eu
out=${1:?usage: $0 OUT_DIR}
src=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
mkdir -p "$out"
${CC:-cc} -std=c11 -O2 -Wall -Wextra -Werror -Wpedantic -I"$src" \
  "$src/codec_converter_policy.c" "$src/test_converter_policy.c" \
  -o "$out/test_converter_policy"
"$out/test_converter_policy"
printf '%s\n' 'converter policy test passed'
