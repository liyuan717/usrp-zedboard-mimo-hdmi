"""Bind one frozen HDMI/HP1 source closure to one offline bitstream and XSA.

This is intentionally independent of the RF-release provenance contract: the
HDMI derivative has a different wrapper, BD mutation, and artifact names.
It never backfills a receipt and never accepts a source that changed after the
pre-synthesis capture.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP = "hdmi_pipeline_bootstrap.json"
INPUTS = "hdmi_pipeline_inputs.json"
SEAL = "hdmi_pipeline_provenance.json"
ARTIFACTS = ("hdmi_pipeline.bit", "hdmi_pipeline.xsa", "hdmi_pipeline_signoff_route.dcp")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_bootstrap(build: Path) -> dict[str, str]:
    value = json.loads((build / BOOTSTRAP).read_text(encoding="utf-8"))
    rows = value.get("source_sha256")
    if value.get("status") != "FROZEN_BEFORE_VIVADO" or value.get("hardware_tested") is not False or not isinstance(rows, dict):
        raise ValueError("missing HDMI pre-Vivado source freeze")
    for name, expected in rows.items():
        path = ROOT / name
        if path.is_symlink() or not path.is_file() or sha256(path) != expected:
            raise ValueError(f"HDMI source changed after bootstrap: {name}")
    return rows


def verify_inputs(build: Path) -> list[dict[str, str]]:
    rows = json.loads((build / INPUTS).read_text(encoding="utf-8"))
    if rows.get("status") != "CAPTURED_BEFORE_SYNTHESIS" or rows.get("hardware_tested") is not False:
        raise ValueError("missing genuine HDMI pre-synthesis input receipt")
    files = rows.get("files")
    if not isinstance(files, list) or not files:
        raise ValueError("empty HDMI actual Vivado input closure")
    for row in files:
        if row.get("role") not in {"synthesis", "constraint", "design", "recipe"}:
            raise ValueError("invalid HDMI input role")
        path = Path(row["path"])
        if path.is_symlink() or not path.is_file() or sha256(path) != row.get("sha256"):
            raise ValueError(f"HDMI input changed after capture: {path}")
    return files


def capture(build: Path, input_list: Path) -> None:
    if (build / INPUTS).exists() or any((build / name).exists() for name in ARTIFACTS):
        raise ValueError("refusing to backfill HDMI provenance into an existing build")
    frozen = verify_bootstrap(build)
    rows: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for line in input_list.read_text(encoding="utf-8").splitlines():
        role, raw = line.split("\t", 1)
        path = Path(raw).resolve()
        key = (role, str(path))
        if key in seen:
            continue
        seen.add(key)
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"unsafe or missing actual Vivado input: {path}")
        rows.append({"role": role, "path": path.as_posix(), "sha256": sha256(path)})
    captured = {Path(row["path"]).resolve() for row in rows}
    for name in frozen:
        path = (ROOT / name).resolve()
        if path not in captured:
            rows.append({"role": "recipe", "path": path.as_posix(), "sha256": sha256(path)})
    value = {"status": "CAPTURED_BEFORE_SYNTHESIS", "hardware_tested": False,
             "files": rows, "bootstrap_sha256": sha256(build / BOOTSTRAP)}
    (build / INPUTS).write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    verify_inputs(build)


def seal(build: Path) -> None:
    verify_bootstrap(build)
    verify_inputs(build)
    if (build / SEAL).exists():
        raise ValueError("refusing to replace HDMI provenance seal")
    for name in ARTIFACTS:
        if not (build / name).is_file():
            raise ValueError(f"missing release artifact: {name}")
    value = {"status": "PRE_SYNTHESIS_INPUTS_UNCHANGED_BIT_XSA_BOUND",
             "hardware_tested": False,
             "inputs_sha256": sha256(build / INPUTS),
             "artifacts_sha256": {name: sha256(build / name) for name in ARTIFACTS}}
    (build / SEAL).write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("capture", "verify", "seal"))
    parser.add_argument("--build", required=True, type=Path)
    parser.add_argument("--input-list", type=Path)
    args = parser.parse_args()
    build = args.build.resolve()
    if not build.is_relative_to(ROOT / "build"):
        raise ValueError("build must be below repository build/")
    if args.mode == "capture":
        if args.input_list is None:
            raise ValueError("capture requires actual Vivado --input-list")
        capture(build, args.input_list)
    elif args.mode == "verify":
        verify_bootstrap(build)
        verify_inputs(build)
    else:
        seal(build)
    print(f"HDMI_PIPELINE_PROVENANCE_{args.mode.upper()}_PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
