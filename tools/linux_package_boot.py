#!/usr/bin/env python3
"""Create BOOT.BIN from explicitly supplied ZedBoard artifacts; never flash media."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import struct
import subprocess
import zipfile
from pathlib import Path


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def require_arm32_elf(path: Path) -> None:
    header = path.read_bytes()[:24]
    if len(header) < 24 or header[:6] != b"\x7fELF\x01\x01" or struct.unpack_from("<H", header, 18)[0] != 40:
        raise ValueError(f"Expected an ELF32 little-endian ARM artifact: {path.name}")


def bit_identity_and_payload(data: bytes) -> tuple[bytes, bytes, bytes]:
    """Parse a Vivado .bit; only date/time fields may differ on XSA export.

    Do not compare an arbitrary suffix after a hard-coded header offset. Design
    name/UserID/version/CRC, part and every byte of configuration must match.
    """
    preamble = b"\x00\x09\x0f\xf0\x0f\xf0\x0f\xf0\x0f\xf0\x00\x00\x01"
    if not data.startswith(preamble):
        raise ValueError("Invalid Vivado bitstream header")
    offset, fields = len(preamble), {}
    for tag in b"abcd":
        if data[offset:offset + 1] != bytes([tag]) or len(data) < offset + 3:
            raise ValueError("Malformed Vivado bitstream identity field")
        length = struct.unpack_from(">H", data, offset + 1)[0]
        offset += 3
        value = data[offset:offset + length]
        if length < 2 or len(value) != length or value[-1:] != b"\0":
            raise ValueError("Truncated Vivado bitstream identity field")
        fields[tag] = value
        offset += length
    if (not re.fullmatch(rb"\d{4}/\d{2}/\d{2}\x00", fields[ord("c")])
            or not re.fullmatch(rb"\d{2}:\d{2}:\d{2}\x00", fields[ord("d")])):
        raise ValueError("Malformed Vivado bitstream timestamp")
    if data[offset:offset + 1] != b"e" or len(data) < offset + 5:
        raise ValueError("Missing Vivado configuration payload")
    length = struct.unpack_from(">I", data, offset + 1)[0]
    payload = data[offset + 5:]
    if not length or len(payload) != length or b"\xaa\x99\x55\x66" not in payload:
        raise ValueError("Truncated/invalid Vivado configuration payload")
    return fields[ord("a")], fields[ord("b")], payload


def verify_boot_inputs(fsbl: Path, bit: Path, uboot: Path, xsa: Path,
                       fsbl_manifest: Path, rf_release: Path) -> dict:
    """Refuse stale FSBL/PL combinations or unqualified implementation files."""
    require_arm32_elf(fsbl)
    require_arm32_elf(uboot)
    fsbl_record = json.loads(fsbl_manifest.read_text(encoding="utf-8"))
    release = json.loads(rf_release.read_text(encoding="utf-8"))
    xsa_hash, bit_hash = sha256(xsa), sha256(bit)
    if fsbl_record.get("processor") != "ps7_cortexa9_0":
        raise ValueError("FSBL processor mismatch")
    if fsbl_record.get("xsa_sha256") != xsa_hash or fsbl_record.get("fsbl_sha256") != sha256(fsbl):
        raise ValueError("FSBL/XSA fingerprints differ from the actual compiled FSBL receipt")
    if release.get("offline_candidate_pass") is not True:
        raise ValueError("RF implementation is not an offline-qualified candidate")
    if release.get("xsa_sha256") != xsa_hash or release.get("bit_sha256") != bit_hash:
        raise ValueError("RF release manifest does not match supplied bit/XSA")
    approved_profile = {
        "interface_profile": "lvds_2r2t_2v5",
        "vadj_mv": 2500,
        "rx_channels": 2,
        "tx_channels": 2,
        "sample_rate_hz": 4_000_000,
        "interface_clock_hz": 16_000_000,
        "sample_clock_divider": 4,
        "spi_runtime_mode": 1,
        "spi_clock_hz": 3_125_000,
        "rf_rx_transport": "elastic_axis",
        "rf_rx_dma_source_type": 1,
        "rf_rx_elastic_depth_words": 1024,
        "rf_rx_flush_mode": "dmac_ctrl_enable_monitor",
    }
    for field, expected in approved_profile.items():
        value = release.get(field)
        if type(value) is not type(expected) or value != expected:
            raise ValueError(f"Boot profile mismatch: {field} must be {expected!r}")
    with zipfile.ZipFile(xsa) as archive:
        platform = json.loads(archive.read("xsa.json"))
        devices = platform.get("devices", [])
        if len(devices) != 1 or devices[0].get("part", {}).get("name") != "xc7z020clg484-1":
            raise ValueError("XSA must target the selected xc7z020clg484-1 carrier")
        embedded = [name for name in archive.namelist() if name.lower().endswith(".bit")]
        if len(embedded) != 1:
            raise ValueError("XSA must contain exactly one matching deployment bitstream")
        embedded_bytes = archive.read(embedded[0])
        if (hashlib.sha256(embedded_bytes).hexdigest() != bit_hash
                and bit_identity_and_payload(embedded_bytes) != bit_identity_and_payload(bit.read_bytes())):
            raise ValueError("XSA contains a different deployment bitstream identity/configuration")
    return release


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fsbl", type=Path, required=True)
    parser.add_argument("--bit", type=Path, required=True)
    parser.add_argument("--u-boot", type=Path, required=True)
    parser.add_argument("--xsa", type=Path, required=True)
    parser.add_argument("--fsbl-manifest", type=Path, required=True)
    parser.add_argument("--rf-release", type=Path, required=True)
    parser.add_argument("--bootgen", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    for path in (args.fsbl, args.bit, args.u_boot, args.xsa, args.fsbl_manifest, args.rf_release, args.bootgen):
        if not path.is_file():
            raise SystemExit(f"required file is absent: {path}")
    if args.output.exists():
        raise SystemExit("refusing to overwrite BOOT.BIN output")
    release = verify_boot_inputs(args.fsbl, args.bit, args.u_boot, args.xsa,
                                 args.fsbl_manifest, args.rf_release)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    bif = args.output.with_suffix(".bif")
    bif.write_text(
        "the_ROM_image:\n{\n"
        f'  [bootloader] "{args.fsbl.resolve().as_posix()}"\n'
        f'  "{args.bit.resolve().as_posix()}"\n'
        f'  "{args.u_boot.resolve().as_posix()}"\n'
        "}\n", encoding="utf-8"
    )
    subprocess.run([str(args.bootgen), "-arch", "zynq", "-image", str(bif), "-o", str(args.output), "-w"], check=True)
    manifest = {"schema_version": 1, "architecture": "zynq-7000", "sd_written": False,
                "xsa_sha256": sha256(args.xsa), "rf_release": release,
                "hardware_tested": False,
                "inputs_sha256": {p.name: sha256(p) for p in (args.fsbl, args.bit, args.u_boot)},
                "boot_bin_sha256": sha256(args.output)}
    with zipfile.ZipFile(args.xsa) as archive:
        embedded = next(name for name in archive.namelist() if name.lower().endswith(".bit"))
        manifest["xsa_embedded_bit_sha256"] = hashlib.sha256(archive.read(embedded)).hexdigest()
    manifest["bit_configuration_sha256"] = hashlib.sha256(bit_identity_and_payload(args.bit.read_bytes())[2]).hexdigest()
    manifest["bit_equivalence_basis"] = "full identity/configuration equality; only parsed date/time may differ"
    args.output.with_suffix(".json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
