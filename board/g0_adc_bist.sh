uname -a
cat /proc/sys/kernel/random/boot_id
head -1 /proc/mounts
if ! test -d /sys/kernel/debug/iio; then mount -t debugfs debugfs /sys/kernel/debug; fi
phy=
adc=
for d in /sys/bus/iio/devices/iio:device*; do
 name=$(cat "$d/name")
 echo "$d $name"
 if test "$name" = ad9361-phy; then phy=$d; fi
 if test "$name" = cf-ad9361-lpc; then adc=$d; fi
done
test -n "$phy"
test -n "$adc"
test "$(cat "$phy/out_altvoltage1_TX_LO_powerdown")" = 1
test "$(cat "$phy/out_altvoltage0_RX_LO_powerdown")" = 1
cat "$phy/in_voltage_sampling_frequency"
cat "$phy/out_voltage_sampling_frequency"
cat "$phy/ensm_mode"
echo -60 > "$phy/out_voltage0_hardwaregain"
echo -60 > "$phy/out_voltage1_hardwaregain"
wget -q -O /tmp/g0_adc_capture.ko http://169.254.209.83:8013/g0adc/g0_adc_capture.ko
test "$(busybox crc32 /tmp/g0_adc_capture.ko)" = "eb521d1a /tmp/g0_adc_capture.ko"
if ! test -f /sys/bus/platform/devices/7c430000.rf-receiver-v2/capture_state; then insmod /tmp/g0_adc_capture.ko; fi
owner=/sys/bus/platform/devices/7c430000.rf-receiver-v2
cat "$owner/capture_state"
phy_debug=/sys/kernel/debug/iio/$(basename "$phy")
adc_debug=/sys/kernel/debug/iio/$(basename "$adc")
for addr in 0x80000400 0x80000440 0x80000480 0x800004c0; do
 echo "$addr 0x51" > "$adc_debug/direct_reg_access"
 echo "$addr" > "$adc_debug/direct_reg_access"
 test "$(cat "$adc_debug/direct_reg_access")" = 0x51
done
# Disable IQ/DC correction in FPGA for exact digital PRBS comparison. All
# four voltage lanes are enabled; tag lanes are physically fixed enabled.
restore_bist() { echo 0 > "$phy_debug/bist_prbs"; }
trap restore_bist EXIT
echo 2 > "$phy_debug/bist_prbs"
echo PN9 > "$adc_debug/pseudorandom_err_check"
sleep 1
cat "$adc_debug/pseudorandom_err_check"
echo 262144 > "$owner/capture_records"
cat "$owner/capture_state"
cat "$owner/capture_data" > /tmp/g0-pn9-0.raw
wc -c /tmp/g0-pn9-0.raw
busybox crc32 /tmp/g0-pn9-0.raw
python3 -c "import socket,pathlib; s=socket.create_connection(('169.254.209.83',8014),5); s.sendall(pathlib.Path('/tmp/g0-pn9-0.raw').read_bytes()); s.shutdown(socket.SHUT_WR); s.close()"
echo 131072 > "$owner/capture_records"
cat "$owner/capture_state"
cat "$owner/capture_data" > /tmp/g0-pn9-1.raw
wc -c /tmp/g0-pn9-1.raw
busybox crc32 /tmp/g0-pn9-1.raw
python3 -c "import socket,pathlib; s=socket.create_connection(('169.254.209.83',8014),5); s.sendall(pathlib.Path('/tmp/g0-pn9-1.raw').read_bytes()); s.shutdown(socket.SHUT_WR); s.close()"
cat "$adc_debug/pseudorandom_err_check"
restore_bist
trap - EXIT
cat "$phy_debug/bist_prbs"
cat "$phy/out_altvoltage1_TX_LO_powerdown"
cat "$phy/out_altvoltage0_RX_LO_powerdown"
cat "$owner/capture_state"
dmesg | tail -18
