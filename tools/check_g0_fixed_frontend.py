#!/usr/bin/env python3
"""G0 hybrid receiver: integer NCO/RRC models, remaining PHY explicitly float."""
import argparse
import json
from pathlib import Path
import zlib
import numpy as np
from g0_fixed_frontend_model import fixed_frontend
from g0_packet_probe import receive
from g0_packet_fec import source_pattern
from check_g0_packet_model import simulate
from check_g0_causal_model import bounds


def compare(blocks):
    if not blocks:raise ValueError('no restored block')
    for block,data in blocks.items():
        if data!=source_pattern(block).tobytes():raise ValueError('post-receive source mismatch')
    return sum(len(x) for x in blocks.values())


def main(a):
    out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);rows=[]
    def check(name,y,expected_failure=False):
        baseline,basebytes=receive(y,causal=True);bounds(baseline);compare(basebytes)
        r,blocks=receive(y,causal=True,frontend=fixed_frontend);bounds(r);equal=compare(blocks)
        if not expected_failure and len(blocks)!=r['eligible_complete_FEC_blocks']:raise ValueError('fixed island FEC failure '+name)
        if set(blocks)!=set(basebytes) or any(blocks[k]!=basebytes[k] for k in blocks):raise ValueError('delivered block regression '+name)
        if expected_failure and not (r['CRC_bad_PHY_headers'] and len(blocks)<r['eligible_complete_FEC_blocks']):
            raise ValueError('bad PHY header no longer exercises failed block')
        for mode,report in [('float',baseline),('fixed_islands',r)]:
            (out/f'{name}-{mode}.json').write_text(json.dumps(report,indent=2)+'\n')
        row={'name':name,'samples':len(y),'float_good_packets':baseline['CRC_good_link_packets'],
             'fixed_island_good_packets':r['CRC_good_link_packets'],'expected_packets':r['expected_packets_in_observed_range'],
             'eligible_blocks':r['eligible_complete_FEC_blocks'],'restored_blocks':len(blocks),'source_bytes_equal':equal,
             'same_delivered_blocks_as_float':True,'passed':True}
        rows.append(row);print(json.dumps(row),flush=True)
        return r,blocks
    for name,damage,reverse,sf in [('clean',None,False,None),('reversed_IQ',None,True,None),
                                  ('burst','payload_burst',False,4),('bad_header','PHY_header',False,5)]:
        y=4096*simulate(a.waveform,damage,reverse,count_frames=8,damage_superframe=sf)
        check('model_'+name,y,expected_failure=damage=='PHY_header')
    for index,path in enumerate(a.capture):
        if path.stat().st_size%16:raise ValueError('truncated ADC record')
        raw=np.fromfile(path,dtype=np.dtype([('tag','<u8'),('iq','<i2',(4,))]))
        crc=f'{zlib.crc32(raw):08x}'
        if crc!=json.loads((path.parent/'receipt.json').read_text())['capture']['crc32']:raise ValueError('capture CRC differs')
        if np.any(np.diff(raw['tag'])!=1):raise ValueError('ADC tag gap')
        y=raw['iq'][:,0::2]+1j*raw['iq'][:,1::2]
        report,blocks=check(path.parent.name,y);rows[-1]['capture_crc32']=crc
        if index!=0:continue
        cut=800000;prefix=y[:cut];rng=np.random.default_rng(323241)
        r0,b0=receive(prefix,causal=True,frontend=fixed_frontend)
        changed=y.copy();rms=float(np.sqrt(np.mean(abs(prefix)**2)))
        changed[cut:]=rng.normal(0,3*rms,changed[cut:].shape)+1j*rng.normal(0,3*rms,changed[cut:].shape)
        r1,b1=receive(changed,causal=True,frontend=fixed_frontend);bounds(r0);bounds(r1)
        if any(blocks.get(k)!=v or b1.get(k)!=v for k,v in b0.items()):raise ValueError('future changed decoded prefix')
        for r in (r0,r1):
            for key in ('coarse_peak_sample','coarse_known_sample','NCO_commit_sample'):
                if r['startup'][key]!=report['startup'][key]:raise ValueError('future changed causal config boundary')
            if len(r['frontend_candidate_calls'])!=len(report['frontend_candidate_calls']):
                raise ValueError('future changed frontend configuration count')
            for ca,cb in zip(r['frontend_candidate_calls'],report['frontend_candidate_calls']):
                for key in ('commit_endpoint_sample','NCO_phase_u32','NCO_step_u32'):
                    if ca[key]!=cb[key]:raise ValueError('future changed integer NCO configuration')
        frames={f['superframe']:f['packets'] for f in r0['frames'] if f['PHY_header_ok']}
        for r in (report,r1):
            others={f['superframe']:f['packets'] for f in r['frames'] if f['PHY_header_ok']}
            if any(others.get(k)!=v for k,v in frames.items()):raise ValueError('future changed prefix CRC')
        row={'name':'prefix_and_corrupted_future','samples':cut,'source_bytes_equal':compare(b0),
             'integer_NCO_configuration_and_past_CRC_unchanged':True,'passed':True}
        rows.append(row);print(json.dumps(row),flush=True)
        (out/'prefix.json').write_text(json.dumps(r0,indent=2)+'\n')
    receipt={'scope':'G0 integer NCO/RRC software models inside FLOAT calibration/notch/estimation/interpolation/PHY',
        'cases':rows,'full_fixed_point_PHY_or_connected_RTL_verified':False,
        'new_RF_transmissions':False,'g0_passed':False}
    (out/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--waveform',type=Path,required=True)
    p.add_argument('--capture',type=Path,action='append',default=[]);p.add_argument('--output',type=Path,required=True)
    main(p.parse_args())
