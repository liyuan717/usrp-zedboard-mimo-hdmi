#!/usr/bin/env python3
"""Opt-in integer coarse acquisition inside the existing hybrid packet model.

Default receive is unchanged. Both sides use integer NCO/RRC, Q32/Q16-F1 and
integer448 training. Only candidate coarse acquisition changes. Source/golden
reports are consulted AFTER decoding; additional CRC erasures fail even when
FEC hides them. This does not measure RTL admission/service or pass full G0.
"""
import argparse
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import sys
import traceback
import zlib
import numpy as np
from check_g0_causal_model import bounds
from check_g0_fixed_interpolation import accepted_packets, compare_source, same_causal_configuration
from check_g0_packet_model import simulate
from g0_fixed_differential_model import FixedDifferentialSelector, coarse_nco_configuration, self_test
from g0_fixed_frontend_model import fixed_frontend
from g0_fixed_interpolation_model import fixed_sampler
from g0_fixed_training_model import FixedTrainingEstimator
from g0_packet_probe import receive


def write(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n')


def decode(y, integer):
    estimator = FixedTrainingEstimator()
    report, blocks = receive(y, causal=True, frontend=fixed_frontend, sampler=fixed_sampler,
        channel_estimator=estimator, differential_selector=FixedDifferentialSelector if integer else None)
    report['training_candidate_calls'] = estimator.calls
    return report, blocks


def phase_configuration_controls():
    def nearest(x):
        floor=x.numerator//x.denominator
        return floor+int(x-floor>=Fraction(1,2))
    mask=(1<<48)-1; count=0
    for raw in [0,1,2,3,0x7fffffff,0x80000000,0xfffffffd,0xfffffffe,0xffffffff]:
        for sense in [0,1]:
            signed=raw if raw<1<<31 else raw-(1<<32)
            directed=signed if sense else -signed
            for known,origin in [(0,0),(1,0),(2,0),(3,0),(158444,0),(0,mask),(1,mask-1),(0,1),(mask,0)]:
                config=coarse_nco_configuration(raw,sense,known,origin)
                offset=(known-origin)&mask
                assert config['step_u32']==nearest(Fraction(directed,4))&0xffffffff
                assert config['phase_u32']==nearest(Fraction(directed*offset,4))&0xffffffff
                assert config['offset_u48']==offset and config['phase_origin_index48']==origin
                count+=1
    return {'independent_Fraction_phase_step_controls':count,'both_senses_signed_halfturn_ties_and_48bit_origin_wrap':True,
            'phase_rounded_once_after_raw_product':True}


def main(a):
    out = a.output.resolve(); out.mkdir(parents=True, exist_ok=False)
    rows, failures = [], []
    receipt = {'scope': 'integer differential coarse selection in hybrid receiver; fine CFO/calibration/timing remain float',
               'status': 'started', 'command': sys.argv, 'cases': rows, 'failed_cases': failures,
               'historical_reference': str(a.reference.resolve()), 'new_RF_transmissions': False, 'g0_passed': False}
    write(out / 'receipt.json', receipt)
    receipt['arithmetic'] = self_test()
    receipt['arithmetic'].update(phase_configuration_controls())
    files = ['g0_causal_acquisition.py', 'g0_packet_probe.py', 'g0_fixed_differential_model.py',
             'g0_fixed_frontend_model.py', 'g0_fixed_interpolation_model.py', 'g0_fixed_training_model.py',
             'check_g0_fixed_differential.py']
    tools = Path(__file__).resolve().parent
    receipt['source_snapshot'] = {name: {'bytes': (tools / name).stat().st_size,
        'sha256': hashlib.sha256((tools / name).read_bytes()).hexdigest()} for name in files}
    write(out / 'receipt.json', receipt)

    def record(row):
        rows.append(row)
        if not row['passed']: failures.append(row['name'])
        receipt['status'] = 'running'; write(out / 'receipt.json', receipt)
        print(json.dumps(row), flush=True)

    def check(name, y, crc=None):
        try:
            baseline, oldbytes = decode(y, False)
            write(out / (name + '-baseline.json'), baseline)
            candidate, newbytes = decode(y, True)
            write(out / (name + '-fixed_differential.json'), candidate)
            # From this point onward references may be used for validation.
            bounds(baseline); bounds(candidate)
            oldequal, newequal = compare_source(oldbytes), compare_source(newbytes)
            oldgood, newgood = accepted_packets(baseline), accepted_packets(candidate)
            saved = json.loads((a.reference / (name + '-fixed_training.json')).read_text())
            historic = accepted_packets(saved)
            same_baseline = historic == oldgood
            same_blocks = set(oldbytes) == set(newbytes) and all(oldbytes[k] == newbytes[k] for k in oldbytes)
            lost, gained = sorted(oldgood - newgood), sorted(newgood - oldgood)
            oldstart, newstart = baseline['startup'], candidate['startup']
            dispatch = candidate['differential_candidate_dispatch_checks']
            assert len(dispatch) == len(candidate['differential_candidate_calls'])
            assert all(row['bit_exact'] and row['actual_step_u32'] == row['expected_step_u32'] and
                       row['actual_phase_u32'] == row['expected_phase_u32'] and row['explicit_integer_configuration'] for row in dispatch)
            row = {'name': name, 'capture_crc32': crc, 'samples': len(y),
                   'baseline_CRC_good_packets': len(oldgood), 'candidate_CRC_good_packets': len(newgood),
                   'historical_CRC_good_packets': len(historic), 'historical_baseline_CRC_set_equal': same_baseline,
                   'expected_packets': candidate['expected_packets_in_observed_range'],
                   'additional_erased_packet_sequences': lost, 'newly_accepted_packet_sequences': gained,
                   'same_CRC_set_as_baseline': oldgood == newgood,
                   'same_delivered_blocks': same_blocks, 'baseline_source_bytes_equal': oldequal,
                   'source_bytes_equal': newequal, 'recovered_blocks': len(newbytes),
                   'eligible_blocks': candidate['eligible_complete_FEC_blocks'],
                   'coarse_peak_unchanged': oldstart['coarse_peak_sample'] == newstart['coarse_peak_sample'],
                   'coarse_CFO_delta_hz': newstart['coarse_CFO_hz'] - oldstart['coarse_CFO_hz'],
                   'raw_phase_CFO_delta_hz': dispatch[-1]['raw_phase_CFO_hz'] - oldstart['coarse_CFO_hz'],
                   'applied_coarse_CFO_hz': dispatch[-1]['applied_CFO_hz'],
                   'coarse_NCO_step_u32': dispatch[-1]['actual_step_u32'],
                   'coarse_NCO_phase_u32': dispatch[-1]['actual_phase_u32'],
                   'coarse_NCO_phase_origin_index48': dispatch[-1]['phase_origin_index48'],
                   'coarse_NCO_phase_offset_u48': dispatch[-1]['phase_offset_u48'],
                   'coarse_NCO_phase_rounded_once_after_raw_product': True,
                   'coarse_NCO_descriptor_step_and_phase_bit_exact': True,
                   'fine_CFO_delta_hz': newstart['fine_CFO_correction_hz'] - oldstart['fine_CFO_correction_hz'],
                   'final_NCO_CFO_delta_hz': candidate['tracking_NCO_CFO_hz'] - baseline['tracking_NCO_CFO_hz'],
                   'startup_training_symbols': newstart['training_symbols_used'],
                   'candidate_windows_tried': len(candidate['differential_candidate_calls']),
                   'passed': not lost and same_blocks and same_baseline and len(newbytes) == candidate['eligible_complete_FEC_blocks']}
            record(row)
            return candidate, newbytes
        except Exception as error:
            write(out / (name + '-error.json'), {'exception': repr(error), 'traceback': traceback.format_exc()})
            record({'name': name, 'capture_crc32': crc, 'passed': False, 'error': repr(error)})
            return None, None

    def prefix_check(y, reference, expected_blocks):
        name = 'prefix_and_corrupted_future'
        try:
            cut = 800000; prefix = y[:cut]; changed = y.copy(); rng = np.random.default_rng(323241)
            rms = float(np.sqrt(np.mean(abs(prefix) ** 2)))
            changed[cut:] = rng.normal(0, 3*rms, changed[cut:].shape) + 1j*rng.normal(0, 3*rms, changed[cut:].shape)
            r0, b0 = decode(prefix, True); write(out / 'prefix-fixed_differential.json', r0)
            r1, b1 = decode(changed, True); write(out / 'corrupted_future-fixed_differential.json', r1)
            bounds(r0); bounds(r1)
            assert all(expected_blocks.get(k) == value and b1.get(k) == value for k, value in b0.items()), 'future changed source bytes'
            for variant in (r0, r1):
                same_causal_configuration(reference, variant, cut)
                assert variant['differential_candidate_calls'] == reference['differential_candidate_calls'], 'future changed differential candidates'
                assert variant['differential_candidate_dispatch_checks'] == reference['differential_candidate_dispatch_checks'], 'future changed coarse NCO descriptor dispatch'
            past = {f['superframe']: f['packets'] for f in r0['frames'] if f['PHY_header_ok']}
            for variant in (reference, r1):
                actual = {f['superframe']: f['packets'] for f in variant['frames'] if f['PHY_header_ok']}
                assert all(actual.get(k) == value for k, value in past.items()), 'future changed past CRC decisions'
            for i, call in enumerate(r0['training_candidate_calls']):
                for tx in range(2):
                    for key in ('selected_rx', 'symbol_step_s32', 'H_raw_s26', 'energy_u50', 'branch_invalid'):
                        assert call['tx'][tx][key] == r1['training_candidate_calls'][i]['tx'][tx][key] == reference['training_candidate_calls'][i]['tx'][tx][key], 'future changed integer training'
            record({'name': name, 'prefix_samples': cut, 'source_bytes_equal': compare_source(b0),
                    'differential_NCO_sampling_training_and_past_CRC_unchanged': True, 'passed': True})
        except Exception as error:
            write(out / (name + '-error.json'), {'exception': repr(error), 'traceback': traceback.format_exc()})
            record({'name': name, 'passed': False, 'error': repr(error)})

    for name, reverse in [('model_clean', False), ('model_reversed_IQ', True)]:
        if not a.case or name in a.case:
            check(name, 4096 * simulate(a.waveform, reverse=reverse, count_frames=8))
    for ordinal, capture in enumerate(a.capture):
        name = capture.parent.name
        if a.case and name not in a.case: continue
        if capture.stat().st_size % 16: raise ValueError('truncated ADC record')
        raw = np.fromfile(capture, dtype=np.dtype([('tag', '<u8'), ('iq', '<i2', (4,))]))
        crc = f'{zlib.crc32(raw):08x}'
        if crc != json.loads((capture.parent / 'receipt.json').read_text())['capture']['crc32']:
            raise ValueError('capture CRC differs')
        if np.any(np.diff(raw['tag']) != 1): raise ValueError('ADC tag gap')
        y = raw['iq'][:, 0::2] + 1j * raw['iq'][:, 1::2]
        report, blocks = check(name, y, crc)
        if ordinal == 0 and report is not None and not a.skip_prefix:
            prefix_check(y, report, blocks)
    receipt['status'] = 'failed' if failures else 'passed'
    receipt['complete_fixed_PHY_or_RTL_admission_verified'] = False
    write(out / 'receipt.json', receipt)
    if failures: raise SystemExit('saved failed cases: ' + ', '.join(failures))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--waveform', type=Path, required=True)
    parser.add_argument('--capture', type=Path, action='append', default=[])
    parser.add_argument('--reference', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--case', action='append', help='select a subset for a retained diagnostic rerun')
    parser.add_argument('--skip-prefix', action='store_true', help='omit separate prefix/future controls for targeted diagnostics')
    main(parser.parse_args())
