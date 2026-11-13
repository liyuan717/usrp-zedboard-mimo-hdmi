#!/usr/bin/env python3
"""Finite actual-source calibrated signed16 differential-candidate fixtures.

No packet decoder, saved/golden CFO, H or peaks are inputs. Full capture CRC is
verified. Search begins at calibration commit+1600, preserving all no-hit starts.
A selected first candidate is not a fine-training acceptance claim.
"""
import argparse
import json
from pathlib import Path
import zlib
import numpy as np
from g0_causal_acquisition import calibrated_input, differential_scores
from g0_packet_probe import PERIOD, public_prefix
from g0_fixed_differential_model import quantize_adc, direct_scores, candidates, self_test


def float_candidates(y, origin, arm):
    scores, co, _ = differential_scores(y, public_prefix()[256:384, 0])
    search = max(0, arm - origin); result = []
    while search < scores.shape[1]:
        hits = np.flatnonzero(np.max(scores[:, search:], axis=0) > .20)
        if not len(hits):
            break
        first = search + int(hits[0]); end = first + 512
        if end > scores.shape[1]:
            break
        sense, offset = np.unravel_index(np.argmax(scores[:, first:end]), (2, 512))
        k = first + int(offset); rx = int(np.argmax(abs(co[sense, k])))
        raw_cfo = float(np.angle(co[sense, k, rx]) * 4_000_000 / (2 * np.pi * 4))
        result.append({'first_hit_index': origin + first, 'window_end_exclusive': origin + end,
                       'selected_start_index': origin + k, 'sense': int(sense), 'selected_rx': rx,
                       'score': float(scores[sense, k]),
                       'coarse_CFO_hz': -raw_cfo if sense else raw_cfo,
                       'fine_training_validated': False})
        search = end
    return result


