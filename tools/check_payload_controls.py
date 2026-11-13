#!/usr/bin/env python3
"""Compile numeric and ownership mutations against passed payload snapshots."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

from workspace_storage_guard import check as disk_check, GIB


def main(a):
    project = Path(__file__).resolve().parents[1]
    out = a.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    before = disk_check('before-payload-controls')
    (out / 'disk-before.json').write_text(json.dumps(before, indent=2) + '\n')
    if before['disk']['available_bytes'] < 5 * GIB:
        raise SystemExit('Under5GiB: handle storage first.')
    try:
        references = {'numeric': a.numeric.resolve(), 'datapath': a.datapath.resolve()}
        hashes = {}
        for mode, ref in references.items():
            receipt = json.loads((ref / 'receipt.json').read_text())
            assert receipt['simulation'].startswith('G0_PAYLOAD_' + mode.upper() + '_PASS')
            hashes[mode] = receipt['source_sha256']
            for name, expected in hashes[mode].items():
                assert hashlib.sha256((ref / name).read_bytes()).hexdigest() == expected
                assert hashlib.sha256((project / 'sim/g0' / name).read_bytes()).hexdigest() == expected
        # Each witness requires a successful compile/elaboration and a specific
        # arithmetic/protocol assertion. A compiler failure is never a witness.
        changes = [
            ('TX0_offset_one', 'numeric', 'payload_phase_cost_probe.sv',
             'if(state==PRIME1)begin floor_phase[0]<=next0;',
             'if(state==PRIME1)begin floor_phase[0]<=floor_phase[0];',
             'Fatal: exact Fraction phase boundary'),
            ('negative_division_truncates', 'numeric', 'payload_phase_cost_probe.sv',
             'increment[tx]<=-$signed(next_quotient)-42\'sd1;',
             'increment[tx]<=-$signed(next_quotient);',
             'Fatal: exact Fraction phase boundary'),
            ('last_group_extrapolated', 'numeric', 'payload_phase_cost_probe.sv',
             "delta[0]<=cfg_group==273?42'sd0:", 'delta[0]<=',
             'Fatal: exact Fraction phase boundary'),
            ('H_limb_shift_16', 'numeric', 'payload_rotate_cost_probe.sv',
             'product1})<<<17)', 'product1})<<<16)',
             'Fatal: full H26 rotation boundary'),
            ('square_cross_shift_17', 'numeric', 'payload_ml_cost_probe.sv',
             'lh})<<<18)', 'lh})<<<17)',
             'Fatal: wide ML all-candidate metric oracle'),
            ('ML_tie_overwrites_first', 'numeric', 'payload_ml_cost_probe.sv',
             'if(metric_candidate==0 || metric<best_metric)',
             'if(metric_candidate==0 || metric<=best_metric)',
             'Fatal: wide ML boundary result'),
            ('sample_identity_ignored', 'datapath', 'payload_datapath_cost_probe.sv',
             "(identity_bad?3'd3:in_error)", 'in_error',
             'Fatal: payload result identity/error'),
            ('held_cancel_not_immediate', 'datapath', 'payload_ml_cost_probe.sv',
             'assign out_valid=valid && !rst && !cancel;',
             'assign out_valid=valid && !rst;',
             'Fatal: payload cancel edge'),
        ]
        controls = []
        for label, mode, target, old, new, witness in changes:
            ref = references[mode]
            d = out / label
            d.mkdir()
            for p in ref.glob('*.hex'):
                os.link(p, d / p.name)
            for name in hashes[mode]:
                shutil.copyfile(ref / name, d / name)
            shutil.copyfile(ref / 'tb.sv', d / 'tb.sv')
            p = d / target
            source = p.read_text()
            assert source.count(old) == 1, (label, old)
            p.write_text(source.replace(old, new))

            def run(step, args):
                with (d / (step + '.log')).open('w') as f:
                    subprocess.run(args, cwd=d, stdout=f, stderr=subprocess.STDOUT, check=True)

            run('compile', [str(a.vivado_bin / 'xvlog'), '--sv',
                            *[str(d / n) for n in hashes[mode]], str(d / 'tb.sv')])
            run('elaborate', [str(a.vivado_bin / 'xelab'), 'tb', '-s', 'payload_control_tb', '-mt', 'off'])
            try:
                run('simulate', [str(a.vivado_bin / 'xsim'), 'payload_control_tb', '-runall'])
            except subprocess.CalledProcessError:
                pass
            log = (d / 'simulate.log').read_text()
            assert witness in log and 'G0_PAYLOAD_NUMERIC_PASS' not in log and 'G0_PAYLOAD_DATAPATH_PASS' not in log, (label, log[-3000:])
            row = {'name': label, 'reference': mode,
                   'detected_by': next(s for s in log.splitlines() if s.startswith(witness))}
            controls.append(row)
            print(json.dumps(row), flush=True)
        result = {'scope': __doc__, 'references': {k: str(v.relative_to(project)) for k, v in references.items()},
                  'source_sha256': hashes, 'negative_controls': controls, 'g0_passed': False}
        (out / 'receipt.json').write_text(json.dumps(result, indent=2) + '\n')
    finally:
        (out / 'disk-after.json').write_text(json.dumps(disk_check('after-payload-controls'), indent=2) + '\n')


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('output', 'numeric', 'datapath', 'vivado-bin'):
        p.add_argument('--' + name, type=Path, required=True)
    main(p.parse_args())
