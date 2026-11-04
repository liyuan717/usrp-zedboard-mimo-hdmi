"""Emit public Python-oracle synchronization/H/Y diagnostics for one RXTH frame."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from mimo_host.rf_rx_tracking import RFLocalTrackingFrontEnd


def pair(value: complex) -> list[float]:
    return [float(value.real), float(value.imag)]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rxth", type=Path, required=True)
    args = parser.parse_args()
    raw = args.rxth.read_bytes()
    front = RFLocalTrackingFrontEnd(); candidates = []
    for offset in range(0, len(raw), 8224): candidates.extend(front.feed(raw[offset:offset + 8224]))
    candidates.extend(front.finish())
    if len(candidates) != 1: parser.error(f"expected one candidate, got {len(candidates)}")
    c = candidates[0]
    print(json.dumps({
        "cfo_hz": c.sync["cfo_hz"],
        "timing": c.sync.get("timing_offset_samples", c.sync.get("initial_timing_offset_samples")),
        "sfo_ppm": c.sync.get("sfo_ppm", c.sync.get("initial_sfo_ppm")),
        "h": [pair(value) for value in c.effective_channel.reshape(-1)],
        "y0": [pair(value) for value in c.detector_y[:, 0]],
        "pilot_phase": c.timing_trace[0].get("phase_radians"),
    }, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
