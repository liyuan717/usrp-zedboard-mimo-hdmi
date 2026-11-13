#!/usr/bin/env python3
"""Fail-closed, read-only routed CDC/external-I/O pre-signoff receipt.

This is deliberately separate from ``receiver_v2_signoff_gates.tcl``.  It
does not alter constraints, waive ``report_cdc`` rows, or declare board/MTBF
success.  The receipt is PASS only when timing is nonnegative, a reviewed
exact Info manifest matches, all typed warning endpoints match, the two known
reset CDC-10 representatives are present, and the routed Tcl observer passes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

from cdc_info_manifest import info_rows
from cdc_critical_contract import validate_critical_rows
from cdc_typed_inventory import InventoryError, inventory, parse_report

TIMING_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s+(\d+)\s+\d+\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s+(\d+)\s+", re.M)
DRC_ALL_TABLE_RE = re.compile(r"^\|\s*([A-Z][A-Z0-9_-]*-\d+)\s*\|\s*([^|]+?)\s*\|.*?\|\s*(\d+)\s*\|\s*$", re.M)
AUDIT_CLOSURE = (
    "capture_reset_guard_contract.tcl", "release_assertion_contract.tcl",
    "release_assertion_effect.tcl", "hdmi_epoch_cdc_contract.tcl",
    "receiver_v2_external_io_contract.tcl",
    "tests/run_routed_cdc_external_presignoff.py", "tests/routed_cdc_external_audit.tcl",
    "tests/cdc_typed_exact_pairs.tcl", "tests/cdc_typed_sync_stages.tcl",
    "tests/cdc_typed_inventory.py", "tests/cdc_info_manifest.py",
    "tests/cdc_critical_contract.py",
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def require_completed_implementation(build: dict) -> None:
    """Use the board builder's actual terminal contract, never a status alias."""
    if (build.get("status") != "PASS" or build.get("mode") != "implement"
            or type(build.get("returncode")) is not int or build["returncode"] != 0
            or build.get("source_guard") is not True or build.get("vendor_guard") is not True
            or build.get("tool_errors") != [] or build.get("hdmi") is not True):
        raise RuntimeError("build is not a guarded PASS implementation with HDMI")


def bind_checkpoint(build: dict, receipt_path: Path, dcp: Path) -> None:
    name = "project/receiver_v2_routed.dcp"
    artifact = build.get("artifacts", {}).get(name)
    if (not isinstance(artifact, dict) or dcp.resolve() != (receipt_path.parent / name).resolve()
            or artifact.get("sha256") != sha256(dcp)
            or type(artifact.get("size_bytes")) is not int
            or artifact["size_bytes"] != dcp.stat().st_size):
        raise RuntimeError("DCP is not the final routed artifact bound by this build receipt")


def bind_audit_closure(build: dict, receipt_path: Path, live: Path) -> tuple[Path, dict]:
    frozen = receipt_path.parent / "sources/pl/receiver_v2/integration"
    hashes = {}
    for relative in AUDIT_CLOSURE:
        expected = build.get("sources", {}).get("pl/receiver_v2/integration/" + relative)
        if not isinstance(expected, str) or len(expected) != 64:
            raise RuntimeError("build lacks audit source binding: " + relative)
        for path in (frozen / relative, live / relative):
            if not path.is_file() or sha256(path) != expected:
                raise RuntimeError("changed or missing audit source: " + str(path))
            hashes[str(path)] = expected
    return frozen, hashes


def parse_drc(text: str) -> dict:
    """An absent/truncated summary is unproven, not an empty violation set."""
    if "Report DRC" not in text or "1. REPORT SUMMARY" not in text:
        raise RuntimeError("unrecognized DRC report")
    totals = re.findall(r"^\s*Checks found:\s*(\d+)\s*$", text, re.M)
    if len(totals) != 1:
        raise RuntimeError("missing or duplicate DRC Checks found total")
    rows = DRC_ALL_TABLE_RE.findall(text)
    seen = set()
    counts = 0
    violations, advisories = [], []
    for rule, severity, count_text in rows:
        severity = severity.strip()
        if rule in seen or severity not in {"Error", "Critical Warning", "Warning", "Advisory"}:
            raise RuntimeError("duplicate rule or unknown DRC severity")
        seen.add(rule)
        count = int(count_text)
        counts += count
        record = {"rule": rule, "severity": severity, "count": count}
        if count:
            (advisories if severity == "Advisory" else violations).append(record)
    if counts != int(totals[0]):
        raise RuntimeError("DRC summary rows do not account for Checks found total")
    if "2. REPORT DETAILS" not in text:
        raise RuntimeError("DRC report lacks details")
    return {"checks_found": counts, "violations": violations, "advisories": advisories}


