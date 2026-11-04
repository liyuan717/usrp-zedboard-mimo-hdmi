"""Create a non-destructive production/simulation ADI HDL tree for LVDS builds.

F01 is the DMAC descriptor-length gate; F03 is the fixed four-channel cpack
identity path.  Both reviewed changes are applied to a new copy, byte-hashed,
and recorded in a manifest.  Vivado packaging and XSim must consume this same
derived directory; the immutable vendor tree is never edited.
"""
from __future__ import annotations
import argparse, hashlib, json, shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
F01 = Path("library/axi_dmac/address_generator.v")
F03 = Path("library/util_pack/util_cpack2/util_cpack2_impl.v")
F05 = Path("library/axi_dmac/axi_dmac_ip.tcl")
EXPECTED = {
    F01.as_posix(): "b3e22c135249e0d6aba862f72fdb15d15208d636b59bfd34082b4d67ad662295",
    F03.as_posix(): "b498c6bd4f9575fbdca35855ed03d7ceb65ebc59d71dc28628a2d478100d78ef",
}

def digest(path: Path) -> str: return hashlib.sha256(path.read_bytes()).hexdigest()
def replace_once(text: str, old: str, new: str, name: str) -> str:
    if text.count(old) != 1: raise ValueError(f"{name}: reviewed patch context changed")
    return text.replace(old, new)
def write_lf(path: Path, text: str) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle: handle.write(text)

def main() -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("--source", type=Path, required=True); parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(); source, output = args.source.resolve(), args.output.resolve()
    if output.exists(): raise ValueError(f"refusing to overwrite derived vendor tree: {output}")
    if not (source / "library").is_dir() or not (source / ".github").is_dir(): raise ValueError("source is not an ADI HDL tree")
    manifest_path = source.parent / "source-manifest.json"
    if not manifest_path.is_file(): raise ValueError("source-manifest.json beside source is required")
    original_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    shutil.copytree(source, output)
    patches = {}
    try:
        target = output / F01; before = digest(target)
        text = replace_once(target.read_text(encoding="utf-8"),
            "      end else if (addr_valid == 1'b0 && eot == 1'b1) begin\n",
            "      end else if (req_ready == 1'b0 && addr_valid == 1'b0 && eot == 1'b1) begin\n"
            "        // Match the addr_valid state machine: while waiting for a descriptor,\n"
            "        // keep the prefetched final length. Otherwise another BL handshake\n"
            "        // can overwrite it before this descriptor's address is issued.\n", "F01")
        write_lf(target, text); after = digest(target)
        if after != EXPECTED[F01.as_posix()]: raise ValueError(f"F01 hash mismatch: {after}")
        patches[F01.as_posix()] = {"before_sha256": before, "after_sha256": after, "patch_sha256": digest(ROOT / "pl/patches/adi/address_generator_descriptor_gate.patch")}
        target = output / F03; before = digest(target)
        text = target.read_text(encoding="utf-8")
        anchor = "    .out_valid (out_valid));\n\n"
        addition = """    .out_valid (out_valid));

  // Project-local fast path ONLY for fixed full four-channel FIFO capture.
  // Identity packing has no rotation/partial vector; delayed ready otherwise
  // drops the first word after each valid gap. Other configurations unchanged.
  wire direct_four_fifo = NUM_OF_CHANNELS == 4 && SAMPLES_PER_CHANNEL == 1 &&
    INTERFACE_TYPE == 1 && PARALLEL_OR_SERIAL_N == 0 &&
    enable == {NUM_OF_CHANNELS{1'b1}};

"""
        text = replace_once(text, anchor, addition, "F03-anchor")
        text = replace_once(text,
            "    end else if (ready == 1'b1 && data_wr_en == 1'b1) begin\n"
            "      out_valid_int <= 1'b1;\n"
            "      packed_sync <= out_sync;\n",
            "    end else if (direct_four_fifo) begin\n"
            "      out_valid_int <= data_wr_en;\n"
            "      packed_sync <= data_wr_en;\n"
            "    end else if (ready == 1'b1 && data_wr_en == 1'b1) begin\n"
            "      out_valid_int <= 1'b1;\n"
            "      packed_sync <= out_sync;\n", "F03-valid")
        text = replace_once(text,
            "    if (data_wr_en == 1'b1) begin\n      for (i = 0; i < NUM_OF_CHANNELS * SAMPLES_PER_CHANNEL; i = i + 1) begin",
            "    if (direct_four_fifo && data_wr_en == 1'b1) begin\n      out_data_int <= interleaved_data;\n    end else if (data_wr_en == 1'b1) begin\n      for (i = 0; i < NUM_OF_CHANNELS * SAMPLES_PER_CHANNEL; i = i + 1) begin", "F03-data")
        write_lf(target, text); after = digest(target)
        if after != EXPECTED[F03.as_posix()]: raise ValueError(f"F03 hash mismatch: {after}")
        patches[F03.as_posix()] = {"before_sha256": before, "after_sha256": after, "patch_sha256": digest(ROOT / "pl/patches/adi/cpack4_fifo_burst_identity.patch")}
        target = output / F05
        before = digest(target)
        text = target.read_text(encoding="utf-8")
        for bus in ("s_axis", "m_axis"):
            text = replace_once(text, f'adi_add_bus_clock "{bus}_aclk" "{bus}"\n',
                "# F05: Vivado 2025.2 already infers the physical clock interface.\n"
                "# Associate that canonical interface; do not add a duplicate\n"
                "# *_signal_clock whose empty CLK_DOMAIN prevents BD reopening.\n"
                f'set canonical_clock [ipx::get_bus_interfaces {bus}_aclk -of_objects [ipx::current_core]]\n'
                'if {[llength $canonical_clock] != 1} {error "Missing canonical AXIS clock interface"}\n'
                'set canonical_assoc [ipx::add_bus_parameter ASSOCIATED_BUSIF $canonical_clock]\n'
                f'set_property value {bus} $canonical_assoc\n', "F05-" + bus)
        write_lf(target, text)
        patches[F05.as_posix()] = {"before_sha256": before, "after_sha256": digest(target),
                                  "kind": "packaging_metadata_only_no_hdl_change",
                                  "patch_sha256": digest(ROOT / "pl/patches/adi/dmac_axis_clock_metadata.patch")}
    except Exception:
        shutil.rmtree(output); raise
    files = {entry["path"]: entry["sha256"] for entry in original_manifest.get("files", [])}
    for rel in (F01, F03, F05):
        files[rel.as_posix()] = digest(output / rel)
    (output.parent / "source-manifest.json").write_text(json.dumps({"source_commit": original_manifest.get("source_commit"), "derived_for": "lvds_2r2t_2v5", "production_and_simulation_same_tree": True, "patches": patches, "files": [{"path": key, "sha256": value} for key, value in sorted(files.items())]}, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"derived_tree": str(output), "file_count": len(files),
                      "patched_source_sha256": {rel.as_posix(): files[rel.as_posix()] for rel in (F01, F03, F05)}},
                     sort_keys=True))
    return 0
if __name__ == "__main__": raise SystemExit(main())
