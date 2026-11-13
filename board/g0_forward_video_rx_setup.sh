phy=/sys/bus/iio/devices/iio:device0
test "$(cat "$phy/name")" = ad9361-phy
test "$(cat "$phy/ensm_mode")" = fdd
test "$(cat "$phy/out_altvoltage1_TX_LO_powerdown")" = 1
echo 1 > "$phy/out_altvoltage0_RX_LO_powerdown"
echo 4000000 > "$phy/in_voltage_sampling_frequency"
echo A_BALANCED > "$phy/in_voltage0_rf_port_select"
echo A_BALANCED > "$phy/in_voltage1_rf_port_select"
echo manual > "$phy/in_voltage0_gain_control_mode"
echo manual > "$phy/in_voltage1_gain_control_mode"
echo 60 > "$phy/in_voltage0_hardwaregain"
echo 60 > "$phy/in_voltage1_hardwaregain"
echo 5500000000 > "$phy/out_altvoltage0_RX_LO_frequency"
echo 4000000 > "$phy/in_voltage_rf_bandwidth"
test "$(cat "$phy/in_voltage_sampling_frequency")" = 4000000
v3_lo=$(cat "$phy/out_altvoltage0_RX_LO_frequency")
test "$v3_lo" -ge 5499999990
test "$v3_lo" -le 5500000010
echo RX_LO_actual_hz=$v3_lo
cat "$phy/in_voltage_rf_bandwidth"
cat "$phy/in_voltage0_rf_port_select"
cat "$phy/in_voltage1_rf_port_select"
cat "$phy/in_voltage0_hardwaregain"
cat "$phy/in_voltage1_hardwaregain"
v3_ensm=$(cat "$phy/ensm_mode")
echo RX_ENSM=$v3_ensm
test "$v3_ensm" = fdd
test "$(cat "$phy/out_altvoltage1_TX_LO_powerdown")" = 1
echo 0 > "$phy/out_altvoltage0_RX_LO_powerdown"
test "$(cat "$phy/out_altvoltage1_TX_LO_powerdown")" = 1
