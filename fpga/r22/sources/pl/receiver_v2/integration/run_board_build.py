#!/usr/bin/env python3
"""Freeze the complete RXv2 source closure before any Vivado board build."""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


def digest(path):
    h=hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda:f.read(1024*1024),b""):h.update(block)
    return h.hexdigest()


def tool_errors(output, final_implementation_verified=False):
    """Some Vivado startup errors do not propagate to the process exit code."""
    errors=[]
    for line in output.splitlines():
        if re.match(r"^\s*(?:ERROR|CRITICAL WARNING):", line):
            # The first route can fail timing before the explicit post-route
            # optimisation. Only the immutable Tcl's final completion marker,
            # after ALL physical gates, can retire this one stale diagnostic.
            # IOB/DRC/other critical warnings are never hidden by that marker.
            if final_implementation_verified and re.match(r"^\s*CRITICAL WARNING: \[Timing 38-282\]",line):
                continue
            errors.append(line)
    return errors


def exact_completion(output, marker):
    return re.search(r'^'+re.escape(marker)+r'\s*$',output,re.MULTILINE) is not None


def overlay_receipt_file(vendor):
    for name in ('WFIFO_RESET_HARDENED_RECEIPT.json', 'RESET_HARDENED_ADI_RECEIPT.json', 'RXONLY_OVERLAY_RECEIPT.json'):
        if (vendor/name).is_file():
            return vendor/name
    raise ValueError('prepared overlay receipt is missing')


