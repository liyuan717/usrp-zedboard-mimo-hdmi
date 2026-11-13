#!/usr/bin/env python3
"""Unfrozen finite G0 channel sounder, NOT the production PHY/profile.

Public repeated preamble, separate TX training and independent dual QPSK
payload measure mapping/CFO/channel conditioning before protocol freeze.
"""
import argparse
import json
from pathlib import Path
import numpy as np
from scipy import signal

RATE=4_000_000
SPS=4
PERIOD=8192
PREAMBLE_START=256
TRAIN_START=[384,896]
TRAIN_LENGTH=512
PAYLOAD_START=1408
PAYLOAD_END=7424


def rrc(beta=.35,span=10):
    t=np.arange(-span*SPS//2,span*SPS//2+1,dtype=float)/SPS
    h=np.empty_like(t)
    for n,x in enumerate(t):
        if abs(x)<1e-12:h[n]=1+beta*(4/np.pi-1)
        elif abs(abs(4*beta*x)-1)<1e-12:
            h[n]=beta/np.sqrt(2)*((1+2/np.pi)*np.sin(np.pi/(4*beta))+(1-2/np.pi)*np.cos(np.pi/(4*beta)))
        else:h[n]=(np.sin(np.pi*x*(1-beta))+4*beta*x*np.cos(np.pi*x*(1+beta)))/(np.pi*x*(1-(4*beta*x)**2))
    return h/np.linalg.norm(h)


def symbols():
    rng=np.random.default_rng(0x56334730)
    def qpsk(n):
        b=rng.integers(0,2,(n,2));return ((1-2*b[:,0])+1j*(1-2*b[:,1]))/np.sqrt(2)
    s=np.zeros((PERIOD,2),dtype=complex)
    s[256:384,0]=np.tile(qpsk(16),8)
    for tx in range(2):s[TRAIN_START[tx]:TRAIN_START[tx]+TRAIN_LENGTH,tx]=qpsk(TRAIN_LENGTH)
    s[PAYLOAD_START:PAYLOAD_END,0]=qpsk(PAYLOAD_END-PAYLOAD_START)
    s[PAYLOAD_START:PAYLOAD_END,1]=qpsk(PAYLOAD_END-PAYLOAD_START)
    pilots=[]
    for pos in range(PAYLOAD_START,PAYLOAD_END-1,66):
        s[pos]=[1,0];s[pos+1]=[0,1];pilots.append(pos)
    return s,np.array(pilots)


def generate(out,repetitions):
    out.mkdir(parents=True,exist_ok=False)
    s,pilots=symbols();h=rrc();up=np.zeros((PERIOD*SPS,2),dtype=complex);up[::SPS]=s
    period=np.stack([signal.lfilter(h,[1],up[:,tx]) for tx in range(2)],axis=1)
    scale=.12/max(np.max(np.abs(period.real)),np.max(np.abs(period.imag)))
    period*=scale
    iq=np.empty((len(period),4),dtype='<f4');iq[:,0::2]=period.real;iq[:,1::2]=period.imag
    with (out/'iq.fc32').open('wb') as f:
        for _ in range(repetitions):iq.tofile(f)
    receipt={'scope':'G0 channel sounder only, unfrozen; not production V3 waveform',
        'sample_rate_hz':RATE,'symbol_rate_hz':RATE//SPS,'period_symbols':PERIOD,
        'period_samples':len(period),'repetitions':repetitions,'total_samples':len(period)*repetitions,
        'duration_seconds':len(period)*repetitions/RATE,'rrc_rolloff':.35,'rrc_span_symbols':10,
        'tx_filter_group_delay_samples':20,'max_IQ_component':float(np.max(np.abs(iq))),
        'scale':scale,'public_preamble_symbols':[256,384],'orthogonal_training_ranges':[[384,896],[896,1408]],
        'pilot_pairs':len(pilots),'payload_symbols_per_stream':PAYLOAD_END-PAYLOAD_START-2*len(pilots),
        'QPSK':'I,Q = (1-2*b)/sqrt(2); I bit then Q bit','production_use_allowed':False}
    (out/'waveform.json').write_text(json.dumps(receipt,indent=2)+'\n')
    print(json.dumps(receipt))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--repetitions',type=int,default=64)
    a=p.parse_args()
    if not 1<=a.repetitions<=120:raise ValueError('finite sounder limited to less than one second')
    generate(a.output,a.repetitions)
