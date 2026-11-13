#!/bin/sh
# Foreground receiver for a local, matched V3 bundle.
set -eu
test "$#" = 1 || { echo "usage: $0 STATE_DIRECTORY" >&2; exit 2; }
bundle=$(CDPATH= cd "$(dirname "$0")" && pwd)
state=$1
umask 077
mkdir -p "$state"
exec 9>>"$state/receiver.lock"
flock -n 9 || { echo "V3 receiver already owns this state directory" >&2; exit 1; }

test "$(uname -r)" = 6.12.0-xilinx
test "$(busybox devmem 0x7c430000)" = 0x50533356
test "$(busybox devmem 0x7c430008)" = 0x00000000
test ! -d /sys/module/g0_adc_capture
test ! -d /sys/module/g0_dma_ring
test -x "$bundle/g0-stream-decode"
test -x "$bundle/runtime/ld-linux-armhf.so.3"

phy=/sys/bus/iio/devices/iio:device0
test "$(cat "$phy/name")" = ad9361-phy
child=
stopping=0
stop_receiver() {
    stopping=1
    if [ -n "$child" ]; then kill -TERM "$child" 2>/dev/null || :; fi
}
power_down_rx() {
    result=$?
    trap - EXIT
    if ! echo 1 > "$phy/out_altvoltage0_RX_LO_powerdown"; then result=1; fi
    exit "$result"
}
trap stop_receiver TERM INT
trap power_down_rx EXIT
echo 1 > "$phy/out_altvoltage1_TX_LO_powerdown"
test "$(cat "$phy/out_altvoltage1_TX_LO_powerdown")" = 1
echo 1 > "$phy/out_altvoltage0_RX_LO_powerdown"
if ! grep -q ' /sys/kernel/debug debugfs ' /proc/mounts; then
    mount -t debugfs debugfs /sys/kernel/debug
fi
test "$(cat /sys/kernel/debug/iio/iio:device0/bist_prbs)" = 0

for name in g0_source_ring cec i2c-xiic clk-axi-clkgen adv7511 adi_axi_hdmi_present; do
    if [ ! -d "/sys/module/$(echo "$name" | tr '-' '_')" ]; then
        insmod "$bundle/modules/$name.ko"
    fi
done
test -c /dev/v3-g0-source-ring
test -c /dev/dri/card0
[ "$stopping" = 0 ] || exit 0
. "$bundle/g0_forward_video_rx_setup.sh"
[ "$stopping" = 0 ] || exit 0

"$bundle/runtime/ld-linux-armhf.so.3" --library-path "$bundle/runtime" \
    "$bundle/g0-stream-decode" /dev/v3-g0-source-ring "$state/report.json" \
    paced source-dma-live hdmi &
child=$!
if [ "$stopping" = 1 ]; then stop_receiver; fi
result=0
wait "$child" || result=$?
if [ "$stopping" = 1 ] && [ "$result" -gt 128 ]; then
    result=0
    wait "$child" || result=$?
fi
exit "$result"
