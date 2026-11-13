#!/usr/bin/env python3
"""G0 candidate: bounded calibration, differential CFO and past-only timing.

Floating reference only. No claim of RTL/fixed-point service or full G0 pass.
Only public preamble/training is accepted. Startup calibration and the frame
used to estimate CFO are discarded; NCO/filter state restarts are explicit.
Pilot lookahead in the downstream receiver is separately bounded, not part
of this acquisition API. Processing latency budgets still need RTL proof.
"""
import numpy as np
from scipy import signal
from g0_sounder import RATE,SPS,PREAMBLE_START,TRAIN_START,TRAIN_LENGTH,rrc
from g0_public_acquisition import interpolate


def calibrated_input(samples,period):
    end=period*SPS
    commit=end+2048
    if len(samples)<=commit+6000:raise ValueError('insufficient samples for calibration and public training')
    y=np.asarray(samples,dtype=complex).copy()
    # Frozen after a bounded interval. Appending/changing future samples must
    # not alter this offset, notch decision, frequency or commit boundary.
    y-=y[:end].mean(axis=0)
    v=y[:end];win=512;choices=[]
    for rx in range(2):
        z=v[:-1,rx].conj()*v[1:,rx]
        cs=np.r_[0,np.cumsum(z)];cp=np.r_[0,np.cumsum(abs(v[:,rx])**2)]
        cross=cs[win:]-cs[:-win]
        pa=cp[win:-1]-cp[:-win-1];pb=cp[win+1:]-cp[1:-win]
        score=abs(cross)**2/np.maximum(1e-20,pa*pb)
        score[pa<.05*np.mean(abs(v[:,rx])**2)*win]=0
        k=int(np.argmax(score));choices.append((float(score[k]),rx,k))
    confidence,rx,k=max(choices)
    info={'coherence_score':confidence,'receiver':rx,'window_samples':[k,k+win+1],
          'calibration_window_end_sample':end,'calibration_commit_sample':commit,
          'estimation_latency_budget_samples':2048,'processing_latency_verified_in_RTL':False,
          'enabled':confidence>=.93,'threshold':.93}
    if info['enabled']:
        block=v[k:k+win+1,rx]
        frequency=float(np.polyfit(np.arange(len(block))/RATE,np.unwrap(np.angle(block)),1)[0]/(2*np.pi))
        pole=np.exp(2j*np.pi*frequency/RATE)
        y[commit:]=signal.lfilter([1,-pole],[1,-.995*pole],y[commit:],axis=0)
        info.update({'frequency_hz':frequency,'pole_radius':.995})
    y[:commit]=0
    return y,info


def differential_scores(y,pre):
    d=pre[:-1].conj()*pre[1:];d-=np.mean(d)
    ref=np.zeros(len(d)*SPS,dtype=complex);ref[::SPS]=d
    z=y[:-SPS].conj()*y[SPS:];power=np.sum(abs(z)**2,axis=1)
    energy=np.zeros(len(z))
    for phase in range(SPS):
        cs=np.r_[0,np.cumsum(power[phase::SPS])]
        values=cs[len(d):]-cs[:-len(d)]
        energy[phase:phase+len(values)*SPS:SPS]=values
    scores=[];correlations=[]
    for sense in [False,True]:
        template=ref.conj() if sense else ref
        co=np.stack([signal.correlate(z[:,rx],template,mode='valid',method='fft') for rx in range(2)],axis=1)
        score=np.sum(abs(co)**2,axis=1)/np.maximum(1e-20,np.sum(abs(ref)**2)*energy[:len(co)])
        scores.append(score);correlations.append(co)
    return np.array(scores),np.array(correlations),len(ref)+SPS


