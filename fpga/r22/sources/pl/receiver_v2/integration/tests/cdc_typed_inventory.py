#!/usr/bin/env python3
"""Fail-closed typed inventory for Vivado ``report_cdc -details`` evidence.

The report is evidence input, not a waiver.  Only the warning families
explicitly bound to the reviewed RXv2 hierarchy are classified; an unknown
warning, malformed row, duplicate endpoint, summary/detail mismatch, or bus
range mismatch is an error rather than an assumed new CDC category.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import re
from typing import Iterable


class InventoryError(RuntimeError):
    pass


WARNING_IDS = {"CDC-6", "CDC-15", "CDC-17"}
AD9361_ROOT = "receiver_v2_i/axi_ad9361/inst/"
HDMI_ROOT = "receiver_v2_i/axi_hdmi_core/inst/"
WFIFO_PARENT = "receiver_v2_i/rf_rx_fifo/inst"
MAILBOX_PARENT = "receiver_v2_i/axi_hdmi_core/inst/i_frame_mailbox"
GRAY_SOURCE_PARENT = "receiver_v2_i/axi_hdmi_core/inst/i_tx_core"
GRAY_DEST_PARENT = "receiver_v2_i/axi_hdmi_core/inst/i_vdma"
SUMMARY_RE = re.compile(r"^\s*(CDC-\d+)\s+(\w+)\s+(\d+)\s+")
CLOCK_RE = re.compile(r"^\s*(Source|Destination) Clock:\s*(\S.*?)\s*$")
ENDPOINT_RE = re.compile(
    r"^(?P<parent>.+)/(?P<register>[A-Za-z0-9_]+_reg)"
    r"(?P<bits>\[(?P<hi>\d+)(?::(?P<lo>\d+))?\])?/(?P<pin>[CD])$")


@dataclass(frozen=True)
class CdcRow:
    number: int
    cdc_id: str
    severity: str
    description: str
    depth: int
    exception: str
    source_clock: str
    destination_clock: str
    source: str
    destination: str


@dataclass(frozen=True)
class Endpoint:
    parent: str
    register: str
    bits: tuple[int, ...]
    pin: str


def endpoint(text: str) -> Endpoint:
    match = ENDPOINT_RE.match(text)
    if not match:
        raise InventoryError(f"unparseable endpoint: {text}")
    if match.group("hi") is None:
        # CDC-3/CDC-9 single-bit scalar rows have no Verilog bit suffix. They
        # are valid endpoints for the separate exact Info manifest, but never
        # enter the bundled-data bit expansion below.
        return Endpoint(match.group("parent"), match.group("register"), (), match.group("pin"))
    hi = int(match.group("hi"))
    lo = int(match.group("lo") or hi)
    return Endpoint(match.group("parent"), match.group("register"),
                    tuple(range(min(lo, hi), max(lo, hi) + 1)), match.group("pin"))


def _row_fields(line: str) -> list[str] | None:
    fields = re.split(r"\s{2,}", line.strip())
    if len(fields) < 3 or not re.fullmatch(r"\d+", fields[0]) or not re.fullmatch(r"CDC-\d+", fields[1]):
        return None
    if len(fields) != 8:
        raise InventoryError(f"malformed CDC detail row ({len(fields)} fields): {line}")
    return fields


def parse_report(text: str) -> tuple[dict[str, int], list[CdcRow]]:
    """Parse all detailed rows and bind each to its report clock block."""
    summary: dict[str, int] = {}
    all_summary: dict[tuple[str, str], int] = {}
    rows: list[CdcRow] = []
    source_clock: str | None = None
    destination_clock: str | None = None
    for raw in text.splitlines():
        summary_match = SUMMARY_RE.match(raw)
        if summary_match:
            cdc_id, severity, count = summary_match.groups()
            if severity not in {"Info", "Warning", "Critical"}:
                raise InventoryError(f"unknown CDC summary severity: {severity}")
            key = (cdc_id, severity)
            if key in all_summary:
                raise InventoryError(f"duplicate CDC summary ID/severity: {key}")
            if any(identifier == cdc_id for identifier, _ in all_summary):
                raise InventoryError(f"CDC ID has conflicting summary severities: {cdc_id}")
            all_summary[key] = int(count)
            if severity == "Warning":
                if cdc_id in summary:
                    raise InventoryError(f"duplicate warning summary ID: {cdc_id}")
                summary[cdc_id] = int(count)
            continue
        clock_match = CLOCK_RE.match(raw)
        if clock_match:
            if clock_match.group(1) == "Source":
                source_clock = clock_match.group(2)
                destination_clock = None
            else:
                destination_clock = clock_match.group(2)
            continue
        fields = _row_fields(raw)
        if fields is None:
            continue
        if source_clock is None or destination_clock is None:
            raise InventoryError(f"CDC detail row lacks clock block: {raw}")
        number, cdc_id, severity, description, depth, exception, source, destination = fields
        if severity not in {"Info", "Warning", "Critical"}:
            raise InventoryError(f"unknown CDC detail severity: {severity}")
        try:
            depth_i = int(depth)
        except ValueError as exc:
            raise InventoryError(f"noninteger CDC depth: {raw}") from exc
        rows.append(CdcRow(int(number), cdc_id, severity, description, depth_i, exception,
                           source_clock, destination_clock, source, destination))
    if not rows:
        raise InventoryError("no CDC detailed rows parsed")
    if not summary:
        raise InventoryError("no warning summary parsed")
    detail = Counter(row.cdc_id for row in rows if row.severity == "Warning")
    if set(summary) != set(detail):
        raise InventoryError(f"warning summary/detail ID mismatch: summary={dict(summary)} detail={dict(detail)}")
    for cdc_id, count in summary.items():
        if detail[cdc_id] != count:
            raise InventoryError(f"warning summary/detail count mismatch for {cdc_id}: {count}!={detail[cdc_id]}")
    # Warning-only reconciliation could silently lose a truncated Info or
    # Critical row. Every severity must account for its own summary count.
    all_detail = Counter((row.cdc_id, row.severity) for row in rows)
    if dict(all_detail) != all_summary:
        raise InventoryError(f"all-severity CDC summary/detail count mismatch: "
                             f"summary={all_summary} detail={dict(all_detail)}")
    unknown = set(summary) - WARNING_IDS
    if unknown:
        raise InventoryError(f"unclassified warning CDC IDs: {sorted(unknown)}")
    return summary, rows


def _same_bits(source: Endpoint, destination: Endpoint, row: CdcRow) -> None:
    if source.bits != destination.bits:
        raise InventoryError(f"bit range mismatch: {row.source} -> {row.destination}")


def _reject_tx(*endpoints: Endpoint) -> None:
    if any("/i_tx/" in item.parent or "/axi_ad9361_tx" in item.parent for item in endpoints):
        raise InventoryError(f"retired TX hierarchy appeared in CDC warning: {[item.parent for item in endpoints]}")


def _pair(parent: str, source: Endpoint, destination: Endpoint) -> bool:
    return source.parent == parent and destination.parent == parent


def classify(row: CdcRow) -> str:
    if row.severity != "Warning":
        raise InventoryError("only warning rows may enter typed inventory")
    source, destination = endpoint(row.source), endpoint(row.destination)
    _reject_tx(source, destination)
    if source.pin != "C" or destination.pin != "D":
        raise InventoryError(f"unexpected source/destination pin orientation: {row.source} -> {row.destination}")
    if (row.cdc_id in {"CDC-15", "CDC-17"} and source.register == "up_xfer_data_reg" and
            destination.register == "d_data_cntrl_int_reg" and _pair(source.parent, source, destination)):
        _same_bits(source, destination, row)
        if not (source.parent.startswith(AD9361_ROOT) or source.parent.startswith(HDMI_ROOT)):
            raise InventoryError(f"control/observer outside reviewed axi_ad9361/axi_hdmi_core roots: {source.parent}")
        if "/i_observer" in source.parent and "/i_observer_xfer" in source.parent:
            return "observer"
        if "/i_xfer_cntrl" in source.parent:
            return "control"
        raise InventoryError(f"unreviewed up_xfer_cntrl parent: {source.parent}")
    if (row.cdc_id in {"CDC-15", "CDC-17"} and source.register == "d_xfer_data_reg" and
            destination.register == "up_data_status_int_reg" and _pair(source.parent, source, destination)):
        _same_bits(source, destination, row)
        if (not (source.parent.startswith(AD9361_ROOT) or source.parent.startswith(HDMI_ROOT)) or
                "/i_xfer_status" not in source.parent):
            raise InventoryError(f"status outside reviewed axi_ad9361/axi_hdmi_core i_xfer_status hierarchy: {source.parent}")
        return "status"
    if (row.cdc_id == "CDC-15" and source.register == "d_count_reg" and
            destination.register == "up_d_count_reg" and _pair(source.parent, source, destination)):
        _same_bits(source, destination, row)
        if (not (source.parent.startswith(AD9361_ROOT) or source.parent.startswith(HDMI_ROOT)) or
                "/i_clock_mon" not in source.parent):
            raise InventoryError(f"monitor outside reviewed axi_ad9361/axi_hdmi_core i_clock_mon hierarchy: {source.parent}")
        return "monitor"
    if (row.cdc_id == "CDC-15" and source.register == "src_payload_hold_reg" and
            destination.register == "dst_addr_reg" and _pair(MAILBOX_PARENT, source, destination)):
        _same_bits(source, destination, row)
        return "mailbox"
    if (row.cdc_id == "CDC-6" and source.register == "din_enable_reg" and
            destination.register == "dout_enable_m1_reg" and _pair(WFIFO_PARENT, source, destination)):
        _same_bits(source, destination, row)
        return "wfifo_enable"
    if (row.cdc_id == "CDC-17" and source.register == "din_rinit_reg" and
            destination.register == "dout_rinit_reg" and _pair(WFIFO_PARENT, source, destination)):
        _same_bits(source, destination, row)
        return "wfifo_rinit"
    if (row.cdc_id == "CDC-6" and source.register == "hdmi_raddr_g_reg" and
            destination.register == "vdma_raddr_g_m1_reg" and
            source.parent == GRAY_SOURCE_PARENT and destination.parent == GRAY_DEST_PARENT):
        _same_bits(source, destination, row)
        return "hdmi_gray"
    raise InventoryError(f"unclassified warning endpoint: {row.cdc_id} {row.source} -> {row.destination}")


def exact_pairs(row: CdcRow, kind: str) -> list[dict[str, object]]:
    source, destination = endpoint(row.source), endpoint(row.destination)
    _same_bits(source, destination, row)
    pairs = []
    for bit in source.bits:
        pairs.append({"kind": kind, "cdc_id": row.cdc_id, "source_clock": row.source_clock,
                      "destination_clock": row.destination_clock,
                      "source": f"{source.parent}/{source.register}[{bit}]/C",
                      "destination": f"{destination.parent}/{destination.register}[{bit}]/D",
                      "source_parent": source.parent, "destination_parent": destination.parent,
                      "bit": bit})
    return pairs


def inventory(text: str) -> dict[str, object]:
    summary, rows = parse_report(text)
    warnings = [row for row in rows if row.severity == "Warning"]
    seen: set[tuple[str, str, str, str, str]] = set()
    typed: Counter[str] = Counter()
    pairs: list[dict[str, object]] = []
    records: list[dict[str, object]] = []
    for row in warnings:
        key = (row.cdc_id, row.source_clock, row.destination_clock, row.source, row.destination)
        if key in seen:
            raise InventoryError(f"duplicate exact CDC pair: {key}")
        seen.add(key)
        kind = classify(row)
        typed[kind] += 1
        records.append({**asdict(row), "kind": kind})
        pairs.extend(exact_pairs(row, kind))
    pair_seen = {(pair["source_clock"], pair["destination_clock"], pair["source"], pair["destination"])
                 for pair in pairs}
    if len(pair_seen) != len(pairs):
        raise InventoryError("duplicate expanded exact bit pair")
    return {"schema": "rfv2-cdc-typed-inventory-v1", "summary_warning_counts": summary,
            "detail_warning_counts": dict(Counter(row.cdc_id for row in warnings)),
            "typed_warning_rows": dict(sorted(typed.items())), "expanded_exact_pairs": pairs,
            "warning_rows": records, "physical_observation_only": True, "release_ready": False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.out.exists():
        raise SystemExit(f"out must be fresh: {args.out}")
    result = inventory(args.report.read_text(encoding="utf-8", errors="strict"))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print("CDC_TYPED_INVENTORY_PASS warning_rows=%d expanded_pairs=%d" %
          (len(result["warning_rows"]), len(result["expanded_exact_pairs"])))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (InventoryError, OSError, UnicodeError) as exc:
        raise SystemExit(f"CDC typed inventory refused: {exc}") from exc
