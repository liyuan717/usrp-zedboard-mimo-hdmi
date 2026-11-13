#!/usr/bin/env python3
"""Verify and materialize the saved R22 inputs for Vivado 2025.2."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import zlib

ROOT = Path(__file__).resolve().parents[1]
INPUTS = ROOT / "fpga/r22"


def verify():
    manifest = json.loads((INPUTS / "provenance.json").read_text())
    for name, record in manifest["inputs"].items():
        data = (INPUTS / name).read_bytes()
        if hashlib.sha256(data).hexdigest() != record["sha256"]:
            raise ValueError(f"FPGA input hash mismatch: {name}")

    artifacts = ROOT / "artifacts"
    identity = json.loads((artifacts / "r22-identities.json").read_text())["bitstream"]
    bitstream = (artifacts / identity["path"]).read_bytes()
    if (len(bitstream) != identity["bytes"]
            or hashlib.sha256(bitstream).hexdigest() != identity["sha256"]
            or f"{zlib.crc32(bitstream):08x}" != identity["crc32"]):
        raise ValueError("Archived R22 bitstream identity mismatch")

    evidence = ROOT / "docs/evidence"
    receipts = json.loads((evidence / "provenance.json").read_text())
    for name, record in receipts.items():
        if hashlib.sha256((evidence / name).read_bytes()).hexdigest() != record["published_sha256"]:
            raise ValueError(f"Receipt hash mismatch: {name}")
    overlay = artifacts / "sd-offline-source-overlay"
    for profile in ("raw-ram", "sd-offline"):
        decoder = json.loads((artifacts / f"{profile}-decoder-identity.json").read_text())
        for name, expected in decoder["source_sha256"].items():
            path = overlay / name
            if profile != "sd-offline" or not path.is_file():
                path = ROOT / name
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise ValueError(f"{profile} decoder source hash mismatch: {name}")

    print(f"Verified {len(manifest['inputs'])} R22 inputs, {len(receipts)} receipts, "
          "both decoder source snapshots and the bitstream.")
    return manifest["inputs"]


def prepare(output, inputs):
    output = output.resolve()
    if not re.fullmatch(r"[A-Za-z0-9_./ +\-]+", str(output)):
        raise ValueError("Choose an output path with letters, numbers, spaces or _ . + -")
    output.mkdir(parents=True)
    replacements = {
        b"@R22_ROOT@": bytes(output),
        b"@R22_PROJECT@": bytes(output / "project"),
        b"@R22_ROUTE@": bytes(output / "route"),
    }
    for name in inputs:
        data = (INPUTS / name).read_bytes()
        for marker, value in replacements.items():
            data = data.replace(marker, value)
        target = output / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    (output / "route").mkdir()
    print(f"Materialized R22 build inputs in {output}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="new directory for materialized Vivado inputs")
    arguments = parser.parse_args()
    inputs = verify()
    if arguments.output:
        prepare(arguments.output, inputs)
