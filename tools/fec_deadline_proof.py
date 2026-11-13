"""Bind the fixed-8 FEC replacement to its byte and deadline regression."""

import hashlib
import json


NAME = 'fec_stream_recovery_cost_probe.sv'


def validate_deadline_replacement(baseline_sha256, current, proof_path):
    assert proof_path is not None, 'fixed-8 FEC replacement needs deadline proof'
    proof = json.loads(proof_path.read_text())
    actual = hashlib.sha256(current.read_bytes()).hexdigest()
    assert proof['baseline_sha256'] == baseline_sha256 and proof['candidate_sha256'] == actual
    assert proof['stream']['source_sha256'][NAME] == actual and proof['stream']['banks'] == 8
    stream = proof['stream']['simulation']
    assert stream.startswith('G0_FEC_STREAM_PASS banks=8 terminal_jobs=17 exact_bytes=130176 restored_bytes=61440 ')
    assert ' deadline_edges=2 ' in stream and ' full8queue_cycles=4799935' in stream
    assert set(proof['wrap_intervals']) == {'1', '8', '15'} and proof['early_deadline_mutant_rejected']
    assert all(marker.startswith(f'FEC_SHARED_DEADLINE_PASS deadline={interval} ')
               for interval, marker in proof['wrap_intervals'].items())
    assert proof['returncode'] == 0 and proof['net']['LUT'] < 0
    assert proof['net']['BRAM36_equivalent'] == proof['net']['DSP'] == 0
    assert min(proof['OOC_timing_ns']['WNS'], proof['OOC_timing_ns']['WHS']) >= 0
    return {'name': NAME, 'baseline_sha256': baseline_sha256, 'compiled_sha256': actual,
            'proof': str(proof_path.resolve()), 'same_fixed8_RS20_32_S512_and_expiry_edges': True}
