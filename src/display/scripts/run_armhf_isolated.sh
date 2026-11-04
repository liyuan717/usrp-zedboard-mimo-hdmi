#!/bin/sh
# Execute the packaged ARM presenter without changing system-wide loader paths.
set -eu
bundle_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec "$bundle_dir/lib/ld-linux-armhf.so.3" \
  --library-path "$bundle_dir/lib" \
  "$bundle_dir/hdmi-presenter" "$@"
