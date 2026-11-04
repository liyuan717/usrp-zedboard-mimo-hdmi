"""Create a test-only static-channel RXTH fixture from the Python waveform oracle.

This is never imported by a receive service.  It intentionally knows a public
test payload only to generate an input waveform; the C++ receiver has no such
input and is checked solely for header/task production.
"""
from __future__ import annotations

import argparse
import json
import struct
import zlib
from pathlib import Path

import numpy as np

from mimo_host.waveform import WaveformProfile, generate_waveform


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--frames", type=int, default=1)
    parser.add_argument("--cfo-hz", type=float, default=0.0)
    parser.add_argument("--timing-samples", type=float, default=0.0)
    parser.add_argument("--sfo-ppm", type=float, default=0.0)
    parser.add_argument("--step-frame", type=int,
                        help="test-only frame boundary at which timing/SFO step is applied")
    parser.add_argument("--timing-step-samples", type=float, default=0.0,
                        help="test-only additional raw timing offset after --step-frame")
    parser.add_argument("--sfo-step-ppm", type=float, default=0.0,
                        help="test-only additional SFO after --step-frame")
    parser.add_argument("--noise-rms", type=float, default=0.0)
    parser.add_argument("--rx-scale", type=float, default=1.0,
                        help="test-only post-channel scale before RXTH quantisation; <=1 adds headroom")
    parser.add_argument("--report-json", type=Path,
                        help="optional deterministic generation/clip report")
    args = parser.parse_args()
    # Long tracking regression remains test-only, but must cover enough
    # frames for a 100 ppm fractional origin error to accumulate visibly.
    if not 1 <= args.frames <= 1000:
        parser.error("--frames must be 1..1000")
    if not np.isfinite(args.rx_scale) or not 0.0 < args.rx_scale <= 1.0:
        parser.error("--rx-scale must be finite in (0, 1]")
    h = np.array([[0.80 + 0.10j, 0.18 - 0.05j], [0.10 + 0.08j, 0.72 - 0.08j]])
    ideal = np.concatenate(
        [generate_waveform(WaveformProfile(), frame_id=37 + index)["samples"] @ h.T
         for index in range(args.frames)], axis=0
    )
    if abs(args.sfo_ppm) > 500 or abs(args.timing_samples) > 0.5 or abs(args.cfo_hz) > 10_000:
        parser.error("fixture impairment outside bounded native-receiver test range")
    if args.step_frame is not None and not 1 <= args.step_frame < args.frames:
        parser.error("--step-frame must be within the emitted frame sequence")
    if (args.timing_step_samples or args.sfo_step_ppm) and args.step_frame is None:
        parser.error("--timing-step-samples/--sfo-step-ppm require --step-frame")
    if abs(args.sfo_ppm + args.sfo_step_ppm) > 500 or abs(args.timing_samples + args.timing_step_samples) > 0.5:
        parser.error("post-step impairment outside bounded native-receiver test range")
    ref = 838.0
    scale = 1.0 + args.sfo_ppm * 1e-6
    # A step is expressed in the same raw-coordinate convention as the
    # continuous fixture: positive timing makes the post-step waveform late;
    # positive SFO makes its raw duration longer.  Keeping the source
    # coordinate continuous at the known frame boundary lets the receiver
    # distinguish a one-shot timing innovation from a sustained clock-rate
    # innovation without exposing any TX truth at runtime.
    step_source = None
    if args.step_frame is None:
        raw_count = int(np.ceil(len(ideal) * scale + args.timing_samples - args.sfo_ppm * 1e-6 * ref + 4))
        raw_coordinate = np.arange(raw_count, dtype=np.float64)
        coordinate = (raw_coordinate - args.timing_samples + args.sfo_ppm * 1e-6 * ref) / scale
    else:
        step_source = args.step_frame * 3840
        step_raw = args.timing_samples - args.sfo_ppm * 1e-6 * ref + scale * step_source
        step_scale = 1.0 + (args.sfo_ppm + args.sfo_step_ppm) * 1e-6
        raw_end = step_raw + args.timing_step_samples + step_scale * (len(ideal) - step_source)
        raw_count = int(np.ceil(raw_end + 4))
        raw_coordinate = np.arange(raw_count, dtype=np.float64)
        coordinate = (raw_coordinate - args.timing_samples + args.sfo_ppm * 1e-6 * ref) / scale
        post = raw_coordinate >= step_raw
        coordinate[post] = step_source + (raw_coordinate[post] - step_raw - args.timing_step_samples) / step_scale
    rx = np.empty((raw_count, 2), dtype=np.complex128)
    for channel in range(2):
        rx[:, channel] = (
            np.interp(coordinate, np.arange(len(ideal)), ideal[:, channel].real, left=0.0, right=0.0)
            + 1j * np.interp(coordinate, np.arange(len(ideal)), ideal[:, channel].imag, left=0.0, right=0.0)
        )
    rx *= np.exp(2j * np.pi * args.cfo_hz * np.arange(raw_count)[:, None] / 4_000_000)
    rx_clean = rx.copy()
    if args.noise_rms:
        rng = np.random.default_rng(8021)
        rx += args.noise_rms / np.sqrt(2) * (
            rng.standard_normal(rx.shape) + 1j * rng.standard_normal(rx.shape)
        )
    rx *= args.rx_scale
    rx_clean *= args.rx_scale
    noise = rx - rx_clean
    padded = np.pad(rx, ((0, (-len(rx)) % 1024), (0, 0)))
    normalized = np.stack((padded[:, 0].real, padded[:, 0].imag,
                           padded[:, 1].real, padded[:, 1].imag), axis=1)
    clipped_components = int(np.count_nonzero((normalized < -0.999) | (normalized > 0.999)))
    words = np.rint(np.clip(normalized, -0.999, 0.999) * 2047).astype("<i2") << 4
    records = []
    for sequence, at in enumerate(range(0, len(words), 1024)):
        prefix = struct.pack("<4sBBHIIQI", b"RXTH", 1, 32, 1, 5, sequence, at, 1024)
        records.append(prefix + struct.pack("<I", zlib.crc32(prefix)) + words[at:at + 1024].tobytes())
    args.output.write_bytes(b"".join(records))
    report = {"path": str(args.output), "blocks": len(records), "frame_id": 37, "frames": args.frames,
              "cfo_hz": args.cfo_hz, "timing_samples": args.timing_samples,
              "sfo_ppm": args.sfo_ppm, "noise_rms_complex_rms": args.noise_rms,
              "step_frame": args.step_frame, "timing_step_samples": args.timing_step_samples,
              "sfo_step_ppm": args.sfo_step_ppm,
              "noise_component_rms": args.noise_rms / np.sqrt(2), "rx_scale": args.rx_scale,
              "clean_complex_power_pre_quant": float(np.mean(np.abs(rx_clean) ** 2)),
              "noise_complex_power_pre_quant": float(np.mean(np.abs(noise) ** 2)),
              "component_peak_pre_clip": float(np.max(np.abs(normalized))),
              "clip_component_count": clipped_components,
              "clip_component_fraction": float(clipped_components / normalized.size)}
    if args.report_json:
        args.report_json.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