def audit_ledger(path: Path) -> dict[str, str]:
    rows = path.read_text(encoding="utf-8", errors="strict").splitlines()[1:]
    result: dict[str, str] = {}
    for row in rows:
        fields = row.split("\t", 2)
        if len(fields) != 3 or fields[0] in result or fields[1] not in {"PASS", "FAIL"}:
            raise RuntimeError(f"malformed audit ledger row: {row!r}")
        result[fields[0]] = fields[1]
    expected = {"capture_reset_guard_post_route", "release_assertion_topology",
                "release_assertion_effect", "hdmi_epoch_ready_cdc", "hdmi_epoch_ack_cdc",
                "external_io_portsets", "external_io_static_topology"}
    if set(result) != expected:
        raise RuntimeError(f"audit ledger check set mismatch: {sorted(result)}")
    return result


def write_pairs(path: Path, pairs: list[dict[str, object]]) -> None:
    fields = ("kind", "cdc_id", "source_clock", "destination_clock", "source", "destination",
              "source_parent", "destination_parent", "bit")
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("\t".join(fields) + "\n")
        for pair in pairs:
            if set(fields) - set(pair):
                raise RuntimeError(f"malformed typed pair: {pair}")
            values = [str(pair[field]) for field in fields]
            if any("\t" in value or "\n" in value for value in values):
                raise RuntimeError("unsafe typed pair field")
            handle.write("\t".join(values) + "\n")


