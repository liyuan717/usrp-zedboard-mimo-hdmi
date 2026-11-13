#!/usr/bin/env python3
"""G0 candidate controls, real-ADC replay and prefix causality checks.

This is a floating reference, not PL timing/fixed-point/video acceptance.
The expected source is accessed only AFTER receiver outputs are available.
"""
import argparse
import json
import zlib
from pathlib import Path
import numpy as np
from check_g0_sounder_model import simulate as sounder_simulate
from analyze_g0_sounder import analyze
from check_g0_packet_model import simulate as packet_simulate
from g0_packet_probe import receive,source_pattern,RATE,PERIOD,SPS,HEADER_START


def bounds(r):
    startup=r['startup'];notch=r['quiet_notch']
    if notch['window_samples'][-1]>notch['calibration_window_end_sample']:raise ValueError('calibration looks ahead')
    if notch['calibration_commit_sample']-notch['calibration_window_end_sample']<2048:raise ValueError('calibration estimator latency missing')
    if startup['coarse_known_sample']-startup['coarse_last_sample_used']<128:raise ValueError('coarse estimator latency missing')
    if startup['coarse_known_sample']>=startup['last_training_sample']:raise ValueError('coarse CFO unavailable for training')
    if startup['NCO_commit_sample']-startup['last_training_sample']<512:raise ValueError('NCO estimator latency missing')
    for state in r['timing_states']:
        first_header=state['preamble_sample']+(HEADER_START-256)*state['sample_period']
        if state['known_at_sample']>=first_header:raise ValueError('timing unavailable before header')
        if state['latest_peak_used']-state['earliest_peak_used']>3:raise ValueError('unbounded timing history')
        if state['preamble_sample']<=startup['NCO_commit_sample']:raise ValueError('past samples use future NCO')


def compare_source(blocks):
    if not blocks:raise ValueError('no decoded source block')
    for block,data in blocks.items():
        if data!=source_pattern(block).tobytes():raise ValueError('post-receive source mismatch')
    return sum(len(data) for data in blocks.values())


def write(path,value):path.write_text(json.dumps(value,indent=2)+'\n')


