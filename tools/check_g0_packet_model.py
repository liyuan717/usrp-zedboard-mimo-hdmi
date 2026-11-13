#!/usr/bin/env python3
"""Controls for the unfrozen G0 packet receiver, not a full G0/G1 pass."""
import argparse
import json
from pathlib import Path
import numpy as np
from g0_packet_probe import (RATE,SPS,PERIOD,DATA_INDICES,HEADER_START,DATA_START,
    receive,phy_header,parse_phy,link_packet,parse_link)
from g0_packet_fec import GENERATOR,K,SIZE,encode,recover,source_pattern


def simulate(waveform,damage=None,reverse=False,count_frames=6,damage_superframe=None):
    raw=np.fromfile(waveform/'iq.fc32',dtype='<f4',count=(count_frames+1)*PERIOD*SPS*4).reshape(-1,4)
    tx=raw[:,0::2]+1j*raw[:,1::2]
    if damage=='payload_burst':
        # Erase one whole packet slot on BOTH streams, including its pilots.
        sf=2 if damage_superframe is None else damage_superframe
        start=sf*PERIOD+int(DATA_INDICES[2192]);end=sf*PERIOD+int(DATA_INDICES[4383])+1
        tx[start*SPS:end*SPS]=0
    elif damage and damage.startswith('PHY_header'):
        sf={'PHY_header':3,'PHY_header_first':1,'PHY_header_last':5}[damage] if damage_superframe is None else damage_superframe
        tx[(sf*PERIOD+HEADER_START)*SPS:(sf*PERIOD+DATA_START)*SPS]=0
    count=count_frames*PERIOD*SPS;coordinate=713.31+np.arange(count)/(1+43/1e6)
    tx=np.stack([np.interp(coordinate,np.arange(len(tx)),tx[:,ch].real)+1j*np.interp(coordinate,np.arange(len(tx)),tx[:,ch].imag) for ch in range(2)],axis=1)
    t=np.arange(count)/RATE;cfo=[-236000,-235998.5]
    H=np.array([[1,.48+.11j],[.6-.1j,.19j]])
    leak=[.12+.09j,.08-.05j]
    y=sum((tx[:,ch]*np.exp(2j*np.pi*cfo[ch]*t)+leak[ch]*np.exp(2j*np.pi*(cfo[ch]+1000000)*t))[:,None]*H[None,:,ch] for ch in range(2))
    rng=np.random.default_rng(0x56335001)
    y+=rng.normal(0,.0002,y.shape)+1j*rng.normal(0,.0002,y.shape)
    return y.conj() if reverse else y


def check(a):
    a.output.mkdir(parents=True,exist_ok=False)
    existing=json.loads(a.fec_reference.read_text())
    if GENERATOR.tolist()!=existing['matrix_rows']:raise ValueError('matrix differs from independently checked C++ reference')
    source=source_pattern(3);coded=encode(source)
    # Exactly k valid symbols, repair first, with twelve missing sources.
    ids=list(range(20,32))+list(range(12,20))
    valid={i:coded[i].tobytes() for i in ids}
    if not np.array_equal(recover(valid),source):raise ValueError('critical FEC recovery mismatch')
    if recover(dict(list(valid.items())[:-1])) is not None:raise ValueError('insufficient symbols accepted')
    packet=link_packet(37,6,96,coded[0]);bad=bytearray(packet);bad[111]^=1
    if parse_link(bytes(bad),37,6,96)!=(None,'CRC'):raise ValueError('single-bit link error accepted')
    if parse_link(packet,38,6,96)!=(None,'identity_or_bounds'):raise ValueError('cross-session accepted')
    header=bytearray(phy_header(37,6));header[12]^=1
    if parse_phy(bytes(header))!=(None,'CRC'):raise ValueError('bad header length used before CRC')
    receipts=[]
    for name,damage,reverse in [('independent_phase_CFO_SFO',None,False),('IQ_reversal',None,True),
                                ('burst_erasure_recovery','payload_burst',False),('bad_PHY_header_bounded_loss','PHY_header',False),
                                ('first_bad_header_counts','PHY_header_first',False),('last_bad_header_counts','PHY_header_last',False)]:
        r,restored=receive(simulate(a.waveform,damage,reverse))
        if not restored:raise ValueError('no complete recovered FEC block '+name)
        if any(data!=source_pattern(b).tobytes() for b,data in restored.items()):raise ValueError('post-decode source mismatch '+name)
        if r['IQ_conjugated']!=reverse:raise ValueError('wrong IQ sense '+name)
        if abs(r['sample_rate_offset_ppm']-43)>2:raise ValueError('wrong SFO '+name)
        if name in ('independent_phase_CFO_SFO','IQ_reversal') and r['link_erased_or_missing_in_observed_range']:
            raise ValueError('unexpected no-damage erasures '+name)
        if damage=='payload_burst' and not (r['link_erased_or_missing_in_observed_range'] and all(b['recovered'] for b in r['blocks'])):
            raise ValueError('burst did not exercise erasure recovery')
        if damage=='PHY_header' and not (r['CRC_bad_PHY_headers'] and any(not b['recovered'] for b in r['blocks'])):
            raise ValueError('bad header did not exercise bounded failed block')
        if damage in ('PHY_header_first','PHY_header_last'):
            if r['expected_packets_in_observed_range']!=80 or r['CRC_good_link_packets']!=64:
                raise ValueError('leading/trailing bad header disappeared from loss denominator')
        (a.output/(name+'.json')).write_text(json.dumps(r,indent=2)+'\n')
        row={'name':name,'passed':True,'expected_packets':r['expected_packets_in_observed_range'],
            'good_packets':r['CRC_good_link_packets'],'erased_packets':r['link_erased_or_missing_in_observed_range'],
            'complete_blocks':r['eligible_complete_FEC_blocks'],'recovered_blocks':r['recovered_complete_FEC_blocks'],
            'source_bytes_equal':sum(len(data) for data in restored.values()),'bad_PHY_headers':r['CRC_bad_PHY_headers']}
        receipts.append(row);print(json.dumps(row),flush=True)
    (a.output/'receipt.json').write_text(json.dumps({'scope':'offline G0 packet controls only; not PL/production/video',
        'matrix_matches_independent_Cpp':True,'FEC_exact_k_and_insufficient_passed':True,
        'link_single_bit_and_cross_session_rejected':True,'bad_length_header_CRC_rejected':True,
        'cases':receipts,'g0_passed':False},indent=2)+'\n')


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--waveform',type=Path,required=True)
    p.add_argument('--fec-reference',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    check(p.parse_args())