def generate(capture, out, report_only=False):
    dtype = np.dtype([('tag', '<u8'), ('iq', '<i2', (4,))])
    raw = np.memmap(capture, dtype=dtype, mode='r')
    crc = f'{zlib.crc32(raw):08x}'
    expected = json.loads((capture.parent / 'receipt.json').read_text())['capture']['crc32']
    if crc != expected:
        raise ValueError('capture CRC differs from capture receipt')
    arm = PERIOD * 4 + 2048 + 1600
    origin = arm - 4
    # Grow the observations without consulting a previously decoded peak.
    stop = min(len(raw), arm + 65536 + 2048)
    attempts = []
    while True:
        iq = np.asarray(raw['iq'][:stop], dtype=np.float64)
        samples = iq[:, 0::2] + 1j * iq[:, 1::2]
        y, calibration = calibrated_input(samples, PERIOD)
        adc, err = quantize_adc(y[origin:])
        c, e, n = direct_scores(adc)
        integer = candidates(c, e, n, origin=origin, arm_start=arm)
        floating = float_candidates(y[origin:], origin, arm)
        attempts.append({'prefix_end': stop, 'integer_complete_candidates': len(integer),
                         'floating_complete_candidates': len(floating)})
        if integer and floating:
            break
        if stop == len(raw):
            raise ValueError('no complete integer and floating candidate in capture')
        stop = min(len(raw), stop + 65536)
    observed_integer, observed_floating = integer, floating
    required = max(integer[0]['last_required_ADC_index'], floating[0]['window_end_exclusive'] - 1 + 508)
    stop = required + 1 + 160
    adc, err = quantize_adc(y[origin:stop])
    c, e, n = direct_scores(adc)
    integer = candidates(c, e, n, origin=origin, arm_start=arm)
    floating = float_candidates(y[origin:stop], origin, arm)
    tags = np.asarray(raw['tag'][origin:stop], dtype=np.uint64)
    if np.any(np.diff(tags) != 1):
        raise ValueError('source tag gap in finite fixture')
    if not integer or not floating:
        raise AssertionError('finite prefix lost first candidate')
    first, old = integer[0], floating[0]
    same = all(first[k] == old[k] for k in ['first_hit_index', 'selected_start_index', 'sense', 'selected_rx'])
    name = capture.parent.name
    # Large C/E arrays are deliberately not saved: the RTL checker recomputes
    # its own direct oracle from these finite ADC input vectors.
    if not report_only:
        np.savez_compressed(out / (name + '.npz'), adc=adc, adc_tag=tags)
    metadata = {'schema': 'g0-differential-fixture-v1', 'name': name,
                'scope': 'calibrated signed16 ADC to first exact differential candidate; not fine-CFO acceptance',
                'source': {'capture': str(capture.resolve()), 'crc32': crc, 'record_count': len(raw),
                           'capture_slice': [origin, stop], 'calibration': calibration,
                           'preprocessing': 'bounded floating mean/notch, then nearest ties-even signed16; no IQ sense applied',
                           'not_unchanged_raw_ADC': True, 'quantization_max_error': err,
                           'expected_payload_CFO_H_or_passed_peak_input': False},
                'input': {'length': len(adc), 'lane_order': ['RX0I', 'RX0Q', 'RX1I', 'RX1Q'],
                          'bits': 16, 'maximum_absolute': int(np.max(abs(adc.astype(np.int64)))),
                          'first_tag_u64': int(tags[0]), 'arm_start_capture_index': arm,
                          'arm_start_tag_u64': int(tags[4]),
                          'first_complete_candidate_endpoint_ordinal': 508,
                          'first_armed_candidate_endpoint_ordinal': 512,
                          'tail_samples_after_first_window_dependency': 160,
                          'no_hit_prefix_candidates': first['first_hit_index'] - arm,
                          'search_prefix_preserved': True, 'npz_written': not report_only},
                'coordinates': {'z': 'z[k]=conj(adc[k])*adc[k+4], endpointtag=adc_tag[k+4]',
                                'correlation': 'C[k] uses z[k+4*j],j0..126; outputtag=adc_tag[k]; rightendpoint=k+508',
                                'RTL_tag': 'epoch16 preserved, low48 arithmetic modulo2^48; no epoch carry',
                                'C_order': 'sense0 RX0 I,Q RX1 I,Q; sense1 RX0 I,Q RX1 I,Q',
                                'arm_is_not_golden_alignment': 'calibration_commit+1600; every earlier no-hit start is included'},
                'integer_candidates': integer, 'floating_candidates': floating,
                'pretrim_search_candidates': {
                    'observation_prefix_end': attempts[-1]['prefix_end'],
                    'integer': observed_integer, 'floating': observed_floating,
                    'purpose': 'all completed candidate windows encountered during bounded prefix growth; not fine-CFO acceptance or fixture alignment input'},
                'comparison': {'first_candidate_identity_equal': same,
                               'coarse_CFO_difference_hz': first['phase']['sense_corrected_CFO_hz'] - old['coarse_CFO_hz'],
                               'score_difference': first['score_float_diagnostic'] - old['score']},
                'prefix_search_attempts': attempts,
                'limits': ['software calibration/notch remains floating', 'no fine-training accept/retry implemented here',
                           'software known index is a data/budget marker, not measured estimator service',
                           'no NCO dispatch, timing estimator, header, payload, RF or full G0 claim']}
    (out / (name + '.json')).write_text(json.dumps(metadata, indent=2) + '\n')
    print(json.dumps({'name': name, 'length': len(adc), 'first_hit': first['first_hit_index'],
                      'selected': first['selected_start_index'], 'same_identity': same,
                      'CFO_delta_hz': metadata['comparison']['coarse_CFO_difference_hz']}), flush=True)
    return metadata


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--capture', type=Path, action='append', required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--report-only', action='store_true', help='write compact JSON only when storage is constrained')
    a = p.parse_args(); out = a.output.resolve(); out.mkdir(parents=True, exist_ok=False)
    receipt = {'scope': 'offline integer differential candidates, not complete acquisition/G0',
               'arithmetic': self_test(), 'fixtures': [], 'g0_passed': False}
    (out / 'receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
    for capture in a.capture:
        metadata = generate(capture.resolve(), out, a.report_only)
        receipt['fixtures'].append({'name': metadata['name'], 'metadata': metadata['name'] + '.json',
                                    'vectors': None if a.report_only else metadata['name'] + '.npz',
                                    'input_records': metadata['input']['length'],
                                    'comparison': metadata['comparison']})
        (out / 'receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')


if __name__ == '__main__':
    main()
