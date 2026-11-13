#!/usr/bin/env python3
"""Finite actual-source NCO/RRC/Q32/F1/two-column training fixtures.

The signed16 input is AFTER the existing floating calibration/notch and IQ
sense correction, followed by its exact nearest-ties-even adapter. It is NOT
an unchanged raw ADC capture. CFO/timing come only from the existing causal
public receiver. Saved normal reports are compared only after receive returns.
No RTL service-time, full PHY, hardware, or RF acceptance is claimed here.
"""
import argparse
import json
import math
from pathlib import Path
import zlib

import numpy as np

from check_g0_fixed_interpolation import accepted_packets
from g0_fixed_frontend_model import fixed_frontend
from g0_fixed_interpolation_model import fixed_sampler, exact_signed20_lanes
from g0_fixed_training_model import FixedTrainingEstimator, estimate_training, training_signs
from g0_packet_probe import receive, public_prefix, HEADER_START, DATA_START
from g0_sounder import rrc

Q32 = 1 << 32
MASK32 = Q32 - 1
MASK48 = (1 << 48) - 1
MASK80 = (1 << 80) - 1


def write(path, data):
    path.write_text(json.dumps(data, indent=2) + '\n')


def nearest_binary_float(value, bits):
    """Independent exact nearest, ties toward +infinity for timing fields."""
    numerator, denominator = float(value).as_integer_ratio()
    lower, remainder = divmod(numerator << bits, denominator)
    return lower + int(2 * remainder >= denominator)


