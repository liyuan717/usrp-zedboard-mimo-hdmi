#!/usr/bin/env python3
"""Compile actual seal/remembership/ownership faults against the16-block progress oracle."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

from crc_fec_pipeline_probe import SOURCES
from fec_window_retirement_probe import TB
from workspace_storage_guard import check, GIB


def main(a):
    v = Path(__file__).resolve().parents[1]
    out = a.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    before = check('before-FEC-window-controls')
    (out / 'disk-before.json').write_text(json.dumps(before, indent=2) + '\n')
    assert before['disk']['available_bytes'] >= 5 * GIB
    name = 'link_fec_ingress_cost_probe.sv'
    source = (v / 'sim/g0' / name).read_text()
    faults = [
        ('disconnect_seal', '.seal_valid(seal_valid),', ".seal_valid(1'b0),", ['window progress census']),
        ('seal_current_range', 'retire_offset>={28\'d0,range_count}', 'retire_offset<{28\'d0,range_count}',
         ['window terminal partial-source contract', 'window missing early source']),
        ('release_owner_before_terminal', "   retire_bank<=retire_bank+1'b1;",
         "   retire_bank<=retire_bank+1'b1;\n   if(seal_valid)bank_live[retire_bank]<=0;",
         ['window held source/owner lost']),
    ]
    rows = []
    try:
        for label, old, new, witnesses in faults:
            assert source.count(old) == 1, label
            folder = out / label
            folder.mkdir()
            for n in SOURCES:
                shutil.copyfile(v / 'sim/g0' / n, folder / n)
            (folder / name).write_text(source.replace(old, new))
            for n in ('generator.hex', 'inverse.hex', 'bytes.hex', 'source.hex'):
                shutil.copyfile(a.reference / n, folder / n)
            (folder / 'tb.sv').write_text(TB.replace('@EXPECTED@', '16').replace('@BASE@', '00000000'))
            bp = a.vivado_bin.resolve()
            for step, args in [('compile', [bp/'xvlog', '--sv', *SOURCES, 'tb.sv']),
                               ('elaborate', [bp/'xelab', 'tb', '-s', 'fault_tb', '-mt', 'off']),
                               ('simulate', [bp/'xsim', 'fault_tb', '-runall'])]:
                with (folder / (step + '.log')).open('w') as log:
                    proc = subprocess.run([str(x) for x in args], cwd=folder, stdout=log,
                                          stderr=subprocess.STDOUT, check=step != 'simulate')
            log = (folder / 'simulate.log').read_text()
            witness = next((line for line in log.splitlines() if line.startswith('Fatal:') and any(w in line for w in witnesses)), None)
            assert witness and 'timeout' not in witness and 'G0_FEC_WINDOW_CENSUS' not in log, log[-2000:]
            rows.append({'fault': label, 'compiled': True, 'rejected': True, 'witness': witness,
                         'mutant_sha256': hashlib.sha256((folder/name).read_bytes()).hexdigest()})
        result = {'scope': __doc__, 'positive_source_sha256': hashlib.sha256(source.encode()).hexdigest(),
                  'controls': rows, 'full_G0_passed': False}
        (out / 'receipt.json').write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps(result), flush=True)
    finally:
        (out / 'disk-after.json').write_text(json.dumps(check('after-FEC-window-controls'), indent=2) + '\n')


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('output', 'reference', 'vivado-bin'):
        p.add_argument('--' + name, type=Path, required=True)
    main(p.parse_args())
