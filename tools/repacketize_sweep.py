#!/usr/bin/env python3
"""Measure alternative symbol padding using the identical encoded NAL bytes."""
import argparse
import json
from pathlib import Path
from codec_sweep import measure_stream

if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--sweep", type=Path, required=True)
    p.add_argument("--symbol-bytes", type=int, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    summary = json.loads((args.sweep / "summary.json").read_text())
    for candidate in summary["candidates"]:
        candidate.update(measure_stream((args.sweep / candidate["name"] / "video.h264").read_bytes(), args.symbol_bytes))
    summary["status"] = "offline_identical_bitstream_repacketization_only"
    args.output.write_text(json.dumps(summary, indent=2) + "\n")
