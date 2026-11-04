"""Bind pre-synthesis Vivado inputs to one final bit/XSA; never backfill old builds."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import zipfile
from pathlib import Path

if __package__:
    from .rf_release_bootstrap import verify_bootstrap
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from rf_release_bootstrap import verify_bootstrap

REQUIRED_RECIPES = {
    "pl/scripts/create_zedboard_rf_system.tcl",
    "pl/scripts/create_zedboard_rf_buffered_lvds_system.tcl",
    "pl/scripts/add_detector_module.tcl",
    "pl/scripts/rf_signoff_checks.tcl",
    "pl/scripts/rf_build_provenance.py",
    "pl/scripts/capture_zedboard_rf_inputs.tcl",
    "pl/scripts/rf_release_bootstrap.py",
    "pl/scripts/launch_zedboard_rf_release.py",
    "build/vendor_lvds_2v5_v5/source-manifest.json",
}
REQUIRED_RTL = {"pl/rtl/rf_rx_elastic_fifo.v", "pl/rtl/rf_rx_dma_enable_monitor.v",
                "pl/rtl/rf_lvds_forward_clock.v", "pl/rtl/mimo_detector_bd.v"}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_inputs(receipt: dict, repo: Path) -> dict[str, str]:
    if receipt.get("status") != "CAPTURED_BEFORE_SYNTHESIS" or receipt.get("hardware_tested") is not False:
        raise ValueError("Missing genuine pre-synthesis input receipt")
    rows = receipt.get("files")
    if not isinstance(rows, list) or not rows:
        raise ValueError("Empty Vivado input closure")
    names: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict) or row.get("role") not in {"synthesis", "constraint", "recipe", "design"}:
            raise ValueError("Invalid Vivado input row")
        path = Path(row["path"]).resolve()
        if path.is_symlink() or not path.is_file() or sha256(path) != row.get("sha256"):
            raise ValueError(f"Build input changed after pre-synthesis capture: {path}")
        if path.is_relative_to(repo.resolve()):
            name = path.relative_to(repo.resolve()).as_posix()
            names[name] = row["sha256"]
    if not (REQUIRED_RECIPES | REQUIRED_RTL).issubset(names):
        raise ValueError("Vivado input receipt lacks the fixed release source/recipe closure")
    return names


def verify_build_identity(build: Path) -> None:
    text = (build / "rf_build_receipt.txt").read_text(encoding="utf-8")
    for key, expected in {"part": "xc7z020clg484-1", "vivado": "2025.2",
                          "adi_commit": "4366bd63c1fe56b680b69145981a4493c6f8753a"}.items():
        values = re.findall(rf"^{key}=(.+)$", text, re.MULTILINE)
        if len(values) != 1 or values[0].strip() != expected:
            raise ValueError(f"Formal hardware build identity mismatch: {key}")


def verify_hardware_provenance(build: Path, repo: Path) -> dict[str, str]:
    sealed = json.loads((build / "rf_build_provenance.json").read_text(encoding="utf-8"))
    inputs = build / "rf_build_inputs.json"
    verify_bootstrap(build, repo)
    verify_build_identity(build)
    if (sealed.get("status") != "PRE_SYNTHESIS_INPUTS_UNCHANGED_BIT_XSA_BOUND"
            or sealed.get("hardware_tested") is not False
            or sealed.get("inputs_sha256") != sha256(inputs)):
        raise ValueError("RF hardware provenance is missing or stale")
    for name in ("zedboard_rf.bit", "zedboard_rf.xsa", "rf_signoff_route.dcp"):
        if sealed.get("artifacts_sha256", {}).get(name) != sha256(build / name):
            raise ValueError(f"Hardware artifact differs from its build-time provenance: {name}")
    with zipfile.ZipFile(build / "zedboard_rf.xsa") as archive:
        devices = json.loads(archive.read("xsa.json")).get("devices", [])
        if len(devices) != 1 or devices[0].get("part", {}).get("name") != "xc7z020clg484-1":
            raise ValueError("Independent XSA target part does not match the approved board")
    captured = json.loads(inputs.read_text(encoding="utf-8"))
    if captured.get("bootstrap_sha256") != sha256(build / "rf_bootstrap_inputs.json"):
        raise ValueError("Pre-synthesis inputs do not bind the original launch bootstrap")
    names = verify_inputs(captured, repo)
    for path in (build / "rf_build_receipt.txt", build / "rf_project_settings.txt",
                 build / "mimo_zedboard_rf.srcs/sources_1/bd/zedboard_rf_system/zedboard_rf_system.bd"):
        if names.get(path.resolve().relative_to(repo.resolve()).as_posix()) != sha256(path):
            raise ValueError("The consumed BD/project/identity container is absent from the source closure")
    return names


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("capture", "verify", "seal"))
    parser.add_argument("--build", type=Path, required=True)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--input-list", type=Path)
    args = parser.parse_args()
    receipt_path = args.build / "rf_build_inputs.json"
    bootstrap = verify_bootstrap(args.build, args.repo)
    verify_build_identity(args.build)
    if args.mode == "capture":
        if (receipt_path.exists() or (args.build / "zedboard_rf.bit").exists()
                or (args.build / "mimo_zedboard_rf.runs/synth_1/zedboard_rf_system_wrapper.dcp").exists()):
            raise ValueError("Cannot backfill a receipt into an existing synthesis/release candidate")
        if args.input_list is None:
            parser.error("capture requires the actual Vivado --input-list")
        rows = []
        seen = set()
        for line in args.input_list.read_text(encoding="utf-8").splitlines():
            role, name = line.split("\t", 1)
            path = Path(name).resolve()
            key = (role, str(path))
            if key in seen:
                continue
            seen.add(key)
            if not path.is_file() or path.is_symlink():
                raise ValueError(f"Actual Vivado input is missing or unsafe: {path}")
            rows.append({"role": role, "path": path.as_posix(), "sha256": sha256(path)})
        captured_paths = {Path(row["path"]).resolve() for row in rows}
        for name in bootstrap:
            path = (args.repo / name).resolve()
            if path not in captured_paths:
                rows.append({"role": "recipe", "path": path.as_posix(), "sha256": sha256(path)})
        receipt = {"status": "CAPTURED_BEFORE_SYNTHESIS", "hardware_tested": False,
                   "files": rows, "scope": "vivado_compile_order_constraints_and_fixed_recipes",
                   "bootstrap_sha256": sha256(args.build / "rf_bootstrap_inputs.json")}
        names = verify_inputs(receipt, args.repo)
        if any(names.get(name) != expected for name, expected in bootstrap.items()):
            raise ValueError("Pre-Vivado recipe freeze and actual synthesis inputs disagree")
        receipt_path.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    else:
        verify_inputs(json.loads(receipt_path.read_text(encoding="utf-8")), args.repo)
        if args.mode == "seal":
            seal = args.build / "rf_build_provenance.json"
            if seal.exists():
                raise ValueError("Refusing to replace hardware provenance")
            value = {"status": "PRE_SYNTHESIS_INPUTS_UNCHANGED_BIT_XSA_BOUND", "hardware_tested": False,
                     "diagnostic_only": json.loads((args.build / "rf_bootstrap_inputs.json").read_text()).get("diagnostic_only", False),
                     "rx_iddr_alignment_trial": json.loads((args.build / "rf_bootstrap_inputs.json").read_text()).get("rx_iddr_alignment_trial"),
                     "inputs_sha256": sha256(receipt_path), "artifacts_sha256": {
                         name: sha256(args.build / name)
                         for name in ("zedboard_rf.bit", "zedboard_rf.xsa", "rf_signoff_route.dcp")}}
            seal.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    print("RF_BUILD_PROVENANCE", args.mode, "PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
