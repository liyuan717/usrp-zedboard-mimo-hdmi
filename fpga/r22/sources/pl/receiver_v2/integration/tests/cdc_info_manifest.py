#!/usr/bin/env python3
"""Create or check an exact CDC Info endpoint manifest.

The generated file is deliberately *not* an approval.  It is a review input:
an engineer must inspect every row and set ``reviewed`` to true before a
pre-signoff runner can use it.  Counts alone are never accepted.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re

from cdc_typed_inventory import CdcRow, InventoryError, parse_report


def row_key(row: CdcRow) -> dict[str, object]:
    return {"cdc_id": row.cdc_id, "severity": row.severity,
            "description": row.description, "depth": row.depth,
            "exception": row.exception, "source_clock": row.source_clock,
            "destination_clock": row.destination_clock, "source": row.source,
            "destination": row.destination}


def info_rows(report: Path) -> list[dict[str, object]]:
    _summary, rows = parse_report(report.read_text(encoding="utf-8", errors="strict"))
    result = []
    for row in rows:
        if row.severity != "Info":
            continue
        # CDC-3 sources may be vendor FDRE primitives (not only ``*_reg``),
        # so use a deliberately narrow pin syntax here and retain the exact
        # full strings in the reviewed manifest.
        source_is_seq = row.source.endswith("/C")
        source_is_port = re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\[\d+\])?", row.source) is not None
        if not (source_is_seq or source_is_port) or not (row.destination.endswith("/D") or
                                                         row.destination.endswith("/PRE") or
                                                         row.destination.endswith("/CLR")):
            raise InventoryError(f"unreviewable CDC Info pin orientation: {row.source} -> {row.destination}")
        result.append(row_key(row))
    if not result:
        raise InventoryError("CDC report has no Info rows")
    if len({tuple(sorted(row.items())) for row in result}) != len(result):
        raise InventoryError("duplicate CDC Info endpoint row")
    return sorted(result, key=lambda item: (str(item["source_clock"]), str(item["destination_clock"]),
                                             str(item["source"]), str(item["destination"])))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.out.exists():
        raise SystemExit(f"out must be fresh: {args.out}")
    payload = {"schema": "rfv2-cdc-info-manifest-v1", "reviewed": False,
               "review_note": "candidate generated from one report; not an approval",
               "info_rows": info_rows(args.report)}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"CDC_INFO_MANIFEST_CANDIDATE rows={len(payload['info_rows'])} reviewed=false")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (InventoryError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"CDC Info manifest refused: {exc}") from exc
