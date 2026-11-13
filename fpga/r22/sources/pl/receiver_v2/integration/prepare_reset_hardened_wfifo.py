#!/usr/bin/env python3
"""Prepare/validate the portable r7 WFIFO RAM-address hardening overlay.

The parent is a fully admitted r6 HDMI-reset overlay, not an arbitrary ADI
tree.  This script never modifies that parent and refuses a non-fresh child.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import shutil
import stat
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATIC = HERE / "reset_hardened_wfifo_manifest.json"
RECEIPT = "WFIFO_RESET_HARDENED_RECEIPT.json"
PARENT_RECEIPT = "RESET_HARDENED_ADI_RECEIPT.json"
REL = "library/util_wfifo/util_wfifo.v"
CORE_REL = "library/axi_hdmi_tx/axi_hdmi_tx_core.v"
VDMA_REL = "library/axi_hdmi_tx/axi_hdmi_tx_vdma.v"
CHANGED = (REL, CORE_REL, VDMA_REL)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree_records(root: Path, exclude: set[str] | None = None) -> list[str]:
    exclude = exclude or set()
    out: list[str] = []
    for path in sorted(root.rglob("*")):
        meta = path.lstat()
        if stat.S_ISLNK(meta.st_mode) or getattr(meta, "st_file_attributes", 0) & 0x400:
            raise ValueError(f"link/reparse point forbidden: {path}")
        if path.is_file() and path.relative_to(root).as_posix() not in exclude:
            out.append(path.relative_to(root).as_posix() + "\t" + sha(path))
    return out


def digest(records: list[str]) -> str:
    return hashlib.sha256(("\n".join(records) + "\n").encode()).hexdigest()


def reject_root_link(path: Path, label: str) -> Path:
    candidate = path.absolute()
    meta = candidate.lstat()
    if stat.S_ISLNK(meta.st_mode) or getattr(meta, "st_file_attributes", 0) & 0x400:
        raise ValueError(f"{label} root link/reparse point forbidden: {candidate}")
    return candidate.resolve(strict=True)


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def controls(parent: Path, include_static: bool) -> dict[str, Path]:
    result = {
        "preparer": Path(__file__).resolve(),
        "patcher": HERE / "wfifo_reset_patch.py",
        "parent_r6_receipt": parent / PARENT_RECEIPT,
        "parent_r6_static_manifest": HERE / "reset_hardened_adi_manifest.json",
    }
    if include_static:
        result["static_manifest"] = STATIC
    return result


def hashes(items: dict[str, Path]) -> dict[str, str]:
    return {role: sha(path) for role, path in items.items()}


def require_unchanged(before: dict[str, str], items: dict[str, Path], label: str) -> None:
    if hashes(items) != before:
        raise ValueError(f"{label} changed during preparation")


def parent_static_identity(parent: Path) -> dict:
    r6_static = json.loads((HERE / "reset_hardened_adi_manifest.json").read_text(encoding="utf-8"))
    receipt = json.loads((parent / PARENT_RECEIPT).read_text(encoding="utf-8"))
    return {
        "parent_overlay": "receiver-v2-reset-hardened-r6",
        "r6_static_manifest_sha256": sha(HERE / "reset_hardened_adi_manifest.json"),
        "r6_expected_tree_file_count": r6_static["expected_output_tree_file_count"],
        "r6_expected_tree_sha256": r6_static["expected_output_tree_sha256"],
        "r6_changed_file_sha256": r6_static["changed_file_sha256"],
        "r6_status": receipt["status"],
    }


def make_candidate(out: Path, parent_identity: dict) -> dict:
    records = tree_records(out, {RECEIPT})
    return {
        "schema": "receiver-v2-wfifo-reset-hardened-r7/v1",
        "parent_static_identity": parent_identity,
        "changed_file_sha256": {rel: sha(out / rel) for rel in CHANGED},
        "expected_output_tree_file_count": len(records),
        "expected_output_tree_sha256": digest(records),
    }


def load_static() -> dict:
    data = json.loads(STATIC.read_text(encoding="utf-8"))
    if data.get("schema") != "receiver-v2-wfifo-reset-hardened-r7/v1" or set(data.get("changed_file_sha256", {})) != set(CHANGED):
        raise ValueError("invalid r7 static manifest")
    return data


def prepare(parent: Path, out: Path, propose: bool = False) -> dict:
    if out.exists():
        raise ValueError("output must be fresh")
    parent = reject_root_link(parent, "parent r6")
    out = out.absolute()
    r6 = load_module("reset_hardened_r6", HERE / "prepare_reset_hardened_adi.py")
    patch = load_module("wfifo_reset_patch", HERE / "wfifo_reset_patch.py")
    items = controls(parent, not propose)
    before = hashes(items)
    parent_receipt_sha = r6.validate_reset_hardened_adi(parent)
    if parent_receipt_sha != before["parent_r6_receipt"]:
        raise ValueError("r6 parent receipt changed during admission")
    identity = parent_static_identity(parent)
    shutil.copytree(parent, out)
    if r6.validate_reset_hardened_adi(parent) != parent_receipt_sha:
        raise ValueError("r6 parent changed during copy")
    require_unchanged(before, items, "r7 admission controls")
    target = out / REL
    target.write_text(patch.patch_util_wfifo(parent / REL), encoding="utf-8", newline="\n")
    for rel in (CORE_REL, VDMA_REL):
        (out / rel).write_text(patch.patch_epoch_source(parent / rel, rel), encoding="utf-8", newline="\n")
    candidate = make_candidate(out, identity)
    if propose:
        (out / "r7_manifest_candidate.json").write_text(json.dumps(candidate, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return candidate
    static = load_static()
    if candidate != static:
        raise ValueError("r7 output differs from reviewed static manifest")
    require_unchanged(before, items, "r7 admission controls")
    if r6.validate_reset_hardened_adi(parent) != parent_receipt_sha:
        raise ValueError("r6 parent changed before receipt publication")
    after = hashes(items)
    receipt = {
        "schema": "receiver-v2-wfifo-reset-hardened-r7-receipt/v1",
        "status": "PREPARED_NOT_BUILT",
        "hardware_verified": False,
        "parent_provenance_path": str(parent),
        "parent_r6_receipt_sha256": parent_receipt_sha,
        "parent_static_identity": identity,
        "preparer_sha256": sha(Path(__file__).resolve()),
        "patcher_sha256": sha(HERE / "wfifo_reset_patch.py"),
        "parent_input_sha256": {REL: patch.EXPECTED_PARENT_SHA256,
                                 CORE_REL: patch.EXPECTED_CORE_SHA256,
                                 VDMA_REL: patch.EXPECTED_VDMA_SHA256},
        "admission_control_sha256_before": before,
        "admission_control_sha256_after": after,
        "changed_file_sha256": candidate["changed_file_sha256"],
        "output_tree_records_excluding_receipt": tree_records(out, {RECEIPT}),
        "output_tree_sha256_excluding_receipt": candidate["expected_output_tree_sha256"],
    }
    (out / RECEIPT).write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return receipt


def validate_reset_hardened_wfifo(out: Path) -> str:
    static = load_static()
    out = reject_root_link(out, "r7 output")
    receipt = json.loads((out / RECEIPT).read_text(encoding="utf-8"))
    if receipt.get("schema") != "receiver-v2-wfifo-reset-hardened-r7-receipt/v1":
        raise ValueError("r7 receipt schema mismatch")
    if receipt.get("status") != "PREPARED_NOT_BUILT" or receipt.get("hardware_verified") is not False:
        raise ValueError("r7 receipt status is not pre-build")
    if receipt.get("parent_static_identity") != static["parent_static_identity"]:
        raise ValueError("r7 parent identity mismatch")
    if receipt.get("preparer_sha256") != sha(Path(__file__).resolve()) or receipt.get("patcher_sha256") != sha(HERE / "wfifo_reset_patch.py"):
        raise ValueError("r7 live admission control source hash mismatch")
    expected_roles = {"preparer", "patcher", "parent_r6_receipt", "parent_r6_static_manifest", "static_manifest"}
    before, after = receipt.get("admission_control_sha256_before"), receipt.get("admission_control_sha256_after")
    if not isinstance(before, dict) or set(before) != expected_roles or before != after:
        raise ValueError("r7 admission-control freeze mismatch")
    if receipt.get("parent_r6_receipt_sha256") != before["parent_r6_receipt"]:
        raise ValueError("r7 parent r6 receipt freeze mismatch")
    current = {"preparer": Path(__file__).resolve(), "patcher": HERE / "wfifo_reset_patch.py", "parent_r6_static_manifest": HERE / "reset_hardened_adi_manifest.json", "static_manifest": STATIC}
    for role, path in current.items():
        if before[role] != sha(path):
            raise ValueError("r7 current admission-control source hash mismatch")
    records = tree_records(out, {RECEIPT})
    if len(records) != static["expected_output_tree_file_count"] or digest(records) != static["expected_output_tree_sha256"]:
        raise ValueError("r7 full tree differs from static manifest")
    if receipt.get("changed_file_sha256") != static["changed_file_sha256"] or {rel: sha(out / rel) for rel in CHANGED} != static["changed_file_sha256"]:
        raise ValueError("r7 changed-file pin mismatch")
    if receipt.get("output_tree_records_excluding_receipt") != records or receipt.get("output_tree_sha256_excluding_receipt") != digest(records):
        raise ValueError("r7 receipt cannot self-sign output tree")
    return sha(out / RECEIPT)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent-r6", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--propose-manifest", action="store_true")
    parser.add_argument("--validate", action="store_true")
    args = parser.parse_args()
    if args.validate:
        print(validate_reset_hardened_wfifo(args.out)); return 0
    if not args.parent_r6:
        parser.error("--parent-r6 required for preparation")
    print(json.dumps(prepare(args.parent_r6, args.out, args.propose_manifest), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