def acquire(samples,period,public,dc_block=True,frontend=None,differential_selector=None,startup_estimator=None):
    if public.shape!=(1408,2):raise ValueError('only public prefix accepted')
    if startup_estimator is not None and (frontend is None or differential_selector is None):
        raise ValueError('integer startup requires explicit integer coarse descriptor and frontend')
    y,notch=calibrated_input(samples,period);h=rrc();time=np.arange(len(y))/RATE
    frontend_reports=[];differential_dispatch_checks=[]
    def filter_at(source,cfo,start,nco_config=None):
        if frontend is not None:
            kwargs={'nco_config':nco_config} if nco_config is not None else {}
            result,info=frontend(source,cfo,start,dc_block,**kwargs)
            frontend_reports.append(info)
            return result
        corrected=np.zeros_like(y)
        if nco_config is None:
            corrected[start:]=source[start:]*np.exp(-2j*np.pi*cfo*time[start:])[:,None]
        else:
            phase=(np.arange(len(source)-start,dtype=np.uint64)*np.uint64(nco_config['step_u32'])+
                   np.uint64(nco_config['phase_u32']))&np.uint64(0xffffffff)
            corrected[start:]=source[start:]*np.exp(2j*np.pi*phase.astype(float)/(1<<32))[:,None]
        if dc_block:corrected[start:]=signal.lfilter([1,-1],[1,-.995],corrected[start:],axis=0)
        return signal.lfilter(h,[1],corrected,axis=0)
    selector=None
    if differential_selector is None:
        scores,correlations,reference_samples=differential_scores(y,public[256:384,0])
        score_count=len(scores[0])
    else:
        selector=differential_selector(y,public[256:384,0])
        score_count=selector.score_count;reference_samples=512
    search=notch['calibration_commit_sample']+1600
    stage=None;final_nco_config=None;startup_dispatch_checks=[]
    while search<score_count-4096:
        if selector is None:
            hits=np.flatnonzero(np.max(scores[:,search:],axis=0)>.20)
            if not len(hits):break
            first=search+int(hits[0]);end=min(first+512,len(scores[0]))
            sense,offset=np.unravel_index(np.argmax(scores[:,first:end]),(2,end-first));k=first+int(offset)
            raw_cfo=float(np.angle(correlations[sense,k,np.argmax(abs(correlations[sense,k]))])*RATE/(2*np.pi*SPS))
            differential_score=float(scores[sense,k])
        else:
            selected=selector.next_candidate(search)
            if selected is None:break
            first=selected['first_hit_index'];end=selected['window_end_exclusive']
            k=selected['selected_start_index'];sense=selected['sense']
            raw_cfo=selected['phase']['applied_raw_CFO_hz']
            differential_score=selected['score_float_diagnostic']
        known=end+reference_samples+128
        cfo=-raw_cfo if sense else raw_cfo
        source=y.conj() if sense else y
        nco_config=selector.nco_configuration(selected,known) if selector is not None else None
        mf=filter_at(source,cfo,known,nco_config)
        if selector is not None and frontend is not None:
            expected_step=selected['phase']['coarse_NCO_step_u32']
            expected_phase=nco_config['phase_u32']
            actual=frontend_reports[-1]
            if actual['NCO_step_u32']!=expected_step or actual['NCO_phase_u32']!=expected_phase:
                raise ValueError('coarse frontend configuration differs from integer candidate descriptor')
            differential_dispatch_checks.append({'commit_endpoint_sample':known,
                'expected_step_u32':expected_step,'actual_step_u32':actual['NCO_step_u32'],
                'expected_phase_u32':expected_phase,'actual_phase_u32':actual['NCO_phase_u32'],
                'phase_equation':nco_config['phase_equation'],
                'phase_origin_index48':nco_config['phase_origin_index48'],
                'phase_offset_u48':nco_config['offset_u48'],
                'directed_phase_s33':nco_config['directed_phase_s33'],
                'explicit_integer_configuration':actual.get('NCO_configuration_reconstructed_from_float_Hz') is False,
                'raw_phase_CFO_hz':selected['phase']['sense_corrected_CFO_hz'],
                'applied_CFO_hz':cfo,'bit_exact':True})
        peak=k+len(h)//2
        training=np.arange(TRAIN_START[0]+32,TRAIN_START[0]+TRAIN_LENGTH-32)
        positions=peak+(training-PREAMBLE_START)*SPS
        keep=positions>known+len(h)*2;training=training[keep];positions=positions[keep]
        search=end
        if len(training)<128 or positions[-1]>=len(y)-len(h):continue
        commit=int(np.ceil(positions[-1]))+512
        if startup_estimator is None:
            g=interpolate(mf,positions)*np.conj(public[training,0])[:,None]
            rx=int(np.argmax(np.mean(abs(g)**2,axis=0)))
            block=g[:len(g)//16*16,rx].reshape(-1,16).mean(axis=1)
            t=(positions[:len(block)*16]/RATE).reshape(-1,16).mean(axis=1)
            correction=float(np.polyfit(t,np.unwrap(np.angle(block)),1)[0]/(2*np.pi))
            coherent=g[:,rx]*np.exp(-2j*np.pi*correction*positions/RATE)
            quality=float(abs(coherent.mean())**2/np.mean(abs(coherent)**2))
            if abs(correction)>20000 or quality<.40:continue
        else:
            startup_result=startup_estimator(interpolate(mf,positions),positions,training,public,nco_config,commit,0)
            if not startup_result['accepted']:continue
            correction=startup_result['fine_CFO_correction_hz']
            quality=startup_result['quality_float_diagnostic']
            final_nco_config=startup_result['final_nco_config']
        stage={'coarse_peak_sample':peak,'coarse_known_sample':known,'coarse_CFO_hz':cfo,
            'coarse_last_sample_used':end+reference_samples,'coarse_latency_budget_samples':128,
            'fine_CFO_correction_hz':correction,'last_training_sample':float(positions[-1]),
            'training_symbols_used':len(training),'training_coherent_fraction':quality,
            'NCO_commit_sample':commit,'estimation_latency_budget_samples':512,
            'processing_latency_verified_in_RTL':False,'IQ_conjugated':bool(sense),
            'differential_public_score':differential_score}
        cfo+=correction
        break
    if stage is None:raise ValueError('causal differential acquisition found no valid public TX0 training')
    # Restart final NCO/filter epoch after estimation is available. Earlier
    # samples cannot be decoded with a frequency learned in their future.
    source=y.conj() if sense else y
    mf=filter_at(source,cfo,commit,final_nco_config)
    if startup_estimator is not None:
        actual=frontend_reports[-1]
        if actual['NCO_step_u32']!=final_nco_config['step_u32'] or actual['NCO_phase_u32']!=final_nco_config['phase_u32']:
            raise ValueError('final frontend differs from exact startup NCO descriptor')
        startup_dispatch_checks.append({'commit_endpoint_sample':commit,
            'expected_step_u32':final_nco_config['step_u32'],'actual_step_u32':actual['NCO_step_u32'],
            'expected_phase_u32':final_nco_config['phase_u32'],'actual_phase_u32':actual['NCO_phase_u32'],
            'explicit_integer_configuration':actual.get('NCO_configuration_reconstructed_from_float_Hz') is False,
            'phase_origin_index48':final_nco_config['phase_origin_index48'],
            'phase_offset_u48':final_nco_config['offset_u48'],'bit_exact':True})
    # Extra64 PUBLIC training symbols remove the repeated-preamble ambiguity.
    pre=public[PREAMBLE_START:TRAIN_START[0]+64,0]
    ref=np.zeros(len(pre)*SPS,dtype=complex);ref[::SPS]=pre
    co=np.stack([signal.correlate(mf[:,rx],ref,mode='valid',method='fft') for rx in range(2)],axis=1)
    cs=np.r_[0,np.cumsum(np.sum(abs(mf)**2,axis=1))]
    power=(cs[len(ref):]-cs[:-len(ref)])/SPS
    score=np.sum(abs(co)**2,axis=1)/np.maximum(1e-20,len(pre)*power)
    peaks=[];known_times=[];search=commit+1600
    while search<len(score)-8:
        hits=np.flatnonzero(score[search:]>.50)
        if not len(hits):break
        start=search+int(hits[0]);end=min(start+8,len(score)-1)
        k=start+int(np.argmax(score[start:end]))
        if 0<k<len(score)-1:
            a,b,c=score[k-1:k+2];den=a-2*b+c;frac=float(.5*(a-c)/den) if den else 0
            if abs(frac)>1:frac=0
            peaks.append(float(k+frac));known_times.append(end+len(ref))
        search=k+int(period*SPS*.8)
    if len(peaks)<2:raise ValueError('not enough causal public peaks for timing lock')
    steps=[];states=[];locked=[];history=[];events=[]
    for i,peak in enumerate(peaks):
        history.append((i,peak));history=history[-4:]
        if len(history)<2:continue
        start=history[0][0];past=np.array([value for _,value in history])
        frames=np.rint((past-past[0])/(period*SPS))
        step=float(np.polyfit(frames,past,1)[0]/period)
        if abs(step/SPS-1)>.0002:
            events.append({'sample':peak,'reason':'SFO_outside_200ppm','SFO_ppm':(step/SPS-1)*1e6})
            history=[(i,peak)]
            continue
        locked.append(peak);steps.append(step);states.append({'preamble_sample':peaks[i],'known_at_sample':known_times[i],
            'earliest_peak_used':start,'latest_peak_used':i,'sample_period':step,'SFO_ppm':(step/SPS-1)*1e6})
    if not locked:raise ValueError('no period inside causal timing bounds')
    report={'acquisition_method':'bounded_quiet_notch_differential_past_timing',
        'quiet_notch':notch,'startup':stage,'tracking_NCO_CFO_hz':cfo,'coarse_CFO_hz':stage['coarse_CFO_hz'],
        'IQ_conjugated':bool(sense),'post_NCO_DC_blocker_alpha':.995 if dc_block else None,
        'preamble_count':len(peaks),'all_preamble_samples':peaks,'timing_states':states,'timing_reacquisition_events':events,
        'startup_until_first_timing_lock_sample':states[0]['known_at_sample'] if states else None,
        'sample_rate_offset_ppm':float(np.median([s['SFO_ppm'] for s in states])),
        'timing_model':'current and up to3 past public peaks only; first uncalibrated frame discarded',
        'uses_expected_payload_or_session':False,'g0_passed':False}
    if frontend is not None:report['frontend_candidate_calls']=frontend_reports
    if selector is not None:
        report['differential_candidate_calls']=selector.calls
        report['differential_candidate_scan_chunks']=selector.scan_chunks
        report['differential_candidate_dispatch_checks']=differential_dispatch_checks
        report['differential_candidate_scope']='signed16/direct127/rational512/CORDIC coarse selection only; calibration/fine CFO/quality/timing remain float'
    if startup_estimator is not None:
        report['startup_candidate_calls']=startup_estimator.calls
        report['startup_candidate_dispatch_checks']=startup_dispatch_checks
        report['startup_candidate_scope']='integer variable-M CFO/quality and exact rational final NCO; calibration/timing remain float, no RTL dispatch claim'
        report['differential_candidate_scope']='integer coarse and startup estimators; calibration/timing remain float'
    return mf,np.array(locked),np.array(steps),report
