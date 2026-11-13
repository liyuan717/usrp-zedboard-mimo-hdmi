#!/usr/bin/env python3
"""Prepare a receiver FAT32 update on a copy of an existing SD image file."""
import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from stage_g0_receive_sd_image import FILES, SECTOR, identity
from workspace_storage_guard import GIB, check


def fat_extent(image, size):
    subprocess.run(["sfdisk", "--verify", image], check=True, capture_output=True)
    table = json.loads(subprocess.check_output(["sfdisk", "--json", image]))["partitiontable"]
    if table["label"] != "dos" or table["sectorsize"] != SECTOR:
        raise ValueError("Expected a DOS partition table with 512-byte sectors")
    boot, = (p for p in table["partitions"] if p.get("bootable"))
    start = boot["start"] * SECTOR
    end = start + boot["size"] * SECTOR
    if boot["type"].lower() not in ("b", "c") or not SECTOR <= start < end <= size:
        raise ValueError("Expected one active FAT32 boot partition inside the backup image")
    return start, end


def outside_partition_digest(image, start, end, size):
    digest = sha256()
    with image.open("rb") as stream:
        for offset, length in ((0, start), (end, size - end)):
            stream.seek(offset)
            while length:
                data = stream.read(min(length, 1024 * 1024))
                if not data:
                    raise ValueError("SD image ended before the declared extent")
                digest.update(data)
                length -= len(data)
    return digest.hexdigest()


def stage(args):
    source, bundle, out = args.backup_image, args.bundle, args.output
    backup = identity(source)
    inventory = {name: identity(bundle / name) for name in FILES}
    receipt = json.loads((bundle / "RECEIVER_BOOT.json").read_text())
    if receipt["files"] != inventory or receipt["SD_written"]:
        raise ValueError("Boot files differ from the offline bundle receipt")
    start, end = fat_extent(source, backup["bytes"])
    before = check("before-receiver-sd-update-file")
    if before["disk"]["available_bytes"] < backup["bytes"] + 5 * GIB:
        raise RuntimeError("Insufficient free space for a full copy and the disk reserve")
    outside = outside_partition_digest(source, start, end, backup["bytes"])
    out.mkdir(parents=True, exist_ok=False)
    mcopy = str(args.mcopy.absolute())
    env = {**os.environ, "MTOOLSRC": "/dev/null"}
    try:
        with tempfile.TemporaryDirectory(dir=out) as scratch_dir:
            scratch = Path(scratch_dir)
            candidate = scratch / "updated.img"
            shutil.copyfile(source, candidate)
            fat = f"{candidate}@@{start}"
            with (out / "mcopy.log").open("x") as log:
                for name in FILES:
                    subprocess.run([mcopy, "-o", "-i", fat, bundle / name, "::/" + name],
                                   check=True, env=env, stdout=log, stderr=subprocess.STDOUT)
                for name, expected in inventory.items():
                    readback = scratch / name
                    subprocess.run([mcopy, "-i", fat, "::/" + name, readback],
                                   check=True, env=env, stdout=log, stderr=subprocess.STDOUT)
                    if identity(readback) != expected:
                        raise ValueError("Boot file differs after update: " + name)
            if identity(source) != backup:
                raise ValueError("Backup image changed during staging")
            updated = identity(candidate)
            if updated["bytes"] != backup["bytes"] or outside_partition_digest(
                    candidate, start, end, updated["bytes"]) != outside:
                raise ValueError("Bytes outside the boot partition changed")
            candidate.rename(out / "updated.img")
    finally:
        (out / "disk-after.json").write_text(json.dumps(check("after-receiver-sd-update-file"), indent=2) + "\n")
    result = {
        "scope": __doc__, "backup_image": str(source.absolute()), "backup_identity": backup,
        "updated_image": updated, "boot_partition_start_bytes": start, "boot_partition_end_bytes": end,
        "outside_partition_sha256": outside, "files_read_back": inventory,
        "disk_before": before, "physical_SD_written": False, "hardware_cold_boot_passed": False,
    }
    (out / "SD_UPDATE.json").write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("backup-image", "bundle", "mcopy", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    stage(parser.parse_args())
