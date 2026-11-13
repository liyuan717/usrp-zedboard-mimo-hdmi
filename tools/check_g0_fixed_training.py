#!/usr/bin/env python3
"""Integer training-CFO/H numerical controls, actual ADC replay, and fixtures.

Golden source bytes are accessed only after receive() returns. Baseline uses
the existing integer NCO/RRC/Q32/Q16-F1 islands and FLOAT training estimation.
Failures and extra CRC erasures remain in the output receipt; no G0 claim.
"""
import argparse
from fractions import Fraction
import json
from pathlib import Path
import zlib
import numpy as np
from check_g0_causal_model import bounds
from check_g0_fixed_interpolation import accepted_packets, compare_source, same_causal_configuration
from check_g0_packet_model import simulate
from g0_fixed_frontend_model import fixed_frontend
from g0_fixed_interpolation_model import fixed_sampler
from g0_fixed_training_model import (FixedTrainingEstimator, TrainingInvalid, estimate_training,
    exact_training_lanes, nearest_div, training_signs, unwrap_phase32,
    uniform_time_channel, verify_normalization, WEIGHTS, OLS_DIVISOR, Q32)
from g0_packet_probe import public_prefix, receive
from phase_angle_cost_probe import PHASE_ERROR_BOUND_LSB, verify_table_and_bound


