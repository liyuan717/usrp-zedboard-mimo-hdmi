#!/usr/bin/env python3
"""Independent host decoder + scalar-formula BT.601 check of native pixels."""
import argparse
import json
from pathlib import Path
import subprocess
import zlib
import numpy as np


def oracle(path):
    rows = []
    frame_bytes = 1024 * 600 * 3 // 2
    with subprocess.Popen(["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:v:0",
                           "-pix_fmt", "yuv420p", "-f", "rawvideo", "-"], stdout=subprocess.PIPE) as p:
        while True:
            raw = p.stdout.read(frame_bytes)
            if not raw:
                break
            if len(raw) != frame_bytes:
                raise ValueError("decoded raw frame is truncated")
            y = np.frombuffer(raw, np.uint8, 614400).reshape(600, 1024).astype(np.int32) - 16
            u = np.frombuffer(raw, np.uint8, 153600, 614400).reshape(300, 512).astype(np.int32) - 128
            v = np.frombuffer(raw, np.uint8, 153600, 768000).reshape(300, 512).astype(np.int32) - 128
            u = u.repeat(2, axis=0).repeat(2, axis=1)
            v = v.repeat(2, axis=0).repeat(2, axis=1)
            r = np.clip((298 * y + 409 * v + 128) // 256, 0, 255)
            g = np.clip((298 * y - 100 * u - 208 * v + 128) // 256, 0, 255)
            b = np.clip((298 * y + 516 * u + 128) // 256, 0, 255)
            rgb = ((r >> 3) << 11 | (g >> 2) << 5 | (b >> 3)).astype("<u2").tobytes()
            rows.append({"capture_id": len(rows) + 1, "rgb565_crc32": zlib.crc32(rgb)})
        if p.wait():
            raise RuntimeError("host decode failed")
    return rows


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--video", type=Path, required=True)
    p.add_argument("--native-report", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    rows = oracle(a.video)
    native = json.loads(a.native_report.read_text())["pixels"]
    result = {"independent_pixels_match": rows == native, "frames": len(rows),
              "native_frames": len(native), "pixels": rows, "g0_passed": False}
    a.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({k: v for k, v in result.items() if k != "pixels"}))
    if rows != native:
        raise SystemExit(1)
