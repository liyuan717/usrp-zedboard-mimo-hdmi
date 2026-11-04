#!/bin/sh
set -eu

presenter=$1

if output=$("$presenter" --backend null --codec-output-fd 0 </dev/null 2>&1); then
  echo "codec fd 0 unexpectedly accepted EOF without STOP_ACK" >&2
  exit 1
fi
printf '%s\n' "$output" | grep -F "codec stdout EOF before STOP_ACK" >/dev/null

if output=$("$presenter" --backend null --codec-output-fd 0 --max-records 1 2>&1); then
  echo "codec mode unexpectedly accepted max-records" >&2
  exit 1
fi
printf '%s\n' "$output" | grep -F "require helper STOP_ACK" >/dev/null
