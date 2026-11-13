#!/usr/bin/env python3
"""G0 acquisition controls: independent CFO/SFO, carrier leak, IQ reversal.

The changed-payload negative control must stay acquired yet fail data
comparison, proving that known payload is not an acquisition shortcut.
"""
import argparse
import json
from pathlib import Path
import numpy as np
from scipy import signal
from analyze_g0_sounder import analyze
from g0_sounder import symbols,rrc,SPS,PERIOD,RATE,PAYLOAD_START,PAYLOAD_END


def simulate(cfo,sfo,leak,lo_offset=0,conjugated=False,change_payload=False,initial_phase=(0,0),capture_start=713.31):
    s,pilots=symbols();rng=np.random.default_rng(9127)
    if change_payload:
        k=np.arange(PAYLOAD_START,PAYLOAD_END)
        k=k[~np.isin(k,np.r_[pilots,pilots+1])]
        bits=rng.integers(0,2,(len(k),2,2))
        s[k]=((1-2*bits[:,:,0])+1j*(1-2*bits[:,:,1]))/np.sqrt(2)
    up=np.zeros((PERIOD*SPS,2),dtype=complex);up[::SPS]=s
    period=np.stack([signal.lfilter(rrc(),[1],up[:,tx]) for tx in range(2)],axis=1)*.18
    tx=np.tile(period,(8,1));count=PERIOD*SPS*6;coordinate=capture_start+np.arange(count)/(1+sfo/1e6)
    tx=np.stack([np.interp(coordinate,np.arange(len(tx)),tx[:,ch].real)+1j*np.interp(coordinate,np.arange(len(tx)),tx[:,ch].imag) for ch in range(2)],axis=1)
    t=np.arange(count)/RATE;H=np.array([[1,.18+.1j],[.1-.16j,.85j]])
    y=sum((tx[:,ch]*np.exp(2j*np.pi*cfo[ch]*t)+leak[ch]*np.exp(2j*np.pi*(cfo[ch]+lo_offset)*t))[:,None]*H[None,:,ch]*np.exp(1j*initial_phase[ch]) for ch in range(2))
    y+=rng.normal(0,.0002,y.shape)+1j*rng.normal(0,.0002,y.shape)
    return y.conj() if conjugated else y


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args()
    a.output.mkdir(parents=True,exist_ok=False)
    cases=[{'name':'independent_CFO_SFO','cfo':[12345,13567],'sfo':-35,'leak':[0j,0j]},
        {'name':'large_CFO_carrier_leak','cfo':[-236000,-235998.5],'sfo':43,'leak':[.12+.09j,.08-.05j]},
        {'name':'LO_shift_IQ_reverse','cfo':[278000,278030],'sfo':15,'leak':[.12+.09j,.08-.05j],'lo_offset':1000000,'conjugated':True},
        {'name':'changed_payload_negative','cfo':[12345,13567],'sfo':-35,'leak':[0j,0j],'change_payload':True}]
    receipts=[]
    for case in cases:
        params={k:v for k,v in case.items() if k!='name'}
        y=simulate(**params)
        r=analyze(y,dc_block=not case.get('lo_offset'))
        if abs(r['sample_rate_offset_ppm']-case['sfo'])>2:raise ValueError('SFO estimate differs in '+case['name'])
        if max(abs(np.array(r['CFO_median_hz'])-case['cfo']))>20:raise ValueError('CFO estimate differs in '+case['name'])
        if r['IQ_conjugated']!=case.get('conjugated',False):raise ValueError('IQ sense differs')
        if case.get('change_payload'):
            if not .45<r['uncoded_BER']<.55:raise ValueError('changed payload must fail data comparison')
        elif r['payload_bit_errors']:raise ValueError('unexpected synthetic data mismatch')
        last=r['fine_CFO_public_training']['last_training_sample']
        first_payload=r['fine_CFO_public_training']['preamble_sample']+(PAYLOAD_START-256)*SPS
        if last>=first_payload:raise ValueError('fine CFO used samples after first payload')
        row={'name':case['name'],'complete_periods':r['complete_period_count'],
            'bits_compared':r['payload_bits_compared'],'bit_errors':r['payload_bit_errors'],
            'CFO_hz':r['CFO_median_hz'],'SFO_ppm':r['sample_rate_offset_ppm'],
            'IQ_conjugated':r['IQ_conjugated'],'fine_CFO_before_payload':True,'passed':True}
        receipts.append(row)
        (a.output/(case['name']+'.json')).write_text(json.dumps(r,indent=2)+'\n')
        print(json.dumps(row),flush=True)
    (a.output/'receipt.json').write_text(json.dumps({'scope':'G0 floating model only, not PL/net throughput',
        'cases':receipts,'g0_passed':False},indent=2)+'\n')
