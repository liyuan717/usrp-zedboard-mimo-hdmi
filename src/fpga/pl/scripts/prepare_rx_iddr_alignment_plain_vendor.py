"""Apply only the hardware-proven RX IDDR alignment to the plain LVDS vendor tree."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil


COMMIT = "4366bd63c1fe56b680b69145981a4493c6f8753a"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepare(source, output):
    if output.exists():
        raise ValueError("choose a fresh plain-IDDR vendor directory")
    manifest = json.loads((source.parent / "source-manifest.json").read_text())
    if (
        manifest.get("source_commit") != COMMIT
        or manifest.get("derived_for") != "lvds_2r2t_2v5"
        or manifest.get("diagnostic_only") is True
        or manifest.get("rx_observer_abi") not in (None, 0)
        or manifest.get("rx_iddr_alignment_trial")
    ):
        raise ValueError("requires the plain baseline LVDS tree")
    for row in manifest["files"]:
        path = source / row["path"]
        if (
            path.is_symlink()
            or not path.resolve().is_relative_to(source.resolve())
            or sha(path) != row["sha256"]
        ):
            raise ValueError("baseline manifest mismatch: " + row["path"])
    rel = "library/axi_ad9361/xilinx/axi_ad9361_lvds_if.v"
    text = (source / rel).read_text(encoding="utf-8")
    anchor = "  ad_data_in #(\n    .FPGA_TECHNOLOGY (FPGA_TECHNOLOGY),"
    if text.count(anchor) != 2 or ".IDDR_CLK_EDGE" in text:
        raise ValueError("unexpected RX frame/data instance context")
    patched = text.replace(
        anchor,
        '  ad_data_in #(\n    .IDDR_CLK_EDGE ("SAME_EDGE_PIPELINED"),\n    .FPGA_TECHNOLOGY (FPGA_TECHNOLOGY),',
    )
    shutil.copytree(source, output)
    (output / rel).write_text(patched, encoding="utf-8", newline="\n")
    files = {row["path"]: row["sha256"] for row in manifest["files"]}
    before = files[rel]
    files[rel] = sha(output / rel)
    manifest.update(
        source_root=str(output.resolve()),
        rx_iddr_alignment_trial="same_edge_pipelined",
        plain_iddr_alignment_preparation_sha256=sha(Path(__file__)),
        alignment_patch={
            "path": rel,
            "before_sha256": before,
            "after_sha256": files[rel],
            "scope": "RX six data IDDRs and RX frame IDDR only; no observer, TX or common-default change",
        },
        files=[{"path": path, "sha256": value} for path, value in sorted(files.items())],
    )
    (output.parent / "source-manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = prepare(args.source.resolve(), args.output.resolve())
    print(json.dumps(result["alignment_patch"]))


if __name__ == "__main__":
    main()
