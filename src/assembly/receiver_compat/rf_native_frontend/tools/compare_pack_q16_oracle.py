"""Strict direct H/Y Q16 pack comparison; test-only Python oracle."""
from __future__ import annotations

import argparse

from ps.common import gram, protocol


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--native", required=True)
    args = parser.parse_args()
    expected = bytearray(gram.prepare_task(
        [[1 + 0j, 0j], [0j, 1 + 0j]], [1 + 0j, 1j], protocol.Modulation.QPSK,
        frame_id=1, seed=1, iterations=64, proposal_step_raw=32768, config_id=2,
    )["input_bytes"])
    # Reviewed RNT contract requires a full 64-step run; legacy Python used
    # min=1. This is the only intentionally canonicalized byte range.
    expected[36:38] = (64).to_bytes(2, "little")
    actual = open(args.native, "rb").read()
    if actual != expected:
        first = next(i for i, (a, b) in enumerate(zip(actual, expected)) if a != b)
        raise SystemExit(f"mismatch at {first}: native={actual[first]} oracle={expected[first]}")
    print({"status": "direct_hy_pack_match", "bytes": len(actual)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
