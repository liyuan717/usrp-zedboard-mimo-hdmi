#!/usr/bin/env python3
"""Blind public-preamble acquisition and floating G0 channel measurements.

Payload references are used only to compare decisions AFTER public training
and pilot-based estimation. This is an offline G0 model, never a PS RF path
or proof of final PHY/CRC/FEC/net throughput.
"""
import argparse
import itertools
import json
from pathlib import Path
import numpy as np
from scipy import signal
from g0_public_acquisition import acquire,interpolate
from g0_sounder import RATE,SPS,PERIOD,PREAMBLE_START,TRAIN_START,TRAIN_LENGTH,PAYLOAD_START,PAYLOAD_END,rrc,symbols


def analyze(samples,tags=None,dc_block=True,causal=False,compare_receiver_weights=False):
    y=np.asarray(samples,dtype=complex);s,pilots=symbols();h=rrc()
    acquisition_fn=acquire
    if causal:
        from g0_causal_acquisition import acquire as acquisition_fn
    mf,peakpos,sample_period,acquisition=acquisition_fn(y,PERIOD,s[:1408].copy(),dc_block)
    sample_periods=np.broadcast_to(sample_period,peakpos.shape)
    cfo=acquisition['tracking_NCO_CFO_hz']
    points=np.array([1+1j,1-1j,-1+1j,-1-1j])/np.sqrt(2)
    candidates=np.array(list(itertools.product(points,repeat=2)))
    data_indices=np.arange(PAYLOAD_START,PAYLOAD_END)
    data_indices=data_indices[np.all(np.abs(s[data_indices])>0,axis=1)]
    # Exclude both orthogonal pilot symbols from payload comparisons.
    data_indices=data_indices[~np.isin(data_indices,np.r_[pilots,pilots+1])]
    frames=[]
    for peak,sample_period in zip(peakpos,sample_periods):
        positions=peak+(np.arange(PERIOD)-PREAMBLE_START)*sample_period
        if positions[0]<len(h) or positions[-1]>=len(y)-len(h):continue
        r=interpolate(mf,positions);times=(positions-peak)/RATE
        H=np.zeros((2,2),dtype=complex);frequencies=[];train_evm=[];training_residuals=[]
        for tx in range(2):
            k=np.arange(TRAIN_START[tx]+32,TRAIN_START[tx]+TRAIN_LENGTH-32)
            g=r[k]*np.conj(s[k,tx])[:,None]
            strong=int(np.argmax(np.mean(abs(g)**2,axis=0)))
            # Averaged short windows estimate each CBX's CFO independently.
            block=g[:len(g)//16*16,strong].reshape(-1,16).mean(axis=1)
            tb=times[k[:len(block)*16]].reshape(-1,16).mean(axis=1)
            f=float(np.polyfit(tb,np.unwrap(np.angle(block)),1)[0]/(2*np.pi))
            H[:,tx]=np.mean(g*np.exp(-2j*np.pi*f*times[k])[:,None],axis=0)
            model=s[k,tx,None]*H[None,:,tx]*np.exp(2j*np.pi*f*times[k])[:,None]
            residual=r[k]-model
            train_evm.append(float(np.sqrt(np.mean(abs(residual)**2)/np.mean(abs(model)**2))))
            if compare_receiver_weights:training_residuals.append(residual)
            frequencies.append(f)
        phase=np.empty((len(data_indices),2));pilot_reports=[]
        for tx in range(2):
            k=pilots+tx
            model=H[None,:,tx]*np.exp(2j*np.pi*frequencies[tx]*times[k])[:,None]
            z=np.sum(np.conj(model)*r[k],axis=1)/np.sum(abs(model)**2,axis=1)
            unwrapped=np.unwrap(np.angle(z))
            smooth=signal.savgol_filter(unwrapped,7,1)
            phase[:,tx]=2*np.pi*frequencies[tx]*times[data_indices]+np.interp(times[data_indices],times[k],smooth)
            fit=np.polyfit(times[k],unwrapped,1)
            residual=unwrapped-np.polyval(fit,times[k])
            pilot_reports.append({'CFO_hz':float(cfo+frequencies[tx]+fit[0]/(2*np.pi)),
                'phase_fit_residual_rms_deg':float(np.std(residual)*180/np.pi),
                'pilot_phase_noise_rms_deg':float(np.std(unwrapped-smooth)*180/np.pi)})
        channel=H[None,:,:]*np.exp(1j*phase)[:,None,:]
        prediction=np.einsum('nrt,kt->nrk',channel,candidates)
        residual=r[data_indices,:,None]-prediction
        distance=np.sum(abs(residual)**2,axis=1)
        decisions=candidates[np.argmin(distance,axis=1)]
        expected=s[data_indices]
        def errors_by_tx(values):
            return (np.count_nonzero((values.real<0)!=(expected.real<0),axis=0)
                    +np.count_nonzero((values.imag<0)!=(expected.imag<0),axis=0))
        by_tx=errors_by_tx(decisions)
        bit_errors=int(by_tx.sum())
        symbol_errors=np.count_nonzero(decisions!=expected)
        model=np.einsum('nrt,nt->nr',channel,expected)
        evm=float(np.sqrt(np.mean(abs(r[data_indices]-model)**2)/np.mean(abs(model)**2)))
        singular=np.linalg.svd(H,compute_uv=False)
        frame={'preamble_sample':float(peak),'H_real':H.real.tolist(),'H_imag':H.imag.tolist(),
            'H_power_dB':(20*np.log10(np.maximum(abs(H),1e-12))).tolist(),
            'condition_number':float(singular[0]/max(singular[-1],1e-12)),
            'training_CFO_hz':[float(cfo+f) for f in frequencies], 'training_EVM_rms':train_evm,
            'pilot_estimates':pilot_reports,'payload_bit_errors':int(bit_errors),'payload_bits':int(expected.size*2),
            'payload_symbol_errors':int(symbol_errors),'payload_EVM_rms':evm,
            'payload_bit_errors_by_TX':by_tx.tolist()}
        if compare_receiver_weights:
            residuals=np.concatenate(training_residuals)
            covariance=residuals.T@residuals.conj()/len(residuals)
            powers=covariance.diagonal().real
            # Train before payload. Regularize the finite covariance near a
            # zero residual; its scalar normalization does not affect ranking.
            floor=max(float(powers.mean())*1e-6,np.finfo(float).tiny)
            diagonal=np.sum(abs(residual)**2/np.maximum(powers,floor)[None,:,None],axis=1)
            inverse=np.linalg.inv(covariance+floor*np.eye(2))
            whitened=np.einsum('nrk,rs,nsk->nk',residual.conj(),inverse,residual).real
            frame['training_residual_receiver_power']=powers.tolist()
            frame['training_channel_column_norm']=np.linalg.norm(H,axis=0).tolist()
            frame['weighted_payload_bit_errors_by_TX']={
                name:errors_by_tx(candidates[np.argmin(cost,axis=1)]).tolist()
                for name,cost in (('diagonal',diagonal),('full_covariance',whitened))}
        frames.append(frame)
    if not frames:raise ValueError('No complete sounder periods inside capture')
    bits=sum(f['payload_bits'] for f in frames);errors=sum(f['payload_bit_errors'] for f in frames)
    result={'scope':'G0 float offline sounder model, not production PL or RF packet/CRC/FEC/net throughput',
        'samples':len(y),'duration_seconds':len(y)/RATE,**acquisition,
        'complete_period_count':len(frames),'payload_bits_compared':bits,'payload_bit_errors':errors,
        'uncoded_BER':errors/bits,'condition_number_median':float(np.median([f['condition_number'] for f in frames])),
        'payload_EVM_median':float(np.median([f['payload_EVM_rms'] for f in frames])),
        'CFO_median_hz':[float(np.median([f['pilot_estimates'][tx]['CFO_hz'] for f in frames])) for tx in range(2)],
        'payload_bits_by_TX':[bits//2]*2,
        'payload_bit_errors_by_TX':np.sum([f['payload_bit_errors_by_TX'] for f in frames],axis=0).tolist(),
        'frames':frames,'g0_passed':False}
    if compare_receiver_weights:
        result['weighted_payload_bit_errors_by_TX']={
            name:np.sum([f['weighted_payload_bit_errors_by_TX'][name] for f in frames],axis=0).tolist()
            for name in ('diagonal','full_covariance')}

    if tags is not None:
        result['tag_gaps']=int(np.count_nonzero(np.diff(tags)!=1))
        result['ADC_clipped_IQ_words']=int(np.count_nonzero((samples.real<=-2048)|(samples.real>=2047))+np.count_nonzero((samples.imag<=-2048)|(samples.imag>=2047)))
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('capture',type=Path);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--no-dc-block',action='store_true')
    p.add_argument('--causal-acquisition',action='store_true')
    p.add_argument('--compare-receiver-weights',action='store_true',help='offline training-only noise weighting control')
    a=p.parse_args()
    raw=np.fromfile(a.capture,dtype=np.dtype([('tag','<u8'),('iq','<i2',(4,))]))
    y=raw['iq'][:,0::2]+1j*raw['iq'][:,1::2]
    result=analyze(y,raw['tag'],dc_block=not a.no_dc_block,causal=a.causal_acquisition,compare_receiver_weights=a.compare_receiver_weights)
    a.output.write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({k:v for k,v in result.items() if k!='frames' and k!='all_preamble_samples'}))
