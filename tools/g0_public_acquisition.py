#!/usr/bin/env python3
"""G0 offline acquisition using ONLY public preamble/training symbols.

17 CFO aliases and both IQ senses are searched. No transmitted payload,
expected session/sequence or frame alignment is accepted by this API.
The across-preamble timing fit is offline; production must implement causal
tracking in PL and verify its finite NCO/timing state independently.
"""
import numpy as np
from scipy import signal
from g0_sounder import RATE,SPS,PREAMBLE_START,TRAIN_START,TRAIN_LENGTH,rrc


def interpolate(x,positions):
    t=np.arange(len(x))
    return np.stack([np.interp(positions,t,x[:,c].real)+1j*np.interp(positions,t,x[:,c].imag) for c in range(2)],axis=1)


def acquire(samples,period,public,dc_block=True):
    if public.shape != (1408,2):
        raise ValueError('only public preamble and training prefix is accepted')
    y=np.asarray(samples,dtype=complex);y=y-y.mean(axis=0);s=np.asarray(public);h=rrc()
    lag=16*SPS;window=64*SPS;coarse=[]
    for rx in range(2):
        z=np.conj(y[:-lag,rx])*y[lag:,rx]
        cs=np.r_[0,np.cumsum(z)];cp=np.r_[0,np.cumsum(abs(y[:,rx])**2)]
        zsum=cs[window:]-cs[:-window]
        pa=cp[window:-lag]-cp[:-window-lag];pb=cp[window+lag:]-cp[lag:-window]
        score=abs(zsum)**2/np.maximum(1e-20,pa*pb)
        score[pa<np.percentile(pa,30)]=0
        idx=int(np.argmax(score));coarse.append((float(score[idx]),float(np.angle(zsum[idx])*RATE/(2*np.pi*lag)),rx,idx))
    periodic_score,wrapped_cfo,rx,start=max(coarse)
    pre=s[256:384,0];reference=np.zeros(len(pre)*SPS,dtype=complex);reference[::SPS]=pre
    # Lag64 only measures CFO modulo62.5kHz. Select the integer ambiguity
    # and IQ sense using the PUBLIC preamble, never payload/golden alignment.
    hypotheses=[];best=None;time=np.arange(len(y))/RATE
    for alias in range(-8,9):
        frequency=wrapped_cfo+alias*RATE/lag
        corrected=y*np.exp(-2j*np.pi*frequency*time)[:,None]
        # CBX leakage is receiver DC only after CFO correction.
        if dc_block:corrected=signal.lfilter([1,-1],[1,-.995],corrected,axis=0)
        candidate_mf=np.stack([signal.lfilter(h,[1],corrected[:,c]) for c in range(2)],axis=1)
        cs=np.r_[0,np.cumsum(np.sum(abs(candidate_mf)**2,axis=1))]
        power=(cs[len(reference):]-cs[:-len(reference)])/SPS
        for conjugated in [False,True]:
            ref=reference.conj() if conjugated else reference
            correlations=np.stack([signal.correlate(candidate_mf[:,c],ref,mode='valid',method='fft') for c in range(2)],axis=1)
            candidate_score=np.sum(abs(correlations)**2,axis=1)/np.maximum(1e-20,len(pre)*power)
            confidence=float(candidate_score.max())
            hypotheses.append({'raw_CFO_hz':frequency,'IQ_conjugated':conjugated,'public_preamble_score':confidence})
            if best is None or confidence>best[0]:
                best=(confidence,frequency,conjugated,candidate_mf.copy(),candidate_score.copy())
    confidence,raw_cfo,conjugated,mf,score=best
    if conjugated:mf=mf.conj()
    cfo=-raw_cfo if conjugated else raw_cfo
    initial_peaks,_=signal.find_peaks(score,distance=int(period*SPS*.8),height=max(.12,float(score.max())*.45))
    coarse_cfo=cfo
    # Refine the common NCO from the FIRST available public TX0 training,
    # before payload. Coarse-CFO bias otherwise moves the large leakage tone
    # away from the DC-blocker zero. No payload decisions enter this estimate.
    fine_training=None
    for peak in initial_peaks:
        k=np.arange(TRAIN_START[0]+32,TRAIN_START[0]+TRAIN_LENGTH-32)
        positions=peak+(k-PREAMBLE_START)*SPS
        if positions[0]<len(h) or positions[-1]>=len(y)-len(h):continue
        r=interpolate(mf,positions);g=r*np.conj(s[k,0])[:,None]
        strong=int(np.argmax(np.mean(abs(g)**2,axis=0)))
        block=g[:len(g)//16*16,strong].reshape(-1,16).mean(axis=1)
        t=(positions[:len(block)*16]/RATE).reshape(-1,16).mean(axis=1)
        correction=float(np.polyfit(t,np.unwrap(np.angle(block)),1)[0]/(2*np.pi))
        if abs(correction)>20000:raise ValueError('public training fine-CFO estimate outside coarse uncertainty')
        cfo+=correction
        fine_training={'preamble_sample':int(peak),'receiver':strong,'correction_hz':correction,
            'uses_payload':False,'last_training_sample':float(positions[-1])}
        break
    if fine_training is None:raise ValueError('no complete public training for fine CFO')
    source=y.conj() if conjugated else y
    corrected=source*np.exp(-2j*np.pi*cfo*time)[:,None]
    if dc_block:corrected=signal.lfilter([1,-1],[1,-.995],corrected,axis=0)
    mf=np.stack([signal.lfilter(h,[1],corrected[:,c]) for c in range(2)],axis=1)
    correlations=np.stack([signal.correlate(mf[:,c],reference,mode='valid',method='fft') for c in range(2)],axis=1)
    cs=np.r_[0,np.cumsum(np.sum(abs(mf)**2,axis=1))]
    power=(cs[len(reference):]-cs[:-len(reference)])/SPS
    score=np.sum(abs(correlations)**2,axis=1)/np.maximum(1e-20,len(pre)*power)
    peaks,_=signal.find_peaks(score,distance=int(period*SPS*.8),height=max(.12,float(score.max())*.45))
    peakpos=[]
    for k in peaks:
        if not 0<k<len(score)-1:continue
        a,b,c=score[k-1:k+2];den=a-2*b+c
        frac=float(.5*(a-c)/den) if den else 0
        if abs(frac)>1:frac=0
        peakpos.append(float(k+frac))
    if len(peakpos)<3:raise ValueError(f'Only {len(peakpos)} public preambles detected; coarse score {periodic_score}, correlation max {score.max()}')
    peakpos=np.array(peakpos);numbers=np.rint((peakpos-peakpos[0])/(period*SPS))
    slope,intercept=np.polyfit(numbers,peakpos,1)
    sample_period=slope/period
    sfo=(sample_period/SPS-1)*1e6
    report={'coarse_CFO_hz':coarse_cfo,'tracking_NCO_CFO_hz':cfo,'periodic_score':periodic_score,
        'fine_CFO_public_training':fine_training,'wrapped_CFO_hz':wrapped_cfo,
        'IQ_conjugated':conjugated,'acquisition_method':'public_preamble_CFO_alias_bank',
        'post_NCO_DC_blocker_alpha':.995 if dc_block else None,'acquisition_hypotheses':hypotheses,
        'sample_rate_offset_ppm':float(sfo),'preamble_count':len(peakpos),
        'sample_period_fit_residual_rms':float(np.std(peakpos-np.polyval([slope,intercept],numbers))),
        'all_preamble_samples':peakpos.tolist(),
        'timing_model':'offline fit across public preambles; not a causal PL loop proof'}
    return mf,peakpos,sample_period,report
