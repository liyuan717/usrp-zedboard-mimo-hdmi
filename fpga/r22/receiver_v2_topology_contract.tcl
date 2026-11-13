# SPDX-License-Identifier: Apache-2.0
#
# Static, tool-independent constants shared by the RX v2 block-design builder
# and its topology checks.  Do not source a historical RF builder here.

namespace eval receiver_v2_topology {
  namespace export require_equal require_file require_dir
  variable part xc7z020clg484-1
  variable vivado_prefix 2025.2
  variable ad9361_base 0x79020000
  variable core_base 0x7c430000
  variable output_dmac_base 0x7c440000
  variable spi_base 0x44a00000
  variable gpio_base 0x41200000
  # HDMI presentation observer ABI 1.  This is HDMI sideband only: it must
  # not consume the RF HP0 path or output-DMAC interrupt slot.
  variable hdmi_observer_base 0x44a20000
  variable hdmi_observer_irq_index 11
  variable hdmi_observer_irq_spi 55
  variable output_irq_index 8
  variable output_irq_spi 52
  variable slot_bytes 512
  variable slot_beats 64
  variable stream_width 64
  variable stream_keep 8
  variable fabric_clock_hz 50000000
  variable rf_interface_clock_hz 16000000
  variable adi_commit 4366bd63c1fe56b680b69145981a4493c6f8753a
}

proc receiver_v2_topology::require_equal {label actual expected} {
  if {$actual ne $expected} {error "$label must be $expected, got $actual"}
}

proc receiver_v2_topology::require_file {path} {
  if {![file isfile $path]} {error "Required RX v2 source is missing: $path"}
  return [file normalize $path]
}

proc receiver_v2_topology::require_dir {path} {
  if {![file isdirectory $path]} {error "Required RX v2 directory is missing: $path"}
  return [file normalize $path]
}
