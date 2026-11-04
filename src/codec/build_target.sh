#!/usr/bin/env bash
# Cross-compile only the helper against an already-built isolated FFmpeg
# staging tree.  This script never invokes Buildroot and never touches SD/JTAG.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SYSROOT=""
CROSS_PREFIX=""
OUT="$DIR/out"
ENABLE_NEON601=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --sysroot) SYSROOT="$2"; shift 2 ;;
    --cross-prefix) CROSS_PREFIX="$2"; shift 2 ;;
    --out) OUT="$2"; shift 2 ;;
    --enable-neon601) ENABLE_NEON601=1; shift ;;
    *) echo "usage: $0 --sysroot <staging> --cross-prefix <triplet-prefix> [--out <dir>] [--enable-neon601]" >&2; exit 2 ;;
  esac
done

[[ -n "$SYSROOT" && -d "$SYSROOT/usr/include/libavcodec" ]] || { echo "FFmpeg staging headers missing" >&2; exit 2; }
[[ -n "$CROSS_PREFIX" && -x "${CROSS_PREFIX}gcc" ]] || { echo "cross compiler missing" >&2; exit 2; }
mkdir -p "$OUT"

CC="${CROSS_PREFIX}gcc"
sources=("$DIR/video_codec_helper.c" "$DIR/h264_annexb_validator.c" "$DIR/codec_converter_policy.c")
neon_flags="-DMIMO_CODEC_ENABLE_NEON601=0"
if [ "$ENABLE_NEON601" -eq 1 ]; then
  # The direct converter is built only for the actual Cortex-A9/ARMv7 NEON
  # target.  Default builds retain the portable libswscale path exactly.
  sources+=("$DIR/diagnostics/neon_converter/yuv420p_rgb565_neon.c")
  neon_flags="-DMIMO_CODEC_ENABLE_NEON601=1 -mcpu=cortex-a9 -mfpu=neon -mfloat-abi=hard"
fi
"$CC" --sysroot="$SYSROOT" -std=c11 -O3 -g -Wall -Wextra -Werror -Wpedantic $neon_flags \
  -D_POSIX_C_SOURCE=200809L -I"$DIR" -I"$SYSROOT/usr/include" \
  "${sources[@]}" -L"$SYSROOT/usr/lib" \
  -lavcodec -lavutil -lswscale -lm -lz -lpthread \
  -Wl,-rpath-link,"$SYSROOT/usr/lib" -o "$OUT/mimo-video-codec-helper"
"${CROSS_PREFIX}readelf" -h "$OUT/mimo-video-codec-helper" | grep -Eq 'Machine:.*ARM' || {
  echo "output is not ARM" >&2; exit 3;
}
if [ "$ENABLE_NEON601" -eq 1 ]; then
  echo "built non-deployable ARM candidate with explicit neon601 support: $OUT/mimo-video-codec-helper"
else
  echo "built non-deployable ARM candidate with sws-only policy: $OUT/mimo-video-codec-helper"
fi
