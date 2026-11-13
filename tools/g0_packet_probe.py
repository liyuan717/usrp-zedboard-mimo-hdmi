#!/usr/bin/env python3
"""Finite G0 VRL3/CRC/FEC RF probe; unfrozen diagnostic profile 0xf0.

Generate and decode are independent: decode takes raw ADC samples and public
constants only. verify compares source bytes AFTER decoding. Payloads are
synthetic coding symbols, not video. All RF processing here is offline;
the final RF data plane, including FEC, must run in PL.
"""
import argparse
import itertools
import json
import struct
import zlib
from pathlib import Path
import numpy as np
from scipy import signal
from g0_sounder import RATE,SPS,PREAMBLE_START,TRAIN_START,TRAIN_LENGTH,rrc
from g0_public_acquisition import acquire,interpolate
from g0_packet_fec import K,N,SIZE,GENERATOR,encode,recover,source_pattern

PERIOD=20000
HEADER_START=1408
HEADER_SYMBOLS=384
DATA_START=HEADER_START+HEADER_SYMBOLS
PACKETS_PER_STREAM=8
PACKET_BYTES=32+SIZE+4
STREAM_BYTES=PACKETS_PER_STREAM*PACKET_BYTES
DATA_SYMBOLS=STREAM_BYTES*4
PILOTS=DATA_START+np.arange(DATA_SYMBOLS//64)*66
DATA_INDICES=(PILOTS[:,None]+np.arange(2,66)[None,:]).ravel()
PAYLOAD_END=int(DATA_INDICES[-1])+1
PHY=struct.Struct('<2sBBIIHHBBH')
LINK=struct.Struct('<4sBBHIIIIIHH')


class ChannelEstimateInvalid(ValueError):
    """An explicitly rejected training window, not an arithmetic/program error."""


def public_prefix():
    rng=np.random.default_rng(0x56334730)
    def qpsk(n):
        bits=rng.integers(0,2,(n,2))
        return ((1-2*bits[:,0])+1j*(1-2*bits[:,1]))/np.sqrt(2)
    s=np.zeros((1408,2),dtype=complex)
    s[256:384,0]=np.tile(qpsk(16),8)
    for tx in range(2):s[TRAIN_START[tx]:TRAIN_START[tx]+TRAIN_LENGTH,tx]=qpsk(TRAIN_LENGTH)
    return s


def with_crc(data):return data+struct.pack('<I',zlib.crc32(data))


def good_crc(data):return len(data)>=4 and zlib.crc32(data[:-4])==struct.unpack('<I',data[-4:])[0]


def phy_header(session,sf):
    return with_crc(PHY.pack(b'VP',1,0xf0,session,sf,STREAM_BYTES,STREAM_BYTES,8,8,0))


def parse_phy(data):
    if len(data)!=24 or not good_crc(data):return None,'CRC'
    magic,major,profile,session,sf,b0,b1,n0,n1,flags=PHY.unpack(data[:-4])
    if (magic,major,profile,b0,b1,n0,n1,flags)!=(b'VP',1,0xf0,STREAM_BYTES,STREAM_BYTES,8,8,0):
        return None,'profile_or_bounds'
    return {'session':session,'superframe':sf},None


def link_packet(session,sf,seq,coded):
    block,index=divmod(seq,N)
    return with_crc(LINK.pack(b'VRL3',1,1 if index<K else 2,0,session,sf,seq,0xffffffff,block,index,SIZE)+coded.tobytes())


def parse_link(data,session,sf,seq):
    if len(data)!=PACKET_BYTES or not good_crc(data):return None,'CRC'
    magic,major,kind,flags,sid,frame,packet,hint,block,index,size=LINK.unpack(data[:32])
    if (magic,major,kind,flags,sid,frame,packet,hint,block,index,size)!=(
            b'VRL3',1,1 if seq%N<K else 2,0,session,sf,seq,0xffffffff,seq//N,seq%N,SIZE):
        return None,'identity_or_bounds'
    return data[32:-4],None


def map_bytes(data):
    bits=np.unpackbits(np.frombuffer(data,dtype=np.uint8),bitorder='big').astype(np.int8).reshape(-1,2)
    return ((1-2*bits[:,0])+1j*(1-2*bits[:,1]))/np.sqrt(2)


def demap(symbols):
    bits=np.stack([symbols.real<0,symbols.imag<0],axis=1).ravel()
    return np.packbits(bits,bitorder='big').tobytes()


def generate(out,frames,session):
    if frames<8 or frames>48 or frames%2:raise ValueError('8..48 even frames; finite duration <=.96s')
    out.mkdir(parents=True,exist_ok=False)
    prefix=public_prefix();h=rrc();cache={};maximum=0.;scale=.14
    with (out/'iq.fc32').open('wb') as f:
        for sf in range(frames):
            s=np.zeros((PERIOD,2),dtype=complex);s[:1408]=prefix
            bits=np.unpackbits(np.frombuffer(phy_header(session,sf),dtype=np.uint8),bitorder='big').astype(np.int8)
            s[HEADER_START:DATA_START,0]=np.repeat(1-2*bits,2)
            for tx in range(2):
                payload=bytearray()
                for slot in range(PACKETS_PER_STREAM):
                    seq=sf*16+slot*2+tx;block,index=divmod(seq,N)
                    if block not in cache:cache[block]=encode(source_pattern(block))
                    payload.extend(link_packet(session,sf,seq,cache[block][index]))
                s[DATA_INDICES,tx]=map_bytes(payload)
            s[PILOTS]=[1,0];s[PILOTS+1]=[0,1]
            up=np.zeros((PERIOD*SPS,2),dtype=complex);up[::SPS]=s
            iq=signal.lfilter(h,[1],up,axis=0)*scale
            packed=np.empty((len(iq),4),dtype='<f4');packed[:,0::2]=iq.real;packed[:,1::2]=iq.imag
            maximum=max(maximum,float(np.max(np.abs(packed))))
            if maximum>.125:raise ValueError('G0 amplitude bound exceeded')
            packed.tofile(f)
    result={'scope':'unfrozen G0 diagnostic VRL3 packet profile, synthetic payload, not video or production PL',
        'profile_id':240,'feedback_enabled':False,'FMCOMMS3_TX_enabled':False,'session':session,
        'sample_rate_hz':RATE,'symbol_rate_hz':RATE//SPS,'period_symbols':PERIOD,'period_samples':PERIOD*SPS,
        'repetitions':frames,'total_samples':frames*PERIOD*SPS,'duration_seconds':frames*PERIOD*SPS/RATE,
        'max_IQ_component':maximum,'scale':scale,'PHY_header_bytes':24,'PHY_header_encoding':'BPSK repeat2, TX0 only',
        'PHY_header_symbols':[HEADER_START,DATA_START],'payload_end_symbol':PAYLOAD_END,
        'tail_zero_symbols':PERIOD-PAYLOAD_END,'pilot_pairs':len(PILOTS),'packets_per_stream':8,
        'link_packet_bytes':PACKET_BYTES,'coded_link_bps_calculated':16*PACKET_BYTES*8/.02,
        'FEC':{'k':K,'n':N,'symbol_bytes':SIZE,'gf_polynomial':285,'interleave_blocks':1},
        'source_symbol_bps_calculated':16*SIZE*8/.02*K/N,
        'video_upper_bps_before_tail_fragment_waste_calculated':16*(SIZE-40)*8/.02*K/N,
        'production_use_allowed':False,'g0_passed':False}
    (out/'waveform.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result))


def channel(r,times,public):
    H=np.zeros((2,2),dtype=complex);frequencies=[];evm=[]
    for tx in range(2):
        k=np.arange(TRAIN_START[tx]+32,TRAIN_START[tx]+TRAIN_LENGTH-32)
        g=r[k]*np.conj(public[k,tx])[:,None]
        strong=int(np.argmax(np.mean(abs(g)**2,axis=0)))
        block=g[:len(g)//16*16,strong].reshape(-1,16).mean(axis=1)
        tb=times[k[:len(block)*16]].reshape(-1,16).mean(axis=1)
        f=float(np.polyfit(tb,np.unwrap(np.angle(block)),1)[0]/(2*np.pi))
        H[:,tx]=np.mean(g*np.exp(-2j*np.pi*f*times[k])[:,None],axis=0)
        model=public[k,tx,None]*H[None,:,tx]*np.exp(2j*np.pi*f*times[k])[:,None]
        evm.append(float(np.sqrt(np.mean(abs(r[k]-model)**2)/np.mean(abs(model)**2))))
        frequencies.append(f)
    return H,np.array(frequencies),evm


def receive(samples,dc_block=False,causal=False,frontend=None,sampler=None,channel_estimator=None,differential_selector=None,startup_estimator=None,wire_observer=None):
    public=public_prefix()
    acquisition_fn=acquire
    if causal:
        from g0_causal_acquisition import acquire as acquisition_fn
    if frontend is not None and not causal:raise ValueError('candidate frontend requires causal acquisition')
    if sampler is not None and not causal:raise ValueError('candidate sampler requires causal acquisition')
    if channel_estimator is not None and not causal:raise ValueError('candidate channel estimator requires causal acquisition')
    if differential_selector is not None and not causal:raise ValueError('candidate differential selector requires causal acquisition')
    if startup_estimator is not None and not causal:raise ValueError('candidate startup estimator requires causal acquisition')
    kwargs={'frontend':frontend} if frontend is not None else {}
    if differential_selector is not None:kwargs['differential_selector']=differential_selector
    if startup_estimator is not None:kwargs['startup_estimator']=startup_estimator
    mf,peaks,step,acquisition=acquisition_fn(samples,PERIOD,public,dc_block,**kwargs)
    steps=np.broadcast_to(step,peaks.shape)
    points=np.array([1+1j,1-1j,-1+1j,-1-1j])/np.sqrt(2)
    candidates=np.array(list(itertools.product(points,repeat=2)))
    rows=[];packets={};sessions=set();sampling_reports=[]
    for peak,step in zip(peaks,steps):
        positions=peak+(np.arange(PERIOD)-PREAMBLE_START)*step
        if positions[0]<len(rrc()) or positions[-1]>=len(samples)-len(rrc()):continue
        if sampler is None:
            r=interpolate(mf,positions);times=(positions-peak)/RATE
        else:
            r,positions,sampling_info=sampler(mf,peak,step,PERIOD,PREAMBLE_START)
            times=(positions-sampling_info['timing_origin_sample'])/RATE
            sampling_reports.append(sampling_info)
        try:
            H,freq,evm=(channel if channel_estimator is None else channel_estimator)(r,times,public)
        except ChannelEstimateInvalid as error:
            # No H means no header or payload decisions for this period. Keep
            # the public-timing erasure row so later valid periods can recover.
            rows.append({'preamble_sample':float(peak),'PHY_header_ok':False,
                         'PHY_header_error':'training_invalid','training_error':str(error)})
            continue
        k=np.arange(HEADER_START,DATA_START)
        model=H[None,:,0]*np.exp(2j*np.pi*freq[0]*times[k])[:,None]
        soft=np.real(np.sum(model.conj()*r[k],axis=1))/np.sum(abs(model)**2,axis=1)
        header=np.packbits(soft.reshape(-1,2).sum(axis=1)<0,bitorder='big').tobytes()
        info,why=parse_phy(header)
        row={'preamble_sample':float(peak),'PHY_header_ok':info is not None,'PHY_header_error':why,
            'H_real':H.real.tolist(),'H_imag':H.imag.tolist(),'condition_number':float(np.linalg.cond(H)),
            'training_EVM_rms':evm,'training_CFO_hz':(acquisition['tracking_NCO_CFO_hz']+freq).tolist()}
        rows.append(row)
        # CRC and bounded profile validation precede all use of header IDs,
        # lengths and packet counters. A bad header produces no payload bytes.
        if info is None:continue
        sf=info['superframe'];session=info['session'];sessions.add(session)
        row.update(info);phase=np.empty((len(DATA_INDICES),2));pilot_cfo=[]
        for tx in range(2):
            k=PILOTS+tx
            model=H[None,:,tx]*np.exp(2j*np.pi*freq[tx]*times[k])[:,None]
            z=np.sum(model.conj()*r[k],axis=1)/np.sum(abs(model)**2,axis=1)
            unwrapped=np.unwrap(np.angle(z));smooth=signal.savgol_filter(unwrapped,7,1)
            phase[:,tx]=2*np.pi*freq[tx]*times[DATA_INDICES]+np.interp(times[DATA_INDICES],times[k],smooth)
            pilot_cfo.append(float(acquisition['tracking_NCO_CFO_hz']+freq[tx]+np.polyfit(times[k],unwrapped,1)[0]/(2*np.pi)))
        effective=H[None,:,:]*np.exp(1j*phase)[:,None,:]
        prediction=np.einsum('nrt,kt->nrk',effective,candidates)
        metric=np.sum(abs(r[DATA_INDICES,:,None]-prediction)**2,axis=1)
        decisions=candidates[np.argmin(metric,axis=1)]
        # Decision EVM is diagnostic only; CRC determines usable packets.
        row['decision_EVM_rms']=float(np.sqrt(np.mean(np.min(metric,axis=1))/np.mean(np.sum(abs(r[DATA_INDICES])**2,axis=1))))
        row['pilot_CFO_hz']=pilot_cfo;row['packets']=[]
        for tx in range(2):
            data=demap(decisions[:,tx])
            for slot in range(PACKETS_PER_STREAM):
                seq=sf*16+slot*2+tx
                wire=data[slot*PACKET_BYTES:(slot+1)*PACKET_BYTES]
                # Optional finite offline diagnostics receive immutable bytes;
                # their own profile parser must check CRC and identities. The
                # legacy receiver's admission and FEC result stay unchanged.
                if wire_observer is not None:wire_observer(wire,session,sf,seq)
                payload,why=parse_link(wire,session,sf,seq)
                row['packets'].append({'packet_seq':seq,'tx':tx,'slot':slot,'accepted':payload is not None,'error':why})
                if payload is not None:
                    identity=(session,seq)
                    if identity in packets and packets[identity]!=payload:raise ValueError('conflicting valid duplicate')
                    packets[identity]=payload
    if len(sessions)>1:raise ValueError('diagnostic capture contains multiple sessions; no cross-session FEC')
    if not sessions:raise ValueError('no complete CRC-valid physical header')
    session=next(iter(sessions));valid=[r for r in rows if r['PHY_header_ok']]
    sfids=[r['superframe'] for r in valid]
    anchor=valid[0]
    for row in rows:
        relative=round((row['preamble_sample']-anchor['preamble_sample'])/(PERIOD*SPS))
        inferred=anchor['superframe']+relative
        if row['PHY_header_ok'] and row['superframe']!=inferred:
            raise ValueError('CRC-valid frame sequence conflicts with public timing')
        row['timing_inferred_superframe']=inferred
    # Include complete leading/trailing periods with a bad PHY header. Their
    # slots are erasures inferred from public timing and a valid anchor;
    # this does not recover their header or emit fabricated payload.
    lo=min(row['timing_inferred_superframe'] for row in rows)
    hi=max(row['timing_inferred_superframe'] for row in rows)
    if lo<0:raise ValueError('diagnostic frame wrap is not supported')
    if hi-lo>48:raise ValueError('unbounded or wrapped diagnostic sequence range')
    eligible=[b for b in range(lo*16//N,(hi+1)*16//N) if b*N>=lo*16 and (b+1)*N<=(hi+1)*16]
    blocks=[];restored={}
    for block in eligible:
        received={idx:packets[(session,block*N+idx)] for idx in range(N) if (session,block*N+idx) in packets}
        source=recover(received)
        if source is not None:restored[block]=source.tobytes()
        blocks.append({'block':block,'received_indices':sorted(received),'erased_indices':[i for i in range(N) if i not in received],
            'recovered':source is not None,'source_bytes_delivered':K*SIZE if source is not None else 0})
    expected_packets=(hi-lo+1)*16;good=len([key for key in packets if lo*16<=key[1]<(hi+1)*16])
    source_bytes=sum(len(v) for v in restored.values())
    result={'scope':'G0 offline RF packet/CRC/FEC diagnostic only, synthetic data; not video or production PL',
        **acquisition,'session':session,'sample_count':len(samples),'capture_seconds':len(samples)/RATE,
        'complete_physical_periods':len(rows),'CRC_good_PHY_headers':len(valid),
        'CRC_bad_PHY_headers':sum(r['PHY_header_error']=='CRC' for r in rows),
        'header_bounds_failures':sum(r['PHY_header_error']=='profile_or_bounds' for r in rows),
        'observed_superframe_range':[lo,hi],'missing_superframes_in_observed_range':sorted(set(range(lo,hi+1))-set(sfids)),
        'expected_packets_in_observed_range':expected_packets,'CRC_good_link_packets':good,
        'link_erased_or_missing_in_observed_range':expected_packets-good,'link_erasure_fraction':1-good/expected_packets,
        'eligible_complete_FEC_blocks':len(eligible),'recovered_complete_FEC_blocks':len(restored),
        'FEC_source_bytes_delivered':source_bytes,'delivered_source_bps_over_full_capture':source_bytes*8/(len(samples)/RATE),
        'source_bps_over_eligible_block_windows':source_bytes*8/(len(eligible)*.04) if eligible else None,
        'packet_goodput_bps_over_observed_range':good*SIZE*8/((hi-lo+1)*.02),
        'source_comparison':'not performed by receiver; use separate verify command',
        'pilot_phase_smoothing_points':7,'conservative_pilot_lookahead_symbols':7*66,
        'reference_delivery_contract':'batch after complete period; no claim of zero-lookahead symbol delivery or PL service latency',
        'feedback_enabled':False,'FMCOMMS3_TX_enabled':False,'g0_passed':False,'blocks':blocks,'frames':rows}
    if sampler is not None:result['sampling_candidate_calls']=sampling_reports
    if channel_estimator is not None:
        result['training_invalid_periods']=sum(r['PHY_header_error']=='training_invalid' for r in rows)
    return result,restored


def decode(capture,out,dc_block,causal=False):
    out.mkdir(parents=True,exist_ok=False)
    raw=np.fromfile(capture,dtype=np.dtype([('tag','<u8'),('iq','<i2',(4,))]))
    tags=raw['tag'];gaps=int(np.count_nonzero(np.diff(tags)!=1))
    if gaps:raise ValueError('raw ADC sample discontinuity; epoch reacquisition not implemented in G0 model')
    samples=raw['iq'][:,0::2]+1j*raw['iq'][:,1::2]
    result,restored=receive(samples,dc_block,causal)
    result['tag_gaps']=gaps
    result['ADC_clipped_IQ_words']=int(np.count_nonzero((raw['iq']<=-2048)|(raw['iq']>=2047)))
    for block,data in restored.items():(out/f'block-{block}.source.bin').write_bytes(data)
    (out/'receipt.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({k:v for k,v in result.items() if k not in ('frames','blocks','acquisition_hypotheses','all_preamble_samples','timing_states')}))


def verify(out):
    receipt=json.loads((out/'receipt.json').read_text());rows=[]
    for block in receipt['blocks']:
        if not block['recovered']:continue
        b=block['block'];data=(out/f'block-{b}.source.bin').read_bytes();expected=source_pattern(b).tobytes()
        rows.append({'block':b,'bytes_compared':len(data),'equal':data==expected})
    result={'scope':'separate post-decode synthetic source comparison; not production/video/G0 pass',
        'blocks':rows,'bytes_compared':sum(r['bytes_compared'] for r in rows),
        'all_compared_blocks_equal':bool(rows) and all(r['equal'] for r in rows),'g0_passed':False}
    (out/'verify.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result))
    if not result['all_compared_blocks_equal']:raise SystemExit(1)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='command',required=True)
    g=sub.add_parser('generate');g.add_argument('--output',type=Path,required=True);g.add_argument('--frames',type=int,default=40)
    g.add_argument('--session',type=lambda s:int(s,0),default=0x56335001)
    d=sub.add_parser('decode');d.add_argument('capture',type=Path);d.add_argument('--output',type=Path,required=True)
    d.add_argument('--dc-block',action='store_true')
    d.add_argument('--causal-acquisition',action='store_true')
    v=sub.add_parser('verify');v.add_argument('output',type=Path)
    a=p.parse_args()
    if a.command=='generate':generate(a.output,a.frames,a.session)
    elif a.command=='decode':decode(a.capture,a.output,a.dc_block,a.causal_acquisition)
    else:verify(a.output)
