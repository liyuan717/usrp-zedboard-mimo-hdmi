"""Freeze RF recipes before launching Vivado; always create a new candidate."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/zedboard"))
from build_features import resolve as resolve_build_features

RECIPES = (
    "pl/scripts/launch_zedboard_rf_release.py", "pl/scripts/create_zedboard_rf_system.tcl",
    "pl/scripts/create_zedboard_rf_buffered_lvds_system.tcl", "pl/scripts/add_detector_module.tcl",
    "pl/scripts/create_zedboard_rf_continuous_system.tcl",
    "pl/scripts/rf_signoff_checks.tcl", "pl/scripts/rf_build_provenance.py",
    "pl/scripts/capture_zedboard_rf_inputs.tcl",
    "pl/scripts/rf_release_bootstrap.py",
    "pl/scripts/generate_zedboard_rf_lvds_timing.py", "pl/scripts/generate_zedboard_spi_timing.py",
    "pl/scripts/prepare_zedboard_lvds_vendor.py", "pl/scripts/package_adi_ip_catalog.tcl",
    "scripts/zedboard/build_features.py",
    "pl/patches/adi/address_generator_descriptor_gate.patch",
    "pl/patches/adi/cpack4_fifo_burst_identity.patch", "pl/patches/adi/dmac_axis_clock_metadata.patch",
    "pl/constraints/zedboard_fmcomms3_lvds_2v5.xdc", "pl/constraints/zedboard_rf_sync_attributes.xdc",
    "board/zedboard/rf_timing_budget_lvds_2r2t_2v5.json", "board/zedboard/rf_spi_timing_budget.json",
    "build/vendor_lvds_2v5_v5/source-manifest.json",
)
RTL = ("mimo_lcg_rng", "mimo_qam_stepper", "mimo_gradient_2x2", "mimo_cost_2x2",
       "mimo_axis_frame_rx", "mimo_mhgd_kernel_2x2", "mimo_axis_frame_tx",
       "mimo_axi_lite_regs", "mimo_mhgd_axis_top")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_iddr_alignment_simulation(path: Path, vendor: Path, expected_abi=2) -> dict:
    result = json.loads(path.read_text())
    if (result.get("passed") is not True or result.get("hardware_tested") is not False
            or result.get("rx_observer_abi") != expected_abi
            or result.get("rx_iddr_alignment_trial") != "same_edge_pipelined"
            or result.get("baseline_observed_4b_reproduced") is not True
            or result.get("baseline_valid_count") != 0
            or result.get("candidate_word_order_verified") is not True
            or result.get("candidate_ordered_samples_verified", 0) < 900):
        raise ValueError("IDDR primitive/word-order comparison has not passed")
    rows = result.get("source_sha256", {})
    for rel in ("library/axi_ad9361/xilinx/axi_ad9361_lvds_if.v", "library/xilinx/common/ad_data_in.v"):
        source = str((vendor / rel).resolve())
        if source not in rows:
            raise ValueError("IDDR simulation does not bind the selected vendor")
    for name, expected in rows.items():
        if digest(Path(name)) != expected:
            raise ValueError("IDDR simulation source changed: " + name)
    return result


def verify_pn_pair_simulation(path: Path, vendor: Path) -> dict:
    result = json.loads(path.read_text())
    required = {'known_sequence_real_checker_and_valid_gaps', 'held_CDC_and_CPU_read',
                'swapped_words', 'bit_error', 'repeated_state', 'skipped_state',
                'selector9_not_device_specific', 'reset'}
    if (result.get('passed') is not True or result.get('hardware_tested') is not False
            or result.get('rx_observer_abi') != 3 or not required.issubset(result.get('cases', []))):
        raise ValueError('coherent PN-pair/real checker simulation has not passed')
    rows = result.get('source_sha256', {})
    files = ['library/common/' + n for n in
             ('up_xfer_cntrl.v', 'up_xfer_status.v', 'up_adc_channel.v', 'ad_pnmon.v')]
    files += ['library/axi_ad9361/' + n for n in
              ('rf_rx_pn_pair_observer.v', 'axi_ad9361_rx_pnmon.v')]
    for p in [vendor / rel for rel in files] + [ROOT / 'pl/tb/tb_rf_rx_pn_pair.sv']:
        if str(p.resolve()) not in rows:
            raise ValueError('PN-pair simulation does not bind the selected sources')
    for name, expected in rows.items():
        if digest(Path(name)) != expected:
            raise ValueError('PN-pair simulation source changed: ' + name)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build", type=Path, required=True)
    parser.add_argument("--vivado", type=Path, required=True)
    parser.add_argument("--rx-observer", action="store_true", help="build the read-only diagnostic observer variant")
    parser.add_argument("--observer-sim-result", type=Path)
    parser.add_argument("--iddr-alignment-sim-result", type=Path)
    parser.add_argument("--pn-pair-sim-result", type=Path)
    args = parser.parse_args()
    build = args.build.resolve()
    if build.exists() or not build.is_relative_to(ROOT / "build") or build == ROOT / "build":
        raise ValueError("Choose a fresh candidate directory beneath repository build/")
    if os.environ.get("MIMO_ZEDBOARD_PART", "xc7z020clg484-1") != "xc7z020clg484-1":
        raise ValueError("Formal RF release is locked to xc7z020clg484-1")
    continuous = os.environ.get("MIMO_RF_CONTINUOUS", "0")
    if continuous not in ("0", "1"):
        raise ValueError("MIMO_RF_CONTINUOUS must be 0 or 1")
    required_env = ("ADI_HDL_DIR", "MIMO_RF_EXTERNAL_BUDGET_XDC", "MIMO_RF_SPI_BUDGET_XDC")
    for name in required_env:
        if not os.environ.get(name):
            raise ValueError(f"Set the reviewed {name} input before launch")
    features = resolve_build_features(os.environ)
    hdmi = "1" if features["enable_hdmi"] else "0"
    paths = [ROOT / name for name in RECIPES]
    if hdmi == "1":
        paths += [ROOT / "pl/constraints/zedboard_hdmi_adv7511.xdc"]
    paths += [ROOT / f"pl/rtl/{name}.sv" for name in RTL]
    paths += [ROOT / f"pl/rtl/{name}.v" for name in (
        "mimo_detector_bd", "rf_static_clkdiv", "rf_static_clkdiv4", "rf_lvds_forward_clock",
        "rf_rx_elastic_fifo", "rf_rx_dma_enable_monitor")]
    paths += [Path(os.environ[name]).resolve() for name in required_env[1:]]
    vendor = Path(os.environ["ADI_HDL_DIR"]).resolve()
    vendor_manifest_path = vendor.parent / "source-manifest.json"
    vendor_manifest = json.loads(vendor_manifest_path.read_text())
    alignment_trial = vendor_manifest.get("rx_iddr_alignment_trial")
    if alignment_trial not in (None, "same_edge_pipelined"):
        raise ValueError("unreviewed RX IDDR alignment variant")
    if alignment_trial:
        if args.iddr_alignment_sim_result is None:
            raise ValueError("IDDR alignment trial requires primitive simulation")
        alignment_abi = vendor_manifest.get("rx_observer_abi")
        if args.rx_observer:
            if alignment_abi not in (2, 3):
                raise ValueError("observer IDDR alignment requires ABI2/3")
        elif vendor_manifest.get("diagnostic_only") is True or alignment_abi not in (None, 0):
            raise ValueError("plain IDDR alignment requires a non-diagnostic baseline tree")
        verify_iddr_alignment_simulation(
            args.iddr_alignment_sim_result, vendor, alignment_abi or 0
        )
        paths += [args.iddr_alignment_sim_result.resolve(), vendor_manifest_path.resolve()]
        paths += [ROOT / name for name in ("pl/scripts/prepare_rx_iddr_alignment_vendor.py",
            "pl/scripts/prepare_rx_iddr_alignment_plain_vendor.py",
            "pl/scripts/run_rx_iddr_alignment_sim.py", "pl/tb/tb_rf_rx_iddr_alignment.sv")]
    elif args.iddr_alignment_sim_result is not None:
        raise ValueError("IDDR simulation supplied for a baseline vendor tree")
    if bool(vendor_manifest.get("diagnostic_only")) != args.rx_observer:
        raise ValueError("Diagnostic vendor and explicit --rx-observer selection must agree")
    if args.rx_observer:
        observer_abi = vendor_manifest.get("rx_observer_abi")
        if observer_abi not in (1, 2, 3) or args.observer_sim_result is None:
            raise ValueError("Observer requires a supported ABI and a completed simulation result")
        simulation = json.loads(args.observer_sim_result.read_text())
        if simulation.get("passed") is not True or simulation.get("hardware_tested") is not False:
            raise ValueError("Observer simulation has not passed")
        if simulation.get("rx_observer_abi", 1) != observer_abi:
            raise ValueError("Observer simulation ABI differs from build ABI")
        for name, expected in simulation["source_sha256"].items():
            if digest(Path(name)) != expected:
                raise ValueError("Observer simulation source changed: " + name)
        paths += [vendor_manifest_path, args.observer_sim_result.resolve()]
        paths += [ROOT / name for name in ("pl/scripts/prepare_rx_observer_vendor.py",
            "pl/scripts/run_rx_observer_sim.py", "pl/tb/tb_rf_rx_observer.sv",
            "pl/tb/tb_rf_rx_frame_window.sv", "pl/rtl/rf_rx_frame_window_observer.v",
            "pl/rtl/rf_rx_channel_observer.v", "pl/rtl/rf_rx_frame_observer.v")]
        if observer_abi == 3:
            if not alignment_trial or args.pn_pair_sim_result is None:
                raise ValueError('ABI3 requires aligned receiver and PN-pair simulation')
            verify_pn_pair_simulation(args.pn_pair_sim_result, vendor)
            paths += [args.pn_pair_sim_result.resolve()]
            paths += [ROOT / name for name in ('pl/scripts/prepare_rx_pn_pair_vendor.py',
                'pl/scripts/run_rx_pn_pair_sim.py', 'pl/tb/tb_rf_rx_pn_pair.sv',
                'pl/rtl/rf_rx_pn_pair_observer.v')]
        elif args.pn_pair_sim_result is not None:
            raise ValueError('PN-pair simulation supplied for another ABI')
    elif args.pn_pair_sim_result is not None:
        raise ValueError('PN-pair simulation requires diagnostic observer')
    paths += [vendor / name for name in ("library/axi_dmac/axi_dmac_ip.tcl",
        "library/axi_dmac/address_generator.v", "library/util_pack/util_cpack2/util_cpack2_impl.v")]
    for path in paths:
        if not path.is_file() or not path.is_relative_to(ROOT):
            raise ValueError(f"Bootstrap input must be an existing repository source: {path}")
    rows = {path.relative_to(ROOT).as_posix(): digest(path) for path in paths}
    build.mkdir(parents=True)
    receipt = {"status": "RECIPES_FROZEN_BEFORE_VIVADO", "hardware_tested": False,
               "part": "xc7z020clg484-1", "source_sha256": rows,
               "hdmi_enabled": hdmi == "1", "enable_hdmi": features["enable_hdmi"],
               "display_mode": features["display_mode"],
               "continuous": continuous == "1",
               "rx_iddr_alignment_trial": alignment_trial,
               "diagnostic_only": args.rx_observer, "rx_observer_abi": observer_abi if args.rx_observer else 0}
    (build / "rf_bootstrap_inputs.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    env = os.environ.copy()
    env["ENABLE_HDMI"] = hdmi
    env["DISPLAY_MODE"] = str(features["display_mode"])
    env["MIMO_RF_BUILD_PYTHON"] = sys.executable
    env["MIMO_ZEDBOARD_PART"] = "xc7z020clg484-1"
    env["MIMO_RF_RX_OBSERVER"] = "1" if args.rx_observer else "0"
    env["MIMO_RF_RX_OBSERVER_ABI"] = str(observer_abi if args.rx_observer else 0)
    wrapper = (
        "pl/scripts/create_zedboard_rf_continuous_system.tcl"
        if continuous == "1"
        else "pl/scripts/create_zedboard_rf_buffered_lvds_system.tcl"
    )
    command = [str(args.vivado), "-mode", "batch", "-source",
               str(ROOT / wrapper),
               "-tclargs", str(build), "implement"]
    with (build / "vivado_build.log").open("w", encoding="utf-8") as log:
        result = subprocess.run(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, check=False)
    print("RF_RELEASE_BUILD_EXIT", result.returncode, build)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