def write(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n')


def rational_nearest(value):
    lower = value.numerator // value.denominator
    return lower + int(value - lower >= Fraction(1, 2))


def fixture(directory, name, lanes, signs, tx, provenance):
    error = 0
    try:
        info = estimate_training(lanes, signs, tx)
    except TrainingInvalid as failure:
        error, info = 2, failure.diagnostics
    np.savez(directory / (name + '.npz'), input_iq=np.asarray(lanes, dtype=np.int32),
        public_signs=np.asarray(signs, dtype=np.int8), tx=np.int32(tx),
        expected_error=np.int32(error), expected_selected_rx=np.int32(info['selected_rx']),
        expected_step_s32=np.int32(info.get('symbol_step_s32', 0)),
        expected_H_s26=np.asarray(info.get('H_raw_s26', [0] * 4), dtype=np.int32))
    write(directory / (name + '.json'), {'provenance': provenance, 'input_bits': 21,
        'input_scale': 4, 'output_H_bits': 26, 'output_H_scale': 64,
        'expected_error': error, 'diagnostics': info, 'g0_passed': False})
    return error, info


def controls(directory):
    directory.mkdir(parents=True, exist_ok=False)
    public = public_prefix()
    _, signs = training_signs(public, 0)
    scale = (1 << 20) - 1
    tied = np.tile(signs * scale, (1, 2)).astype(np.int64)
    # Both RX branches are identical and include an exact negative signed21 rail.
    negative = int(np.flatnonzero(tied[:, 0] < 0)[0]);tied[negative, 0] = -(1 << 20);tied[negative, 2] = -(1 << 20)
    error, info = fixture(directory, 'rails_energy_tie', tied, signs, 0, 'synthetic full signed21 rails; exact RX energy tie')
    assert error == 0 and info['selected_rx'] == 0 and info['energy_u50'][0] == info['energy_u50'][1]
    _, signs1 = training_signs(public, 1)
    negative_pi = -np.tile(signs1 * 131071, (1, 2)).astype(np.int64)
    error, info = fixture(directory, 'constant_pi', negative_pi, signs1, 1, 'synthetic negative-real despread; constant pi remains valid')
    assert error == 0 and info['symbol_step_s32'] == 0
    weak_zero = tied.copy();weak_zero[:, 2:] = 0
    error, info = fixture(directory, 'weak_branch_zero', weak_zero, signs, 0, 'unselected RX has28 zero blocks; selected valid branch must survive')
    assert error == 0 and info['branch_invalid'] == [False, True]
    zero = np.zeros((448, 4), dtype=np.int64)
    error, _ = fixture(directory, 'all_zero_invalid', zero, signs, 0, 'selected zero blocks explicitly invalid')
    assert error == 2
    flip = np.repeat(np.where(np.arange(28) % 2, -1, 1), 16)
    ambiguous = np.tile(signs * 1000 * flip[:, None], (1, 2))
    error, _ = fixture(directory, 'half_turn_invalid', ambiguous, signs, 0, 'selected adjacent block phase exactly halfturn; explicitly invalid')
    assert error == 2
    for tx, frequency in [(0, 23000.), (1, -23000.)]:
        index, known = training_signs(public, tx)
        sequence = (known[:, 0] + 1j * known[:, 1]) / np.sqrt(2)
        rotation = np.exp(2j * np.pi * frequency * (index - 256) / 1e6 + 2.9j)
        data = sequence[:, None] * rotation[:, None] * np.array([15000 + 4000j, 60000 - 12000j])[None, :]
        raw = np.empty((448, 4), dtype=np.int64)
        raw[:, 0::2] = np.rint(data.real).astype(np.int64);raw[:, 1::2] = np.rint(data.imag).astype(np.int64)
        error, info = fixture(directory, f'phase_wrap_TX{tx}', raw, known, tx,
            'synthetic branch1 strongest and multiple phase wraps; generation frequency is not estimator input')
        assert error == 0 and info['selected_rx'] == 1
        assert abs(info['symbol_step_s32'] / Q32 * 1e6 - frequency) < 1
    for step in [-1000000, -1, 0, 1, 1000000]:
        phase = [(0x71234567 + b * 16 * step) & 0xffffffff for b in range(28)]
        unwrapped, _, ambiguous = unwrap_phase32(phase)
        assert not ambiguous
        assert nearest_div(sum(w * p for w, p in zip(WEIGHTS, unwrapped)), OLS_DIVISOR) == step
    for numerator in [-3, -1, 0, 1, 3, -(1 << 66), (1 << 66) + 1]:
        for denominator in [2, 58464, 4096, 1 << 40]:
            assert nearest_div(numerator, denominator) == rational_nearest(Fraction(numerator, denominator))
    for distance in [-64, 0, 64]:
        assert unwrap_phase32([0, (1 << 31) + distance])[2]
    assert not unwrap_phase32([0, (1 << 31) + 65])[2]
    assert unwrap_phase32([0, 1 << 31])[0] == [0, -(1 << 31)]
    assert unwrap_phase32([1 << 31, 0])[0] == [-(1 << 31), 0]
    try:
        exact_training_lanes(np.full((448, 2), .0625 + 0j))
    except ValueError:
        pass
    else:
        raise AssertionError('non-exact scale4 input was silently rounded')
    return {'normalization': verify_normalization(), 'CORDIC': verify_table_and_bound(),
            'synthetic_fixtures': 7, 'energy_tie_RX0': True, 'weak_invalid_ignored': True,
            'selected_zero_or_halfturn_rejected': True, 'constant_pi_valid': True,
            'signed_half_rounding_and_OLS_exact_ramps_passed': True,
            'exact_scale4_input_required': True, 'g0_passed': False}


def report_differences(reference, candidate):
    if len(reference['frames']) != len(candidate['frames']):
        raise ValueError('training candidate changed sampled frame count')
    f = np.array([x['training_CFO_hz'] for x in candidate['frames']]) - np.array([x['training_CFO_hz'] for x in reference['frames']])
    def h(report):
        return np.array([np.array(x['H_real']) + 1j * np.array(x['H_imag']) for x in report['frames']])
    delta = h(candidate) - h(reference)
    return {'maximum_CFO_difference_hz': float(abs(f).max()),
            'maximum_H_component_difference_original_units': float(max(abs(delta.real).max(), abs(delta.imag).max())),
            'maximum_complex_H_difference_original_units': float(abs(delta).max())}


def audit_receipt(a):
    """Reproduce input-derived numeric bounds from saved decoded reports only."""
    out = a.output.resolve();out.mkdir(parents=True, exist_ok=False)
    base = a.audit_receipt.resolve().parent
    normal = json.loads(a.audit_receipt.read_text())
    if normal['status'] != 'passed':
        raise ValueError('numeric audit requires a completed normal receipt')
    names = [row['name'] for row in normal['cases'] if row.get('capture_crc32')]
    if not names:
        raise ValueError('normal receipt has no actual ADC cases')
    weight_abs_sum = sum(abs(w) for w in WEIGHTS)
    step_bound = .5 + PHASE_ERROR_BOUND_LSB * weight_abs_sum / OLS_DIVISOR
    rows, phase_errors, cfo_ratios, h_ratios = [], [], [], []
    for name in names:
        stats = json.loads((base / (name + '-training-stats.json')).read_text())
        fixed = json.loads((base / (name + '-fixed_training.json')).read_text())
        uniform = json.loads((base / (name + '-uniform_float.json')).read_text())
        calls = fixed['training_candidate_calls']
        assert len(calls) == len(fixed['frames']) == len(uniform['frames']), 'audit frame count mismatch'
        for tx in (0, 1):
            selected = [row for row in stats if row['tx'] == tx]
            assert len(selected) == len(calls), 'audit statistics count mismatch'
            rows.append({'capture': name, 'tx': tx, 'periods': len(selected),
                'selected_branches': sorted(set(row['selected_rx'] for row in selected)),
                'minimum_strong_to_weak_energy_ratio': min(row['energy_ratio_strong_to_weak'] for row in selected),
                'maximum_raw_signed21_abs': max(row['raw_abs_max'] for row in selected),
                'maximum_block_signed27_abs': max(row['block_sum_abs_max'] for row in selected),
                'minimum_block_magnitude_original_units': np.min([row['block_magnitude_min_original_units'] for row in selected], axis=0).tolist(),
                'maximum_adjacent_phase_rad': np.max([row['max_adjacent_phase_rad'] for row in selected], axis=0).tolist(),
                'residual_CFO_minmax_hz': [min(row['residual_CFO_hz'] for row in selected), max(row['residual_CFO_hz'] for row in selected)]})
        for call, ff, fu in zip(calls, fixed['frames'], uniform['frames']):
            hf = np.array(ff['H_real']) + 1j * np.array(ff['H_imag'])
            hu = np.array(fu['H_real']) + 1j * np.array(fu['H_imag'])
            assert len(call['tx']) == 2
            for tx, info in enumerate(call['tx']):
                block = np.array(info['block_sums'])
                for rx in (0, 1):
                    ideal = np.angle(block[:, 2 * rx] + 1j * block[:, 2 * rx + 1]) * Q32 / (2 * np.pi)
                    error = (np.array(info['phases_u32'][rx]) - ideal + Q32 / 2) % Q32 - Q32 / 2
                    phase_errors.extend(abs(error).tolist())
                cfo_error = abs(ff['training_CFO_hz'][tx] - fu['training_CFO_hz'][tx])
                cfo_bound = step_bound * 4e6 / (Q32 * info['sample_period_adapter'])
                cfo_ratios.append(cfo_error / cfo_bound)
                assert cfo_error <= cfo_bound + 1e-9, 'CFO exceeded input-derived bound'
                # Q16 endpoint errors e_i obey |e_i| <= .5/65536 ADC sample.
                # Mean training spacing includes (e_last-e_first)/447, whereas
                # hardware H uses an integer symbol offset from preamble256.
                largest_offset = info['first_symbol'] + 447 - 256
                time_error = (.5 / 65536) * (1 + 2 * largest_offset / 447) / 4e6
                phase_error = (step_bound * largest_offset / Q32 * 2 * np.pi + np.pi / 4096
                    + 2 * np.pi * (abs(info['residual_CFO_hz_adapter']) + cfo_bound) * time_error)
                max_g_norm = np.sqrt(2) * info['input_max_absolute'] / 4
                coefficient_error = np.sqrt(2) / (2 * 65536)
                normalization_rel_error = abs(1735428857 / (2**40 / (448 * np.sqrt(2))) - 1)
                rotation_component_error = .5 / (64 * np.sqrt(2))
                h_bound = (max_g_norm * (2 * np.sin(min(phase_error, np.pi) / 2) + coefficient_error)
                    + rotation_component_error + .5 / 64
                    + normalization_rel_error * (max_g_norm * (1 + coefficient_error) + rotation_component_error))
                delta = hf[:, tx] - hu[:, tx]
                h_error = max(abs(delta.real).max(), abs(delta.imag).max())
                h_ratios.append(float(h_error / h_bound))
                assert h_error <= h_bound, 'H exceeded input-derived component bound'
    assert max(phase_errors) < PHASE_ERROR_BOUND_LSB, 'CORDIC exceeded analytical bound'
    result = {'scope': 'read-only analysis of completed normal software training receipts',
        'source_receipt': str(a.audit_receipt.resolve()), 'measured_training': rows,
        'actual_CORDIC_max_error_phase_LSB': max(phase_errors),
        'declared_CORDIC_bound_phase_LSB': PHASE_ERROR_BOUND_LSB,
        'sum_abs_OLS_weights': weight_abs_sum,
        'symbol_step_error_bound_LSB_vs_uniform_float': step_bound,
        'CFO_criterion': 'abs(f_integer-f_uniform)<=step_bound*RATE/(2^32*sample_period); uniform-vs-Q16-time OLS is separately reported',
        'H_criterion': 'input-norm bound for 12-bit phase rounding, Q16 sincos, F4 rotation, Q40 normalization, final /64 H rounding; independent of CRC decisions',
        'maximum_actual_CFO_error_fraction_of_bound': max(cfo_ratios),
        'maximum_actual_H_component_error_fraction_of_bound': max(h_ratios),
        'all_input_derived_numeric_bounds_passed': True, 'new_RF_transmissions': False, 'g0_passed': False}
    write(out / 'receipt.json', result)
    print(json.dumps({key: value for key, value in result.items() if key != 'measured_training'}), flush=True)


def invalid_recovery(a):
    """Faults alter training observations, never inject known CFO or payload."""
    out=a.output.resolve();out.mkdir(parents=True,exist_ok=False)
    y=4096*simulate(a.waveform,count_frames=8)
    base_est=FixedTrainingEstimator()
    baseline,basebytes=receive(y,causal=True,frontend=fixed_frontend,sampler=fixed_sampler,channel_estimator=base_est)
    bounds(baseline);compare_source(basebytes)
    basegood=accepted_packets(baseline);fault_ordinal=1
    damaged_sf=baseline['frames'][fault_ordinal]['superframe']
    rows=[]
    for fault in ('zero_training','halfturn_training'):
        estimator=FixedTrainingEstimator();calls=0
        def damaged_sampler(mf,peak,step,period,preamble_start):
            nonlocal calls
            r,positions,info=fixed_sampler(mf,peak,step,period,preamble_start)
            if calls==fault_ordinal:
                index,signs=training_signs(public_prefix(),0)
                r=r.copy()
                if fault=='zero_training':r[index]=0
                else:
                    alternation=np.repeat(np.where(np.arange(28)%2,-1,1),16)
                    value=(signs[:,0]+1j*signs[:,1])*250*alternation
                    r[index]=value[:,None]
                info['injected_training_fault']=fault
            calls+=1
            return r,positions,info
        report,blocks=receive(y,causal=True,frontend=fixed_frontend,sampler=damaged_sampler,channel_estimator=estimator)
        bounds(report);equal=compare_source(blocks)
        report['training_candidate_calls']=estimator.calls
        good=accepted_packets(report)
        expected_missing=set(range(damaged_sf*16,(damaged_sf+1)*16))
        bad=report['frames'][fault_ordinal]
        assert report['training_invalid_periods']==1
        assert not bad['PHY_header_ok'] and bad['PHY_header_error']=='training_invalid' and 'packets' not in bad
        assert bad['timing_inferred_superframe']==damaged_sf
        assert basegood-good==expected_missing and not good-basegood
        assert report['expected_packets_in_observed_range']==baseline['expected_packets_in_observed_range']
        assert len(blocks)==1 and report['eligible_complete_FEC_blocks']==2
        assert all(data==basebytes[block] for block,data in blocks.items())
        assert len(estimator.calls)==len(report['frames']) and estimator.calls[fault_ordinal]['status']=='invalid'
        assert all(call['status']=='valid' for call in estimator.calls[fault_ordinal+1:])
        assert all(frame['PHY_header_ok'] for frame in report['frames'][fault_ordinal+1:])
        row={'name':fault,'injected_period_ordinal':fault_ordinal,'damaged_superframe_observed_after_baseline_receive':damaged_sf,
             'invalid_periods':1,'bad_period_emitted_payload':False,'expected_missing_sequences':sorted(expected_missing),
             'CRC_good_packets':len(good),'expected_packets':report['expected_packets_in_observed_range'],
             'later_periods_with_valid_header':len(report['frames'])-fault_ordinal-1,
             'recovered_blocks':len(blocks),'eligible_blocks':2,'source_bytes_equal':equal,
             'later_training_reestimated':True,'passed':True}
        rows.append(row);write(out/(fault+'.json'),report);print(json.dumps(row),flush=True)
    write(out/'receipt.json',{'scope':'targeted training-invalid period erasure and subsequent recovery after receiver error-handler integration',
        'normal_large_capture_baseline_receipt':'build/g0-fixed-training-r1/receipt.json',
        'normal_receipt_provenance':'normal suite completed before narrow training-invalid handler; arithmetic unchanged',
        'fault_injection_boundary':'final sampler/public training observations only', 'cases':rows,'passed':True,
        'new_RF_transmissions':False,'g0_passed':False})


def main(a):
    out = a.output.resolve();out.mkdir(parents=True, exist_ok=False)
    rows, failures = [], []
    receipt = {'scope': 'integer training CFO/H software island, not streaming RTL/complete PHY',
               'status': 'started', 'cases': rows, 'failed_cases': failures, 'g0_passed': False,
               'new_RF_transmissions': False}
    write(out/'receipt.json', receipt)
    arithmetic = controls(out/'fixtures');write(out/'arithmetic.json', arithmetic)
    print(json.dumps({'name': 'training_controls', 'passed': True}), flush=True)

    def decode(y, estimator=None):
        return receive(y, causal=True, frontend=fixed_frontend, sampler=fixed_sampler, channel_estimator=estimator)

    def check(name, y, damaged_header=False, capture_crc=None):
        baseline, basebytes = decode(y);bounds(baseline);compare_source(basebytes)
        uniform, uniformbytes = decode(y, uniform_time_channel);bounds(uniform);compare_source(uniformbytes)
        estimator = FixedTrainingEstimator(retain_vectors=capture_crc is not None)
        candidate, blocks = decode(y, estimator);bounds(candidate);equal = compare_source(blocks)
        candidate['training_candidate_calls'] = estimator.calls
        basegood, uniformgood, newgood = accepted_packets(baseline), accepted_packets(uniform), accepted_packets(candidate)
        lost, gained = sorted(basegood-newgood), sorted(newgood-basegood)
        same_blocks = set(blocks) == set(basebytes) and all(blocks[k] == basebytes[k] for k in blocks)
        expected_failure = bool(candidate['CRC_bad_PHY_headers'] and len(blocks) < candidate['eligible_complete_FEC_blocks'])
        passed = not lost and same_blocks and (expected_failure if damaged_header else len(blocks) == candidate['eligible_complete_FEC_blocks'])
        row = {'name': name, 'baseline_CRC_good_packets': len(basegood), 'uniform_float_CRC_good_packets': len(uniformgood),
            'integer_training_CRC_good_packets': len(newgood), 'expected_packets': candidate['expected_packets_in_observed_range'],
            'additional_erased_packet_sequences': lost, 'newly_accepted_packet_sequences': gained,
            'uniform_float_additional_erased_packet_sequences': sorted(basegood-uniformgood),
            'same_CRC_set_as_baseline': basegood == newgood, 'recovered_blocks': len(blocks),
            'eligible_blocks': candidate['eligible_complete_FEC_blocks'], 'source_bytes_equal': equal,
            'same_delivered_blocks': same_blocks, 'expected_bad_header_failure_preserved': expected_failure if damaged_header else None,
            'uniform_time_model_change': report_differences(baseline, uniform),
            'fixed_vs_uniform': report_differences(uniform, candidate), 'capture_crc32': capture_crc, 'passed': passed}
        rows.append(row)
        if not passed:
            failures.append(name)
        for mode, report in [('baseline', baseline), ('uniform_float', uniform), ('fixed_training', candidate)]:
            write(out/f'{name}-{mode}.json', report)
        if capture_crc:
            for tx, record in enumerate(estimator.vectors[0]):
                fixture(out/'fixtures', name+f'-TX{tx}', record['lanes'], record['signs'], tx,
                        {'capture': name, 'crc32': capture_crc, 'frame': 0, 'source': 'actual decoded sampler output; expected source consulted only after receive'})
            # Characterize measured inputs using public training only.
            stats = []
            for frame in estimator.vectors:
                for record in frame:
                    info=record['report'];raw=record['lanes'];sign=record['signs'];selected=info['selected_rx']
                    block=np.array(info['block_sums']);magnitudes=np.hypot(block[:, 0::2], block[:, 1::2])/(16*4*np.sqrt(2))
                    energy=info['energy_u50'];step=info['sample_period_adapter']
                    stats.append({'tx':record['tx'], 'selected_rx':selected, 'energy_ratio_strong_to_weak': energy[selected]/max(1,energy[1-selected]),
                        'raw_abs_max':int(abs(raw).max()), 'block_sum_abs_max':int(abs(block).max()),
                        'block_magnitude_min_original_units':magnitudes.min(axis=0).tolist(),
                        'block_magnitude_max_original_units':magnitudes.max(axis=0).tolist(),
                        'max_adjacent_phase_rad':[value*2*np.pi/Q32 for value in info['maximum_adjacent_phase_delta_LSB']],
                        'residual_CFO_hz':info['residual_CFO_hz_adapter'], 'sample_period':step})
            write(out/f'{name}-training-stats.json', stats)
        print(json.dumps(row), flush=True);receipt['status']='running';write(out/'receipt.json', receipt)
        return baseline, basebytes, candidate, blocks

    for name, damage, reverse, sf in [('clean', None, False, None), ('reversed_IQ', None, True, None),
                                    ('burst', 'payload_burst', False, 4), ('bad_header', 'PHY_header', False, 5)]:
        y=4096*simulate(a.waveform,damage,reverse,count_frames=8,damage_superframe=sf)
        check('model_'+name,y,damaged_header=damage=='PHY_header')
    for capture_index,path in enumerate(a.capture):
        if path.stat().st_size%16:raise ValueError('truncated ADC capture')
        raw=np.fromfile(path,dtype=np.dtype([('tag','<u8'),('iq','<i2',(4,))]))
        crc=f'{zlib.crc32(raw):08x}'
        if crc!=json.loads((path.parent/'receipt.json').read_text())['capture']['crc32']:raise ValueError('ADC CRC differs')
        if np.any(np.diff(raw['tag'])!=1):raise ValueError('ADC tag gap')
        y=raw['iq'][:,0::2]+1j*raw['iq'][:,1::2]
        baseline,basebytes,full,fullbytes=check(path.parent.name,y,capture_crc=crc)
        if capture_index:continue
        cut=800000;prefix=y[:cut];changed=y.copy();rng=np.random.default_rng(323241)
        rms=float(np.sqrt(np.mean(abs(prefix)**2)))
        changed[cut:]=rng.normal(0,3*rms,changed[cut:].shape)+1j*rng.normal(0,3*rms,changed[cut:].shape)
        for mode,reference,expectedblocks in [('baseline',baseline,basebytes),('fixed_training',full,fullbytes)]:
            est0=FixedTrainingEstimator() if mode=='fixed_training' else None
            est1=FixedTrainingEstimator() if mode=='fixed_training' else None
            r0,b0=decode(prefix,est0);r1,b1=decode(changed,est1);bounds(r0);bounds(r1)
            errors=[]
            try:
                assert all(expectedblocks.get(k)==value and b1.get(k)==value for k,value in b0.items()),'future changed source bytes'
                same_causal_configuration(reference,r0,cut);same_causal_configuration(reference,r1,cut)
                past={f['superframe']:f['packets'] for f in r0['frames'] if f['PHY_header_ok']}
                for report in (reference,r1):
                    actual={f['superframe']:f['packets'] for f in report['frames'] if f['PHY_header_ok']}
                    assert all(actual.get(k)==value for k,value in past.items()),'future changed past CRC'
                if est0:
                    for a0,b1call,whole in zip(est0.calls,est1.calls,full['training_candidate_calls']):
                        for aa,bb,cc in zip(a0['tx'],b1call['tx'],whole['tx']):
                            for key in ('selected_rx','symbol_step_s32','H_raw_s26','energy_u50','branch_invalid'):
                                assert aa[key]==bb[key]==cc[key],'future changed integer training result '+key
            except (AssertionError,KeyError) as error:
                errors.append(str(error))
            if est0:r0['training_candidate_calls']=est0.calls;r1['training_candidate_calls']=est1.calls
            write(out/f'prefix-{mode}.json',r0);write(out/f'corrupted_future-{mode}.json',r1)
            row={'name':'prefix_and_corrupted_future_'+mode,'source_bytes_equal':compare_source(b0),'errors':errors,'passed':not errors}
            rows.append(row)
            if errors:failures.append(row['name'])
            print(json.dumps(row),flush=True)
    receipt.update({'status':'passed' if not failures else 'failed','arithmetic':arithmetic,
        'uniform_symbol_OLS_explicit_model_change':True,'input_scale4_exact':True,'H_output_scale':64,
        'complete_RTL_PHY_or_estimator_service_verified':False})
    write(out/'receipt.json',receipt)
    if failures:raise SystemExit('saved failed cases: '+', '.join(failures))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--waveform',type=Path)
    parser.add_argument('--capture',type=Path,action='append',default=[])
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--invalid-recovery-only',action='store_true')
    parser.add_argument('--audit-receipt',type=Path,help='read completed normal suite reports and independently audit numeric bounds')
    args=parser.parse_args()
    if args.audit_receipt:
        if args.invalid_recovery_only:parser.error('numeric audit and invalid recovery are separate modes')
        audit_receipt(args)
    else:
        if args.waveform is None:parser.error('--waveform is required for decode/recovery modes')
        if args.invalid_recovery_only:invalid_recovery(args)
        else:main(args)
