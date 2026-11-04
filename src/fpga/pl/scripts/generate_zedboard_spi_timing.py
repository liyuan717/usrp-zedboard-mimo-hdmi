"""Generate explicit conservative SPI IO budgets, never a false-path waiver."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path


def build_xdc(profile: dict) -> str:
    if (profile.get("profile_id") != "lvds_2r2t_2v5_spi_mode1"
            or type(profile.get("spi_mode")) is not int or profile.get("spi_mode") != 1
            or profile.get("controller_clock_hz") != 50_000_000
            or profile.get("sck_ratio") != 16 or profile.get("status") != "PREBOARD_CONSERVATIVE_APPROVED"):
        raise ValueError("SPI budget is not the reviewed fixed mode-1 / 50-MHz / divide-16 model")
    dev, board = profile["device_model"], profile["board_assumptions"]
    for obj in (dev, board):
        for key, value in obj.items():
            if key == "basis":
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"Invalid SPI timing value: {key}")
    if board["margin_ns"] < 0 or dev["miso_tco_min_ns"] > dev["miso_tco_max_ns"]:
        raise ValueError("Invalid SPI margin/tCO interval")
    if any(board[a] > board[b] for a, b in (
        ("clock_and_return_trace_min_ns", "clock_and_return_trace_max_ns"),
        ("output_data_minus_clock_min_ns", "output_data_minus_clock_max_ns"))):
        raise ValueError("Invalid SPI board interval")
    margin = board["margin_ns"]
    lines = ["# Generated SPI mode-1 (CPOL=0/CPHA=1) budget; not measured 2.5-V timing.",
             "# spi_mode=1",
             "# MISO: external rising-edge launch. MOSI: external falling-edge capture.",
             "# CS: both edges conservatively cover setup before first rising and hold after last falling.",
             "# Controller ext_spi_clk input capture remains conservatively constrained without CE waivers.",
             f"# profile_status={profile['status']}",
             "set rf_spi_source [get_pins -hier -filter {NAME =~ */rf_spi/ext_spi_clk}]",
             "# Source cardinality is checked by the independent routed topology audit.",
             "create_generated_clock -name ad9361_spi_clk -source $rf_spi_source -divide_by 16 [get_ports spi_clk]"]
    for bound, val in (("min", dev["miso_tco_min_ns"] + board["clock_and_return_trace_min_ns"] - margin),
                       ("max", dev["miso_tco_max_ns"] + board["clock_and_return_trace_max_ns"] + margin)):
        lines.append(f"set_input_delay -clock ad9361_spi_clk -{bound} {val:.3f} [get_ports spi_miso]")
    for port, prefix in (("spi_mosi", "mosi"), ("spi_csn", "cs")):
        minimum = -dev[f"{prefix}_hold_ns"] + board["output_data_minus_clock_min_ns"] - margin
        maximum = dev[f"{prefix}_setup_ns"] + board["output_data_minus_clock_max_ns"] + margin
        if port == "spi_csn":
            lines += [f"set_output_delay -clock ad9361_spi_clk -min {minimum:.3f} [get_ports {port}]",
                      f"set_output_delay -clock ad9361_spi_clk -max {maximum:.3f} [get_ports {port}]"]
        extra = " -add_delay" if port == "spi_csn" else ""
        lines += [f"set_output_delay -clock ad9361_spi_clk -clock_fall{extra} -min {minimum:.3f} [get_ports {port}]",
                  f"set_output_delay -clock ad9361_spi_clk -clock_fall{extra} -max {maximum:.3f} [get_ports {port}]"]
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    text = build_xdc(json.loads(args.profile.read_text(encoding="utf-8")))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
