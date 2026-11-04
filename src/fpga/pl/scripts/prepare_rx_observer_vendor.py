"""Add a read-only RX observer to a NEW, fully hash-checked derived ADI tree."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[2]
COMMIT = "4366bd63c1fe56b680b69145981a4493c6f8753a"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def replace(text, old, new):
    if text.count(old) != 1:
        raise ValueError("observer patch context changed: " + old[:70])
    return text.replace(old, new, 1)


def prepare(source, output, frame_window=False):
    if output.exists():
        raise ValueError("choose a fresh observer vendor directory")
    manifest = json.loads((source.parent / "source-manifest.json").read_text())
    if manifest.get("source_commit") != COMMIT or manifest.get("derived_for") != "lvds_2r2t_2v5":
        raise ValueError("requires the reviewed derived LVDS source")
    # Validate every declared file before copying. No packaged/generated tree.
    for entry in manifest["files"]:
        p = source / entry["path"]
        if p.is_symlink() or not p.resolve().is_relative_to(source.resolve()) or sha(p) != entry["sha256"]:
            raise ValueError("source manifest mismatch: " + str(p))
    edits = {}
    frame_rtl = "rf_rx_frame_window_observer.v" if frame_window else "rf_rx_frame_observer.v"
    observer_rtl = ("rf_rx_channel_observer.v", frame_rtl)
    def edit(rel, transform):
        p = output / rel
        before = sha(p)
        p.write_text(transform(p.read_text()), encoding="utf-8", newline="\n")
        edits[rel] = {"before_sha256": before, "after_sha256": sha(p)}
    shutil.copytree(source, output)
    try:
        folder = "library/axi_ad9361/"
        for name in observer_rtl:
            shutil.copyfile(ROOT / "pl/rtl" / name, output / folder / name)
        anchor = '  "axi_ad9361_rx_pnmon.v" ' + chr(92) + '\n'
        inserted = ''.join('  "' + name + '" ' + chr(92) + '\n' for name in observer_rtl)
        edit(folder + "axi_ad9361_ip.tcl", lambda t: replace(t, anchor, inserted + anchor))
        def channel(t):
            t = replace(t, '  wire    [11:0]  adc_data_s;',
                '  wire [31:0] rx_observer_snapshot_up;\n'
                '  rf_rx_channel_observer i_rx_observer (\n'
                '    .adc_clk(adc_clk), .adc_rst(adc_rst), .adc_valid(adc_valid),\n'
                '    .adc_data(adc_data), .up_clk(up_clk), .up_rstn(up_rstn),\n'
                '    .snapshot_up(rx_observer_snapshot_up));\n\n'
                '  wire    [11:0]  adc_data_s;')
            t = replace(t, '    .CHANNEL_ID (CHANNEL_ID),\n    .USERPORTS_DISABLE',
                '    .CHANNEL_ID (CHANNEL_ID),\n    .DEBUG_READ_DATA_ENABLE (1),\n    .USERPORTS_DISABLE')
            return replace(t, "    .adc_read_data ('d0),", "    .adc_read_data ('d0),\n    .up_debug_read_data (rx_observer_snapshot_up),")
        edit(folder + "axi_ad9361_rx_channel.v", channel)
        def adc_channel(t):
            t = replace(t, "  parameter   CHANNEL_ID = 4'h0,", "  parameter   CHANNEL_ID = 4'h0,\n  parameter   DEBUG_READ_DATA_ENABLE = 0,")
            t = replace(t, '  input   [31:0]  adc_read_data,', '  input   [31:0]  adc_read_data,\n  input   [31:0]  up_debug_read_data,')
            return replace(t, "4'h2: up_rdata_int <= { up_adc_read_data_s};", "4'h2: up_rdata_int <= DEBUG_READ_DATA_ENABLE ? up_debug_read_data : up_adc_read_data_s;")
        edit("library/common/up_adc_channel.v", adc_channel)
        def lvds(t):
            t = replace(t, '  output              up_drp_locked\n);', '  output [31:0]       rx_observer_up,\n  output              up_drp_locked\n);')
            extra = ('    .data_rise(rx_data_1_s), .data_fall(rx_data_0_s), .r1_mode(rx_r1_mode),\n'
                     if frame_window else '')
            return replace(t, '  // r1mode\n',
                '  ' + frame_rtl[:-2] + ' i_rx_frame_observer (\n'
                '    .adc_clk(l_clk), .adc_rst(rst), .frame(rx_frame_s),\n'
                + extra + '    .adc_valid(adc_valid_p), .adc_status(adc_status_p),\n'
                '    .delay_locked(rx_locked), .up_clk(up_clk), .up_rstn(up_rstn),\n'
                '    .snapshot_up(rx_observer_up));\n\n  // r1mode\n')
        edit(folder + "xilinx/axi_ad9361_lvds_if.v", lvds)
        def top(t):
            t = replace(t, '  wire            adc_status_s;', '  wire [31:0]     rx_observer_up_s;\n  wire            adc_status_s;')
            anchor = '  if (CMOS_OR_LVDS_N == 1) begin\n'
            t = replace(t, anchor, anchor + "  assign rx_observer_up_s = 32'd0;\n")
            # Only the LVDS instance has the differential receive clock pins.
            t = replace(t, '    .rx_clk_in_p (rx_clk_in_p),', '    .rx_observer_up (rx_observer_up_s),\n    .rx_clk_in_p (rx_clk_in_p),')
            return replace(t, '    .up_adc_gpio_in (up_adc_gpio_in),', '    .up_adc_gpio_in (rx_observer_up_s),')
        edit(folder + "axi_ad9361.v", top)
    except Exception:
        shutil.rmtree(output)
        raise
    files = {x["path"]: x["sha256"] for x in manifest["files"]}
    for rel in edits:
        files[rel] = sha(output / rel)
    for name in observer_rtl:
        files[folder + name] = sha(output / folder / name)
    manifest.update(source_root=str(output.resolve()), diagnostic_only=True,
                    rx_observer_abi=2 if frame_window else 1, observer_patch_files=edits,
                    observer_preparation_sha256=sha(Path(__file__)),
                    files=[{"path": k, "sha256": v} for k, v in sorted(files.items())])
    (output.parent / "source-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--frame-window", action="store_true", help="select ABI 2 rolling frame/DDR snapshots")
    a = p.parse_args()
    r = prepare(a.source.resolve(), a.output.resolve(), a.frame_window)
    print(json.dumps({"diagnostic_only": True, "files": len(r["files"]), "patched": r["observer_patch_files"]}))


if __name__ == "__main__":
    main()
