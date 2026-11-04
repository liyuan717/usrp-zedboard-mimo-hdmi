# Standard ZedBoard ADV7511 HDMI output and onboard I2C mux pins.
# Used only when ENABLE_HDMI=1 is selected.

set_property -dict {PACKAGE_PIN W18 IOSTANDARD LVCMOS33} [get_ports hdmi_out_clk]
set_property -dict {PACKAGE_PIN W17 IOSTANDARD LVCMOS33 IOB TRUE} [get_ports hdmi_vsync]
set_property -dict {PACKAGE_PIN V17 IOSTANDARD LVCMOS33 IOB TRUE} [get_ports hdmi_hsync]
set_property -dict {PACKAGE_PIN U16 IOSTANDARD LVCMOS33 IOB TRUE} [get_ports hdmi_data_e]
set_property -dict {PACKAGE_PIN Y13 IOSTANDARD LVCMOS33 IOB TRUE} [get_ports "hdmi_data[0]"]
set_property -dict {PACKAGE_PIN AA13 IOSTANDARD LVCMOS33 IOB TRUE} [get_ports "hdmi_data[1]"]
set_property -dict {PACKAGE_PIN AA14 IOSTANDARD LVCMOS33 IOB TRUE} [get_ports "hdmi_data[2]"]
set_property -dict {PACKAGE_PIN Y14 IOSTANDARD LVCMOS33 IOB TRUE} [get_ports "hdmi_data[3]"]
set_property -dict {PACKAGE_PIN AB15 IOSTANDARD LVCMOS33 IOB TRUE} [get_ports "hdmi_data[4]"]
set_property -dict {PACKAGE_PIN AB16 IOSTANDARD LVCMOS33 IOB TRUE} [get_ports "hdmi_data[5]"]
set_property -dict {PACKAGE_PIN AA16 IOSTANDARD LVCMOS33 IOB TRUE} [get_ports "hdmi_data[6]"]
set_property -dict {PACKAGE_PIN AB17 IOSTANDARD LVCMOS33 IOB TRUE} [get_ports "hdmi_data[7]"]
set_property -dict {PACKAGE_PIN AA17 IOSTANDARD LVCMOS33 IOB TRUE} [get_ports "hdmi_data[8]"]
set_property -dict {PACKAGE_PIN Y15 IOSTANDARD LVCMOS33 IOB TRUE} [get_ports "hdmi_data[9]"]
set_property -dict {PACKAGE_PIN W13 IOSTANDARD LVCMOS33 IOB TRUE} [get_ports "hdmi_data[10]"]
set_property -dict {PACKAGE_PIN W15 IOSTANDARD LVCMOS33 IOB TRUE} [get_ports "hdmi_data[11]"]
set_property -dict {PACKAGE_PIN V15 IOSTANDARD LVCMOS33 IOB TRUE} [get_ports "hdmi_data[12]"]
set_property -dict {PACKAGE_PIN U17 IOSTANDARD LVCMOS33 IOB TRUE} [get_ports "hdmi_data[13]"]
set_property -dict {PACKAGE_PIN V14 IOSTANDARD LVCMOS33 IOB TRUE} [get_ports "hdmi_data[14]"]
set_property -dict {PACKAGE_PIN V13 IOSTANDARD LVCMOS33 IOB TRUE} [get_ports "hdmi_data[15]"]

set_property -dict {PACKAGE_PIN AA18 IOSTANDARD LVCMOS33 PULLTYPE PULLUP} [get_ports "iic_mux_scl[1]"]
set_property -dict {PACKAGE_PIN Y16 IOSTANDARD LVCMOS33 PULLTYPE PULLUP} [get_ports "iic_mux_sda[1]"]
set_property -dict {PACKAGE_PIN AB4 IOSTANDARD LVCMOS33 PULLTYPE PULLUP} [get_ports "iic_mux_scl[0]"]
set_property -dict {PACKAGE_PIN AB5 IOSTANDARD LVCMOS33 PULLTYPE PULLUP} [get_ports "iic_mux_sda[0]"]

# Preliminary source-synchronous budget, NOT measured board timing closure.
# Preserve the IP pixel clock. OUT_CLK_POLARITY=1 captures half a cycle
# after the DATA registers, rather than sampling at their changing edge.
create_generated_clock -name hdmi_forwarded -source [get_pins -hier -filter {NAME =~ */axi_hdmi_core/inst/i_clk_oddr/C}] -divide_by 1 -invert [get_ports hdmi_out_clk]
# Candidate setup budget 2 ns; hold budget 0.5 ns. Display modes require
# hardware acceptance; do not interpret this as a qualified ADV7511 timing model.
set_output_delay -clock hdmi_forwarded -max 2.000 [get_ports {hdmi_data[*] hdmi_vsync hdmi_hsync hdmi_data_e}]
set_output_delay -clock hdmi_forwarded -min -0.500 [get_ports {hdmi_data[*] hdmi_vsync hdmi_hsync hdmi_data_e}]
