#!/usr/bin/env bash
set -euo pipefail
TASK_USRP_AIR_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
TASK_USRP_FLAGS=()
if [[ -n "${UHD_PREFIX:-}" ]]; then
    [[ -f "$UHD_PREFIX/include/uhd/usrp/multi_usrp.hpp" ]] || { echo 'UHD_PREFIX does not contain UHD headers' >&2; exit 2; }
    TASK_USRP_FLAGS+=("-I$UHD_PREFIX/include" "-L$UHD_PREFIX/lib" "-Wl,-rpath,$UHD_PREFIX/lib" -luhd)
else
    pkg-config --exists uhd || { echo 'Install UHD development files or set UHD_PREFIX to your isolated UHD installation' >&2; exit 2; }
    read -r -a TASK_USRP_FLAGS <<< "$(pkg-config --cflags --libs uhd)"
fi
"${CXX:-g++}" -std=c++14 -O2 -Wall -Wextra -Werror -pthread \
    "$TASK_USRP_AIR_ROOT/continuous_tx.cpp" "${TASK_USRP_FLAGS[@]}" \
    -o "$TASK_USRP_AIR_ROOT/continuous_tx"
