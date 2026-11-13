#!/usr/bin/env python3
"""Compare finite raw ADC records and optional PN9 word continuity.

PN9 oracle uses a serial x^9+x^5+1 LFSR, not the vendor's unrolled 24-bit
expressions. This is a digital BIST comparison, never RF BER acceptance.
"""
import argparse
import json
from pathlib import Path
import numpy as np


def pn9_next(word):
    state=int(word)&511
    output=0
    for _ in range(12):
        bit=((state>>8)^(state>>4))&1
        output=(output<<1)|bit
        state=((state<<1)|bit)&511
    return output


def inspect(path,expected_records,pn9=False,ad9361_bist=False):
    if path.stat().st_size != expected_records*16:raise ValueError('capture byte count differs')
    raw=np.fromfile(path,dtype=np.dtype([('tag','<u8'),('iq','<i2',(4,))]))
    tags=raw['tag'];index=tags&np.uint64((1<<48)-1);epochs=tags>>np.uint64(48)
    delta=np.diff(index.astype(np.int64))
    result={'scope':'G0 finite digital ADC capture; not antenna/RF BER/full G0',
        'records':len(raw),'record_bytes':16,'first_index':int(index[0]),'last_index':int(index[-1]),
        'epochs':np.unique(epochs).astype(int).tolist(),'index_gaps':int(np.count_nonzero(delta!=1)),
        'duplicate_or_reordered':int(np.count_nonzero(delta<=0)),
        'iq_min':raw['iq'].min(axis=0).astype(int).tolist(),'iq_max':raw['iq'].max(axis=0).astype(int).tolist(),
        'iq_order':['I0','Q0','I1','Q1'],'RF_transmission':False,'g0_passed':False}
    ok=len(result['epochs'])==1 and result['index_gaps']==0
    if pn9:
        lut=np.array([pn9_next(w) for w in range(4096)],dtype=np.uint16)
        result['pn9']=[]
        for lane in range(4):
            words=raw['iq'][:,lane].astype(np.uint16)&4095
            mismatches=np.flatnonzero(lut[words[:-1]]!=words[1:])
            period=np.unique(words[:min(511,len(words))]).size
            nonzero=bool(np.all((words&511)!=0))
            passed=len(mismatches)==0 and nonzero and period==min(511,len(words))
            result['pn9'].append({'lane':lane,'word_transitions_compared':len(words)-1,
                'mismatches':len(mismatches),'first_mismatch_offsets':mismatches[:8].astype(int).tolist(),
                'unique_words_first_period':period,'nonzero_lfsr_states':nonzero,'passed':bool(passed)})
            ok=ok and passed
    if ad9361_bist:
        # AD9361's device-specific 16-bit pattern is selected by this FPGA
        # when the Linux ADC selector is called "PN9" (numeric zero). It is
        # NOT the standard 9-bit polynomial above. I contains state[15:4];
        # reversed Q contains {I[7:0],state[3:0]}. At each sample the state
        # shifts left once with feedback parity of old bits 15:4,2:1.
        reverse=np.array([int(f'{i:012b}'[::-1],2) for i in range(4096)],dtype=np.uint16)
        samples=raw['iq'].astype(np.uint16)&4095
        result['ad9361_bist']=[]
        for receiver in range(2):
            i=samples[:,receiver*2];q=reverse[samples[:,receiver*2+1]]
            states=(i.astype(np.uint32)<<4)|(q&15)
            parity=np.zeros(len(states),dtype=np.uint32)
            for tap in list(range(4,16))+[1,2]:parity^=(states>>tap)&1
            expected=((states[:-1]<<1)&65535)|parity[:-1]
            bad_state=np.flatnonzero(states[1:]!=expected)
            bad_iq=np.flatnonzero((i&255)!=(q>>4))
            distinct=np.unique(states[:min(65535,len(states))]).size
            nonzero=bool(np.all(states!=0))
            passed=not len(bad_state) and not len(bad_iq) and nonzero and distinct==min(65535,len(states))
            result['ad9361_bist'].append({'receiver':receiver,'state_transitions_compared':len(states)-1,
                'state_errors':len(bad_state),'iq_relation_checks':len(states),'iq_relation_errors':len(bad_iq),
                'unique_states_first_period':distinct,'nonzero_states':nonzero,'passed':bool(passed)})
            ok=ok and passed
        result['pattern']='AD9361 device-specific 16-bit PRBS; Linux ADC selector PN9=0; not standard PN9'
    result['digital_capture_passed']=bool(ok)
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('capture',type=Path)
    p.add_argument('--records',type=int,required=True)
    mode=p.add_mutually_exclusive_group()
    mode.add_argument('--pn9',action='store_true',help='Standard x^9+x^5+1 sequence, NOT AD9361 BIST')
    mode.add_argument('--ad9361-bist',action='store_true',help='Device-specific AD9361 16-bit I/Q sequence')
    p.add_argument('--output',type=Path,required=True)
    a=p.parse_args()
    if a.records<2:raise ValueError('at least two records required')
    result=inspect(a.capture,a.records,a.pn9,a.ad9361_bist)
    a.output.write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result))
    if not result['digital_capture_passed']:raise SystemExit(1)
