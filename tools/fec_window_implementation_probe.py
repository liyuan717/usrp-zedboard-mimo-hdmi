#!/usr/bin/env python3
"""Measure the actual incremental fixed8 window-seal logic; no whole-board cost credit."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

from crc_fec_pipeline_probe import SOURCES
from fec_inverse_cost_probe import routed_measurements
from fec_window_rewrite import transform
from redundancy_diagnostic_audit import cost
from workspace_storage_guard import check, GIB

NAME = 'link_fec_ingress_cost_probe.sv'


def validate_window_replacement(name, baseline, current, proof_path):
    assert name == NAME and proof_path is not None, 'unproved FEC window replacement'
    p = json.loads(proof_path.read_text())
    digest = hashlib.sha256(current.read_bytes()).hexdigest()
    assert p['baseline_sha256'] == baseline and p['optimized_sha256'] == digest
    assert p['exact_seal_only_transform_verified'] and p['functional_positive']['all16_partial_blocks_continue']
    checks = p['closure_checks']
    assert checks['wrap']['base'] == 0xfffffff8 and checks['wrap']['all16_partial_blocks_continue']
    controls = checks['controls']
    assert controls['positive_source_sha256'] == digest and len(controls['controls']) == 3
    assert all(row['compiled'] and row['rejected'] for row in controls['controls'])
    assert checks['CRC_FEC_regression']['source_sha256'][NAME] == digest
    assert 'exact_bytes=54912 restored_bytes=12288' in checks['CRC_FEC_regression']['simulation']
    assert checks['capacity']['source_sha256'][NAME] == digest
    assert 'queued_banks=8 blocked_declarations=16 rejected_ninth=1 resumed_ninth_bytes=512 exact_bytes=82432' in checks['capacity']['simulation']
    assert p['optimized_routed_OOC']['WNS_ns'] >= 0 and p['optimized_routed_OOC']['WHS_ns'] >= 0
    return {'name': name, 'baseline_sha256': baseline, 'compiled_sha256': digest,
            'proof': str(proof_path), 'same_RS20_32_S512_and_eight_banks': True,
            'range_change': 'CRC-good replacement admission seals out-of-range loading jobs; normal partial output/terminal/release, no direct live overwrite'}


def main(a):
    v = Path(__file__).resolve().parents[1]
    out = a.output.resolve()
    if a.review_only:
        receipt = json.loads((out / 'receipt.json').read_text())
        assert transform(a.baseline.read_text()) == (v/'sim/g0'/NAME).read_text()
        wrap = json.loads((a.wrap_positive/'receipt.json').read_text())
        controls = json.loads((a.controls/'receipt.json').read_text())
        regression = json.loads((a.regression/'receipt.json').read_text())
        capacity = json.loads((a.capacity/'receipt.json').read_text())
        for proof in (wrap, regression, capacity):
            assert proof['source_sha256'] == receipt['functional_positive']['source_sha256']
        receipt['closure_checks'] = {'wrap': wrap, 'controls': controls, 'CRC_FEC_regression': regression, 'capacity': capacity}
        # Revalidate resource reports against the actual completed pair.
        assert receipt['baseline_synthesis'] == cost(out/'baseline-synth.rpt')
        assert receipt['optimized_synthesis'] == cost(out/'optimized-synth.rpt')
        assert receipt['optimized_routed_OOC'] == routed_measurements(out)
        path = out/'receipt.json'
        path.write_text(json.dumps(receipt, indent=2)+'\n')
        validate_window_replacement(NAME, receipt['baseline_sha256'], v/'sim/g0'/NAME, path)
        print('FEC_WINDOW_IMPLEMENTATION_AND_CLOSURE_CHECKS_PASS', flush=True)
        return
    out.mkdir(parents=True, exist_ok=False)
    before = check('before-FEC-window-paired-implementation')
    (out / 'disk-before.json').write_text(json.dumps(before, indent=2) + '\n')
    assert before['disk']['available_bytes'] >= 5 * GIB
    try:
        baseline = a.baseline.resolve()
        optimized = v / 'sim/g0' / NAME
        assert transform(baseline.read_text()) == optimized.read_text()
        numeric = json.loads((a.positive / 'receipt.json').read_text())
        assert numeric['all16_partial_blocks_continue']
        assert numeric['source_sha256'][NAME] == hashlib.sha256(optimized.read_bytes()).hexdigest()
        for n in ('generator.hex', 'inverse.hex'):
            shutil.copyfile(a.reference / n, out / n)
        tcl = 'set_param general.maxThreads 2\n'
        for label in ('baseline', 'optimized'):
            folder = out / label
            folder.mkdir()
            for name in SOURCES:
                src = baseline if label == 'baseline' and name == NAME else v / 'sim/g0' / name
                shutil.copyfile(src, folder / name)
            tcl += 'create_project -in_memory -part xc7z020clg484-1\n'
            for name in SOURCES:
                tcl += 'read_verilog -sv {' + str(folder / name) + '}\n'
            tcl += '''synth_design -top link_fec_ingress_cost_probe -part xc7z020clg484-1 -mode out_of_context -flatten_hierarchy rebuilt
create_clock -name processing -period 20 [get_ports clk]
set_input_delay 2 -clock processing [get_ports -filter {DIRECTION == IN && NAME != clk}]
set_output_delay 2 -clock processing [all_outputs]
opt_design
'''
            tcl += f'report_utilization -file {{{out/(label+"-synth.rpt")}}}\n'
            if label == 'optimized':
                tcl += '''place_design
route_design
report_utilization -file utilization.rpt
report_utilization -hierarchical -file hierarchy.rpt
report_timing_summary -file timing.rpt -report_unconstrained
report_drc -file drc.rpt
write_checkpoint -force fec_window.dcp
'''
            tcl += 'close_project\n'
        (out / 'paired.tcl').write_text(tcl)
        with (out / 'ooc.log').open('w') as log:
            subprocess.run([str(a.vivado_bin.resolve() / 'vivado'), '-mode', 'batch', '-source',
                            str(out / 'paired.tcl'), '-nolog', '-nojournal'], cwd=out,
                           stdout=log, stderr=subprocess.STDOUT, check=True)
        old, new = cost(out / 'baseline-synth.rpt'), cost(out / 'optimized-synth.rpt')
        receipt = {'scope': __doc__, 'baseline_sha256': hashlib.sha256(baseline.read_bytes()).hexdigest(),
                   'optimized_sha256': hashlib.sha256(optimized.read_bytes()).hexdigest(),
                   'exact_seal_only_transform_verified': True, 'functional_positive': numeric,
                   'baseline_synthesis': old, 'optimized_synthesis': new,
                   'same_flow_incremental_synthesis_cost': {k: new[k] - old[k] for k in old},
                   'optimized_routed_OOC': routed_measurements(out),
                   'whole_board_resources_not_updated_from_island_sum': True,
                   'whole_board_or_physical_RF_G0_passed': False}
        (out / 'receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
        print(json.dumps(receipt), flush=True)
    finally:
        (out / 'disk-after.json').write_text(json.dumps(check('after-FEC-window-paired-implementation'), indent=2) + '\n')


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('output', 'reference', 'baseline', 'positive', 'vivado-bin'):
        p.add_argument('--' + name, type=Path, required=True)
    p.add_argument('--review-only', action='store_true')
    for name in ('wrap-positive', 'controls', 'regression', 'capacity'):
        p.add_argument('--'+name, type=Path)
    main(p.parse_args())
