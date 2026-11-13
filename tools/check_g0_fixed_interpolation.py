#!/usr/bin/env python3
"""Check Q32/Q16 software sampling after the integer G0 NCO/RRC islands.

This is numerical replay, not bounded-buffer/estimator RTL service proof.
Expected source bytes are consulted only after receive() returns. Regressions
are saved and reported, including extra CRC erasures even if FEC hides them.
"""
import argparse
from fractions import Fraction
from functools import partial
import json
from pathlib import Path
import zlib
import numpy as np
from check_g0_causal_model import bounds
from check_g0_packet_model import simulate
from g0_fixed_frontend_model import fixed_frontend
from g0_fixed_interpolation_model import (MIN20, MAX20, Q16, Q32,
    exact_signed20_lanes, fixed_sampler, interpolate_lanes, nearest_q,
    q32_to_q16, quantized_positions)
from g0_packet_fec import source_pattern
from g0_packet_probe import DATA_START, PERIOD, PREAMBLE_START, receive


def write(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n')


def nearest_fraction(value):
    """Independent rational oracle: choose between floor and ceil, ties upward."""
    lower = value.numerator // value.denominator
    return lower + int(value - lower >= Fraction(1, 2))


def must_reject(function, *args):
    try:
        function(*args)
    except ValueError:
        return
    raise AssertionError('invalid arithmetic/interface input was accepted')


def arithmetic_checks():
    rng = np.random.default_rng(902016)
    rails = [MIN20, MIN20 + 1, -1, 0, 1, MAX20 - 1, MAX20]
    tuples = [(a, b, u) for a in rails for b in rails for u in [0, 1, 32767, 32768, 32769, 65535]]
    tuples += [(int(a), int(b), int(u)) for a, b, u in zip(
        rng.integers(MIN20, MAX20 + 1, 8192), rng.integers(MIN20, MAX20 + 1, 8192),
        rng.integers(0, Q16, 8192))]
    x0 = np.repeat(np.array([t[0] for t in tuples], dtype=np.int64)[:, None], 4, axis=1)
    x1 = np.repeat(np.array([t[1] for t in tuples], dtype=np.int64)[:, None], 4, axis=1)
    weight = np.array([t[2] for t in tuples], dtype=np.int64)
    for fraction_bits in range(3):
        scale = 1 << fraction_bits
        got = interpolate_lanes(x0, x1, weight, fraction_bits)
        oracle = np.array([nearest_fraction(Fraction((a * (Q16 - u) + b * u) * scale, Q16))
                           for a, b, u in tuples], dtype=np.int64)
        assert np.array_equal(got, np.repeat(oracle[:, None], 4, axis=1))
        assert np.all(got >= np.minimum(x0, x1) * scale) and np.all(got <= np.maximum(x0, x1) * scale)
    # Explicit positive/negative halves distinguish floor(x+.5) from ties-even
    # and from signed truncation. Four components also have different slopes.
    for fraction_bits in range(3):
        scale = 1 << fraction_bits
        halves = interpolate_lanes(np.array([[0, 0, -1, 1]]), np.array([[1, -1, 0, 0]]),
                                   np.array([32768 // scale]), fraction_bits)
        assert halves.tolist() == [[1, 0, 1 - scale, scale]]
    assert np.array_equal(interpolate_lanes(x0, x1, weight), interpolate_lanes(x0, x1, weight, 1))
    assert nearest_q(-.5, 0) == 0 and nearest_q(.5, 0) == 1
    for value in [4, 4 + .49 / Q32, 4 - .49 / Q32, 4.0008, 3.9992, 1234567.123456789]:
        assert nearest_q(value, 32) == nearest_fraction(Fraction.from_float(value) * Q32)
    positions_q32 = np.array([-Q32 - 32768, -32769, -32768, -1, 0, 32767, 32768,
                             Q32 - 32769, Q32 - 32768, Q32 - 1, Q32, 5 * Q32 - 1], dtype=np.int64)
    index, u = q32_to_q16(positions_q32)
    q16_oracle = np.array([nearest_fraction(Fraction(int(x), Q16)) for x in positions_q32])
    assert np.array_equal(index * Q16 + u, q16_oracle)
    q16_weight_error = int(abs(q16_oracle * Q16 - positions_q32).max()) / Q32
    assert q16_weight_error <= 1 / (2 * Q16)
    assert index[8] == 1 and u[8] == 0 and index[11] == 5 and u[11] == 0
    # Independent continuous-value bound: Q16 weight rounding plus signed20
    # final rounding. Carry maps to the exact right endpoint, never wraps to x0.
    worst = [Fraction(0)] * 3
    for _ in range(4096):
        a, b = [int(v) for v in rng.integers(MIN20, MAX20 + 1, 2)]
        fraction32 = int(rng.integers(0, Q32))
        rounded = nearest_fraction(Fraction(fraction32, Q16))
        used_a, used_b, used_u = (b, b, 0) if rounded == Q16 else (a, b, rounded)
        ideal = Fraction(a) + Fraction((b - a) * fraction32, Q32)
        for fraction_bits in range(3):
            scale = 1 << fraction_bits
            actual = int(interpolate_lanes(np.full((1, 4), used_a), np.full((1, 4), used_b),
                                           np.array([used_u]), fraction_bits)[0, 0])
            error = abs(Fraction(actual, scale) - ideal)
            assert error <= Fraction(abs(b - a), 2 * Q16) + Fraction(1, 2 * scale)
            worst[fraction_bits] = max(worst[fraction_bits], error)
    peak, step = 1234567.123456789, 4 + .49 / Q32
    _, _, _, generated, info = quantized_positions(peak, step, PERIOD, PREAMBLE_START)
    anchor, increment = nearest_fraction(Fraction.from_float(peak) * Q32), nearest_fraction(Fraction.from_float(step) * Q32)
    assert all(int(generated[k]) == anchor + (k - PREAMBLE_START) * increment for k in range(PERIOD))
    worst_q32 = max(abs(Fraction(int(generated[k]), Q32) - (Fraction.from_float(peak)
        + (k - PREAMBLE_START) * Fraction.from_float(step))) for k in [0, PERIOD - 1])
    assert worst_q32 <= Fraction(1 + max(PREAMBLE_START, PERIOD - 1 - PREAMBLE_START), 2 * Q32)
    # Long-running step error is separate from the one-time Q16 weight error.
    drift_step = 4 + .499999 / Q16
    drift32 = abs(Fraction(nearest_q(drift_step, 32), Q32) - Fraction.from_float(drift_step)) * 20000
    drift16 = abs(Fraction(nearest_q(drift_step, 16), Q16) - Fraction.from_float(drift_step)) * 20000
    assert drift32 <= Fraction(20000, 2 * Q32) and drift16 > Fraction(15, 100)
    exact = np.array([[MIN20 / 2 + 1j * MAX20 / 2, -.5 + 1j * .5]])
    assert exact_signed20_lanes(exact).tolist() == [[MIN20, MAX20, -1, 1]]
    must_reject(exact_signed20_lanes, np.array([[.25 + 0j, 0j]]))
    must_reject(exact_signed20_lanes, np.array([[np.nan + 0j, 0j]]))
    must_reject(exact_signed20_lanes, np.array([[(MAX20 + 1) / 2 + 0j, 0j]]))
    must_reject(interpolate_lanes, np.array([[MAX20 + 1] * 4]), np.zeros((1, 4), dtype=int), np.array([0]))
    must_reject(interpolate_lanes, np.zeros((1, 4), dtype=int), np.zeros((1, 4), dtype=int), np.array([Q16]))
    must_reject(fixed_sampler, np.zeros((9, 2), dtype=complex), 8., 4., 1, 0)
    return {
        'signed20_Q16_rational_oracle_cases_per_output_format': len(tuples), 'components_per_case': 4,
        'tested_output_fraction_bits': [0, 1, 2], 'default_output_bits': 21, 'default_output_scale': 4,
        'full_range_rails_half_rounding_u0_u65535_passed': True,
        'Q16_fraction_carry_and_negative_floor_passed': True,
        'Q16_weight_max_error_from_Q32_samples': q16_weight_error,
        'Q16_weight_error_bound_samples': 1 / (2 * Q16),
        'continuous_Q32_to_Q16_error_trials': 4096,
        'error_unit': 'input signed20 RRC integer LSB; divide2 for original receiver units',
        'maximum_observed_interpolation_error_LSB_by_output_fraction_bits': [float(e) for e in worst],
        'full_signed20_error_bound_LSB_by_output_fraction_bits': [
            float(Fraction(MAX20 - MIN20, 2 * Q16) + Fraction(1, 2 << bits)) for bits in range(3)],
        'Q32_anchor_step_accumulation_symbols': PERIOD,
        'maximum_Q32_position_error_samples': float(worst_q32),
        'Q32_position_error_bound_samples': info['Q32_anchor_and_step_error_bound_samples'],
        'Q32_step_drift_20000_symbols_samples': float(drift32),
        'Q16_step_drift_same_input_20000_symbols_samples': float(drift16),
        'non_exact_scale2_input_rejected_without_rounding': True,
        'both_endpoints_required_even_zero_weight': True,
        'passed': True,
    }


class FixtureSampler:
    """Generate finite actual-RRC history; expected values also rational-checked."""
    def __init__(self, directory, provenance, output_fraction_bits=1):
        self.directory, self.provenance, self.written = directory, provenance, False
        self.output_fraction_bits = output_fraction_bits

    def __call__(self, mf, peak, step, period, preamble_start):
        result = fixed_sampler(mf, peak, step, period, preamble_start, self.output_fraction_bits)
        if not self.written:
            self.directory.mkdir(parents=True, exist_ok=False)
            index, weight, _, p32, _ = quantized_positions(peak, step, period, preamble_start)
            for name, begin in [('training_early', 416), ('training_late', 1344), ('data_start', DATA_START)]:
                chosen = np.arange(begin, begin + 16)
                requested, u = index[chosen], weight[chosen]
                start, stop = int(requested.min()) - 8, int(requested.max()) + 138
                tags = np.arange(start, stop, dtype=np.int64)
                lanes = exact_signed20_lanes(mf[start:stop])
                x0, x1 = lanes[requested - start], lanes[requested + 1 - start]
                scale = 1 << self.output_fraction_bits
                expected = interpolate_lanes(x0, x1, u, self.output_fraction_bits)
                oracle = [[nearest_fraction(Fraction((int(a) * (Q16 - int(w)) + int(b) * int(w)) * scale, Q16))
                           for a, b in zip(left, right)] for left, right, w in zip(x0, x1, u)]
                assert np.array_equal(expected, oracle)
                np.savez(self.directory / (name + '.npz'),
                    rrc_endpoint_tags=tags, rrc_center_tags=tags - 20, rrc_lanes=lanes,
                    request_endpoint=requested, request_center=requested - 20,
                    request_u16=u, request_q32=p32[chosen], expected=expected,
                    issue_after_endpoint=requested + 135)
                np.savetxt(self.directory / (name + '.txt'),
                    np.column_stack((requested, u, x0, x1, expected)), fmt='%d')
                write(self.directory / (name + '.json'), {
                    'source': 'first complete period of first CRC-verified actual ADC capture',
                    **self.provenance,
                    'frontend': 'integer NCO/RRC software model after floating calibration/notch',
                    'source_symbol_indices': chosen.tolist(), 'lane_order': ['RX0_I', 'RX0_Q', 'RX1_I', 'RX1_Q'],
                    'RRC_lane_format': 'signed20 at scale2; input/history width unchanged',
                    'output_fraction_bits': self.output_fraction_bits,
                    'output_bits': 20 + self.output_fraction_bits, 'output_scale': 2 * scale,
                    'output_adapter': f'final receiver divides expected integer by{2 * scale}',
                    'request_u16_format': 'unsigned Q16; both adjacent points required even at0',
                    'txt_columns': ['endpoint', 'u16'] + [f'x0_{k}' for k in range(4)]
                                   + [f'x1_{k}' for k in range(4)] + [f'expected_{k}' for k in range(4)],
                    'coordinate': 'capture-relative endpoint index; RTL center=endpoint-20',
                    'issue_after_endpoint': 'request_endpoint+135: proposed delayed-read fixture, NOT measured estimator latency',
                    'raw_vectors': len(tags), 'requests': len(chosen),
                    'expected_matches_independent_rational_oracle': True,
                    'bounded_buffer_or_RTL_service_verified': False, 'g0_passed': False,
                })
            self.written = True
        return result


def compare_source(blocks):
    if not blocks:
        raise ValueError('no restored block')
    for block, data in blocks.items():
        if data != source_pattern(block).tobytes():
            raise ValueError('post-receive source mismatch')
    return sum(len(data) for data in blocks.values())


def accepted_packets(report):
    return {packet['packet_seq'] for frame in report['frames'] if frame['PHY_header_ok']
            for packet in frame['packets'] if packet['accepted']}


def same_causal_configuration(reference, variant, cut):
    for key in ('coarse_peak_sample', 'coarse_known_sample', 'NCO_commit_sample'):
        assert variant['startup'][key] == reference['startup'][key], 'future changed startup boundary'
    assert len(variant['frontend_candidate_calls']) == len(reference['frontend_candidate_calls'])
    for a, b in zip(variant['frontend_candidate_calls'], reference['frontend_candidate_calls']):
        for key in ('commit_endpoint_sample', 'NCO_phase_u32', 'NCO_step_u32'):
            assert a[key] == b[key], 'future changed integer NCO configuration'
    if 'sampling_candidate_calls' in reference:
        earlier = {s['first_endpoint']: s for s in reference['sampling_candidate_calls'] if s['last_endpoint'] + 1 < cut}
        observed = {s['first_endpoint']: s for s in variant['sampling_candidate_calls'] if s['last_endpoint'] + 1 < cut}
        assert set(earlier) == set(observed), 'future changed or dropped a past sampling call'
        for sample in observed.values():
            other = earlier[sample['first_endpoint']]
            for key in ('peak_q32', 'step_q32', 'first_position_q32', 'last_position_q32'):
                assert sample[key] == other[key], 'future changed quantized timing configuration: ' + key


def main(a):
    out = a.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    rows = []
    arithmetic = arithmetic_checks()
    write(out / 'arithmetic.json', arithmetic)
    print(json.dumps({'name': 'independent_arithmetic', **arithmetic}), flush=True)
    failures = []
    sample = partial(fixed_sampler, output_fraction_bits=a.output_fraction_bits)

    def check(name, y, expected_failure=False, sampler=sample):
        baseline, basebytes = receive(y, causal=True, frontend=fixed_frontend)
        bounds(baseline)
        compare_source(basebytes)
        candidate, blocks = receive(y, causal=True, frontend=fixed_frontend, sampler=sampler)
        bounds(candidate)
        equal = compare_source(blocks)
        for mode, report in [('NCO_RRC', baseline), ('NCO_RRC_interpolation', candidate)]:
            write(out / f'{name}-{mode}.json', report)
        basegood, newgood = accepted_packets(baseline), accepted_packets(candidate)
        lost, gained = sorted(basegood - newgood), sorted(newgood - basegood)
        same_blocks = set(blocks) == set(basebytes) and all(blocks[k] == basebytes[k] for k in blocks)
        failure_control = bool(candidate['CRC_bad_PHY_headers'] and len(blocks) < candidate['eligible_complete_FEC_blocks'])
        complete = len(blocks) == candidate['eligible_complete_FEC_blocks']
        passed = same_blocks and not lost and (failure_control if expected_failure else complete)
        row = {'name': name, 'samples': len(y),
            'baseline_CRC_good_link_packets': baseline['CRC_good_link_packets'],
            'interpolation_CRC_good_link_packets': candidate['CRC_good_link_packets'],
            'baseline_CRC_bad_PHY_headers': baseline['CRC_bad_PHY_headers'],
            'interpolation_CRC_bad_PHY_headers': candidate['CRC_bad_PHY_headers'],
            'additional_erased_packet_sequences': lost, 'newly_accepted_packet_sequences': gained,
            'net_additional_link_erasures': candidate['link_erased_or_missing_in_observed_range']
                                           - baseline['link_erased_or_missing_in_observed_range'],
            'expected_packets': candidate['expected_packets_in_observed_range'],
            'eligible_blocks': candidate['eligible_complete_FEC_blocks'],
            'baseline_restored_blocks': len(basebytes), 'restored_blocks': len(blocks),
            'source_bytes_equal': equal, 'same_delivered_blocks_as_NCO_RRC': same_blocks,
            'expected_bad_header_failed_block_preserved': failure_control if expected_failure else None,
            'passed': passed}
        rows.append(row)
        if not passed:
            failures.append(name)
        print(json.dumps(row), flush=True)
        return baseline, basebytes, candidate, blocks

    for name, damage, reverse, sf in [('clean', None, False, None), ('reversed_IQ', None, True, None),
                                    ('burst', 'payload_burst', False, 4), ('bad_header', 'PHY_header', False, 5)]:
        y = 4096 * simulate(a.waveform, damage, reverse, count_frames=8, damage_superframe=sf)
        check('model_' + name, y, expected_failure=damage == 'PHY_header')
    for capture_index, path in enumerate(a.capture):
        if path.stat().st_size % 16:
            raise ValueError('truncated ADC record')
        raw = np.fromfile(path, dtype=np.dtype([('tag', '<u8'), ('iq', '<i2', (4,))]))
        crc = f'{zlib.crc32(raw):08x}'
        if crc != json.loads((path.parent / 'receipt.json').read_text())['capture']['crc32']:
            raise ValueError('capture CRC differs')
        if np.any(np.diff(raw['tag']) != 1):
            raise ValueError('ADC tag gap')
        y = raw['iq'][:, 0::2] + 1j * raw['iq'][:, 1::2]
        sampler = FixtureSampler(out / 'fixtures', {'capture_path': str(path.resolve()),
            'capture_crc32': crc, 'capture_samples': len(y)}, a.output_fraction_bits) if capture_index == 0 else sample
        base, basebytes, full, fullbytes = check(path.parent.name, y, sampler=sampler)
        rows[-1]['capture_crc32'] = crc
        if capture_index != 0:
            continue
        cut = 800000
        if len(y) <= cut:
            raise ValueError('prefix test requires a longer raw capture')
        prefix = y[:cut]
        changed = y.copy()
        rng = np.random.default_rng(323241)
        rms = float(np.sqrt(np.mean(abs(prefix) ** 2)))
        changed[cut:] = rng.normal(0, 3 * rms, changed[cut:].shape) + 1j * rng.normal(0, 3 * rms, changed[cut:].shape)
        for mode, sampler, full_report, full_blocks in [('NCO_RRC', None, base, basebytes),
                                                      ('NCO_RRC_interpolation', sample, full, fullbytes)]:
            r0, b0 = receive(prefix, causal=True, frontend=fixed_frontend, sampler=sampler)
            r1, b1 = receive(changed, causal=True, frontend=fixed_frontend, sampler=sampler)
            bounds(r0)
            bounds(r1)
            write(out / f'prefix-{mode}.json', r0)
            write(out / f'corrupted_future-{mode}.json', r1)
            errors = []
            try:
                assert all(full_blocks.get(k) == data and b1.get(k) == data for k, data in b0.items()), 'future changed decoded prefix'
                for report in (r0, r1):
                    same_causal_configuration(full_report, report, cut)
                past_frames = {f['superframe']: f['packets'] for f in r0['frames'] if f['PHY_header_ok']}
                for report in (full_report, r1):
                    frames = {f['superframe']: f['packets'] for f in report['frames'] if f['PHY_header_ok']}
                    assert all(frames.get(k) == value for k, value in past_frames.items()), 'future changed prefix CRC'
            except (AssertionError, KeyError) as error:
                errors.append(str(error))
            row = {'name': 'prefix_and_corrupted_future_' + mode, 'samples': cut,
                'source_bytes_equal': compare_source(b0), 'configuration_and_past_CRC_unchanged': not errors,
                'errors': errors, 'passed': not errors}
            rows.append(row)
            if errors:
                failures.append(row['name'])
            print(json.dumps(row), flush=True)
    receipt = {
        'scope': 'G0 integer NCO/RRC plus Q32 timing/Q16 final sampling software models; remaining PHY float',
        'arithmetic': arithmetic, 'cases': rows, 'failed_cases': failures,
        'timing_origin': 'quantized peak_q32/2^32; endpoint arrays, center tags require subtract20',
        'interpolation_input_bits': 20, 'output_fraction_bits': a.output_fraction_bits,
        'interpolation_output_bits': 20 + a.output_fraction_bits,
        'interpolation_output_scale': 2 << a.output_fraction_bits,
        'public_acquisition_interpolation_still_float': True,
        'bounded_history_and_estimator_RTL_service_verified': False,
        'full_fixed_point_PHY_or_connected_RTL_verified': False,
        'new_RF_transmissions': False, 'passed': not failures, 'g0_passed': False,
    }
    write(out / 'receipt.json', receipt)
    if failures:
        raise SystemExit('saved failures: ' + ', '.join(failures))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--waveform', type=Path, required=True)
    parser.add_argument('--capture', type=Path, action='append', default=[])
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--output-fraction-bits', type=int, choices=[0, 1, 2], default=1)
    main(parser.parse_args())
