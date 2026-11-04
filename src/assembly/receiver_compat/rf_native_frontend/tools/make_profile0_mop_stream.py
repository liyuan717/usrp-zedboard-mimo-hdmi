"""Turn actual native MIP0 records into reference MOP0 records for tests.

This is an offline verification helper only.  It reads native frontend output,
uses the existing fixed profile-0 integer model at the mandated 64/32768
configuration, and writes MOP0 bytes for a fake IMBC ioctl.  It is never
imported by the receiver/runtime and is not a software fallback.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from ps.common.profile0_reference import evaluate
from ps.common.protocol import INPUT_BYTES, OUTPUT_BYTES, unpack_input_frame

HEADERS = 40
TASKS = 528
RECORD_BYTES = HEADERS + TASKS * INPUT_BYTES
BUILD_ID = 0x7A31C204


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mip-records", type=Path, required=True)
    parser.add_argument("--mop-records", type=Path, required=True)
    args = parser.parse_args()
    source = args.mip_records.read_bytes()
    if not source or len(source) % RECORD_BYTES:
        parser.error("input is not a non-empty native frontend record stream")
    output = bytearray()
    for record_at in range(0, len(source), RECORD_BYTES):
        tasks = source[record_at + HEADERS:record_at + RECORD_BYTES]
        for task_at in range(0, len(tasks), INPUT_BYTES):
            task = unpack_input_frame(tasks[task_at:task_at + INPUT_BYTES])
            if task.config_id != 2 or task.min_iterations != 64 or task.max_iterations != 64 or task.proposal_step_raw != 32768:
                raise SystemExit("unexpected MIP0 profile; refusing reference generation")
            output.extend(evaluate(task, build_id=BUILD_ID).output_bytes)
    args.mop_records.write_bytes(output)
    print({"status": "profile0_mop_ready", "frames": len(source) // RECORD_BYTES,
           "mop_bytes_per_frame": TASKS * OUTPUT_BYTES})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
