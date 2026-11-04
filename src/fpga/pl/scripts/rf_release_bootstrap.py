"""Validate the recipe freeze written before Vivado starts."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

REQUIRED = {"pl/scripts/launch_zedboard_rf_release.py", "pl/scripts/create_zedboard_rf_system.tcl",
            "pl/scripts/create_zedboard_rf_buffered_lvds_system.tcl", "pl/scripts/add_detector_module.tcl",
            "pl/scripts/rf_signoff_checks.tcl", "pl/scripts/rf_build_provenance.py",
            "pl/scripts/capture_zedboard_rf_inputs.tcl", "pl/scripts/rf_release_bootstrap.py"}


def verify_bootstrap(build: Path, repo: Path) -> dict[str, str]:
    path = build / "rf_bootstrap_inputs.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    rows = value.get("source_sha256")
    if (value.get("status") != "RECIPES_FROZEN_BEFORE_VIVADO" or value.get("hardware_tested") is not False
            or value.get("part") != "xc7z020clg484-1" or not isinstance(rows, dict)
            or not REQUIRED.issubset(rows)):
        raise ValueError("Formal release requires the pre-Vivado bootstrap and fixed target part")
    for name, expected in rows.items():
        source = repo / name
        if (source.is_symlink() or not source.resolve().is_relative_to(repo.resolve())
                or not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest() != expected):
            raise ValueError(f"Recipe/RTL changed after bootstrap, before or during Vivado: {name}")
    return rows
