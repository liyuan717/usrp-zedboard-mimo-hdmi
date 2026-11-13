#!/usr/bin/env python3
"""Create a fresh, auditable RX-only ADI HDL overlay.

This tool never edits its reviewed source tree.  It copies the complete ADI
tree required by the current Vivado builder (including scripts, library IP
packaging, component XML, constraints and shared sources) into a target that
must not already exist.  It then removes exactly one ``axi_ad9361_tx i_tx``
instance from the copied core, adds the RX-only TX register stub to both the
ADI packaging Tcl and component.xml file lists, and writes a SHA-256 receipt.

The replacement is valid only for the receiver-v2 LVDS 2R2T configuration.
It deliberately is not a generic AD9361 IP rewrite and does not provide a
transmit waveform producer.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


OVERLAY_NAME = "receiver-v2-rxonly-adi"
STUB_NAME = "rfv2_tx_register_stub.v"
RETIRED_TX_SOURCES = ("axi_ad9361_tx_channel.v", "axi_ad9361_tx.v")
AXI_REL = Path("library/axi_ad9361/axi_ad9361.v")
IP_TCL_REL = Path("library/axi_ad9361/axi_ad9361_ip.tcl")
COMPONENT_REL = Path("library/axi_ad9361/component.xml")
LVDS_REL = Path("library/axi_ad9361/xilinx/axi_ad9361_lvds_if.v")
SOURCE_MANIFEST_PATH = Path(__file__).with_name("adi_rxonly_source_manifest.json")
OVERLAY_HASH_RELATIVE = (
    AXI_REL,
    IP_TCL_REL,
    COMPONENT_REL,
    LVDS_REL,
    Path("library/axi_ad9361") / STUB_NAME,
)
REQUIRED_ROOT_RELATIVE = (
    Path("scripts/adi_env.tcl"),
    Path("library/scripts/adi_ip_xilinx.tcl"),
    Path("library/common/up_axi.v"),
    Path("library/common/up_dac_common.v"),
    Path("library/common/up_dac_channel.v"),
    Path("library/common/up_delay_cntrl.v"),
    Path("library/axi_ad9361/axi_ad9361_rx.v"),
    LVDS_REL,
    Path("library/axi_ad9361/axi_ad9361_ip.tcl"),
    AXI_REL,
    COMPONENT_REL,
)

# This is deliberately a reviewed-overlay tool, not a best-effort patcher for
# arbitrary ADI revisions.  These hashes lock the top-level replacement,
# register-decode semantics and generated package shape that were inspected.
# A future vendor tree with coincidentally named ports must be reviewed and
# explicitly re-pinned before this tool is allowed to edit a fresh copy.
PINNED_SOURCE_SHA256 = {
    "library/axi_ad9361/axi_ad9361.v": "85490d1cae80ca3177d13f8be6b49562cbe97bb9687073af5237a1da4e46e39e",
    "library/axi_ad9361/axi_ad9361_ip.tcl": "ea41f4245f9cc9b1e2fe2e5d7ccbba4388b112719cb84176b40a907cb44b00dc",
    "library/axi_ad9361/component.xml": "cb851140c8c3cef3e622956ead1778de75be77d12a88e0081bd9d326e2385e62",
    "library/axi_ad9361/axi_ad9361_tx.v": "2f596f29effc284273c3d2f498e32d7725155f63d1e249211873ea224b39cc88",
    "library/axi_ad9361/axi_ad9361_rx.v": "a46b35b4a68105f532b1ae7c0a471ba5a211d52c00ca3af56eded37deb7e60d7",
    "library/common/up_dac_common.v": "9186112d2652c4868f6e3efd7b9341cbefc8eec7a16d813d8725a9ce57322cda",
    "library/common/up_dac_channel.v": "b3f6a2eb7e5b5fa254db4503484df1e0b3a1313e5545191209746d33e518f0d5",
    "library/common/up_delay_cntrl.v": "00c94dd4a76d6b1bbecffb5cdbc4f030fb57765c24c3abe3f310f0c94015d666",
    "library/axi_ad9361/xilinx/axi_ad9361_lvds_if.v": "6cba0b965c9189d1be7fb8eaf2e8d57dc431d689a56b597f80ba849afbba3388",
}

# The exact source delimiters deliberately fail closed if the reviewed ADI
# variant changes.  A regex replacement would risk removing an unintended
# generate block in a future vendor revision.
TX_BLOCK_START = "  // transmit\n\n  axi_ad9361_tx #("
TX_BLOCK_END = "\n\n  // axi interface"
LVDS_TX_BLOCK_START = "  // dac-tx interface\n"
LVDS_TX_BLOCK_END = "\n  // transfer to local clock"

RXONLY_INSTANCE = r'''  // RX-only overlay: the ADI core has no native RX-only mode.  Keep the
  // LVDS device interface, RX/tuning path, EN/TXNRX, forwarded clock shell and
  // disabled TDD path intact; replace only the former TX datapath/control.
  // adc_data_sel != 0 now observes zeros, while data_sel == 0 and the RX PN
  // observer retain their original receive behavior.
  rfv2_tx_register_stub i_tx_rxonly (
    .dac_valid (dac_valid_s),
    .dac_data (dac_data_s),
    .dac_clksel (dac_clksel_s),
    .dac_r1_mode (dac_r1_mode),
    .up_dld (up_dac_dld_s),
    .up_dwdata (up_dac_dwdata_s),
    .dac_sync_out (dac_sync_out),
    .dac_enable_i0 (dac_enable_i0),
    .dac_valid_i0 (dac_valid_i0_s),
    .dac_enable_q0 (dac_enable_q0),
    .dac_valid_q0 (dac_valid_q0_s),
    .dac_enable_i1 (dac_enable_i1),
    .dac_valid_i1 (dac_valid_i1_s),
    .dac_enable_q1 (dac_enable_q1),
    .dac_valid_q1 (dac_valid_q1_s),
    .up_pps_irq_mask (dac_up_pps_irq_mask_s),
    .up_dac_gpio_out (up_dac_gpio_out),
    .up_rstn (up_rstn),
    .up_clk (up_clk),
    .up_wreq (up_wreq_s),
    .up_waddr (up_waddr_s),
    .up_wdata (up_wdata_s),
    .up_wack (up_wack_tx_s),
    .up_rreq (up_rreq_s),
    .up_raddr (up_raddr_s),
    .up_rdata (up_rdata_tx_s),
    .up_rack (up_rack_tx_s));

  // Do not let GUI/default parameter changes turn this overlay into a
  // misleading partial transmitter.  The undefined module is elaborated only
  // for unsupported configurations and intentionally causes synthesis to fail.
  generate if ((CMOS_OR_LVDS_N != 0) || (MODE_1R1T != 0) ||
               (DAC_DATAPATH_DISABLE != 1) || (DAC_DDS_DISABLE != 1) ||
               (DAC_USERPORTS_DISABLE != 1) ||
               (DAC_IQCORRECTION_DISABLE != 1) ||
               (DAC_IODELAY_ENABLE != 0) || (TDD_DISABLE != 1) ||
               (USE_SSI_CLK != 0)) begin : g_rfv2_rxonly_configuration_error
    RX_ONLY_ADI_OVERLAY_CONFIGURATION_MISMATCH must_not_elaborate();
  end endgenerate'''


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _freeze_control_inputs(stub: Path) -> dict[str, str]:
    """Hash the local patch controls before any copy/patch side effect."""
    return {
        "preparer": sha256(Path(__file__)),
        "manifest": sha256(SOURCE_MANIFEST_PATH),
        "stub": sha256(stub),
    }


def _assert_control_inputs_unchanged(expected: dict[str, str], stub: Path) -> None:
    observed = _freeze_control_inputs(stub)
    if observed != expected:
        changed = {
            key: {"before": expected.get(key), "after": observed.get(key)}
            for key in sorted(set(expected) | set(observed))
            if expected.get(key) != observed.get(key)
        }
        raise ValueError("RX-only overlay control input changed during preparation: " +
                         json.dumps(changed, sort_keys=True))


def _assert_stub_triplet(expected_hash: str, stub: Path, destination_stub: Path) -> None:
    """Require initial source, current source and copied destination to match."""
    observed = {
        "source_before": expected_hash,
        "source_after": sha256(stub),
        "copied_destination": sha256(destination_stub),
    }
    if len(set(observed.values())) != 1:
        raise ValueError("RX-only stub changed during copy or copy hash differs: " +
                         json.dumps(observed, sort_keys=True))


def _copy_stub_frozen(stub: Path, destination_stub: Path, expected_hash: str) -> None:
    """Copy a checked-in stub only if source-before/source-after/destination agree."""
    if sha256(stub) != expected_hash:
        raise ValueError("RX-only stub changed before copy")
    shutil.copy2(stub, destination_stub)
    _assert_stub_triplet(expected_hash, stub, destination_stub)


def _is_reparse(path: Path) -> bool:
    """Reject symlinks and Windows junction/reparse points before copy/hash."""
    metadata = os.lstat(path)
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


def _reject_reparse_tree(root: Path) -> None:
    if _is_reparse(root):
        raise ValueError(f"reviewed ADI source root is a link/reparse point: {root}")
    for path in root.rglob("*"):
        if _is_reparse(path):
            raise ValueError(f"reviewed ADI source contains a link/reparse point: {path}")


def _load_source_manifest() -> dict[str, object]:
    manifest = json.loads(SOURCE_MANIFEST_PATH.read_text(encoding="utf-8"))
    if manifest.get("schema") != 1:
        raise ValueError("unsupported RX-only ADI source-manifest schema")
    expected_overlay = manifest.get("expected_overlay_file_sha256")
    expected_paths = {path.as_posix() for path in OVERLAY_HASH_RELATIVE}
    if not isinstance(expected_overlay, dict) or set(expected_overlay) != expected_paths:
        raise ValueError("RX-only ADI manifest has incomplete expected overlay hashes")
    if any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
           for value in expected_overlay.values()):
        raise ValueError("RX-only ADI manifest contains malformed expected overlay hash")
    for count_key, digest_key in (
        ("source_tree_file_count", "source_tree_sha256"),
        ("expected_output_tree_file_count", "expected_output_tree_sha256"),
    ):
        if not isinstance(manifest.get(count_key), int) or manifest[count_key] < 1:
            raise ValueError(f"RX-only ADI manifest has malformed {count_key}")
        if not isinstance(manifest.get(digest_key), str) or not re.fullmatch(
            r"[0-9a-f]{64}", manifest[digest_key]
        ):
            raise ValueError(f"RX-only ADI manifest has malformed {digest_key}")
    return manifest


def _closure_records(source: Path, manifest: dict[str, object]) -> list[str]:
    ip_tcl_rel = str(manifest["ip_tcl"])
    ip_tcl = _read_text(source / ip_tcl_rel)
    paths: set[str] = set(str(item) for item in manifest["entry_scripts"])
    paths.add(ip_tcl_rel)
    retired = set(str(item) for item in manifest["retired_from_generated_fileset"])
    for item in re.findall(r'"([^"\n]+\.(?:v|xdc|ttcl))"', ip_tcl):
        if item.startswith("$ad_hdl_dir/"):
            relative = item.removeprefix("$ad_hdl_dir/")
        else:
            relative = (Path("library/axi_ad9361") / item).as_posix()
        normalized = (source / relative).resolve(strict=True)
        if source not in normalized.parents and normalized != source:
            raise ValueError(f"IP Tcl dependency escapes ADI source: {item}")
        rel = normalized.relative_to(source).as_posix()
        if Path(rel).name not in retired:
            paths.add(rel)

    # ``axi_ad9361_ip.tcl`` sources ADI packaging helpers.  Follow the small,
    # explicit ``source`` closure as well: packaging can otherwise change even
    # while all HDL in the generated fileset hashes identically.  Reject any
    # reference that cannot be mapped to a file inside the reviewed tree.
    pending = list(paths)
    visited_tcl: set[str] = set()
    while pending:
        current = pending.pop()
        if not current.endswith(".tcl") or current in visited_tcl:
            continue
        visited_tcl.add(current)
        body = _read_text(source / current)
        for source_expr in re.findall(r"(?m)^\s*source\s+([^\s;]+)", body):
            if source_expr.startswith("$ad_hdl_dir/"):
                candidate_rel = source_expr.removeprefix("$ad_hdl_dir/")
            elif "$" not in source_expr and "[" not in source_expr:
                candidate_rel = Path(os.path.normpath(
                    str(Path(current).parent / source_expr)
                )).as_posix()
            elif "/scripts/" in source_expr:
                # ADI's BD helper derives this path with Tcl variables.  The
                # tail is nevertheless a source-tree-relative scripts path.
                candidate_rel = "library/scripts/" + source_expr.rsplit("/scripts/", 1)[1]
            else:
                raise ValueError(
                    f"cannot audit dynamic Tcl source expression in {current}: {source_expr}"
                )
            candidate = (source / candidate_rel).resolve(strict=True)
            if source not in candidate.parents and candidate != source:
                raise ValueError(f"Tcl dependency escapes ADI source: {source_expr}")
            rel = candidate.relative_to(source).as_posix()
            if _is_reparse(candidate):
                raise ValueError(f"Tcl dependency is linked: {candidate}")
            if rel not in paths:
                paths.add(rel)
                pending.append(rel)
    records = []
    for rel in sorted(paths):
        candidate = source / rel
        if not candidate.is_file() or _is_reparse(candidate):
            raise ValueError(f"closure dependency is missing or linked: {candidate}")
        records.append(f"{rel}\t{sha256(candidate)}")
    return records


def _closure_digest(records: list[str]) -> str:
    return hashlib.sha256(("\n".join(records) + "\n").encode("utf-8")).hexdigest()


def _tree_records(root: Path, *, exclude: set[Path] | None = None) -> list[str]:
    """Hash every ordinary output file in deterministic relative-path order."""
    _reject_reparse_tree(root)
    excluded = {item.as_posix() for item in (exclude or set())}
    records: list[str] = []
    for candidate in sorted(root.rglob("*")):
        if not candidate.is_file():
            continue
        rel = candidate.relative_to(root).as_posix()
        if rel not in excluded:
            records.append(f"{rel}\t{sha256(candidate)}")
    return records


def _assert_pinned_tree(
    root: Path,
    manifest: dict[str, object],
    count_key: str,
    digest_key: str,
    description: str,
    *,
    exclude: set[Path] | None = None,
) -> list[str]:
    """Return a complete content manifest only if it matches the reviewed pin.

    This is a whole-tree content pin, not a claim that every non-ADI file was
    individually manually reviewed.  It prevents a same-named IP, extra IP,
    script, or support RTL replacement from entering a copied overlay.
    """
    records = _tree_records(root, exclude=exclude)
    expected_count = manifest.get(count_key)
    expected_digest = manifest.get(digest_key)
    actual_digest = _closure_digest(records)
    if len(records) != expected_count or actual_digest != expected_digest:
        raise ValueError(
            f"{description} content differs from reviewed manifest: " +
            json.dumps({
                "expected_file_count": expected_count,
                "actual_file_count": len(records),
                "expected_sha256": expected_digest,
                "actual_sha256": actual_digest,
            }, sort_keys=True)
        )
    return records


def retired_tx_word_address(address: int) -> bool:
    """True only for the old common/channel banks, in ADI *word* addresses."""
    if not 0 <= address < (1 << 14):
        raise ValueError("up_axi word address must be a 14-bit unsigned value")
    return ((address >> 7) == 0x20) or (
        (address >> 8) == 0x11 and ((address >> 4) & 0xF) <= 3
    )


def _validate_source_tree(source: Path) -> None:
    if not source.is_dir():
        raise ValueError(f"source is not a directory: {source}")
    _reject_reparse_tree(source)
    manifest = _load_source_manifest()
    _assert_pinned_tree(
        source, manifest,
        "source_tree_file_count", "source_tree_sha256",
        "reviewed ADI source tree",
    )
    if sha256(source / str(manifest["ip_tcl"])) != manifest["ip_tcl_sha256"]:
        raise ValueError("unknown ADI IP Tcl revision; refuse RX-only overlay")
    closure_records = _closure_records(source, manifest)
    if len(closure_records) != manifest.get("closure_file_count"):
        raise ValueError("ADI source closure file count differs; refuse RX-only overlay")
    if _closure_digest(closure_records) != manifest["closure_sha256"]:
        raise ValueError("unknown or incomplete ADI IP closure; refuse RX-only overlay")
    for rel in REQUIRED_ROOT_RELATIVE:
        candidate = source / rel
        if not candidate.is_file():
            raise ValueError(f"reviewed ADI source is missing required dependency: {candidate}")
    mismatched_hashes = {
        rel: {"expected": expected, "actual": sha256(source / rel)}
        for rel, expected in PINNED_SOURCE_SHA256.items()
        if sha256(source / rel) != expected
    }
    if mismatched_hashes:
        raise ValueError(
            "unknown or modified ADI source revision; inspect and re-pin before overlay: "
            + json.dumps(mismatched_hashes, sort_keys=True)
        )

    axi = _read_text(source / AXI_REL)
    required_tokens = (
        "axi_ad9361_tx #(",
        ") i_tx (",
        "axi_ad9361_rx #(",
        ") i_rx (",
        "axi_ad9361_lvds_if #(",
        ") i_dev_if (",
        "if (TDD_DISABLE == 1)",
        "assign up_wack_tdd_s  = 1'b0;",
    )
    missing = [token for token in required_tokens if token not in axi]
    if missing:
        raise ValueError(f"unknown ADI axi_ad9361 variant; required tokens absent: {missing}")
    if axi.count("axi_ad9361_tx #(") != 1 or axi.count(") i_tx (") != 1:
        raise ValueError("unknown ADI axi_ad9361 variant; expected exactly one i_tx instance")
    if axi.count(TX_BLOCK_START) != 1 or axi.find(TX_BLOCK_END, axi.index(TX_BLOCK_START)) < 0:
        raise ValueError("unknown ADI axi_ad9361 variant; TX replacement delimiters are not unique")

    # Validate the exact legacy decoders before copying.  Do not trust comments
    # or a remembered register map.
    dac_common = _read_text(source / "library/common/up_dac_common.v")
    dac_channel = _read_text(source / "library/common/up_dac_channel.v")
    delay = _read_text(source / "library/common/up_delay_cntrl.v")
    tx = _read_text(source / "library/axi_ad9361/axi_ad9361_tx.v")
    decoder_patterns = {
        "DAC common ID 0x10": (dac_common, r"parameter\s+COMMON_ID\s*=\s*6'h10"),
        "DAC common 7-bit select": (dac_common, r"up_waddr\[13:7\]\s*==\s*\{COMMON_ID,1'b0\}"),
        "DAC channel ID 0x11": (dac_channel, r"parameter\s+COMMON_ID\s*=\s*6'h11"),
        "DAC channel select": (dac_channel, r"up_waddr\[7:4\]\s*==\s*CHANNEL_ID"),
        "DAC delay base 0x12": (tx, r"\.BASE_ADDRESS\s*\(6'h12\)"),
        "disabled delay branch": (delay, r"if\s*\(DISABLE\s*==\s*1\)\s*begin"),
        "disabled delay write no-ack": (delay, r"assign\s+up_wack\s*=\s*1'd0"),
        "disabled delay read no-ack": (delay, r"assign\s+up_rack\s*=\s*1'd0"),
    }
    missing = [name for name, (body, pattern) in decoder_patterns.items()
               if re.search(pattern, body) is None]
    if missing:
        raise ValueError(f"reviewed TX decoder semantics differ; missing {missing}")

    # RX IDs 0x00/0x01/0x02 are source-evidenced; disabled TDD drives zero
    # acknowledge in the exact top-level configuration.  Hence neither may
    # overlap our 0x10/0x11 retired word-address banks.
    rx = _read_text(source / "library/axi_ad9361/axi_ad9361_rx.v")
    if ".BASE_ADDRESS (6'h02)" not in rx or "up_adc_common #(\n" not in rx:
        raise ValueError("cannot prove RX address partition for this ADI source")

    # Direction audit: these top-level nets are driven by the receive/PPS
    # machinery, not by the TX replacement.  A future vendor port-direction
    # change must stop the overlay rather than creating a hidden multi-driver.
    direction_patterns = (
        r"input\s+delay_locked\s*[,;]",
        r"input\s+\[31:0\]\s+up_pps_rcounter\s*[,;]",
        r"input\s+up_pps_status\s*[,;]",
        r"output\s+up_pps_irq_mask\s*[,;]",
    )
    if any(re.search(pattern, tx) is None for pattern in direction_patterns):
        raise ValueError("axi_ad9361_tx port directions differ from reviewed RX-only stub audit")


def _replace_tx_instance(axi_path: Path) -> None:
    text = _read_text(axi_path)
    if text.count(TX_BLOCK_START) != 1 or text.find(TX_BLOCK_END, text.index(TX_BLOCK_START)) < 0:
        raise ValueError("copied axi_ad9361.v no longer matches the reviewed source delimiters")
    start = text.index(TX_BLOCK_START)
    end = text.index(TX_BLOCK_END, start)
    old = text[start:end]
    if old.count("axi_ad9361_tx #(") != 1 or ") i_tx (" not in old:
        raise ValueError("refusing to replace an unexpected i_tx block")
    text = text[:start] + "  // transmit (retired)\n\n" + RXONLY_INSTANCE + text[end:]
    if "axi_ad9361_tx #(\n" in text or ") i_tx (" in text:
        raise ValueError("i_tx removal did not complete")
    if text.count("rfv2_tx_register_stub i_tx_rxonly") != 1:
        raise ValueError("stub insertion did not complete")
    axi_path.write_text(text, encoding="utf-8", newline="\n")


def _patch_ip_tcl(ip_tcl: Path) -> None:
    text = _read_text(ip_tcl)
    if STUB_NAME in text:
        raise ValueError("copied AXI AD9361 IP Tcl already names the RX-only stub")
    # Retired TX HDL must not remain in the generated IP fileset.  It has no
    # instance after the top-level substitution; removing it also prevents an
    # unrelated legacy compile warning from being mistaken for RX-only logic.
    for retired in RETIRED_TX_SOURCES:
        line = '  "' + retired + '" ' + chr(92) + '\n'
        if text.count(line) != 1:
            raise ValueError(f"cannot safely retire {retired} from AXI AD9361 IP Tcl")
        text = text.replace(line, "", 1)
    marker = '  "axi_ad9361.v" ]'
    if text.count(marker) != 1:
        raise ValueError("cannot safely add stub to axi_ad9361 IP fileset")
    # Build the Tcl continuation without an escaping ambiguity in this Python
    # source: a literal backslash immediately precedes the physical newline.
    replacement = '  "axi_ad9361.v" ' + chr(92) + '\n  "' + STUB_NAME + '" ]'
    text = text.replace(marker, replacement, 1)
    ip_tcl.write_text(text, encoding="utf-8", newline="\n")


def _patch_lvds_tx_idle(lvds_path: Path) -> None:
    text = _read_text(lvds_path)
    start = text.find(LVDS_TX_BLOCK_START)
    end = text.find(LVDS_TX_BLOCK_END, start)
    if start < 0 or end < 0 or text.count("tx_data_sel <= tx_data_sel + 1'b1;") != 1:
        raise ValueError("unknown LVDS TX serializer structure; refuse physical-idle patch")
    replacement = '''  // RX-only overlay: retain the forwarded TX clock path for the AD9361
  // interface, but make all TX data/frame serializer state deterministic.
  // This removes the otherwise free-running tx_data_sel/frame activity when
  // dac_valid is zero; it does not alter RX capture or EN/TXNRX generation.
  always @(posedge clk) begin
    tx_data_sel <= 2'b00;
    tx_data <= 48'd0;
    tx_clk_p <= {~dac_clksel, dac_clksel};
  end

  always @(posedge clk) begin
    tx_frame_p <= 1'b0;
    tx_data_0_p <= 6'd0;
    tx_data_1_p <= 6'd0;
  end
'''
    text = text[:start] + replacement + text[end:]
    lvds_path.write_text(text, encoding="utf-8", newline="\n")


def _patch_component_xml(component: Path) -> None:
    tree = ET.parse(component)
    root = tree.getroot()
    ns = {"spirit": "http://www.spiritconsortium.org/XMLSchema/SPIRIT/1685-2009"}
    file_sets = root.find("spirit:fileSets", ns)
    if file_sets is None:
        raise ValueError("component.xml has no file sets")
    changed = 0
    for file_set in file_sets.findall("spirit:fileSet", ns):
        name = file_set.findtext("spirit:name", default="", namespaces=ns)
        if name not in {
            "xilinx_anylanguagesynthesis_view_fileset",
            "xilinx_anylanguagebehavioralsimulation_view_fileset",
        }:
            continue
        names = [node.findtext("spirit:name", default="", namespaces=ns)
                 for node in file_set.findall("spirit:file", ns)]
        for retired in RETIRED_TX_SOURCES:
            if names.count(retired) != 1:
                raise ValueError(f"component XML fileset {name} has ambiguous {retired}")
            stale = next(node for node in file_set.findall("spirit:file", ns)
                         if node.findtext("spirit:name", default="", namespaces=ns) == retired)
            file_set.remove(stale)
        if STUB_NAME in names:
            raise ValueError(f"component XML fileset already contains {STUB_NAME}")
        source_nodes = file_set.findall("spirit:file", ns)
        anchor = next((node for node in source_nodes
                       if node.findtext("spirit:name", default="", namespaces=ns) == "axi_ad9361.v"), None)
        if anchor is None:
            raise ValueError(f"component XML fileset {name} lacks axi_ad9361.v")
        stub = ET.Element("{http://www.spiritconsortium.org/XMLSchema/SPIRIT/1685-2009}file")
        stub_name = ET.SubElement(stub, "{http://www.spiritconsortium.org/XMLSchema/SPIRIT/1685-2009}name")
        stub_name.text = STUB_NAME
        stub_type = ET.SubElement(stub, "{http://www.spiritconsortium.org/XMLSchema/SPIRIT/1685-2009}fileType")
        stub_type.text = "verilogSource"
        file_set.insert(list(file_set).index(anchor) + 1, stub)
        changed += 1
    if changed != 2:
        raise ValueError(f"expected to patch synthesis+simulation XML filesets, patched {changed}")

    # Any old generated checksum of axi_ad9361.v is stale after a mechanical
    # overlay patch.  Remove it rather than advertising an incorrect checksum.
    for node in root.findall(".//spirit:file", ns):
        if node.findtext("spirit:name", default="", namespaces=ns) in {"axi_ad9361.v", LVDS_REL.name}:
            for child in list(node):
                if child.tag.endswith("userFileType") and (child.text or "").startswith("CHECKSUM_"):
                    node.remove(child)
    ET.register_namespace("spirit", ns["spirit"])
    ET.register_namespace("xilinx", "http://www.xilinx.com")
    ET.register_namespace("xsi", "http://www.w3.org/2001/XMLSchema-instance")
    tree.write(component, encoding="utf-8", xml_declaration=True)


def _validate_overlay(destination: Path) -> dict[str, str]:
    axi = _read_text(destination / AXI_REL)
    lvds = _read_text(destination / LVDS_REL)
    ip_tcl = _read_text(destination / IP_TCL_REL)
    component = _read_text(destination / COMPONENT_REL)
    stub_match = re.search(
        r"rfv2_tx_register_stub i_tx_rxonly \((.*?)\);\n\n  // Do not let GUI",
        axi,
        flags=re.DOTALL,
    )
    if stub_match is None:
        raise ValueError("cannot isolate RX-only stub connection block for direction audit")
    stub_connections = stub_match.group(1)
    checks = {
        "i_tx_removed": "axi_ad9361_tx #(\n" not in axi and ") i_tx (" not in axi,
        "stub_instance_once": axi.count("rfv2_tx_register_stub i_tx_rxonly") == 1,
        "stub_packaged_tcl": ip_tcl.count(STUB_NAME) == 1,
        "stub_packaged_xml": component.count(f">{STUB_NAME}<") == 2,
        "retired_tx_not_packaged_tcl": all(name not in ip_tcl for name in RETIRED_TX_SOURCES),
        "retired_tx_not_packaged_xml": all(f">{name}<" not in component for name in RETIRED_TX_SOURCES),
        "rx_kept": axi.count("axi_ad9361_rx #(") == 1 and ") i_rx (" in axi,
        "lvds_kept": axi.count("axi_ad9361_lvds_if #(") == 1 and ") i_dev_if (" in axi,
        "disabled_tdd_kept": "assign up_wack_tdd_s  = 1'b0;" in axi,
        "no_tx_multidriver": "assign dac_valid_s" not in axi and "assign dac_data_s" not in axi,
        "rx_owned_inputs_not_stub_driven": all(name not in stub_connections for name in (
            ".delay_locked", ".up_pps_rcounter", ".up_pps_status")),
        "lvds_tx_data_frame_static": "tx_data_sel <= tx_data_sel + 1'b1;" not in lvds and
            "tx_frame_p <= 1'b0;" in lvds and "tx_data_0_p <= 6'd0;" in lvds,
    }
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise ValueError(f"overlay validation failed: {failed}")
    return {rel.as_posix(): sha256(destination / rel) for rel in OVERLAY_HASH_RELATIVE}


def prepare(source: Path, destination: Path, stub: Path) -> dict[str, object]:
    # Do not resolve before the reparse-point audit: resolving first would hide
    # a source-root symlink/junction from the safety check on Windows.
    source = Path(os.path.abspath(source))
    destination = Path(os.path.abspath(destination))
    stub = Path(os.path.abspath(stub))
    if destination.exists():
        raise ValueError(f"refusing non-fresh destination: {destination}")
    if destination == source or source in destination.parents:
        raise ValueError("destination must not be the reviewed source tree or a descendant of it")
    if stub.name != STUB_NAME or not stub.is_file():
        raise ValueError(f"expected checked-in {STUB_NAME}, got {stub}")
    if _is_reparse(stub):
        raise ValueError(f"RX-only stub source must not be a link/reparse point: {stub}")
    control_hashes = _freeze_control_inputs(stub)
    _validate_source_tree(source)

    # Preserve a failed fresh destination for inspection.  A success receipt is
    # written only after every source and packaging check succeeds; never clean
    # up here because a copy/create race must not delete another writer's tree.
    input_manifest = _load_source_manifest()
    source_tree_snapshot = _assert_pinned_tree(
        source, input_manifest,
        "source_tree_file_count", "source_tree_sha256",
        "reviewed ADI source tree after validation",
    )
    snapshot_records = _closure_records(source, input_manifest)
    if len(snapshot_records) != input_manifest["closure_file_count"]:
        raise ValueError("ADI source closure count changed after validation; refuse overlay")
    if _closure_digest(snapshot_records) != input_manifest["closure_sha256"]:
        raise ValueError("ADI source closure changed after validation; refuse overlay")
    shutil.copytree(source, destination)
    if source_tree_snapshot != _tree_records(destination):
        raise ValueError("complete ADI source tree changed during copy; preserved destination has no receipt")
    # Detect source edits during copy before patching the derivative; this also
    # records the exact immutable input closure used by the receipt.
    if snapshot_records != _closure_records(destination, input_manifest):
        raise ValueError("ADI source closure changed during copy; preserved destination has no receipt")
    _copy_stub_frozen(
        stub,
        destination / "library/axi_ad9361" / STUB_NAME,
        control_hashes["stub"],
    )
    _replace_tx_instance(destination / AXI_REL)
    _patch_ip_tcl(destination / IP_TCL_REL)
    _patch_lvds_tx_idle(destination / LVDS_REL)
    _patch_component_xml(destination / COMPONENT_REL)
    overlay_hashes = _validate_overlay(destination)
    expected_overlay_hashes = input_manifest["expected_overlay_file_sha256"]
    if overlay_hashes != expected_overlay_hashes:
        raise ValueError("RX-only overlay output hashes differ from reviewed manifest: " +
                         json.dumps({"expected": expected_overlay_hashes,
                                     "actual": overlay_hashes}, sort_keys=True))
    output_records = _tree_records(destination)
    output_digest = _closure_digest(output_records)
    if (len(output_records) != input_manifest["expected_output_tree_file_count"] or
            output_digest != input_manifest["expected_output_tree_sha256"]):
        raise ValueError(
            "RX-only output tree differs from reviewed manifest: " +
            json.dumps({
                "expected_file_count": input_manifest["expected_output_tree_file_count"],
                "actual_file_count": len(output_records),
                "expected_sha256": input_manifest["expected_output_tree_sha256"],
                "actual_sha256": output_digest,
            }, sort_keys=True)
        )
    # The preparer/manifest/stub are the three local controls which define the
    # patch.  A change at any point invalidates this output; retain the fresh
    # destination as failure evidence and deliberately omit its receipt.
    _assert_control_inputs_unchanged(control_hashes, stub)
    _assert_stub_triplet(
        control_hashes["stub"], stub,
        destination / "library/axi_ad9361" / STUB_NAME,
    )
    receipt = {
        "overlay": OVERLAY_NAME,
        "source": str(source),
        "source_axi_ad9361_sha256": sha256(source / AXI_REL),
        "stub_source_sha256": control_hashes["stub"],
        "preparer_sha256": control_hashes["preparer"],
        "source_manifest_sha256": control_hashes["manifest"],
        "control_input_sha256": control_hashes,
        "input_closure_records": snapshot_records,
        "input_closure_sha256": _closure_digest(snapshot_records),
        "input_source_tree_file_count": len(source_tree_snapshot),
        "input_source_tree_sha256": _closure_digest(source_tree_snapshot),
        "word_address_banks": {
            "retired_common": "0x1000..0x107f",
            "retired_channels": "0x1100..0x113f",
            "unclaimed_disabled_delay": "0x1200..0x12ff",
            "axi_byte_address_note": "AXI byte address is word address multiplied by 4",
        },
        "configuration": {
            "CMOS_OR_LVDS_N": 0,
            "MODE_1R1T": 0,
            "DAC_DATAPATH_DISABLE": 1,
            "DAC_DDS_DISABLE": 1,
            "DAC_USERPORTS_DISABLE": 1,
            "DAC_IQCORRECTION_DISABLE": 1,
            "DAC_IODELAY_ENABLE": 0,
            "TDD_DISABLE": 1,
            "USE_SSI_CLK": 0,
        },
        "overlay_file_sha256": overlay_hashes,
        "output_tree_records_excluding_receipt": output_records,
        "output_tree_sha256_excluding_receipt": output_digest,
    }
    (destination / "RXONLY_OVERLAY_RECEIPT.json").write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path, help="reviewed isolated ADI HDL tree")
    parser.add_argument("--out", required=True, type=Path, help="new overlay destination (must not exist)")
    parser.add_argument(
        "--stub", type=Path,
        default=Path(__file__).with_name(STUB_NAME),
        help="checked-in RX-only stub source",
    )
    args = parser.parse_args(argv)
    try:
        receipt = prepare(args.source, args.out, args.stub)
    except (OSError, ValueError, ET.ParseError) as exc:
        print(f"RX-only ADI overlay preparation refused: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
