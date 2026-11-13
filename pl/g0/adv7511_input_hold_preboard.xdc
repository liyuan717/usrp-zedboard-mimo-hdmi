# ADV7511 Hardware User's Guide Rev D, AC specifications and Figure 2.
# Zero input clock delay: 0.700 ns device hold plus 0.500 ns provisional
# data-to-clock skew allowance. PCB skew is not measured by this constraint.
set_output_delay -clock hdmi_forwarded -min -1.200 [get_ports {hdmi_data[*] hdmi_vsync hdmi_hsync hdmi_data_e}]
