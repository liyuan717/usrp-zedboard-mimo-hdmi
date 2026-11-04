"""Test-only strict C++ task dump versus the existing Python RX oracle.

Never invoke this from a runtime service: Python is intentionally an oracle,
not a fallback path.  A mismatch exits nonzero and reports the first byte;
matching does not prove ARM throughput or RF hardware operation.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from mimo_host.rf_rx_tracking import RFLocalTrackingFrontEnd
from mimo_host.rf_tracking_pl_adapter import TrackingMHGDInputAdapter, TrackingSearchParameters


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rxth", type=Path, required=True)
    parser.add_argument("--native", type=Path, required=True)
    args = parser.parse_args()
    rxth = args.rxth.read_bytes()
    if len(rxth) % 8224:
        parser.error("RXTH input is not a full block sequence")
    front = RFLocalTrackingFrontEnd()
    candidates = []
    for start in range(0, len(rxth), 8224):
        candidates.extend(front.feed(rxth[start:start + 8224]))
    candidates.extend(front.finish())
    if len(candidates) != 1:
        parser.error(f"oracle emitted {len(candidates)} candidates, expected exactly one")
    batch = TrackingMHGDInputAdapter.prepare(
        candidates[0],
        search=TrackingSearchParameters(iterations=64, proposal_step_raw=32768, config_id=2),
        include_input_bytes=True,
    )
    # The published Python adapter still emits its historical min_iterations=1
    # field, while native transport admission deliberately requires the frozen
    # full 64-step run.  Canonicalize only that reviewed wire-contract change;
    # all H/Y-derived Q16 coefficients remain byte-for-byte oracle checks.
    expected_mutable = bytearray(b"".join(batch.input_bytes or ()))
    for offset in range(0, len(expected_mutable), 120):
        expected_mutable[offset + 36:offset + 38] = (64).to_bytes(2, "little")
    expected = bytes(expected_mutable)
    actual = args.native.read_bytes()
    if len(actual) != 40 + len(expected):
        parser.error(f"native output size {len(actual)} does not equal {40 + len(expected)}")
    if actual[:20] != candidates[0].headers[0] or actual[20:40] != candidates[0].headers[1]:
        parser.error("native headers disagree with Python oracle")
    payload = actual[40:]
    if payload != expected:
        first = next(index for index, pair in enumerate(zip(payload, expected)) if pair[0] != pair[1])
        parser.error(f"Q16 MIP0 mismatch at byte {first}: native={payload[first]} oracle={expected[first]}")
    print({"status": "match", "tasks": batch.task_count, "bytes": len(expected)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