def main(a):
    out=a.output;out.mkdir(parents=True,exist_ok=False);rows=[]
    cases=[{'name':'independent_CFO_SFO','cfo':[12345,13567],'sfo':-35,'leak':[0j,0j]},
        {'name':'inband_CW','cfo':[-236000,-235998.5],'sfo':43,'leak':[.12+.09j,.08-.05j]},
        {'name':'LO_shift_IQ_reverse','cfo':[278000,278030],'sfo':15,'leak':[.12+.09j,.08-.05j],'lo_offset':1000000,'conjugated':True},
        {'name':'changed_payload_negative','cfo':[12345,13567],'sfo':-35,'leak':[0j,0j],'change_payload':True},
        {'name':'positive_CFO_SFO_edge','cfo':[350000,351200],'sfo':150,'leak':[0j,0j]},
        {'name':'negative_CFO_SFO_edge','cfo':[-350000,-351200],'sfo':-150,'leak':[0j,0j]}]
    rng=np.random.default_rng(719310)
    for i in range(8):
        common=float(rng.uniform(-350000,350000));cw=bool(i%2)
        cases.append({'name':f'random_phase_start_{i}','cfo':[common,common+float(rng.uniform(-40,40) if cw else rng.uniform(-2000,2000))],
            'sfo':float(rng.uniform(-150,150)),'leak':[.12+.09j,.08-.05j] if cw else [0j,0j],
            'lo_offset':1000000 if cw else 0,'conjugated':bool(i%3),
            'initial_phase':rng.uniform(-np.pi,np.pi,2).tolist(),'capture_start':float(rng.uniform(0,1.5*8192*4))})
    for case in cases:
        params={k:v for k,v in case.items() if k!='name'}
        r=analyze(sounder_simulate(**params),dc_block=not case.get('lo_offset'),causal=True);bounds(r)
        if abs(r['sample_rate_offset_ppm']-case['sfo'])>2:raise ValueError('wrong SFO '+case['name'])
        if max(abs(np.array(r['CFO_median_hz'])-case['cfo']))>20:raise ValueError('wrong independent CFO '+case['name'])
        if r['IQ_conjugated']!=case.get('conjugated',False):raise ValueError('wrong IQ sense')
        if case.get('change_payload'):
            if not .45<r['uncoded_BER']<.55:raise ValueError('changed payload escaped negative comparison')
        elif r['payload_bit_errors']:raise ValueError('unexpected positive model mismatch '+case['name'])
        write(out/(case['name']+'.json'),r)
        row={'name':case['name'],'bits':r['payload_bits_compared'],'bit_errors':r['payload_bit_errors'],
             'CFO_hz':r['CFO_median_hz'],'SFO_ppm':r['sample_rate_offset_ppm'],'bounds_checked':True,'passed':True}
        rows.append(row);print(json.dumps(row),flush=True)
    for name,damage,reverse,sf in [('packet_normal',None,False,None),('packet_IQ_reverse',None,True,None),
                                 ('packet_burst','payload_burst',False,4),('packet_bad_header','PHY_header',False,5)]:
        r,blocks=receive(packet_simulate(a.waveform,damage,reverse,count_frames=8,damage_superframe=sf),causal=True)
        bounds(r);equal=compare_source(blocks)
        if damage is None and r['link_erased_or_missing_in_observed_range']:raise ValueError('clean packet model lost data')
        if damage=='payload_burst' and not (r['link_erased_or_missing_in_observed_range'] and all(b['recovered'] for b in r['blocks'])):
            raise ValueError('burst did not exercise FEC recovery')
        if damage=='PHY_header' and not (r['CRC_bad_PHY_headers'] and any(not b['recovered'] for b in r['blocks'])):
            raise ValueError('header failure did not remain a bounded erasure')
        write(out/(name+'.json'),r)
        row={'name':name,'packets':r['expected_packets_in_observed_range'],'good_packets':r['CRC_good_link_packets'],
             'full_blocks':r['eligible_complete_FEC_blocks'],'restored_blocks':len(blocks),'source_bytes_equal':equal,'passed':True}
        rows.append(row);print(json.dumps(row),flush=True)
    for path in a.capture:
        if path.stat().st_size%16:raise ValueError('truncated ADC record')
        raw=np.fromfile(path,dtype=np.dtype([('tag','<u8'),('iq','<i2',(4,))]))
        crc=f'{zlib.crc32(raw):08x}'
        transport=json.loads((path.parent/'receipt.json').read_text())
        if transport['capture']['crc32']!=crc:raise ValueError('ADC capture differs from measured transport receipt')
        if np.any(np.diff(raw['tag'])!=1):raise ValueError('ADC gap')
        y=raw['iq'][:,0::2]+1j*raw['iq'][:,1::2]
        r,blocks=receive(y,causal=True);bounds(r);equal=compare_source(blocks)
        name=path.parent.name;write(out/(name+'.json'),r)
        if len(blocks)!=r['eligible_complete_FEC_blocks']:raise ValueError('real sampled FEC block failed '+name)
        row={'name':name,'packets':r['expected_packets_in_observed_range'],'good_packets':r['CRC_good_link_packets'],
             'full_blocks':r['eligible_complete_FEC_blocks'],'restored_blocks':len(blocks),'source_bytes_equal':equal,
             'SFO_ppm':r['sample_rate_offset_ppm'],'capture_crc32':crc,'passed':True}
        rows.append(row);print(json.dumps(row),flush=True)
        if path!=a.capture[0]:continue
        cut=800000
        if len(y)<=cut:raise ValueError('prefix test requires a longer raw capture')
        prefix,decoded_prefix=receive(y[:cut],causal=True)
        changed=y.copy();rng=np.random.default_rng(482310)
        rms=float(np.sqrt(np.mean(abs(y[:cut])**2)))
        changed[cut:]=rng.normal(0,3*rms,changed[cut:].shape)+1j*rng.normal(0,3*rms,changed[cut:].shape)
        bad_tail,decoded_bad_tail=receive(changed,causal=True)
        bounds(prefix);bounds(bad_tail)
        for variant,decoded in [(prefix,decoded_prefix),(bad_tail,decoded_bad_tail)]:
            if not decoded_prefix:raise ValueError('prefix contains no full restored block')
            for block,data in decoded_prefix.items():
                if decoded.get(block)!=data or blocks.get(block)!=data:raise ValueError('future changed past source output')
            for key in ['coarse_peak_sample','coarse_known_sample','last_training_sample','NCO_commit_sample']:
                if variant['startup'][key]!=r['startup'][key]:raise ValueError('future changed startup boundary')
            if abs(variant['tracking_NCO_CFO_hz']-r['tracking_NCO_CFO_hz'])>1e-5:raise ValueError('future changed learned CFO')
            full_states={s['known_at_sample']:s for s in r['timing_states']}
            for state in variant['timing_states']:
                if state['known_at_sample']>=cut:continue
                old=full_states[state['known_at_sample']]
                if abs(state['preamble_sample']-old['preamble_sample'])>1e-4 or abs(state['sample_period']-old['sample_period'])>1e-8:
                    raise ValueError('future changed past timing state')
            expected={f['superframe']:f['packets'] for f in prefix['frames'] if f['PHY_header_ok']}
            actual={f['superframe']:f['packets'] for f in variant['frames'] if f['PHY_header_ok']}
            if any(actual.get(sf)!=packets for sf,packets in expected.items()):raise ValueError('future changed past CRC decisions')
        causality={'name':'actual_ADC_prefix_and_corrupt_future','prefix_samples':cut,'prefix_seconds':cut/RATE,
            'restored_prefix_blocks':len(decoded_prefix),'prefix_source_bytes_unchanged':compare_source(decoded_prefix),
            'startup_CFO_timing_and_CRC_unchanged':True,'passed':True}
        rows.append(causality);print(json.dumps(causality),flush=True)
        write(out/'prefix.json',prefix);write(out/'corrupt_future.json',bad_tail)
    write(out/'receipt.json',{'scope':'G0 floating causal-acquisition candidate controls and ADC replays, not PL/video/full G0',
        'cases':rows,'uses_expected_payload_for_acquisition':False,
        'RTL_processing_latency_and_fixed_point_verified':False,'g0_passed':False})


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--waveform',type=Path,required=True)
    p.add_argument('--capture',type=Path,action='append',default=[]);p.add_argument('--output',type=Path,required=True)
    main(p.parse_args())
