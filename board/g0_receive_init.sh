#!/bin/sh
# BusyBox init actions for the dedicated V3 receiver root.
set -eu
PATH=/bin:/sbin
export PATH

case "${1:-}" in
    mounts)
        mount -t devtmpfs devtmpfs /dev
        mount -t proc proc /proc
        mount -t sysfs sysfs /sys
        mount -t tmpfs -o mode=0755,nosuid,nodev,size=8m tmpfs /run
        : > /run/v3-mounts-ready
        ;;
    receiver)
        test -f /run/v3-mounts-ready
        phy=/sys/bus/iio/devices/iio:device0
        tx=$phy/out_altvoltage1_TX_LO_powerdown
        for second in $(busybox seq 0 15); do
            if [ -w "$tx" ] && [ "$(cat "$phy/name")" = ad9361-phy ]; then
                echo 1 > "$tx"
                test "$(cat "$tx")" = 1
                exec /opt/video-radio-v3/g0_receive_service.sh /run/video-radio-v3
            fi
            [ "$second" = 15 ] || sleep 1
        done
        echo "V3 receiver startup timed out waiting for AD9361" >&2
        exit 1
        ;;
    *)
        exit 2
        ;;
esac
