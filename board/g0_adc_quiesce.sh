test "$(cat /sys/kernel/debug/iio/iio:device0/bist_prbs)" = 0
for addr in 0x80000400 0x80000440 0x80000480 0x800004c0; do
 echo "$addr 0x251" > /sys/kernel/debug/iio/iio:device2/direct_reg_access
 echo "$addr" > /sys/kernel/debug/iio/iio:device2/direct_reg_access
 test "$(cat /sys/kernel/debug/iio/iio:device2/direct_reg_access)" = 0x251
done
cat /sys/bus/iio/devices/iio:device0/out_altvoltage0_RX_LO_powerdown
cat /sys/bus/iio/devices/iio:device0/out_altvoltage1_TX_LO_powerdown
cat /sys/bus/platform/devices/7c430000.rf-receiver-v2/capture_state
cat /sys/class/fpga_manager/fpga0/state
head -1 /proc/mounts
