#!/usr/bin/env python3
"""Send exactly the first frozen 42 dual-stream RF bursts (41 unique plus one prefix) at gain 18 dB."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
INTEGRATION = Path('/home/leeeo/Documents/ChatGPT/zedboard+fmcomms3/deliverables/usrp-zedboard-integration-20261026')
FROZEN = HERE / 'photo-tx.iqb'

IQB_HEADER = struct.Struct("<4sQI")
SAMPLES_PER_FRAME = 3840
UNIQUE_FRAME_COUNT = 41
FRAME_COUNT = 42
UNIQUE_SAMPLES = SAMPLES_PER_FRAME * UNIQUE_FRAME_COUNT
SAMPLES = SAMPLES_PER_FRAME * FRAME_COUNT
IQ_BLOCK_MAX = 65536


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def build_iq_stream() -> bytes:
    raw = FROZEN.read_bytes()
    if sha256(raw) != "69332288caa98bcc38d0e08134fa87d9819c8bcafac6e90f91dcf2a3d10352ee":
        raise ValueError("frozen IQB hash mismatch")
    payload = bytearray()
    at = 0
    expected_sequence = 0
    while len(payload) < UNIQUE_SAMPLES * 16:
        if at + IQB_HEADER.size > len(raw):
            raise ValueError("frozen IQB ended before 41 unique frames")
        magic, sequence, count = IQB_HEADER.unpack_from(raw, at)
        at += IQB_HEADER.size
        if magic != b"IQB1" or sequence != expected_sequence or not 1 <= count <= 65536:
            raise ValueError("invalid frozen IQB record")
        expected_sequence += 1
        record_bytes = count * 16
        if at + record_bytes > len(raw):
            raise ValueError("truncated frozen IQB record")
        need = UNIQUE_SAMPLES * 16 - len(payload)
        payload.extend(raw[at : at + min(need, record_bytes)])
        at += record_bytes
    if len(payload) != UNIQUE_SAMPLES * 16:
        raise ValueError("exact 41-unique-frame IQ construction failed")
    first_frame = bytes(payload[: SAMPLES_PER_FRAME * 16])
    transmitted = first_frame + bytes(payload)
    if len(transmitted) != SAMPLES * 16:
        raise ValueError("duplicate-prefix IQ construction failed")
    stream = bytearray()
    sequence = 0
    for sample_offset in range(0, SAMPLES, IQ_BLOCK_MAX):
        count = min(IQ_BLOCK_MAX, SAMPLES - sample_offset)
        stream.extend(IQB_HEADER.pack(b"IQB1", sequence, count))
        stream.extend(
            transmitted[sample_offset * 16 : (sample_offset + count) * 16]
        )
        sequence += 1
    stream.extend(IQB_HEADER.pack(b"IQB1", sequence, 0))
    return bytes(stream)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--simulate", action="store_true")
    args = parser.parse_args()
    if args.out.exists():
        parser.error("output directory already exists")

    authorization = json.loads((HERE / "AUTHORIZATION.json").read_text())
    admission = json.loads((HERE / "tx-admission-gain18.json").read_text())
    if not args.simulate:
        if authorization.get("authorized") is not True:
            raise ValueError("gain18 round is not authorized")
        if authorization.get("tx_gain_db") != 18.0 or authorization.get("maximum_frames") != 42:
            raise ValueError("authorization scope differs")
        for field in (
            "operator_confirmed_connections",
            "power_budget_reviewed",
            "peer_configuration_confirmed",
            "phase_relationship_reviewed",
        ):
            if admission.get(field) is not True:
                raise ValueError("missing admission: " + field)
        if admission.get("tx_gain_db") != 18.0 or admission.get("maximum_frames") != 42:
            raise ValueError("admission gain/frame scope differs")

    stream = build_iq_stream()
    args.out.mkdir(parents=True, exist_ok=False)
    command = [
        str(HERE / "tx-gain18" / "continuous_tx"),
        "--stats",
        str(args.out / "transport.json"),
        "--actual",
        str(args.out / "actual-readback.json"),
        "--freq",
        "5500000000",
        "--bandwidth",
        "40000000",
        "--gain",
        "18.0",
        "--scale",
        "1",
        "--queue",
        "8",
        "--prefill",
        "4",
        "--max-send-samples",
        str(IQ_BLOCK_MAX),
        "--clock",
        "internal",
        "--time",
        "internal",
        "--addr",
        "192.168.10.2",
        "--serial",
        "3095BA5",
    ]
    command += ["--simulate"] if args.simulate else [
        "--enable-tx",
        "YES",
        "--tx-zero-preroll-samples",
        "400000",
    ]
    (args.out / "transport-command.json").write_text(
        json.dumps(command, indent=2) + "\n"
    )
    (args.out / "iq-stream-meta.json").write_text(
        json.dumps(
            {
                "frames": FRAME_COUNT,
                "samples_per_channel": SAMPLES,
                "iqb_stream_sha256": sha256(stream),
                "frozen_iqb_sha256": sha256(FROZEN.read_bytes()),
                "tx_gain_db": 18.0,
                "digital_scale": 1,
            },
            indent=2,
        )
        + "\n"
    )

    with (
        (args.out / "transport.stdout.log").open("xb") as stdout,
        (args.out / "transport.stderr.log").open("xb") as stderr,
    ):
        child = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        )
        try:
            assert child.stdin is not None
            child.stdin.write(stream)
            child.stdin.close()
            if not args.simulate:
                deadline = time.monotonic() + 10
                while True:
                    actual_path = args.out / "actual-readback.json"
                    try:
                        actual = json.loads(actual_path.read_text())
                    except (FileNotFoundError, json.JSONDecodeError):
                        actual = None
                    if actual is not None:
                        break
                    if child.poll() is not None:
                        raise RuntimeError("TX exited before actual readback")
                    if time.monotonic() > deadline:
                        raise TimeoutError("TX actual readback timeout")
                    time.sleep(0.01)
                if actual.get("configuration_matches_requested") is not True:
                    raise ValueError("USRP actual configuration mismatch")
                if any(
                    abs(float(channel.get("gain_db", 0)) - 18.0) > 0.01
                    for channel in actual.get("channels", [])
                ):
                    raise ValueError("actual TX gain is not 18 dB on both channels")
            returncode = child.wait(timeout=25)
        except BaseException:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=3)
            raise

    stats = json.loads((args.out / "transport.json").read_text())
    passed = bool(
        returncode == 0
        and stats.get("transport_pass") is True
        and stats.get("input_samples_per_channel") == SAMPLES
        and stats.get("accepted_samples_per_channel") == SAMPLES
        and stats.get("blocks_accepted") == (SAMPLES + IQ_BLOCK_MAX - 1) // IQ_BLOCK_MAX
        and stats.get("underflows") == 0
        and stats.get("late_errors") == 0
        and stats.get("other_async_errors") == 0
    )
    result = {
        "scope": "TX-only frame1 duplicate prefix followed by unique frames1..41 at 18 dB",
        "simulated": args.simulate,
        "valid": passed,
        "returncode": returncode,
        "frames": FRAME_COUNT,
        "unique_frames": UNIQUE_FRAME_COUNT,
        "duplicate_prefix_frame": 1,
        "samples_per_channel": SAMPLES,
        "iq_blocks": (SAMPLES + IQ_BLOCK_MAX - 1) // IQ_BLOCK_MAX,
        "tx_gain_db": 18.0,
        "statistics": stats,
    }
    (args.out / "RESULT.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
