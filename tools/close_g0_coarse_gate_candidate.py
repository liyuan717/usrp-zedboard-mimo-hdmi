#!/usr/bin/env python3
"""Join completed gate-only proofs before candidate integration; offline build provenance only."""
import argparse
import hashlib
import json
from pathlib import Path
import re
from g0_coarse_gate_implementation_probe import gate10

NAME='differential_score_cost_probe.sv'


def digest(p):return hashlib.sha256(p.read_bytes()).hexdigest()


def validate_coarse_replacement(name,baseline,current,proof_path):
    assert name==NAME and proof_path is not None,'unproved coarse gate change'
    proof=json.loads(proof_path.read_text())
    assert proof['baseline_sha256']==baseline and proof['candidate_sha256']==digest(current)
    assert proof['coarse_gate_only_transform'] and proof['fresh_raw_gate5_zero_gate10_SOURCE_exact']
    assert proof['original136_142_491_and_complete_accepted_stream_unchanged']
    assert proof['arithmetic_634_II8_stalls_and_two_compiled_faults_passed']
    assert proof['false_coarse_invalid_training_then_real_SOURCE_without_reset_passed']
    return {'name':name,'baseline_sha256':baseline,'compiled_sha256':proof['candidate_sha256'],
            'proof':str(proof_path),'candidate_gate':'strict1/10 instead of1/5',
            'training_CFO_quality_PHY_link_CRC_FEC_and_fixed8_unchanged':True,
            'whole_board_cost_or_timing_not_inferred_from_island':True}


def main(a):
    roots={name:getattr(a,name).resolve() for name in ('baseline','fresh','regression','arithmetic','implementation','recovery')}
    receipts={name:json.loads((p/'receipt.json').read_text()) for name,p in roots.items()}
    old,new,reg,arith,impl=(receipts[n] for n in ('baseline','fresh','regression','arithmetic','implementation'))
    baseline=roots['baseline']/NAME;candidate=roots['fresh']/NAME
    assert gate10(baseline.read_text())==candidate.read_text()
    assert old['qualified55RTL_sources_unchanged'] and old['SOURCE_records']==0
    assert old['input_records']==new['input_records']==2097152
    assert old['real_first_tag']==new['real_first_tag']
    assert digest(roots['baseline']/'adc.hex')==digest(roots['fresh']/'adc.hex')
    assert new['all_emitted_SOURCE_pattern_bytes_equal'] and new['SOURCE_records']>0
    assert new['coarse_hit_denominator']==reg['coarse_hit_denominator']==10
    assert reg['original_136_SOURCE_142_records_491_batches_gate_unchanged']
    assert reg['complete_accepted_beat_stream_equal']
    assert reg['SOURCE_records']==136 and reg['records']==142 and reg['batches']==491
    assert arith['vectors']==634 and len(arith['cases'])==3
    assert all(row['compiled'] for row in arith['cases'])
    assert 'G0_DIFFERENTIAL_SCORE_PASS' in arith['cases'][0]['witness']
    assert all('Fatal: score Python exact oracle' in row['witness'] for row in arith['cases'][1:])
    assert impl['exact_candidate_gate_only_transform']
    sha=digest(candidate)
    assert impl['candidate_sha256']==sha and arith['cases'][0]['RTL_sha256']==sha
    assert digest(roots['regression']/NAME)==sha
    assert set(new['explicit_diagnostic_source_variants'])=={NAME}
    assert set(reg['explicit_diagnostic_source_variants'])=={NAME}
    recovery=receipts['recovery']
    assert digest(roots['recovery']/NAME)==sha
    assert recovery['all_emitted_SOURCE_pattern_bytes_equal'] and recovery['SOURCE_records']>0
    diag='\n'.join(recovery['diagnostics'])
    startup=re.search(r'startup_good(\d+) startup_bad(\d+)',diag)
    assert startup and int(startup[1])>0 and int(startup[2])>0
    retry=re.search(r' retry(\d+)',diag)
    assert retry and int(retry[1])>0
    routed=impl['candidate_local_route']
    assert routed['WNS_ns']>=0 and routed['WHS_ns']>=0 and not routed['severe_DRC']
    result={'scope':__doc__,'baseline_sha256':digest(baseline),'candidate_sha256':sha,
            'coarse_gate_only_transform':True,'fresh_raw_gate5_zero_gate10_SOURCE_exact':True,
            'original136_142_491_and_complete_accepted_stream_unchanged':True,
            'arithmetic_634_II8_stalls_and_two_compiled_faults_passed':True,
            'false_coarse_invalid_training_then_real_SOURCE_without_reset_passed':True,
            'recovery_SOURCE_bytes_exact':recovery['SOURCE_bytes_exact'],
            'fresh_raw_SOURCE_records':new['SOURCE_records'],'fresh_raw_SOURCE_bytes_exact':new['SOURCE_bytes_exact'],
            'fresh_raw_restored_bytes_exact':new['restored_bytes_exact'],
            'local_incremental_synthesis_cost':impl['same_flow_local_delta'],
            'local_routed':routed,'artifacts':{name:{'path':str(p),'receipt_sha256':digest(p/'receipt.json')} for name,p in roots.items()},
            'production_profile_frozen':False,'whole_board_or_native_RF_capacity_or_video_or_G0_passed':False}
    a.output.parent.mkdir(parents=True,exist_ok=True)
    assert not a.output.exists()
    a.output.write_text(json.dumps(result,indent=2)+'\n')
    validate_coarse_replacement(NAME,result['baseline_sha256'],candidate,a.output)
    print(json.dumps(result),flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('baseline','fresh','regression','arithmetic','implementation','recovery','output'):
        p.add_argument('--'+name,type=Path,required=True)
    main(p.parse_args())
