#!/usr/bin/env python3
"""G0 erasure budgets from real CRC traces plus explicit burst controls.

This evaluates an ideal MDS erasure-count condition, not RF remeasurement or
PL decoder throughput. Only k20/n32/depth1 matches the measured waveform.
Other mappings are counterfactual replay estimates; partial groups are not
concatenated across captures. No expected payload enters this analysis.
"""
import argparse
import json
from pathlib import Path
import numpy as np
from capacity_budget import residual_probability


def replay(first,good,k,n,depth,mapping='rotating'):
    if mapping not in ('round_robin','rotating'):raise ValueError('unknown mapping')
    span=n*depth;start=((first+span-1)//span)*span
    groups=(first+len(good)-start)//span
    if groups<1:return {'complete_groups':0,'complete_blocks':0,'status':'insufficient_contiguous_observation'}
    selected=np.asarray(good[start-first:start-first+groups*span],dtype=bool).reshape(groups,n,depth)
    columns=np.arange(depth)
    if mapping=='rotating':
        # For even D, simple round-robin permanently assigns every bank to
        # one TX parity. Rotating the bank by symbol number alternates TXs.
        selected=np.stack([selected[:,r,(columns-r)%depth] for r in range(n)],axis=1)
    missing=(~selected).sum(axis=1);failed=missing>n-k
    source_missing=(~selected[:,:k]).sum(axis=1)
    return {'complete_groups':groups,'complete_blocks':groups*depth,'failed_blocks':int(failed.sum()),
        'group_packets':span,'group_nominal_span_ms':span/800*1000,
        'block_failure_fraction':float(failed.mean()),'worst_erased_symbols_per_block':int(missing.max()),
        'source_symbols_missing_after_ideal_FEC':int(source_missing[failed].sum()),
        'packet_loss_in_complete_groups':float((~selected).mean()),'first_complete_group_packet_seq':start,
        'mapping':mapping,'decoder_exercised':False}


def longest_bad(good):
    best=current=0
    for value in good:
        current=0 if value else current+1;best=max(best,current)
    return best


def synthetic(name,packets,rng):
    if name=='IID15pct':return rng.random(packets)>=.15
    if name=='paired_TX_15pct':return np.repeat(rng.random((packets+1)//2)>=.15,2)[:packets]
    if name=='weak_TX1_30pct':
        good=np.ones(packets,dtype=bool);good[1::2]=rng.random(packets//2)>=.30;return good
    mean=int(name.removeprefix('Gilbert15pct_mean_burst_'))
    leave=1/mean;enter=.15*leave/.85;bad=False;good=np.empty(packets,dtype=bool)
    for i,value in enumerate(rng.random(packets)):
        good[i]=not bad
        bad=value>=leave if bad else value<enter
    return good


def main(a):
    profile=json.loads(a.profile.read_text());evidence=json.loads(a.evidence.read_text())
    rows=[];controls=[]
    options=profile['fec_candidates'];depths=profile['interleave_blocks_candidates']
    for run in evidence['runs']:
        trace=run['loss_trace'];good=np.array([b=='1' for b in trace['bits']])
        if len(good)!=run['expected_packets_in_observed_range'] or good.sum()!=run['CRC_good_link_packets']:
            raise ValueError('trace differs from receipt')
        for k,n in options:
            for depth in depths:
                r=replay(trace['first_packet_seq'],good,k,n,depth)
                same=(k,n,depth)==(20,32,1)
                if same and (r['complete_blocks']!=run['eligible_complete_FEC_blocks'] or
                             r['complete_blocks']-r['failed_blocks']!=run['recovered_complete_FEC_blocks']):
                    raise ValueError('depth1 replay disagrees with actual data-decoder result')
                rows.append({'run':run['directory'],'k':k,'n':n,'depth':depth,
                    'same_mapping_as_actual_RF':same,'longest_bad_packet_run':longest_bad(good),**r})
    # Fully specified single-outage controls at every position within a
    # supergroup. Their worst case includes straddling group boundaries.
    for depth in depths:
        span=32*depth
        for outage_packets in (16,32,64):
            worst=0
            for offset in range(span):
                groups=3+(outage_packets+span-1)//span
                good=np.ones(span*groups,dtype=bool);good[span+offset:span+offset+outage_packets]=False
                r=replay(0,good,20,32,depth)
                worst=max(worst,r['failed_blocks'])
            controls.append({'depth':depth,'outage_packets':outage_packets,
                'equivalent_whole_PHY_periods':outage_packets/16,
                'worst_failed_blocks_over_all_start_phases':worst,'decoder_exercised':False})
    models=[];rng=np.random.default_rng(3109361)
    for name in ['IID15pct','paired_TX_15pct','weak_TX1_30pct']+['Gilbert15pct_mean_burst_'+str(n) for n in (4,8,16)]:
        good=synthetic(name,262144,rng)
        for k,n in options:
            for depth in depths:
                for mapping in ['rotating','round_robin']:
                    r=replay(0,good,k,n,depth,mapping)
                    models.append({'model':name,'k':k,'n':n,'depth':depth,
                        'IID15pct_exact_ideal_block_failure_probability':residual_probability(k,n,.15) if name=='IID15pct' else None,
                        **r})
    # A64-packet outage at offset13 intersects3 RS32 blocks with13+ losses
    # each. This catches accidental truncation at a short test-array end.
    control=np.ones(160,dtype=bool);control[45:109]=False
    if replay(0,control,20,32,1)['failed_blocks']!=3:
        raise ValueError('cross-group outage tail was truncated')
    result={'scope':'G0 ideal erasure-count replay/budget only, not additional RF or actual interleaved decoder tests',
        'profile':str(a.profile),'feedback_enabled':False,'actual_trace_replay':rows,
        'single_outage_controls':controls,'synthetic_models':models,
        'interleave_mapping':'group=floor(seq/(n*D)); symbol=floor((seq%(n*D))/D); bank=(seq%D+symbol)%D',
        'mapping_rationale':'bank rotation alternates TX parity for even depth; plain round-robin can isolate weak TX1 into half the blocks',
        'RS32_full_group_latency_ms':{str(d):32*d/800*1000 for d in depths},
        'physical_decoder_and_source_bytes_checked_only_for_measured_depth1':True,'g0_passed':False}
    a.output.write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({'real_runs':len(evidence['runs']),'real_replay_rows':len(rows),
        'synthetic_rows':len(models),'single_outage_controls':controls,'g0_passed':False}))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--profile',type=Path,required=True)
    p.add_argument('--evidence',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    main(p.parse_args())