def validate_rxonly_overlay(vendor, integration=None):
    """Admit only the current prepared RX-only tree, never a raw ADI tree.

    This is provenance/integrity admission, not functional or timing signoff.
    Failed overlay preparation and changed output files must fail before Vivado.
    """
    integration = integration or Path(__file__).resolve().parent
    if (vendor/'WFIFO_RESET_HARDENED_RECEIPT.json').exists():
        if (vendor/'RXONLY_OVERLAY_RECEIPT.json').exists():
            raise ValueError('ambiguous mixed r5/r7 overlay receipts')
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            'rfv2_wfifo_overlay_admission', integration/'prepare_reset_hardened_wfifo.py')
        validator = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(validator)
        return validator.validate_reset_hardened_wfifo(vendor)
    reset_receipt = vendor / "RESET_HARDENED_ADI_RECEIPT.json"
    if reset_receipt.exists():
        if (vendor / "RXONLY_OVERLAY_RECEIPT.json").exists():
            raise ValueError("ambiguous mixed r5/r6 overlay receipts")
        # Load the validator from THIS source snapshot. A live repository
        # import would defeat frozen build inputs and off-machine admission.
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "rfv2_reset_overlay_admission", integration / "prepare_reset_hardened_adi.py")
        validator = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(validator)
        return validator.validate_reset_hardened_adi(vendor, integration)
    import stat
    nodes=[vendor,*sorted(vendor.rglob("*"))]
    for path in nodes:
        metadata=path.lstat()
        if (stat.S_ISLNK(metadata.st_mode) or
            getattr(metadata,"st_file_attributes",0) & 0x400):
            raise ValueError("RX-only overlay contains a link/reparse point")
    receipt_path = vendor / "RXONLY_OVERLAY_RECEIPT.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if receipt.get("overlay") != "receiver-v2-rxonly-adi":
        raise ValueError("a prepared receiver-v2 RX-only ADI overlay is required")
    for field, name in (("preparer_sha256", "prepare_rxonly_adi.py"),
                        ("source_manifest_sha256", "adi_rxonly_source_manifest.json"),
                        ("stub_source_sha256", "rfv2_tx_register_stub.v")):
        if receipt.get(field) != digest(integration / name):
            raise ValueError("stale RX-only overlay provenance: " + field)
    manifest = json.loads((integration / "adi_rxonly_source_manifest.json").read_text())
    required = {"library/axi_ad9361/axi_ad9361.v",
        "library/axi_ad9361/axi_ad9361_ip.tcl",
        "library/axi_ad9361/component.xml",
        "library/axi_ad9361/xilinx/axi_ad9361_lvds_if.v",
        "library/axi_ad9361/rfv2_tx_register_stub.v"}
    expected = manifest.get("expected_overlay_file_sha256", {})
    if set(expected) != required:
        raise ValueError("reviewed manifest lacks the exact RX-only output closure")
    for rel, sha in expected.items():
        if digest(vendor/rel) != sha:
            raise ValueError("RX-only deterministic output differs: " + rel)
    if digest(vendor/"library/axi_ad9361/rfv2_tx_register_stub.v") != receipt["stub_source_sha256"]:
        raise ValueError("RX-only copied stub differs from declared source")
    if receipt.get("overlay_file_sha256") != expected:
        raise ValueError("RX-only changed-file summary is incomplete or differs")
    def records_digest(records):
        return hashlib.sha256(("\n".join(records) + "\n").encode()).hexdigest()
    inputs = receipt.get("input_closure_records", [])
    if (len(inputs) != manifest["closure_file_count"] or
        records_digest(inputs) != manifest["closure_sha256"] or
        receipt.get("input_closure_sha256") != manifest["closure_sha256"]):
        raise ValueError("RX-only input closure is not the reviewed vendor revision")
    expected_config = dict(CMOS_OR_LVDS_N=0, MODE_1R1T=0,
        DAC_DATAPATH_DISABLE=1, DAC_DDS_DISABLE=1, DAC_USERPORTS_DISABLE=1,
        DAC_IQCORRECTION_DISABLE=1, DAC_IODELAY_ENABLE=0, TDD_DISABLE=1,
        USE_SSI_CLK=0)
    if receipt.get("configuration") != expected_config:
        raise ValueError("unsupported RX-only ADI configuration")
    # Reject junctions as well as symlinks before following any file for hashing.
    actual = []
    for path in nodes:
        metadata = path.lstat()
        if (stat.S_ISLNK(metadata.st_mode) or
            getattr(metadata, "st_file_attributes", 0) & 0x400):
            raise ValueError("RX-only overlay contains a link/reparse point")
        if path.is_file() and path != receipt_path:
            actual.append(path.relative_to(vendor).as_posix() + "\t" + digest(path))
    if (actual != receipt.get("output_tree_records_excluding_receipt") or
        records_digest(actual) != receipt.get("output_tree_sha256_excluding_receipt")):
        raise ValueError("RX-only overlay output tree changed after preparation")
    if (len(actual)!=manifest.get("expected_output_tree_file_count") or
        records_digest(actual)!=manifest.get("expected_output_tree_sha256")):
        raise ValueError("RX-only complete vendor tree differs from reviewed manifest")
    return digest(receipt_path)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out",type=Path,required=True)
    p.add_argument("--adi-hdl",type=Path,required=True)
    p.add_argument("--mode",choices=("validate","synth","implement"),default="validate")
    p.add_argument("--no-hdmi",action="store_true")
    p.add_argument("--rf-budget-xdc",type=Path)
    p.add_argument("--spi-budget-xdc",type=Path)
    p.add_argument("--cdc-info-manifest",type=Path,
                   help="independently reviewed exact Info endpoints for the final routed CDC gate")
    p.add_argument("--vivado",type=Path,
                   default=Path(__file__).resolve().with_name("vivado_2025_2_launcher.cmd"))
    a=p.parse_args();root=Path(__file__).resolve().parents[3]
    out=a.out.resolve();vendor=a.adi_hdl.resolve(strict=True)
    overlay_receipt_sha = validate_rxonly_overlay(vendor)
    if (a.rf_budget_xdc is None)!=(a.spi_budget_xdc is None):
        p.error("RF and SPI timing budgets must be supplied together")
    if a.mode=="implement" and (a.rf_budget_xdc is None or a.spi_budget_xdc is None):
        p.error("implementation requires reviewed --rf-budget-xdc and --spi-budget-xdc")
    if a.mode == "implement" and not a.no_hdmi and a.cdc_info_manifest is None:
        p.error("HDMI implementation requires a reviewed --cdc-info-manifest")
    if a.cdc_info_manifest is not None:
        info = json.loads(a.cdc_info_manifest.read_text(encoding="utf-8"))
        if (info.get("schema") != "rfv2-cdc-info-manifest-v1" or info.get("reviewed") is not True
                or not isinstance(info.get("info_rows"), list) or not info["info_rows"]):
            p.error("CDC Info manifest must have reviewed=true and a nonempty exact row set")
    out.mkdir(parents=True,exist_ok=False);snapshot=out/"sources"
    manifest=Path("pl/receiver_v2/rtl_sources.txt")
    paths=[Path(s.strip()) for s in (root/manifest).read_text().splitlines() if s.strip() and not s.lstrip().startswith("#")]
    if len(paths)!=len(set(paths)):raise ValueError("duplicate production source")
    for rel in paths:
        if rel.is_absolute() or ".." in rel.parts or set(rel.parts)&{"tb","tests","test","sim","simulation"}:
            raise ValueError("unsafe/nonproduction source: "+str(rel))
    paths += [manifest,Path("pl/receiver_v2/integration/receiver_v2_topology_contract.tcl"),
        Path("pl/receiver_v2/integration/receiver_v2_signoff_gates.tcl"),
        Path("pl/receiver_v2/integration/receiver_v2_control_merge.tcl"),
        Path("pl/receiver_v2/integration/rf_clock_monitor_cdc.xdc"),
        Path("pl/receiver_v2/integration/capture_reset_guard_cdc.xdc"),
        Path("pl/receiver_v2/integration/capture_reset_guard_contract.tcl"),
        Path("pl/receiver_v2/integration/capture_reset_bd_contract.tcl"),
        Path("pl/receiver_v2/integration/release_assertion_contract.tcl"),
        Path("pl/receiver_v2/integration/release_assertion_effect.tcl"),
        Path("pl/receiver_v2/integration/release_assertion_exact_cdc.xdc"),
        Path("pl/receiver_v2/integration/hdmi_release_assertion_exact_cdc.xdc"),
        Path("pl/receiver_v2/integration/create_zedboard_receiver_v2.tcl"),
        Path("pl/receiver_v2/integration/run_board_build.py"),
        Path("pl/receiver_v2/integration/vivado_2025_2_launcher.cmd"),
        Path("pl/receiver_v2/integration/prepare_rxonly_adi.py"),
        Path("pl/receiver_v2/integration/adi_rxonly_source_manifest.json"),
        Path("pl/receiver_v2/integration/prepare_reset_hardened_adi.py"),
        Path("pl/receiver_v2/integration/prepare_reset_hardened_wfifo.py"),
        Path("pl/receiver_v2/integration/wfifo_reset_patch.py"),
        Path("pl/receiver_v2/integration/reset_hardened_wfifo_manifest.json"),
        Path("pl/receiver_v2/integration/reset_hardened_adi_manifest.json"),
        Path("pl/receiver_v2/integration/hdmi_reset_patch.py"),
        Path("pl/receiver_v2/integration/hdmi_reset_patch_sources/axi_hdmi_tx_reset_sync.v"),
        Path("pl/receiver_v2/integration/hdmi_reset_patch_sources/axi_hdmi_tx_axis_epoch_bridge.v"),
        Path("pl/receiver_v2/integration/rfv2_tx_register_stub.v"),
        Path("pl/constraints/zedboard_fmcomms3_lvds_2v5.xdc"),Path("pl/constraints/zedboard_rf_sync_attributes.xdc")]
    if not a.no_hdmi:
        paths += [Path("pl/constraints/zedboard_hdmi_adv7511.xdc"),
                  Path("pl/constraints/hdmi_presentation_observer_cdc.xdc"),
                  Path("pl/rtl/hdmi_presentation_observer.v"),
                  Path("pl/rtl/rf_axis_status_monitor.v"),
                  Path("pl/scripts/add_hdmi_presentation_observer.tcl"),
                  Path("pl/receiver_v2/integration/hdmi_gray_pointer_cdc.xdc"),
                  Path("pl/receiver_v2/integration/hdmi_gray_pointer_contract.tcl"),
                  Path("pl/receiver_v2/integration/hdmi_mailbox_exact_cdc.xdc"),
                  Path("pl/receiver_v2/integration/hdmi_mailbox_exact_contract.tcl")]
        paths += [Path('pl/receiver_v2/integration/hdmi_epoch_exact_cdc.xdc'),
                  Path('pl/receiver_v2/integration/hdmi_epoch_cdc_contract.tcl')]
    # Audit executable closure is part of the same source snapshot as RTL.
    paths += [Path("pl/receiver_v2/integration") / name for name in (
        "receiver_v2_external_io_contract.tcl",
        "tests/run_routed_cdc_external_presignoff.py", "tests/routed_cdc_external_audit.tcl",
        "tests/cdc_typed_exact_pairs.tcl", "tests/cdc_typed_sync_stages.tcl",
        "tests/cdc_typed_inventory.py", "tests/cdc_info_manifest.py",
        "tests/audit_adi_bundled_delay.tcl", "tests/cdc_info_exact_stages.tcl", "tests/cdc_critical_contract.py",
        "receiver_v2_typed_cdc_gate.tcl")]
    frozen={}
    for rel in paths:
        source=root/rel;dest=snapshot/rel;dest.parent.mkdir(parents=True,exist_ok=True)
        before=digest(source);shutil.copyfile(source,dest)
        if digest(source)!=before or digest(dest)!=before:raise RuntimeError("source changed during freeze: "+str(rel))
        frozen[rel.as_posix()]=before
    # Build only from a frozen copy, never from the caller's mutable vendor
    # directory. Copy links as links so admission rejects them without reading
    # an external target. The complete receipt-bound tree is checked on copy.
    frozen_vendor=snapshot/"vendor"
    shutil.copytree(vendor,frozen_vendor,symlinks=True)
    frozen_integration=snapshot/"pl/receiver_v2/integration"
    if validate_rxonly_overlay(frozen_vendor,frozen_integration)!=overlay_receipt_sha:
        raise RuntimeError("RX-only overlay changed during freeze")
    vendor=frozen_vendor
    env=os.environ.copy();env.update(ADI_HDL_DIR=str(vendor),ADI_ALLOW_VERSION_MISMATCH="YES",ENABLE_HDMI="0" if a.no_hdmi else "1")
    for variable in ("MIMO_RXV2_CDC_INFO_MANIFEST", "MIMO_RXV2_AUDIT_PYTHON", "MIMO_RXV2_AUDIT_VIVADO"):
        env.pop(variable, None)
    if a.cdc_info_manifest is not None:
        original_info = a.cdc_info_manifest.resolve(strict=True)
        frozen_info = snapshot / "reviewed-cdc/info_manifest.json"
        frozen_info.parent.mkdir(parents=True)
        info_hash = digest(original_info)
        shutil.copyfile(original_info, frozen_info)
        if digest(original_info) != info_hash or digest(frozen_info) != info_hash:
            raise RuntimeError("reviewed CDC Info manifest changed during freeze")
        frozen[frozen_info.relative_to(snapshot).as_posix()] = info_hash
        env.update(MIMO_RXV2_CDC_INFO_MANIFEST=str(frozen_info),
                   MIMO_RXV2_AUDIT_PYTHON=str(Path(sys.executable).resolve()),
                   MIMO_RXV2_AUDIT_VIVADO=str(a.vivado.resolve(strict=True)))
    for variable,value in (("MIMO_RF_EXTERNAL_BUDGET_XDC",a.rf_budget_xdc),("MIMO_RF_SPI_BUDGET_XDC",a.spi_budget_xdc)):
        env.pop(variable,None)
        if value is not None:
            value=value.resolve(strict=True);dest=snapshot/"external-budgets"/(variable+".xdc")
            dest.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(value,dest)
            frozen[dest.relative_to(snapshot).as_posix()]=digest(dest);env[variable]=str(dest)
    # Guard the private, validated vendor snapshot as well as normal RTL.
    vendor_inputs={str(f.relative_to(vendor)):digest(f) for f in (vendor/"library").rglob("*")
                   if f.is_file() and f.suffix.lower() in {".v",".sv",".vhd",".vhdl",".vh",".svh",".tcl",".xdc",".xml",".mem"}}
    cmd=[str(a.vivado),"-mode","batch","-log",str(out/"vivado.log"),"-journal",str(out/"vivado.jou"),
         "-source",str(snapshot/"pl/receiver_v2/integration/create_zedboard_receiver_v2.tcl"),
         "-tclargs",str(out/"project"),a.mode]
    receipt={"scope":"offline RXv2 board build only; no RF/board/runtime acceptance",
        "status":"RUNNING","mode":a.mode,"hdmi":not a.no_hdmi,"command":cmd,
        "rxonly_overlay_receipt_sha256":overlay_receipt_sha,
        "sources":frozen,"vendor_inputs":vendor_inputs}
    receipt_path=out/"receipt.json"
    receipt_path.write_text(json.dumps(receipt,indent=2)+"\n")
    r=subprocess.run(cmd,cwd=snapshot,env=env,text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
    (out/"console.log").write_text(r.stdout,encoding="utf-8")
    source_ok=all(digest(snapshot/name)==h for name,h in frozen.items())
    vendor_ok=all(digest(vendor/name)==h for name,h in vendor_inputs.items())
    actual_overlay_receipt = overlay_receipt_file(vendor)
    vendor_ok = vendor_ok and digest(actual_overlay_receipt)==overlay_receipt_sha
    try:
        validate_rxonly_overlay(vendor,frozen_integration)
    except (OSError, ValueError, KeyError):
        vendor_ok=False
    marker={"validate":"RXV2_BD_VALIDATED True","synth":"RXV2_SYNTHESIS_COMPLETE True","implement":"RXV2_IMPLEMENTATION_COMPLETE True"}[a.mode]
    completed=exact_completion(r.stdout,marker)
    final_implementation_verified=(a.mode=="implement" and r.returncode==0 and completed and source_ok and vendor_ok)
    errors=tool_errors(r.stdout,final_implementation_verified)
    passed=r.returncode==0 and completed and source_ok and vendor_ok and not errors
    # Bind actual outputs, including a failed run's diagnostic checkpoint.
    # Only PASS/implement plus the final routed hash is eligible for signoff.
    artifacts = {}
    for name in ("receiver_v2_pre_signoff_routed.dcp", "receiver_v2_routed.dcp",
                 "receiver_v2.bit", "receiver_v2.xsa"):
        artifact = out / "project" / name
        if artifact.is_file():
            artifacts["project/" + name] = {"sha256": digest(artifact), "size_bytes": artifact.stat().st_size}
    if passed and a.mode == "implement":
        required = {"project/" + name for name in ("receiver_v2_pre_signoff_routed.dcp",
                    "receiver_v2_routed.dcp", "receiver_v2.bit", "receiver_v2.xsa")}
        missing = required - set(artifacts)
        if missing or any(record["size_bytes"] <= 0 for record in artifacts.values()):
            errors.append("Missing or empty final implementation artifacts: " + ", ".join(sorted(missing)))
            passed = False
    receipt.update(status="PASS" if passed else "FAIL",returncode=r.returncode,source_guard=source_ok,vendor_guard=vendor_ok,tool_errors=errors,
                   artifacts=artifacts,
                   retired_intermediate_timing_warnings=[line for line in r.stdout.splitlines()
                     if final_implementation_verified and re.match(r"^\s*CRITICAL WARNING: \[Timing 38-282\]",line)])
    receipt_path.write_text(json.dumps(receipt,indent=2)+"\n")
    print(json.dumps({k:receipt[k] for k in ("status","mode","returncode","source_guard","vendor_guard")}))
    if not passed:raise RuntimeError(r.stdout[-6000:])


if __name__=="__main__":main()
