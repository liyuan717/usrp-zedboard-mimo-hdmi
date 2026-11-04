"""Generate LVDS 2R2T source-synchronous timing constraints for the offline build.

The profile records a quantitative engineering model, not a completed board
calibration.  Its status permits only an offline candidate; hardware acceptance
requires the documented PN/IDELAY tap-window measurement.
"""
from __future__ import annotations
import argparse, json, math
from pathlib import Path

def number(mapping: dict, key: str) -> float:
    value = mapping.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValueError(f"invalid numeric field: {key}")
    return float(value)

def build_xdc(profile: dict) -> str:
    if profile.get("profile_id") != "lvds_2r2t_2v5" or profile.get("interface_mode") != "LVDS_25_DUAL_PORT_FULL_DUPLEX_2R2T_DDR":
        raise ValueError("profile is not the approved LVDS 2R2T 2.5-V route")
    if profile.get("status") not in {"PREBOARD_CONSERVATIVE_APPROVED", "MEASURED_APPROVED"}:
        raise ValueError("profile status is not approved for offline implementation")
    if number(profile, "vadj_mv") != 2500 or number(profile, "rx_channels") != 2 or number(profile, "tx_channels") != 2:
        raise ValueError("LVDS profile identity mismatch")
    if abs(number(profile, "complex_sample_rate_msps_per_stream") - 4.0) > 1e-9 or abs(number(profile, "data_clk_period_ns") - 62.5) > 1e-9 or number(profile, "sample_clock_divider") != 4:
        raise ValueError("LVDS profile is locked to 4 MSPS / 16 MHz / divide-by-4")
    dev, board = profile["device_timing_model"], profile["board_assumptions"]
    margin = number(board, "io_margin_ns")
    if margin < 0:
        raise ValueError("Timing margin must not be negative")
    for minimum, maximum in (("interconnect_data_minus_clock_min_ns", "interconnect_data_minus_clock_max_ns"),
                             ("tx_data_minus_fbclk_min_ns", "tx_data_minus_fbclk_max_ns")):
        if number(board, minimum) > number(board, maximum):
            raise ValueError("Invalid board skew interval")
    rx_min = number(dev, "rx_data_clk_to_output_min_ns") + number(board, "interconnect_data_minus_clock_min_ns") - margin
    rx_max = number(dev, "rx_data_clk_to_output_max_ns") + number(board, "interconnect_data_minus_clock_max_ns") + margin
    tx_min = -number(dev, "tx_data_hold_at_ad9361_ns") + number(board, "tx_data_minus_fbclk_min_ns") - margin
    tx_max = number(dev, "tx_data_setup_at_ad9361_ns") + number(board, "tx_data_minus_fbclk_max_ns") + margin
    if rx_min > rx_max or tx_min > tx_max: raise ValueError("invalid source-synchronous bounds")
    xdc = "\n".join([
        "# Generated; do not hand-edit.", f"# profile_id={profile['profile_id']}", f"# profile_status={profile['status']}",
        "# Numerical delays are an offline engineering model, not measured board timing.",
        "# Reuse the Clock Wizard primary clock; preserve its input-jitter constraint.",
        "set ad9361_rx_primary [get_clocks -of_objects [get_ports rx_clk_in_p]]",
        f"set_input_delay -clock $ad9361_rx_primary -min {rx_min:.3f} [get_ports {{rx_frame_in_p rx_data_in_p[*]}}]",
        f"set_input_delay -clock $ad9361_rx_primary -max {rx_max:.3f} [get_ports {{rx_frame_in_p rx_data_in_p[*]}}]",
        "# DDR data can be launched on either edge; retain BOTH launch edges.",
        f"set_input_delay -clock $ad9361_rx_primary -clock_fall -add_delay -min {rx_min:.3f} [get_ports {{rx_frame_in_p rx_data_in_p[*]}}]",
        f"set_input_delay -clock $ad9361_rx_primary -clock_fall -add_delay -max {rx_max:.3f} [get_ports {{rx_frame_in_p rx_data_in_p[*]}}]",
        "set ad9361_fb_clk_src [get_pins -hierarchical -filter {NAME =~ */rf_fb_clock/*i_tx_data_oddr/C}]",
        "# Source cardinality is checked by the independent routed topology audit.",
        "create_generated_clock -name ad9361_fb_clk -source $ad9361_fb_clk_src -divide_by 1 [get_ports tx_clk_out_p]",
        f"set_output_delay -clock ad9361_fb_clk -min {tx_min:.3f} [get_ports {{tx_frame_out_p tx_data_out_p[*]}}]",
        f"set_output_delay -clock ad9361_fb_clk -max {tx_max:.3f} [get_ports {{tx_frame_out_p tx_data_out_p[*]}}]", ""])
    xdc += (f"set_output_delay -clock ad9361_fb_clk -clock_fall -add_delay -min {tx_min:.3f} [get_ports {{tx_frame_out_p tx_data_out_p[*]}}]\n"
            f"set_output_delay -clock ad9361_fb_clk -clock_fall -add_delay -max {tx_max:.3f} [get_ports {{tx_frame_out_p tx_data_out_p[*]}}]\n")
    return xdc


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("profile", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    profile = json.loads(args.profile.read_text(encoding="utf-8"))
    xdc = build_xdc(profile)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(xdc, encoding="utf-8")
    return 0
if __name__ == "__main__": raise SystemExit(main())
