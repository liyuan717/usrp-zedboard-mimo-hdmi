# Generated SPI mode-1 (CPOL=0/CPHA=1) budget; not measured 2.5-V timing.
# spi_mode=1
# MISO: external rising-edge launch. MOSI: external falling-edge capture.
# CS: both edges conservatively cover setup before first rising and hold after last falling.
# Controller ext_spi_clk input capture remains conservatively constrained without CE waivers.
# profile_status=PREBOARD_CONSERVATIVE_APPROVED
set rf_spi_source [get_pins -hier -filter {NAME =~ */rf_spi/ext_spi_clk}]
# Source cardinality is checked by the independent routed topology audit.
create_generated_clock -name ad9361_spi_clk -source $rf_spi_source -divide_by 16 [get_ports spi_clk]
set_input_delay -clock ad9361_spi_clk -min -0.250 [get_ports spi_miso]
set_input_delay -clock ad9361_spi_clk -max 14.250 [get_ports spi_miso]
set_output_delay -clock ad9361_spi_clk -clock_fall -min -1.750 [get_ports spi_mosi]
set_output_delay -clock ad9361_spi_clk -clock_fall -max 2.750 [get_ports spi_mosi]
set_output_delay -clock ad9361_spi_clk -min -0.750 [get_ports spi_csn]
set_output_delay -clock ad9361_spi_clk -max 1.750 [get_ports spi_csn]
set_output_delay -clock ad9361_spi_clk -clock_fall -add_delay -min -0.750 [get_ports spi_csn]
set_output_delay -clock ad9361_spi_clk -clock_fall -add_delay -max 1.750 [get_ports spi_csn]
