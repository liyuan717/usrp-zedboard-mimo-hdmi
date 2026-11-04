"""Derive ABI3 coherent I/Q and adjacent PN-state snapshots from aligned ABI2."""
import argparse
import json
from pathlib import Path
import shutil

from prepare_rx_observer_vendor import ROOT, COMMIT, sha, replace


def prepare(source, output):
    if output.exists():
        raise ValueError('choose a fresh PN-pair vendor directory')
    manifest = json.loads((source.parent / 'source-manifest.json').read_text())
    if (manifest.get('source_commit') != COMMIT or manifest.get('derived_for') != 'lvds_2r2t_2v5'
            or manifest.get('diagnostic_only') is not True or manifest.get('rx_observer_abi') != 2
            or manifest.get('rx_iddr_alignment_trial') != 'same_edge_pipelined'):
        raise ValueError('requires the reviewed aligned ABI2 tree')
    for row in manifest['files']:
        p = source / row['path']
        if p.is_symlink() or not p.resolve().is_relative_to(source.resolve()) or sha(p) != row['sha256']:
            raise ValueError('baseline manifest mismatch: ' + row['path'])
    folder = 'library/axi_ad9361/'
    edits = {}
    rel = folder + 'axi_ad9361_rx_channel.v'
    text = (source / rel).read_text()
    old = '''  rf_rx_channel_observer i_rx_observer (
    .adc_clk(adc_clk), .adc_rst(adc_rst), .adc_valid(adc_valid),
    .adc_data(adc_data), .up_clk(up_clk), .up_rstn(up_rstn),
    .snapshot_up(rx_observer_snapshot_up));'''
    new = '''  rf_rx_pn_pair_observer #(.Q_OR_I_N(Q_OR_I_N),
    .PAIR_STATES(CHANNEL_ID % 2)) i_rx_observer (
    .adc_clk(adc_clk), .adc_rst(adc_rst), .adc_valid(adc_valid),
    .adc_data(adc_data), .adc_data_q(adc_data_q), .pn_sel(adc_pnseq_sel_s),
    .up_clk(up_clk), .up_rstn(up_rstn),
    .snapshot_up(rx_observer_snapshot_up));'''
    # Insert after the PN select declaration; avoid implicit forward wire nets.
    text = replace(text, old + '\n\n', '')
    edits[rel] = replace(text, '  wire    [ 3:0]  adc_pnseq_sel_s;',
                         '  wire    [ 3:0]  adc_pnseq_sel_s;\n' + new)
    rel = folder + 'rf_rx_frame_window_observer.v'
    edits[rel] = replace((source / rel).read_text(), "{8'hd8, observation}", "{8'hd9, observation}")
    rel = folder + 'axi_ad9361_ip.tcl'
    anchor = '  "axi_ad9361_rx_pnmon.v" ' + chr(92) + '\n'
    edits[rel] = replace((source / rel).read_text(), anchor,
                       '  "rf_rx_pn_pair_observer.v" ' + chr(92) + '\n' + anchor)
    shutil.copytree(source, output)
    files = {x['path']: x['sha256'] for x in manifest['files']}
    patches = {}
    for rel, text in edits.items():
        (output / rel).write_text(text, encoding='utf-8', newline='\n')
        patches[rel] = dict(before_sha256=files[rel], after_sha256=sha(output / rel))
        files[rel] = sha(output / rel)
    rel = folder + 'rf_rx_pn_pair_observer.v'
    shutil.copyfile(ROOT / 'pl/rtl/rf_rx_pn_pair_observer.v', output / rel)
    files[rel] = sha(output / rel)
    manifest.update(source_root=str(output.resolve()), rx_observer_abi=3,
                    pn_pair_preparation_sha256=sha(Path(__file__)), pn_pair_patch_files=patches,
                    files=[dict(path=k, sha256=v) for k, v in sorted(files.items())])
    (output.parent / 'source-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    return manifest


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    r = prepare(a.source.resolve(), a.output.resolve())
    print(json.dumps(dict(abi=r['rx_observer_abi'], patches=r['pn_pair_patch_files'])))
