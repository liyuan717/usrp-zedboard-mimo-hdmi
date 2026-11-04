#!/bin/sh
set -eu

presenter=$1
proof=$(mktemp)
trap 'rm -f "$proof"' EXIT HUP INT TERM

# The output already exists: exclusive proof creation must surface a failure.
# Codec EOF is separately invalid too, but this test specifically requires the
# proof-write diagnostic so an earlier CLI validation error cannot satisfy it.
if output=$("$presenter" --backend null --codec-output-fd 0 \
    --proof-json "$proof" </dev/null 2>&1); then
  echo "presenter unexpectedly accepted an existing proof destination" >&2
  exit 1
fi
printf '%s\n' "$output" | grep -F "proof error: proof write failed or destination already exists" >/dev/null