def run_readonly_vivado(vivado: Path, script: Path, arguments: list[str], cwd: Path, log: Path) -> str:
    command = [str(vivado), "-mode", "batch", "-log", str(log.with_suffix(".vivado.log")),
               "-journal", str(log.with_suffix(".jou")), "-source", str(script), "-tclargs", *arguments]
    with log.open("w", encoding="utf-8", errors="replace") as handle:
        result = subprocess.run(command, cwd=cwd, stdout=handle, stderr=subprocess.STDOUT, text=True, timeout=1800)
    text = log.read_text(encoding="utf-8", errors="replace")
    if result.returncode:
        raise RuntimeError(f"Vivado observer exit {result.returncode}: {script.name}")
    return text


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dcp", type=Path, required=True)
    parser.add_argument("--build-receipt", type=Path, required=True)
    parser.add_argument("--info-manifest", type=Path, required=True)
    parser.add_argument("--hdmi-epoch-receipt", type=Path, required=True,
                        help="PASS receipt from run_hdmi_active_reset.py for the final build's pinned overlay")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--vivado", type=Path, default=Path("G:/AMDDesignTools/2025.2/Vivado/bin/vivado.bat"))
    args = parser.parse_args()
    dcp, build_receipt, info_manifest, epoch_receipt, out = (item.resolve() for item in
        (args.dcp, args.build_receipt, args.info_manifest, args.hdmi_epoch_receipt, args.out))
    if out.exists():
        raise SystemExit(f"out must be fresh: {out}")
    if not dcp.is_file() or not build_receipt.is_file() or not info_manifest.is_file() or not epoch_receipt.is_file() or not args.vivado.is_file():
        raise SystemExit("DCP, build receipt, Info manifest, HDMI epoch receipt, or Vivado launcher is missing")
    out.mkdir(parents=True)
    before = {"dcp": sha256(dcp), "build_receipt": sha256(build_receipt),
              "info_manifest": sha256(info_manifest), "hdmi_epoch_receipt": sha256(epoch_receipt)}
    live_integration = Path(__file__).resolve().parents[1]
    build = json.loads(build_receipt.read_text(encoding="utf-8", errors="strict"))
    require_completed_implementation(build)
    bind_checkpoint(build, build_receipt, dcp)
    integration, audit_sources = bind_audit_closure(build, build_receipt, live_integration)
    script = integration / "tests/routed_cdc_external_audit.tcl"
    log = out / "vivado_console.log"
    cmd = [str(args.vivado), "-mode", "batch", "-log", str(out / "vivado.log"),
           "-journal", str(out / "vivado.jou"), "-source", str(script), "-tclargs",
           str(dcp), str(out / "vivado_audit"), str(integration)]
    with log.open("w", encoding="utf-8", errors="replace") as handle:
        completed = subprocess.run(cmd, cwd=out, stdout=handle, stderr=subprocess.STDOUT, text=True, timeout=1800)
    failures: list[str] = []
    if completed.returncode != 0:
        failures.append(f"Vivado audit process exit {completed.returncode}")
    audit_dir = out / "vivado_audit"
    try:
        ledger = audit_ledger(audit_dir / "audit_ledger.tsv")
        failures.extend(f"{name} failed" for name, result in ledger.items() if result != "PASS")
    except (OSError, RuntimeError, UnicodeError) as exc:
        ledger = {}
        failures.append(f"audit ledger unproven: {exc}")
    try:
        report = audit_dir / "implementation_cdc.rpt"
        typed = inventory(report.read_text(encoding="utf-8", errors="strict"))
        summary, rows = parse_report(report.read_text(encoding="utf-8", errors="strict"))
        manifest = json.loads(info_manifest.read_text(encoding="utf-8", errors="strict"))
        if manifest.get("schema") != "rfv2-cdc-info-manifest-v1" or manifest.get("reviewed") is not True:
            raise RuntimeError("Info manifest is absent or not explicitly reviewed=true")
        if manifest.get("info_rows") != info_rows(report):
            raise RuntimeError("Info manifest exact endpoint/source/clock set differs from routed report")
        critical = validate_critical_rows(rows)
        cdc = {"summary_warning_counts": summary, "typed_warning_rows": typed["typed_warning_rows"],
               "expanded_exact_pairs": len(typed["expanded_exact_pairs"]),
               "info_rows": len(info_rows(report)), "critical_exact_pairs": len(critical)}
        pair_tsv = out / "typed_pairs.tsv"
        write_pairs(pair_tsv, typed["expanded_exact_pairs"])
        exact_out = out / "exact_pairs"
        exact_log = run_readonly_vivado(args.vivado, integration / "tests/cdc_typed_exact_pairs.tcl",
                                        [str(dcp), str(pair_tsv), str(exact_out)], out, out / "exact_pairs_console.log")
        if "CDC_TYPED_EXACT_PAIR_OBSERVATION_COMPLETE" not in exact_log:
            raise RuntimeError("exact-pair observer marker absent")
        measured = (exact_out / "exact_pairs.tsv").read_text(encoding="utf-8", errors="strict").splitlines()
        if len(measured) != len(typed["expanded_exact_pairs"]) + 1:
            raise RuntimeError("exact-pair observer count differs from typed inventory")
        sync_out = out / "sync_stages"
        sync_log = run_readonly_vivado(args.vivado, integration / "tests/cdc_typed_sync_stages.tcl",
                                       [str(dcp), str(sync_out)], out, out / "sync_stages_console.log")
        if "CDC_TYPED_SYNC_STAGES_COMPLETE pairs=15" not in sync_log:
            raise RuntimeError("15-pair synchronizer observer marker absent")
        sync_rows = (sync_out / "synchronizer_stages.tsv").read_text(encoding="utf-8", errors="strict").splitlines()
        if len(sync_rows) != 16:
            raise RuntimeError("synchronizer observer row count is not exactly 15")
        cdc["exact_pair_physical_observation"] = {"pairs": len(measured) - 1,
                                                    "sync_stages": len(sync_rows) - 1}
    except (InventoryError, OSError, RuntimeError, UnicodeError, json.JSONDecodeError) as exc:
        cdc = {"unproven": str(exc)}
        failures.append(f"CDC provenance/type proof failed: {exc}")
    try:
        timing_text = (audit_dir / "timing_summary.rpt").read_text(encoding="utf-8", errors="strict")
        matches = TIMING_RE.findall(timing_text)
        if len(matches) != 1:
            raise RuntimeError("could not identify one timing-summary metric row")
        wns, tns, failing_setup, whs, ths, failing_hold = matches[0]
        timing = {"wns_ns": float(wns), "tns_ns": float(tns), "failing_setup_endpoints": int(failing_setup),
                  "whs_ns": float(whs), "ths_ns": float(ths), "failing_hold_endpoints": int(failing_hold)}
        if timing["wns_ns"] < 0 or timing["tns_ns"] < 0 or timing["failing_setup_endpoints"] != 0:
            failures.append("negative setup timing or failing setup endpoints")
        if timing["whs_ns"] < 0 or timing["ths_ns"] < 0 or timing["failing_hold_endpoints"] != 0:
            failures.append("negative hold timing or failing hold endpoints")
    except (OSError, RuntimeError, UnicodeError, ValueError) as exc:
        timing = {"unproven": str(exc)}
        failures.append(f"timing proof failed: {exc}")
    try:
        # Any reported routed DRC violation remains visible and blocks this
        # receipt.  In particular BRAM asynchronous-address and RAM-enable
        # findings must be repaired, not classified as legal CDC traffic.
        drc_text = (audit_dir / "drc.rpt").read_text(encoding="utf-8", errors="strict")
        drc = parse_drc(drc_text)
        if drc["violations"]:
            failures.append("routed DRC has unresolved violations")
    except (OSError, UnicodeError, RuntimeError) as exc:
        drc = {"unproven": str(exc)}
        failures.append(f"DRC proof failed: {exc}")
    try:
        build = json.loads(build_receipt.read_text(encoding="utf-8", errors="strict"))
        require_completed_implementation(build)
        epoch = json.loads(epoch_receipt.read_text(encoding="utf-8", errors="strict"))
        if epoch.get("schema") != "receiver-v2-hdmi-active-reset-sim/v1" or epoch.get("status") != "PASS":
            raise RuntimeError("HDMI epoch functional receipt is not the required PASS schema")
        if epoch.get("hardware_verified") is not False:
            raise RuntimeError("HDMI epoch receipt must retain hardware_verified=false")
        build_sources = build.get("sources")
        build_vendor = build.get("vendor_inputs")
        epoch_hashes = set(epoch.get("input_sha256_after", {}).values())
        required_paths = ("pl/receiver_v2/integration/hdmi_reset_patch.py",
                          "pl/receiver_v2/integration/hdmi_reset_patch_sources/axi_hdmi_tx_reset_sync.v",
                          "pl/receiver_v2/integration/hdmi_reset_patch_sources/axi_hdmi_tx_axis_epoch_bridge.v")
        if not isinstance(build_sources, dict) or any(build_sources.get(path) not in epoch_hashes for path in required_paths):
            raise RuntimeError("HDMI epoch functional receipt is not source-hash bound to this build")
        # A patcher script match is insufficient: prove that the core, VDMA,
        # top, reset helper, epoch bridge and up-control HDL actually compiled
        # by the functional test are byte-identical to the final build's
        # vendor input closure.
        generated = epoch.get("generated_sha256_after")
        expected_vendor_paths = (
            "library/axi_hdmi_tx/axi_hdmi_tx.v",
            "library/axi_hdmi_tx/axi_hdmi_tx_core.v",
            "library/axi_hdmi_tx/axi_hdmi_tx_vdma.v",
            "library/axi_hdmi_tx/axi_hdmi_tx_reset_sync.v",
            "library/axi_hdmi_tx/axi_hdmi_tx_axis_epoch_bridge.v",
            "library/common/up_hdmi_tx.v",
        )
        if not isinstance(build_vendor, dict) or not isinstance(generated, dict):
            raise RuntimeError("build or functional receipt lacks HDMI vendor HDL hash map")
        normalized_generated = {str(path).replace("\\", "/"): digest for path, digest in generated.items()}
        for relative in expected_vendor_paths:
            match = [digest for path, digest in normalized_generated.items() if path.endswith("/" + relative)]
            if len(match) != 1 or build_vendor.get(relative.replace("/", "\\")) != match[0]:
                raise RuntimeError(f"functional HDMI HDL hash does not match final vendor input: {relative}")
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        failures.append(f"build receipt unproven: {exc}")
    except RuntimeError as exc:
        failures.append(f"HDMI epoch functional proof failed: {exc}")
    after = {"dcp": sha256(dcp), "build_receipt": sha256(build_receipt), "info_manifest": sha256(info_manifest),
             "hdmi_epoch_receipt": sha256(epoch_receipt)}
    if before != after:
        failures.append("one or more supplied evidence inputs changed during audit")
    if any(not Path(path).is_file() or sha256(Path(path)) != digest for path, digest in audit_sources.items()):
        failures.append("audit executable closure changed during audit")
    receipt = {"schema": "rfv2-routed-cdc-external-presignoff-v1",
               "status": "PASS" if not failures else "FAIL", "release_ready": False,
               "scope": "offline routed pre-signoff only; no board, FDD, electrical, or MTBF claim",
               "inputs_sha256": before, "audit_sources_sha256": audit_sources,
               "audit_ledger": ledger, "cdc": cdc, "timing": timing, "drc": drc,
               "failures": failures,
               "required_before_release": ["final routed DCP after guarded build receipt PASS/implement", "reviewed exact Info manifest",
                   "all setup/hold timing nonnegative", "independent hardware RF/FDD and electrical validation"]}
    (out / "receipt.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"ROUTED_CDC_EXTERNAL_PRESIGNOFF_{receipt['status']} failures={len(failures)}")
    return 0 if not failures else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, UnicodeError, subprocess.SubprocessError) as exc:
        print(f"routed CDC/external pre-signoff failed: {exc}", file=sys.stderr)
        raise SystemExit(2)
