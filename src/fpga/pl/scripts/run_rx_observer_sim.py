"""Simulate real ADI held-data CDC and the patched CPU read mux with RX observers."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--vendor", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--vivado-bin", type=Path, default=Path("/vivado/2025.2/Vivado/bin"))
    a = p.parse_args()
    a.vendor = a.vendor.resolve()
    manifest = json.loads((a.vendor.parent / "source-manifest.json").read_text())
    abi = manifest.get("rx_observer_abi")
    if not manifest.get("diagnostic_only") or abi not in (1, 2, 3):
        raise ValueError("requires the staged observer tree")
    files = [a.vendor / "library/common/up_xfer_cntrl.v", a.vendor / "library/common/up_xfer_status.v",
             a.vendor / "library/common/up_adc_channel.v"]
    frame_rtl = "rf_rx_frame_window_observer.v" if abi in (2, 3) else "rf_rx_frame_observer.v"
    files += [a.vendor / "library/axi_ad9361" / n for n in ("rf_rx_channel_observer.v", frame_rtl)]
    if abi in (2, 3):
        files.append(a.vendor / "library/axi_ad9361/xilinx/axi_ad9361_lvds_if.v")
    expected = {x["path"]: x["sha256"] for x in manifest["files"]}
    for f in files:
        if hashlib.sha256(f.read_bytes()).hexdigest() != expected[f.relative_to(a.vendor).as_posix()]:
            raise ValueError("simulation source differs from staged build: " + str(f))
    top = "tb_rf_rx_frame_window" if abi in (2, 3) else "tb_rf_rx_observer"
    bench = ROOT / "pl/tb" / (top + ".sv")
    files.append(bench)
    a.output = a.output.resolve();a.output.mkdir(parents=True, exist_ok=False)
    compile_files = list(files)
    if abi in (2, 3):
        original = files[-2].read_text()
        begin = original.index('  // intel-equivalence\n')
        end = original.index('  // transfer to common clock\n', begin)
        helper = a.output / "rx_delineation_under_test.v"
        helper.write_text('''module rx_delineation_under_test (
 input l_clk, input [1:0] rx_frame_s, input [5:0] rx_data_1_s, rx_data_0_s,
 input rx_r1_mode, rx_locked,
 output reg adc_valid_p=0, output reg adc_status_p=0,
 output reg [47:0] adc_data_p=0
);
 reg [1:0] rx_frame=0;
 reg [5:0] rx_data_1=0, rx_data_0=0;
 wire rx_error;
''' + original[begin:end] + '\nendmodule\n')
        # Compile the exact extracted receiver logic, not its unisim wrappers.
        compile_files = [f for f in files if f.suffix != '.v' or f.name != 'axi_ad9361_lvds_if.v']
        compile_files.append(helper)
    commands = [[str(a.vivado_bin / "xvlog"), "--sv", *(["--define", "RX_OBSERVER_ABI3"] if abi == 3 else []), *map(str, compile_files)],
                [str(a.vivado_bin / "xelab"), top, "-s", "rx_observer"],
                [str(a.vivado_bin / "xsim"), "rx_observer", "--runall"]]
    outputs = []
    for i, cmd in enumerate(commands):
        r = subprocess.run(cmd, cwd=a.output, capture_output=True, text=True)
        output = r.stdout + r.stderr;outputs.append(output)
        (a.output / f"step-{i}.txt").write_text(output)
        if r.returncode:
            raise RuntimeError(f"simulation step {i} failed: " + output[-3500:])
    combined = "\n".join(outputs)
    if combined.count("RF_RX_OBSERVER_SIM_PASS") != 1 or "Fatal:" in combined:
        raise RuntimeError("missing unique simulation PASS")
    result = {"passed": True, "hardware_tested": False,
              "rx_observer_abi": abi,
              "cases": ["invalid_input", "coherent_snapshot_and_cpu_read", "saturation", "reset"],
              "source_sha256": {str(f): hashlib.sha256(f.read_bytes()).hexdigest() for f in files}}
    if abi in (2, 3):
        result["cases"] += ["frame_window_data_coherence", "real_2r2t_delineation", "alternating_frame_no_valid", "r1_mode_snapshot"]
    (a.output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print("RF_RX_OBSERVER_SIM_PASS")


if __name__ == "__main__":
    main()
