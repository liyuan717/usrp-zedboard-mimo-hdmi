#!/usr/bin/env python3
"""Opt-in variable-M integer startup CFO/quality in the hybrid packet model.

Default receive is unchanged. Both sides use integer NCO/RRC, Q32/Q16-F1 and
integer448 training and integer coarse acquisition. Only startup changes. Source/golden
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
from g0_fixed_differential_model import FixedDifferentialSelector
from g0_fixed_startup_model import FixedStartupEstimator, estimate_startup, final_nco_configuration
from g0_fixed_frontend_model import fixed_frontend
from g0_fixed_interpolation_model import fixed_sampler
from g0_fixed_training_model import FixedTrainingEstimator
from g0_packet_probe import receive


def write(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n')


def decode(y, integer, fixture_output=None, fixture_name=None):
    estimator = FixedTrainingEstimator()
    startup = FixedStartupEstimator(retain_vectors=fixture_output is not None) if integer else None
    report, blocks = receive(y, causal=True, frontend=fixed_frontend, sampler=fixed_sampler,
        channel_estimator=estimator, differential_selector=FixedDifferentialSelector, startup_estimator=startup)
    report['training_candidate_calls'] = estimator.calls
    if startup is not None and fixture_output is not None:
        for index, vector in enumerate(startup.vectors):
            stem=fixture_output / (fixture_name+'-startup-'+str(index))
            np.savez_compressed(stem.with_suffix('.npz'),lanes=vector['lanes'],signs=vector['signs'],
                endpoints=vector['endpoints'],public_indices=vector['public_indices'])
            write(stem.with_suffix('.json'),{'schema':'startup signed21scale4 lanes[M,4], signs[M,2], endpoint[M], public_indices[M]',
                'raw_ADC_unchanged':False,'software_calibration_notch':True,'RTL_service_measured':False,
                'source_name':fixture_name,'expected':vector['report']})
    return report, blocks


def arithmetic_controls():
    def nearest(x):
        lo=x.numerator//x.denominator
        return lo+int(x-lo>=Fraction(1,2))
    count=0
    for coarse in [0,1,0x7fffffff,0x80000000,0xffffffff]:
        for b in range(8,29):
            d=8*b*(b*b-1)//3
            for num in [0,1,-1,d//2,-d//2,(1<<47)-1,-(1<<47)]:
                for commit,origin in [(0,0),(160636,0),(0,(1<<48)-1),((1<<48)-1,0)]:
                    config=final_nco_configuration(coarse,num,d,commit,origin)
                    signed=coarse if coarse<1<<31 else coarse-(1<<32)
                    exact=Fraction(signed)-Fraction(num,4*d)
                    offset=(commit-origin)% (1<<48)
                    assert config['step_u32']==nearest(exact)&0xffffffff
                    assert config['phase_u32']==nearest(exact*offset)&0xffffffff
                    count+=1
    rows=[]
    for m in [128,129,143,144,303,304,305,319,320,399,400,401,431,432,447,448]:
        signs=np.ones((m,2),dtype=np.int64)
        lanes=np.tile([-(1<<20),-(1<<20),0,0],(m,1))
        r=estimate_startup(lanes,signs,384,0,5000)
        assert r['accepted'] and r['selected_rx']==0 and r['step_s32']==0
        assert r['energy_u50']==[m*(1<<41),0]
        assert r['coherent_sums_s36']==[-m*(1<<25),0]
        assert r['quality_float_diagnostic']==1
        assert r['branch_invalid']==[False,True]
        # Independent centered-coordinate OLS, with no weight/divisor formula.
        u=r['unwrapped_s38'][0];centers=[Fraction(16*b)+Fraction(15,2) for b in range(m//16)]
        mean=sum(centers)/len(centers);numerator=sum((x-mean)*v for x,v in zip(centers,u))
        denominator=sum((x-mean)**2 for x in centers)
        assert Fraction(r['num_s48'],r['D_u16'])==numerator/denominator
        rows.append({'M':m,'B':m//16,'fullrange_constant_quality':True})
    signs=np.ones((400,2),dtype=np.int64)
    zero=estimate_startup(np.zeros((400,4),dtype=np.int64),signs,464,0,5000)
    assert not zero['accepted'] and zero['error']=='selected_phase_invalid'
    half=np.tile([1000,0,0,0],(400,1));half[16:32,0]=-1000
    invalid=estimate_startup(half,signs,464,0,5000)
    assert not invalid['accepted'] and invalid['branch_invalid'][0]
    tail=np.tile([2,0,1,0],(319,1));tail[304:,2]=1000
    r=estimate_startup(tail,np.ones((319,2),dtype=np.int64),400,0,5000)
    assert r['selected_rx']==1 and r['energy_u50'][1]==304+15*1000000 and r['unused_tail_for_OLS']==15
    assert not r['quality_gate_passed']
    # Exact CFO boundary: compare rational slope against 1/50 turn/symbol.
    for b in range(8,29):
        d=8*b*(b*b-1)//3;limit=Fraction(d*(1<<32),50)
        for n in [limit.numerator//limit.denominator,limit.numerator//limit.denominator+1]:
            for sign in [-1,1]:assert (50*abs(sign*n)<=d*(1<<32))==(abs(Fraction(sign*n,d))<=Fraction(1<<32,50))
    return {'independent_fraction_final_NCO_cases':count,'variable_M_controls':rows,
        'zero_and_halfturn_rejected':True,'weak_zero_ignored':True,'tail_changes_strong_RX_and_quality':True,
        'exact_rational_CFO_boundary':True,'quality_phase_origin':'first retained symbol (not absolute ADC tag)'}


def main(a):
    out = a.output.resolve(); out.mkdir(parents=True, exist_ok=False)
    rows, failures = [], []
    receipt = {'scope': 'integer variable-M startup CFO/quality and exact rational final NCO; calibration/timing remain float',
               'status': 'started', 'command': sys.argv, 'cases': rows, 'failed_cases': failures,
               'historical_reference': str(a.reference.resolve()), 'new_RF_transmissions': False, 'g0_passed': False}
    write(out / 'receipt.json', receipt)
    receipt['arithmetic'] = arithmetic_controls()
    files = ['g0_causal_acquisition.py', 'g0_packet_probe.py', 'g0_fixed_differential_model.py',
             'g0_fixed_frontend_model.py', 'g0_fixed_interpolation_model.py', 'g0_fixed_training_model.py',
             'g0_fixed_startup_model.py', 'check_g0_fixed_startup.py']
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
            candidate, newbytes = decode(y, True, out, name)
            write(out / (name + '-fixed_startup.json'), candidate)
            # From this point onward references may be used for validation.
            bounds(baseline); bounds(candidate)
            oldequal, newequal = compare_source(oldbytes), compare_source(newbytes)
            oldgood, newgood = accepted_packets(baseline), accepted_packets(candidate)
            saved = json.loads((a.reference / (name + '-fixed_differential.json')).read_text())
            historic = accepted_packets(saved)
            same_baseline = historic == oldgood
            same_blocks = set(oldbytes) == set(newbytes) and all(oldbytes[k] == newbytes[k] for k in oldbytes)
            lost, gained = sorted(oldgood - newgood), sorted(newgood - oldgood)
            oldstart, newstart = baseline['startup'], candidate['startup']
            final_dispatch=candidate['startup_candidate_dispatch_checks']
            assert len(final_dispatch)==1 and all(x['bit_exact'] and x['explicit_integer_configuration'] and
                x['actual_step_u32']==x['expected_step_u32'] and x['actual_phase_u32']==x['expected_phase_u32'] for x in final_dispatch)
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
                   'startup_M': candidate['startup_candidate_calls'][-1]['M'],
                   'startup_B': candidate['startup_candidate_calls'][-1]['B'],
                   'startup_num_s48': candidate['startup_candidate_calls'][-1]['num_s48'],
                   'startup_D_u16': candidate['startup_candidate_calls'][-1]['D_u16'],
                   'quality_delta': newstart['training_coherent_fraction']-oldstart['training_coherent_fraction'],
                   'final_NCO_dispatch': candidate['startup_candidate_dispatch_checks'],
                   'passed': oldgood == newgood and same_blocks and same_baseline and len(newbytes) == candidate['eligible_complete_FEC_blocks']}
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
            r0, b0 = decode(prefix, True); write(out / 'prefix-fixed_startup.json', r0)
            r1, b1 = decode(changed, True); write(out / 'corrupted_future-fixed_startup.json', r1)
            bounds(r0); bounds(r1)
            assert all(expected_blocks.get(k) == value and b1.get(k) == value for k, value in b0.items()), 'future changed source bytes'
            for variant in (r0, r1):
                same_causal_configuration(reference, variant, cut)
                assert variant['startup_candidate_calls'] == reference['startup_candidate_calls'], 'future changed integer startup'
                assert variant['startup_candidate_dispatch_checks'] == reference['startup_candidate_dispatch_checks'], 'future changed final NCO dispatch'
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


def audit_receipt(directory):
    """Finite saved-vector audit; no capture replay or reference input to decode."""
    import math
    directory=directory.resolve();receipt=json.loads((directory/'receipt.json').read_text())
    assert receipt['status']=='passed'
    def nearest(value):
        floor=value.numerator//value.denominator
        return floor+int(value-floor>=Fraction(1,2))
    from g0_packet_probe import public_prefix
    public=public_prefix();rows=[]
    for metadata in sorted(directory.glob('*-startup-*.json')):
        meta=json.loads(metadata.read_text());expected=meta['expected']
        fixture=np.load(metadata.with_suffix('.npz'));lanes=fixture['lanes'];signs=fixture['signs']
        indices=fixture['public_indices'];endpoints=fixture['endpoints'];m=len(lanes);b=m//16
        assert np.array_equal(signs,np.column_stack((np.where(public[indices,0].real>0,1,-1),np.where(public[indices,0].imag>0,1,-1))))
        assert np.all(np.diff(endpoints)==4) and np.all(np.diff(indices)==1)
        energy=[sum(int(row[2*r])**2+int(row[2*r+1])**2 for row in lanes) for r in range(2)]
        selected=0 if energy[0]>=energy[1] else 1
        assert energy==expected['energy_u50'] and selected==expected['selected_rx']
        de=[]
        for row,sgn in zip(lanes,signs):
            de.append([(int(row[2*r])+1j*int(row[2*r+1]))*(int(sgn[0])-1j*int(sgn[1])) for r in range(2)])
        max_angle_error=0.;rational_slopes=[]
        for r in range(2):
            blocks=[sum(row[r] for row in de[16*j:16*j+16]) for j in range(b)]
            for j,value in enumerate(blocks):
                assert [int(value.real),int(value.imag)]==expected['block_sums_s27'][j][2*r:2*r+2]
                exact=math.atan2(value.imag,value.real)*(1<<32)/(2*math.pi)
                error=abs((expected['phases_u32'][r][j]-exact+(1<<31))%(1<<32)-(1<<31))
                max_angle_error=max(max_angle_error,error)
                assert error<=32
            # Centered-coordinate normal equation independent of the compact D(B).
            x=[Fraction(16*j)+Fraction(15,2) for j in range(b)];mean=sum(x)/b
            u=expected['unwrapped_s38'][r]
            slope=sum((t-mean)*p for t,p in zip(x,u))/sum((t-mean)**2 for t in x)
            assert slope==Fraction(expected['weighted_numerators_s48'][r],expected['D_u16'])
            rational_slopes.append(slope)
        slope=rational_slopes[selected];step=nearest(slope)
        assert step==expected['step_s32']
        # Closed-form phase, rational rotation and independent mean-energy ratio.
        sums=[0,0]
        for index,row in enumerate(de):
            phase=(-index*step)%(1<<32);bin_index=((phase+(1<<19))%(1<<32))//(1<<20)
            c=int(round(65536*math.cos(2*math.pi*bin_index/4096)))
            s=int(round(65536*math.sin(2*math.pi*bin_index/4096)))
            value=row[selected];i,q=int(value.real),int(value.imag)
            sums[0]+=nearest(Fraction(i*c-q*s,4096));sums[1]+=nearest(Fraction(i*s+q*c,4096))
        assert sums==expected['coherent_sums_s36']
        ratio=Fraction(sum(v*v for v in sums),256*m*sum(int(row[selected].real)**2+int(row[selected].imag)**2 for row in de))
        assert (ratio>=Fraction(2,5))==expected['quality_gate_passed']
        assert (abs(slope)<=Fraction(1<<32,50))==expected['CFO_gate_passed']
        # Exact arithmetic makes quality invariant to constant phase origin.
        # Quantized LUT rotation is compared to the ideal rational slope at both origins.
        omega=2*math.pi*float(slope)/(1<<32)
        selected_data=np.array([row[selected] for row in de])
        relative=selected_data*np.exp(-1j*omega*np.arange(m))
        absolute=selected_data*np.exp(-1j*omega*endpoints.astype(float)/4)
        denominator=m*float(np.sum(abs(selected_data)**2))
        ideal_relative=abs(np.sum(relative))**2/denominator
        ideal_absolute=abs(np.sum(absolute))**2/denominator
        assert abs(ideal_relative-ideal_absolute)<1e-12
        # Per-symbol error: half-LUT-bin + step quantization drift + Q16 coefficient
        # vector error, followed by half-LSB rounding of two F4 components.
        phase_error=math.pi/4096+(m-1)*math.pi/(1<<32)
        sum_error=float(np.sum(abs(selected_data)))*(phase_error+math.sqrt(2)/131072)+m*math.sqrt(2)/32
        bound=(2*abs(np.sum(relative))*sum_error+sum_error**2)/denominator
        observed=abs(float(ratio)-ideal_relative)
        assert observed<=bound and abs(ideal_relative-.4)>bound
        cfg=expected['final_nco_config'];coarse=cfg['coarse_signed_step'];adc_step=Fraction(coarse)-slope/4
        assert cfg['step_u32']==nearest(adc_step)&0xffffffff
        assert cfg['phase_u32']==nearest(adc_step*cfg['offset_u48'])&0xffffffff
        rows.append({'fixture':metadata.name,'M':m,'B':b,'selected_rx':selected,
            'maximum_CORDIC_phase_error_LSB':max_angle_error,'quality_integer':float(ratio),
            'quality_ideal_rational_slope':ideal_relative,'quality_absolute_origin':ideal_absolute,
            'quality_error':observed,'quality_absolute_error_bound':bound,
            'quality_gate_margin_exceeds_bound':True,'public_signs_and_direct_blocks_verified':True,
            'independent_Fraction_OLS_rotation_final_NCO':True})
    assert len(rows)>=5
    tools=Path(__file__).resolve().parent;snap={};source_checks={}
    for name,saved in receipt['source_snapshot'].items():
        blob=(tools/name).read_bytes();now={'bytes':len(blob),'sha256':hashlib.sha256(blob).hexdigest()};snap[name]=now
        source_checks[name]=now==saved
    assert all(ok for name,ok in source_checks.items() if name!='check_g0_fixed_startup.py')
    audit={'scope':'finite independent numeric audit of saved startup vectors; no new full replay',
        'status':'passed','cases':rows,'arithmetic':arithmetic_controls(),'normal_run_source_unchanged':source_checks,
        'checker_change':'added this independent finite audit CLI after normal replay; estimator and receiver source bytes unchanged',
        'current_source_snapshot':snap,'normal_receipt':str(directory/'receipt.json'),'g0_passed':False}
    write(directory/'numeric-audit.json',audit)
    print(json.dumps({'status':'passed','finite_fixtures':len(rows),'audit':str(directory/'numeric-audit.json')}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--waveform', type=Path)
    parser.add_argument('--capture', type=Path, action='append', default=[])
    parser.add_argument('--reference', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--audit-receipt',type=Path,help='audit saved finite fixtures without replay')
    parser.add_argument('--case', action='append', help='select a subset for a retained diagnostic rerun')
    parser.add_argument('--skip-prefix', action='store_true', help='omit separate prefix/future controls for targeted diagnostics')
    args=parser.parse_args()
    if args.audit_receipt:audit_receipt(args.audit_receipt)
    else:
        if args.waveform is None or args.reference is None or args.output is None:parser.error('--waveform, --reference and --output required for replay')
        main(args)