def nco_rrc_oracle(values, phase, step):
    """Full-circle scalar NCO and direct41-tap dot products, not lfilter."""
    cosine = [round(65536 * math.cos(2 * math.pi * b / 4096)) for b in range(4096)]
    sine = [round(65536 * math.sin(2 * math.pi * b / 4096)) for b in range(4096)]
    taps = [int(v) for v in np.rint(rrc() * 131072)]
    if len(taps) != 41:
        raise ValueError('fixture requires current41-tap RRC')
    mixed = []
    filtered = []
    for ordinal, raw in enumerate(values):
        bin_index = ((phase + (1 << 19)) & MASK32) >> 20
        co, si = cosine[bin_index], sine[bin_index]
        row = []
        for rx in (0, 1):
            i, q = int(raw[2 * rx]), int(raw[2 * rx + 1])
            row.extend(((i * co - q * si + 16384) // 32768,
                        (i * si + q * co + 16384) // 32768))
        mixed.append(row)
        if ordinal >= 40:
            accum = [sum(taps[k] * mixed[ordinal - k][lane] for k in range(41)) for lane in range(4)]
            filtered.append([(v + 65536) // 131072 for v in accum])
        phase = (phase + step) & MASK32
    mixed, filtered = np.array(mixed, dtype=np.int32), np.array(filtered, dtype=np.int32)
    assert np.all(mixed >= -(1 << 17)) and np.all(mixed < 1 << 17)
    assert np.all(filtered >= -(1 << 19)) and np.all(filtered < 1 << 19)
    return mixed, filtered


def interpolation_oracle(filtered, input_start, peak_q32, step_q32):
    """Closed-form coordinates and direct weighted average, independent F1."""
    endpoints = np.empty((2, 448), dtype=np.int64)
    weights = np.empty((2, 448), dtype=np.uint16)
    output = np.empty((2, 448, 4), dtype=np.int32)
    first_positions = []
    for tx, first_symbol in enumerate((416, 928)):
        first = peak_q32 + (first_symbol - 256) * step_q32
        first_positions.append(first)
        for k in range(448):
            coordinate_q16 = (first + k * step_q32 + 32768) // 65536
            endpoint, mu = divmod(coordinate_q16, 65536)
            endpoints[tx, k], weights[tx, k] = endpoint, mu
            relative = endpoint - input_start - 40
            if relative < 0 or relative + 1 >= len(filtered):
                raise ValueError('both interpolation endpoints must be within finite valid RRC history')
            a, b = filtered[relative], filtered[relative + 1]
            # Weighted sum differs from the implementation's difference-product.
            for lane in range(4):
                numerator = 2 * (int(a[lane]) * (65536 - mu) + int(b[lane]) * mu)
                output[tx, k, lane] = (numerator + 32768) // 65536
    assert np.all(output >= -(1 << 20)) and np.all(output < 1 << 20)
    return output, endpoints, weights, first_positions


def same(actual, expected, label):
    if not np.array_equal(actual, expected):
        raise AssertionError(label + ' differs')


def make_fixture(capture, output, reference_directory):
    if capture.stat().st_size % 16:
        raise ValueError('truncated raw ADC records')
    raw = np.memmap(capture, mode='r', dtype=[('tag', '<u8'), ('iq', '<i2', (4,))])
    capture_crc = f'{zlib.crc32(raw):08x}'
    transport = json.loads((capture.parent / 'receipt.json').read_text())
    if capture_crc != transport['capture']['crc32']:
        raise ValueError('capture does not match transport receipt')
    if np.any(np.diff(raw['tag']) != 1):
        raise ValueError('non-contiguous original capture is outside this fixture generator')
    samples = raw['iq'][:, 0::2] + 1j * raw['iq'][:, 1::2]
    final_frontend = {}
    first_sampler = {}

    def frontend(source, cfo, start, dc_block=False):
        if dc_block:
            raise ValueError('post-NCO floating DC blocker is excluded from this hardware fixture')
        result, info = fixed_frontend(source, cfo, start, False)
        final_frontend.clear()
        final_frontend.update(source=source, matched=result, info=info)
        return result, info

    def sampler(mf, peak, step, period, preamble_start):
        result, positions, info = fixed_sampler(mf, peak, step, period, preamble_start)
        if not first_sampler:
            first_sampler.update(peak=float(peak), step=float(step), info=info)
        return result, positions, info

    estimator = FixedTrainingEstimator(retain_vectors=True)
    # Only actual observations/public training enter receive. No source_pattern,
    # normal report, expected CFO, H, header, or decoded payload is an input.
    report, restored = receive(samples, dc_block=False, causal=True,
        frontend=frontend, sampler=sampler, channel_estimator=estimator)
    if not first_sampler or not estimator.vectors or estimator.calls[0].get('status') != 'valid':
        raise ValueError('first complete receiver period did not produce valid training')
    if len(estimator.vectors[0]) != 2:
        raise ValueError('first period must contain both TX training windows')
    peak, sample_step = first_sampler['peak'], first_sampler['step']
    peak_q32 = nearest_binary_float(peak, 32)
    timing_step = nearest_binary_float(sample_step, 32)
    assert peak_q32 == first_sampler['info']['peak_q32']
    assert timing_step == first_sampler['info']['step_q32']
    first_q32 = peak_q32 + 160 * timing_step
    # Keep live ADC/RRC through the public first-data coordinate and another
    #40 inputs. Training still issues only2x448 requests. This permits a joint
    #TB to compare matrix publication to header/data right-neighbor arrival
    #and inject a same-edge ADC gap while publication and input coexist.
    last_q32 = peak_q32 + (DATA_START - 256) * timing_step
    first_endpoint = (first_q32 + 32768) // Q32
    last_endpoint = (last_q32 + 32768) // Q32
    start, stop = first_endpoint - 40, last_endpoint + 2 + 40
    config = final_frontend['info']
    commit = config['commit_endpoint_sample']
    if not commit <= start < stop <= len(samples):
        raise ValueError('finite fixture must lie wholly in the final NCO epoch')
    phase = (config['NCO_phase_u32'] + (start - commit) * config['NCO_step_u32']) & MASK32
    values = final_frontend['source'][start:stop]
    lanes_float = np.column_stack((values[:, 0].real, values[:, 0].imag, values[:, 1].real, values[:, 1].imag))
    rounded = np.rint(lanes_float)
    if not np.all(np.isfinite(rounded)) or np.any(rounded < -32768) or np.any(rounded > 32767):
        raise ValueError('actual calibrated input outside signed16')
    input_iq = rounded.astype(np.int16)
    mixed, filtered = nco_rrc_oracle(input_iq, phase, config['NCO_step_u32'])
    normal_filtered = exact_signed20_lanes(final_frontend['matched'][start + 40:stop])
    same(filtered, normal_filtered, 'finite restarted RRC vs normal receiver')
    interpolated, endpoints, mu, first_positions = interpolation_oracle(filtered, start, peak_q32, timing_step)
    public = public_prefix()
    columns = []
    signs = []
    for tx in (0, 1):
        expected_normal = estimator.vectors[0][tx]
        same(interpolated[tx], expected_normal['lanes'], 'finite training samples vs normal receiver')
        _, public_signs = training_signs(public, tx)
        same(public_signs, expected_normal['signs'], 'public training signs')
        column = estimate_training(interpolated[tx], public_signs, tx)
        for key in ('symbol_step_s32', 'H_raw_s26', 'energy_u50', 'selected_rx', 'branch_invalid'):
            same(column[key], expected_normal['report'][key], 'finite training column ' + key)
        columns.append(column);signs.append(public_signs)

    # Comparison to the prior complete normal run happens strictly after decode.
    name = capture.parent.name
    reference = json.loads((reference_directory / (name + '-fixed_training.json')).read_text())
    same(accepted_packets(report), accepted_packets(reference), 'normal CRC acceptance set')
    same(report['startup']['NCO_commit_sample'], reference['startup']['NCO_commit_sample'], 'normal NCO commit')
    for key in ('NCO_phase_u32', 'NCO_step_u32', 'commit_endpoint_sample'):
        same(config[key], reference['frontend_candidate_calls'][-1][key], 'normal NCO ' + key)
    same(peak_q32, reference['sampling_candidate_calls'][0]['peak_q32'], 'normal Q32 peak')
    same(timing_step, reference['sampling_candidate_calls'][0]['step_q32'], 'normal Q32 step')
    for tx in (0, 1):
        prior = reference['training_candidate_calls'][0]['tx'][tx]
        for key in ('symbol_step_s32', 'H_raw_s26', 'energy_u50', 'selected_rx', 'branch_invalid'):
            same(columns[tx][key], prior[key], 'saved normal training ' + key)
    states = [state for state in report['timing_states'] if state['preamble_sample'] == peak]
    if len(states) != 1:
        raise ValueError('first complete sampler period has no unique causal timing state')
    timing_state = states[0]
    known_at = timing_state['known_at_sample']
    known_ordinal = known_at - start
    if not 40 <= known_ordinal < stop - start:
        raise ValueError('known-at dependency must occur inside this finite input segment')
    epoch = int(raw['tag'][start]) >> 48
    tag0_index = int(raw['tag'][0]) & MASK48
    tags = np.array([int(t) for t in raw['tag'][start:stop]], dtype=np.uint64)
    if np.any(tags >> np.uint64(48) != epoch):
        raise ValueError('this finite fixture must preserve one source epoch')
    rrc_tags = np.array([(epoch << 48) | ((int(tag) - 20) & MASK48) for tag in tags[40:]], dtype=np.uint64)
    request_tags = np.array([[(epoch << 48) | ((tag0_index + int(n) - 20) & MASK48) for n in row] for row in endpoints], dtype=np.uint64)
    first_absolute = [(tag0_index * Q32 + p - 20 * Q32) & MASK80 for p in first_positions]
    assert first_absolute[1] == (first_absolute[0] + 512 * timing_step) & MASK80
    position_words = np.array([[(p >> (32 * word)) & MASK32 for word in range(3)] for p in first_absolute], dtype=np.uint32)
    positions_relative = np.array([p - start * Q32 - 20 * Q32 for p in first_positions], dtype=np.int64)
    milestones = {}
    for label, symbol in (('HEADER_START', HEADER_START), ('DATA_START', DATA_START)):
        endpoint_position = peak_q32 + (symbol - 256) * timing_step
        endpoint_q16 = (endpoint_position + 32768) // 65536
        endpoint, weight = divmod(endpoint_q16, 65536)
        left_tag = (epoch << 48) | ((tag0_index + endpoint - 20) & MASK48)
        right_tag = (epoch << 48) | ((tag0_index + endpoint - 19) & MASK48)
        required_ordinal = endpoint + 1 - start
        assert 40 <= required_ordinal < stop - start
        same(rrc_tags[required_ordinal - 40], right_tag, 'milestone right-neighbor RRC tag')
        milestones[label] = {'symbol_index': symbol,
            'endpoint_capture_relative_q32': endpoint_position,
            'position_center_u80': (tag0_index * Q32 + endpoint_position - 20 * Q32) & MASK80,
            'rounded_tag_u64': left_tag, 'mu_u16': weight,
            'required_right_endpoint_capture_index': endpoint + 1,
            'required_right_RRC_tag_u64': right_tag,
            'required_ADC_input_ordinal': required_ordinal,
            'raw_input_dependency_cycles_after_first_input': {'ADC_II8': 8 * required_ordinal,
                'ADC_alternating12_13': 12 * required_ordinal + required_ordinal // 2},
            'availability_contract': 'observe actual matching filtered-history right-neighbor handshake; raw ADC coordinate alone omits NCO/RRC pipeline latency',
            'not_a_header_or_payload_decoder': True}
    # Default tag mapping is original capture identity. A TB may translate every
    # input tag and target center by one modulo48 integer offset (also in Q32),
    # preserving a separate chosen epoch; phase/data remain exactly unchanged.
    target = output / name
    np.savez(target.with_suffix('.npz'), input_iq=input_iq, input_tags_u64=tags,
        expected_nco_s18=mixed, expected_rrc_s20=filtered, expected_rrc_tags_u64=rrc_tags,
        training_iq=interpolated, public_signs=np.array(signs, dtype=np.int8),
        request_endpoint_capture_index=endpoints, request_tags_u64=request_tags, request_mu_u16=mu,
        timing_first_position_words32=position_words,
        timing_first_position_relative_q32=positions_relative,
        expected_H_s26=np.array([c['H_raw_s26'] for c in columns], dtype=np.int32),
        expected_step_s32=np.array([c['symbol_step_s32'] for c in columns], dtype=np.int32),
        expected_energy_u50=np.array([c['energy_u50'] for c in columns], dtype=np.uint64),
        expected_selected_rx=np.array([c['selected_rx'] for c in columns], dtype=np.uint8))
    metadata = {'schema': 'g0-training-frontend-fixture-v2', 'name': name,
        'scope': 'finite calibrated actual-source NCO/RRC/Q32/F1/two-column integer training comparison',
        'source': {'capture': str(capture.resolve()), 'crc32': capture_crc, 'record_count': len(raw),
            'capture_slice': [start, stop], 'first_capture_tag_u64': int(raw['tag'][0]),
            'not_unchanged_raw_ADC': True,
            'preprocessing': 'bounded floating mean/calibration/notch; selected IQ conjugation; nearest ties-even signed16 adapter',
            'IQ_conjugated': report['IQ_conjugated'], 'quiet_notch': report['quiet_notch'],
            'post_NCO_DC_blocker': False},
        'input': {'length': len(input_iq), 'lanes': ['RX0I', 'RX0Q', 'RX1I', 'RX1Q'],
            'bits': 16, 'warmup_samples': 40, 'maximum_absolute': int(abs(input_iq.astype(np.int64)).max()),
            'finite_end_rule': 'through DATA_START first-symbol right interpolation endpoint plus40 further input samples',
            'rounding_error_max_absolute': float(abs(rounded - lanes_float).max())},
        'nco_config': {'first_tag_u64': int(tags[0]), 'phase_u32': phase, 'step_u32': config['NCO_step_u32'],
            'original_commit_sample': commit, 'original_commit_phase_u32': config['NCO_phase_u32'],
            'phase_equation': '(original_phase + (slice_start-original_commit)*step) mod2^32; not fresh frequency*slice_start rounding',
            'CFO_estimated_from_public_observations_hz': config['requested_CFO_hz']},
        'timing': {'peak_endpoint_capture_relative_q32': peak_q32, 'step_q32_u35': timing_step,
            'epoch_u16': epoch, 'first_positions_center_u80': first_absolute,
            'position_words_order': 'least significant32, next32, highest16 in uint32[3]',
            'first_symbols': [416, 928], 'symbol_counts': [448, 448],
            'position_equation': '(capture_tag0_index48<<32)+peak32+(symbol-256)*step32-(20<<32), modulo2^80',
            'TX1_offset_from_TX0': '512*step_q32', 'Q16_rounding': 'add32768 to complete Q32 then take index48/mu16; preserve carry',
            'coordinate': 'RRC center tag; original model endpoint minus20',
            'causal_state': timing_state, 'known_at_capture_endpoint': known_at,
            'known_at_input_ordinal': known_ordinal,
            'known_at_minus_first_TX0_endpoint_samples': known_at - first_q32 / Q32,
            'known_at_minus_first_TX0_Q16_endpoint_samples': known_at - (endpoints[0, 0] + int(mu[0, 0]) / 65536),
            'release_cycles_after_first_input': {'ADC_II8': 8 * known_ordinal,
                'ADC_alternating12_13': 12 * known_ordinal + known_ordinal // 2},
            'release_schedule': 'first ADC atcycle0; issue no earlier than acceptance of known_at input; actual estimator compute latency remains unverified',
            'context_generation': 'TB binds current admitted NCO context and established history generation; not a fixed source constant',
            'job_identity': 'TB-owned frame/version identity must remain common to TX0/TX1; source preamble/first-period ordinal identifies fixture only',
            'tag_remap': 'apply one modulo48 integer translation to all ADC/RRC/request indices and add translation<<32 modulo2^80 to each position; choose epoch separately, no carry into epoch'},
        'normal_receiver_comparison': {'first_complete_period_ordinal': 0, 'preamble_sample': peak,
            'RRC_vectors_compared': len(filtered), 'training_vectors_compared': int(interpolated.shape[0] * interpolated.shape[1]),
            'exact_NCO_phase_continuation': True, 'all_RRC_and_training_values_equal': True,
            'saved_reference_directory': str(reference_directory.resolve()),
            'CRC_acceptance_set_equal': True, 'CRC_good_packets': report['CRC_good_link_packets'],
            'recovered_blocks': len(restored), 'restored_bytes': sum(len(v) for v in restored.values()),
            'saved_reference_accessed_after_receive': True, 'expected_payload_or_CFO_input': False},
        'training_columns': columns,
        'milestones': milestones,
        'limitations': ['floating calibration/notch/sense boundary is not RTL',
            'no bounded-history/service/estimator latency proof in this fixture generator',
            'no atomic two-column publication or downstream header/pilot/detection verification'],
        'new_RF_transmissions': False, 'g0_passed': False, 'passed': True}
    write(target.with_suffix('.json'), metadata)
    row = {'name': name, 'npz': str(target.with_suffix('.npz')), 'json': str(target.with_suffix('.json')),
        'inputs': len(input_iq), 'filtered_vectors': len(filtered), 'training_vectors': 896,
        'known_at_input_ordinal': known_ordinal, 'CRC_good_packets': report['CRC_good_link_packets'],
        'RRC_and_training_exact': True, 'passed': True}
    print(json.dumps(row), flush=True)
    return row


def main(a):
    out = a.output.resolve();out.mkdir(parents=True, exist_ok=False)
    receipt = {'scope': __doc__, 'cases': [], 'status': 'running', 'g0_passed': False, 'new_RF_transmissions': False}
    write(out / 'receipt.json', receipt)
    for capture in a.capture:
        try:
            receipt['cases'].append(make_fixture(capture, out, a.reference_directory))
        except Exception as error:
            receipt.update(status='failed', failed_capture=str(capture), failure=repr(error))
            write(out / 'receipt.json', receipt)
            raise
        write(out / 'receipt.json', receipt)
    receipt['status'] = 'passed';write(out / 'receipt.json', receipt)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--capture', type=Path, action='append', required=True)
    parser.add_argument('--reference-directory', type=Path, required=True,
        help='completed g0-fixed-training normal reports; compared only after receive')
    parser.add_argument('--output', type=Path, required=True)
    main(parser.parse_args())
