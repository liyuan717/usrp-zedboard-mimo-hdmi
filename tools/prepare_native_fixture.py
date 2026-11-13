#!/usr/bin/env python3
"""AU-delimited G0 replay fixture; records source identity, not decoder tokens."""
import argparse
from pathlib import Path
import struct
from codec_sweep import access_units, annexb_units

if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("input", type=Path)
    p.add_argument("output", type=Path)
    p.add_argument("--loops", type=int, default=1)
    args = p.parse_args()
    aus = access_units(annexb_units(args.input.read_bytes()))
    if not 1 <= args.loops <= 20 or len(aus) * args.loops > 4096:
        raise ValueError("bounded fixture loop count required")
    with args.output.open("xb") as f:
        for i, au in enumerate(aus * args.loops):
            raw = b"".join(data for _, data in au)
            f.write(struct.pack("<4sHHQQII", b"V3AU", 1, 32, i + 1, i * 50000, len(raw), 0))
            f.write(raw)
    print(len(aus) * args.loops, "source AUs from", args.loops, "explicit clip loops")
