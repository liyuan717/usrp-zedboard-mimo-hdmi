"""Offline-only diagnosis of native MIP0 task decisions.

This utility is deliberately outside the receiver/runtime.  It can use the
deterministic fixture generator's public payload to classify a CRC failure as
either (a) a finite-64-step MHGD/local-search loss or (b) a front-end H/Y
estimate error.  It never supplies a decision to the native receive path.
"""
from __future__ import annotations

import argparse
import json
import struct
from pathlib import Path

import numpy as np
from mimo_host.domain import Modulation
from mimo_host.waveform import HEADER_BYTES, WaveformProfile, bytes_to_indices, generate_waveform
from ps.common.profile0_reference import evaluate, exhaustive_fixed
from ps.common.protocol import INPUT_BYTES, unpack_input_frame

HEADERS = 40
TASKS = 528
RECORD_BYTES = HEADERS + TASKS * INPUT_BYTES


def frame_id_from_header(record: bytes) -> int:
    if record[:4] != b"MRF1" or record[20:24] != b"MRF1":
        raise ValueError("record lacks two public MRF1 headers")
    frame0 = struct.unpack_from("<I", record, 8)[0]
    frame1 = struct.unpack_from("<I", record, 28)[0]
    if frame0 != frame1:
        raise ValueError("public stream frame ids differ")
    return frame0


def expected_indices(frame_id: int) -> np.ndarray:
    # Fixture-only truth: never imported by C++ frontend or runtime.
    waveform = generate_waveform(WaveformProfile(), frame_id=frame_id)
    values = [bytes_to_indices(packet[HEADER_BYTES:], Modulation.QPSK)
              for packet in waveform["packets"]]
    expected = np.stack(values, axis=1)
    if expected.shape != (TASKS, 2):
        raise ValueError(f"unexpected public waveform payload shape {expected.shape}")
    return expected


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mip-records", type=Path, required=True)
    parser.add_argument("--max-details", type=int, default=16)
    args = parser.parse_args()
    source = args.mip_records.read_bytes()
    if not source or len(source) % RECORD_BYTES:
        parser.error("input is not a non-empty native MIP record stream")
    details: list[dict[str, object]] = []
    totals = {"tasks": 0, "mhgd_vs_exhaustive": 0, "exhaustive_vs_tx": 0,
              "mhgd_vs_tx": 0}
    for record_offset in range(0, len(source), RECORD_BYTES):
        record = source[record_offset:record_offset + RECORD_BYTES]
        frame_id = frame_id_from_header(record)
        expected = expected_indices(frame_id)
        for symbol in range(TASKS):
            offset = HEADERS + symbol * INPUT_BYTES
            task = unpack_input_frame(record[offset:offset + INPUT_BYTES])
            result = evaluate(task).output
            mhgd = (result.x_hat0, result.x_hat1)
            exhaustive, exhaustive_cost = exhaustive_fixed(task)
            tx = (int(expected[symbol, 0]), int(expected[symbol, 1]))
            totals["tasks"] += 1
            finite_error = mhgd != exhaustive
            frontend_error = exhaustive != tx
            if finite_error:
                totals["mhgd_vs_exhaustive"] += 1
            if frontend_error:
                totals["exhaustive_vs_tx"] += 1
            if mhgd != tx:
                totals["mhgd_vs_tx"] += 1
            if (finite_error or frontend_error) and len(details) < args.max_details:
                details.append({
                    "rf_frame_id": frame_id,
                    "task_tag": task.frame_id,
                    "symbol_index": symbol,
                    "mhgd": mhgd,
                    "exhaustive": exhaustive,
                    "tx_fixture": tx,
                    "mhgd_cost_raw": result.best_residual_raw,
                    "exhaustive_cost_raw": exhaustive_cost,
                    "finite_search_error": finite_error,
                    "frontend_estimation_error": frontend_error,
                })
    print(json.dumps({"scope": "offline_fixture_diagnosis_only", "records": len(source) // RECORD_BYTES,
                      "totals": totals, "details": details}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
