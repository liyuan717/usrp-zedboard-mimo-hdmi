# Run only through the shared hardware lease. G0 measurement, no TX owner.
if ! grep -q ' /sys/kernel/debug ' /proc/mounts; then mount -t debugfs debugfs /sys/kernel/debug; fi
phy=/sys/bus/iio/devices/iio:device0
test "$(cat "$phy/name")" = ad9361-phy
test "$(cat "$phy/out_altvoltage1_TX_LO_powerdown")" = 1
test "$(cat /sys/kernel/debug/iio/iio:device0/bist_prbs)" = 0
echo 1 > "$phy/out_altvoltage0_RX_LO_powerdown"
echo 4000000 > "$phy/in_voltage_sampling_frequency"
test "$(cat "$phy/in_voltage_sampling_frequency")" = 4000000
echo A_BALANCED > "$phy/in_voltage0_rf_port_select"
echo A_BALANCED > "$phy/in_voltage1_rf_port_select"
echo manual > "$phy/in_voltage0_gain_control_mode"
echo manual > "$phy/in_voltage1_gain_control_mode"
echo 40 > "$phy/in_voltage0_hardwaregain"
echo 40 > "$phy/in_voltage1_hardwaregain"
echo 5500000000 > "$phy/out_altvoltage0_RX_LO_frequency"
echo 2000000 > "$phy/in_voltage_rf_bandwidth"
test "$(cat "$phy/out_altvoltage1_TX_LO_powerdown")" = 1
cat "$phy/in_voltage_sampling_frequency"
cat "$phy/in_voltage_rf_bandwidth"
cat "$phy/in_voltage0_rf_port_select"
cat "$phy/in_voltage1_rf_port_select"
cat "$phy/in_voltage0_gain_control_mode"
cat "$phy/in_voltage1_gain_control_mode"
cat "$phy/in_voltage0_hardwaregain"
cat "$phy/in_voltage1_hardwaregain"
cat "$phy/out_altvoltage0_RX_LO_frequency"
echo 0 > "$phy/out_altvoltage0_RX_LO_powerdown"
test "$(cat "$phy/out_altvoltage1_TX_LO_powerdown")" = 1
cat /sys/bus/platform/devices/7c430000.rf-receiver-v2/capture_state
