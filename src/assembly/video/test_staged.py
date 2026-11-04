"""Host-only failures/provenance/pacing tests; no hardware or child decoder."""
from pathlib import Path
from types import SimpleNamespace
import ast
import contextlib
import copy
import importlib.util
import io
import json
import py_compile
import sys

B = Path(__file__).resolve().parent
D = B.parent
sys.path[:0] = [str(B), str(D / 'resume6-candidate/runtime/python/tools'), str(D / 'resume6-candidate/runtime/python')]
from receive_gate import validate_received
from acceptance_gate import check
import grade_evidence as grader
from diagnostic_pacer import DiagnosticPacer
from read_rxonly_delivery import iter_delivery

with (D / 'finite-video-multi-r1-20261027/board-evidence/received-live.rfd').open('rb') as stream:
    records = list(iter_delivery(stream))
native = dict(captured_blocks=2048, detected_frames=129, delivered_frames=129, crc_rejected_frames=0,
              delivery=dict(mode='file', ipc_dropped_records=0))
aus = validate_received(native, records)
assert len(aus) == 10
extra = [records[0], records[0]] + records
wide = copy.deepcopy(native)
wide.update(detected_frames=131, delivered_frames=131)
assert len(validate_received(wide, extra)) == 10
single = records[1:]
small = copy.deepcopy(native)
small.update(detected_frames=128, delivered_frames=128)
assert len(validate_received(small, single)) == 10
grader.verify_source_aus(grader.recover_aus(records))
negatives = []


def rejects(label, fn):
    try:
        fn()
    except (ValueError, TypeError, KeyError):
        negatives.append(label)
    else:
        raise AssertionError('bad input accepted: ' + label)


for field, value in [('captured_blocks', 2047), ('delivered_frames', 128), ('crc_rejected_frames', 1)]:
    bad = copy.deepcopy(native)
    bad[field] = value
    rejects('native ' + field, lambda bad=bad: validate_received(bad, records))
rejects('too many warmup replicas', lambda: validate_received(wide, [records[0]] + extra))
rejects('missing final fragment', lambda: validate_received(native, records[:-1]))
bad = list(records)
bad[50], bad[51] = bad[51], bad[50]
rejects('RF record reorder', lambda: validate_received(native, bad))
packets = list(records[-1].packets)
corrupt = bytearray(packets[1])
corrupt[50] ^= 1
packets[1] = bytes(corrupt)
bad = list(records)
bad[-1] = SimpleNamespace(frame_id=128, rejected=False, build_id=1, packets=packets)
rejects('MRF packet corruption', lambda: validate_received(native, bad))

old = D / 'finite-video-multi-r1-20261027/board-evidence'
video = json.loads((old / 'video-report.json').read_text())
proof = json.loads((old / 'hdmi-proof.json').read_text())
# Observed physical submission tokens differ legitimately from codec frame tokens.
# The original 10-source-frame identities/PTS/pixel CRCs must still agree.
proof['scanout_repeats'] = 1800
assert any(a['token'] != p['token'] for a, p in zip(video['accepted'], proof['records']))
ids = [r.frame_id for r in records]
assert not check(native, video, proof, ids)
for field, value in [('token', proof['records'][0]['token']), ('source_frame_id', 8),
                     ('source_session_id', 715), ('payload_crc32', 0), ('source_pts_ns', 0),
                     ('drm_tv_sec', 0)]:
    altered = copy.deepcopy(proof)
    altered['records'][-1][field] = value
    assert check(native, video, altered, ids), field
    negatives.append('physical ' + field)
bad_video = copy.deepcopy(video)
bad_video['accepted'][-1]['token'] = bad_video['accepted'][0]['token']
assert check(native, bad_video, proof, ids)
negatives.append('duplicate codec token')

# Fake time exercises actual 129 records: ten AU starts, exactly one second apart.
clock = [0.0]
pacer = DiagnosticPacer(5, 714, clock=lambda: clock[0], sleep=lambda delay: clock.__setitem__(0, clock[0] + delay))
for record in records:
    pacer.before_record(record, 20)
assert [x['sequence'] for x in pacer.schedule] == list(range(10))
assert abs(pacer.schedule[-1]['released_monotonic_s'] - 9) < 1e-6

spec = importlib.util.spec_from_file_location('staged_display_receiver', B / 'receiver_offline_diag.py')
receiver = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = receiver
spec.loader.exec_module(receiver)
seen = []
receiver.run = lambda a: seen.append(a) or {'schema': 'CLI-test', 'valid': True, 'accepted': [{}] * 10}
tree = ast.parse((B / 'board_supervisor.py').read_text())
assignment = next(n for n in ast.walk(tree) if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'video_argv' for t in n.targets))
argv = eval(compile(ast.Expression(assignment.value), 'board-video-argv', 'eval'),
            {'python': '/usr/bin/python3.12', 'B': Path('/tmp/link-live/short-video-staged-r2'), 'O': Path('/tmp/link-share/short-video-staged-r2-au-hdmi')})
sys.argv = argv[1:]
with contextlib.redirect_stdout(io.StringIO()):
    assert receiver.main() == 0
assert seen[-1].diagnostic_time_scale == 5 and seen[-1].hold_final_frame_ms == 60000
assert seen[-1].offline and seen[-1].max_records == 131 and seen[-1].expected_access_units == 10
for hold, scale in [('60001', '5'), ('60000', '11')]:
    bad = list(argv[1:])
    bad[bad.index('--diagnostic-offline-hold-ms') + 1] = hold
    bad[bad.index('--diagnostic-time-scale') + 1] = scale
    sys.argv = bad
    with contextlib.redirect_stderr(io.StringIO()):
        try:
            receiver.main()
        except SystemExit as error:
            assert error.code == 2
        else:
            raise AssertionError('out of bounds display option accepted')
    negatives.append('display option bounds ' + hold + '/' + scale)

for path in [*B.glob('*.py'), *B.joinpath('boot').glob('*.py')]:
    py_compile.compile(str(path), doraise=True)
result = dict(valid=True, historical_129_records_public_CRC_AUs_valid=True, source_AUs=aus,
              distinct_token_namespaces_positive=True, negative_cases=negatives,
              slow_play_schedule=pacer.schedule, exact_board_display_CLI_valid=True,
              hardware_access=False)
(B / 'OFFLINE-CHECK.json').write_text(json.dumps(result, indent=2) + '\n')
print(json.dumps({'valid': True, 'negative_cases': len(negatives), 'source_AUs': len(aus), 'scheduled_span_s': 9, 'hardware_access': False}))
