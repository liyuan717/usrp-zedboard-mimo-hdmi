"""Fixed, import-safe measured-runtime adapter for the tested finite video worker.

Only explicit callers can run a worker. Importing/building packages does not
open UART, start a transfer server, load modules, or emit RF.
"""
from pathlib import Path
import ast, hashlib, inspect, io, json, tarfile, textwrap, sys

R = Path(__file__).resolve().parents[1]
W = R.parents[1]
D = W / 'deliverables/usrp-zedboard-integration-20261026/realtime-hdmi'
BASE = D / 'short-video-staged-r2-20261030'
C = R / 'candidates/repetition-bound-r2'
N = C / 'arm-compatibility-r3/live-rx-arm'
sys.path.insert(0, str(W / 'photo-console'))
import workflow as wf
from config import APP, sha

PROFILES = {
    'probe': (D / 'candidates/hdmi-refill-probe-r1/live-runtime-probe.tar.gz',
              '3622413bb550bcf9383b515cae6e3ef4d995f0bf2b2c0dced806a62c49cb142b',
              'be802d9a30445889dfbd1a2b75e5bd8bf9bac40055fcebbbe5589585ef86c81b'),
    'priority': (R / 'candidates/hdmi-completion-priority-r1/runtime-package-r1/live-runtime-priority.tar.gz',
                 '66ac62616234da99209aed6ebf557b92751636a01b964c61768d78c785edd4d7',
                 '163df5b25d5db6110fb5e663a09c167980903abf5dca718c95c6bd732dedf233'),
}
ORIGINAL_RUNTIME_SHA = '6d4214aecdb892ed3c25840ecaeff6e580ef3d7a3d6d2a3f14867a16e69d4294'

# This code executes on the ARM Python already used by the frozen worker.
PROBE_HELPERS = '''
def read_refill_probe():
    p = Path('/sys/module/adi_axi_hdmi_present/parameters')
    assert (p / 'measure_refill').read_text().strip() == 'Y'
    value = json.loads((p / 'refill_stats').read_text())
    validate_refill_probe(value)
    return value

def validate_refill_probe(value):
    assert value['version'] == 1 and value['enabled'] is True
    assert type(value['queue_requests']) is int and value['queue_requests'] >= 0
    names = ['queue_to_worker_observation', 'worker_duration', 'boundary_irq_to_repeat_issue']
    assert [m['name'] for m in value['metrics']] == names
    for m in value['metrics']:
        assert all(type(m[k]) is int and m[k] >= 0 for k in ('valid','missing','reversed','sum_ns','max_ns'))
        assert m['reversed'] == 0 and m['sum_saturated'] is False
        bins = m['bins_lt_50us_250us_500us_1ms_2ms_else']
        assert len(bins) == 6 and all(type(x) is int and x >= 0 for x in bins)
        assert sum(bins) == m['valid']
        assert m['max_ns'] <= m['sum_ns']

def check_refill_probe_pair(before, after):
    validate_refill_probe(before)
    validate_refill_probe(after)
    assert before['queue_requests'] == 0 and after['queue_requests'] > 0
    for b, a in zip(before['metrics'], after['metrics']):
        assert b['valid'] == b['missing'] == b['sum_ns'] == b['max_ns'] == 0
        assert a['valid'] > 0
'''


def make_adapter(profile, out):
    """Return worker class and bundle function; no hardware side effects."""
    runtime, runtime_sha, module_sha = PROFILES[profile]
    assert sha(runtime) == runtime_sha
    assert sha(N) == '01afeb4925d2568603a3c5e23d7899242c3110344c907db3f80d0efa7a30bce1'
    out = Path(out)
    source = (R / 'tools/video_core_round.py').read_text()
    selected = [n for n in ast.parse(source).body
                if isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name in ('build', 'VideoManager')]
    assert len(selected) == 2
    context = dict(Path=Path, wf=wf, R=R, W=W, D=D, BASE=BASE, C=C, N=N,
                   APP=APP, sha=sha, O=out, variant='candidate', json=json,
                   hashlib=hashlib, tarfile=tarfile, io=io)
    exec(compile(ast.Module(body=selected, type_ignores=[]), 'tested-video-core-definitions', 'exec'), context)
    original_build = context['build']

    def build(path, run_id, boot_id, secret):
        original_build(out / 'unmeasured-app.tar.gz', run_id, boot_id, secret)
        with tarfile.open(out / 'unmeasured-app.tar.gz') as t:
            rows = t.getmembers()
            assert all(x.isfile() for x in rows) and len({x.name for x in rows}) == len(rows)
            files = {x.name: t.extractfile(x).read() for x in rows}
            modes = {x.name: x.mode for x in rows}
        for v in json.loads(files['FILES.json']):
            assert len(files[v['name']]) == v['bytes']
            assert hashlib.sha256(files[v['name']]).hexdigest() == v['sha256']
        s = files['board_supervisor.py'].decode()
        assert s.count('\ndef main():') == 1
        s = s.replace('\ndef main():', '\n' + PROBE_HELPERS + '\ndef main():')
        needle = "    initial = counters()"
        assert s.count(needle) == 1
        s = s.replace(needle, needle + "\n    assert hashlib.sha256((R / 'modules/adi_axi_hdmi_present.ko').read_bytes()).hexdigest() == auth['HDMI_module_sha256']\n    probe_before = read_refill_probe()\n    assert probe_before['queue_requests'] == 0")
        needle = "                  run_id=auth['run_id'],boot_id=auth['expected_boot_id'],"
        assert s.count(needle) == 1
        s = s.replace(needle, needle + "\n                  runtime_profile=auth['runtime_profile'],refill_probe_before=probe_before,")
        needle = "        save('result.json', result)"
        assert s.count(needle) == 1
        s = s.replace(needle, """        try:
            result['refill_probe_after'] = read_refill_probe()
            if result.get('fresh_RF_stage_completed') and 'video_returncode' in result:
                check_refill_probe_pair(probe_before, result['refill_probe_after'])
            else:
                result['refill_probe_not_exercised'] = True
                assert probe_before == result['refill_probe_after']
        except BaseException as error:
            result['valid'] = False
            result['refill_probe_failure'] = type(error).__name__ + ': ' + str(error)
        """.rstrip() + '\n' + needle)
        compile(s, 'measured-video-board-supervisor', 'exec')
        files['board_supervisor.py'] = s.encode()
        auth = json.loads(files['AUTHORIZATION.json'])
        auth.update(runtime_profile=profile, runtime_sha256=runtime_sha, HDMI_module_sha256=module_sha)
        files['AUTHORIZATION.json'] = json.dumps(auth).encode()
        rows = [dict(name=n, bytes=len(v), sha256=hashlib.sha256(v).hexdigest())
                for n, v in files.items() if n != 'FILES.json']
        files['FILES.json'] = json.dumps(rows).encode()
        with tarfile.open(path, 'x:gz') as t:
            for n, v in files.items():
                info = tarfile.TarInfo(n)
                info.size, info.mode = len(v), modes[n]
                t.addfile(info, io.BytesIO(v))
        review = json.loads((out / 'BUNDLE-REVIEW.json').read_text())
        review.update(runtime_boot_drivers_presenter_PL_CRC_unchanged=False,
                      runtime_profile=profile, runtime_sha256=runtime_sha, HDMI_module_sha256=module_sha,
                      receiver_PL_CRC_presenter_boot_assets_unchanged=True,
                      active_visual_probe_polling=False, probe_reads='only before and after entire display lifecycle',
                      files=rows, hardware_driver_authorization_required=True)
        wf.write_json(out / 'BUNDLE-REVIEW.json', review)
        return rows

    class MeasuredVideoManager(context['VideoManager']):
        def preflight(self):
            assert sha(runtime) == runtime_sha
            super().preflight()
            wf.write_json(self.run / 'runtime-profile.json', dict(name=profile, sha256=runtime_sha,
                          module_sha256=module_sha, receiver_sha256=sha(N), SD_written=False))

        def grade(self, evidence):
            e = json.loads((evidence / 'result.json').read_text())
            if not e['valid']:
                raise RuntimeError('板端有限视频失败：' + e.get('failure', e.get('refill_probe_failure', 'unknown failure')))
            assert e['runtime_profile'] == profile
            checker = {}
            exec(compile(PROBE_HELPERS, 'probe-evidence-validation', 'exec'), checker)
            checker['check_refill_probe_pair'](e['refill_probe_before'], e['refill_probe_after'])
            assert 'refill_probe_failure' not in e
            r = super().grade(evidence)
            r.update(runtime_profile=profile, runtime_sha256=runtime_sha, HDMI_module_sha256=module_sha,
                     refill_probe_before=e['refill_probe_before'], refill_probe_after=e['refill_probe_after'])
            return r

    transfer_source = textwrap.dedent(inspect.getsource(wf.Manager.transfer))
    assert transfer_source.count(ORIGINAL_RUNTIME_SHA) == 1
    changed = transfer_source.replace(ORIGINAL_RUNTIME_SHA, runtime_sha)
    assert changed.replace(runtime_sha, ORIGINAL_RUNTIME_SHA) == transfer_source
    transfer_context = dict(wf.__dict__, RUNTIME=runtime)
    exec(compile(changed, 'fixed-measured-runtime-transfer', 'exec'), transfer_context)
    MeasuredVideoManager.transfer = transfer_context['transfer']
    sender = textwrap.dedent(inspect.getsource(wf.Manager.transmit))
    assert sender.count('161280') == sender.count('frames=42') == sender.count('unique_frames=41') == 1
    sender = sender.replace('161280', '503040').replace('frames=42', 'frames=131').replace('unique_frames=41', 'unique_frames=128')
    assert sender.count('stream = module.build_iq_stream()') == 1
    sender = sender.replace('stream = module.build_iq_stream()',
                            "stream = module.build_iq_stream()\n    assert hashlib.sha256(stream).hexdigest() == '80b0be13f751d4d093db1a237afa5dea7f5da44ea46915143923498a858c3741'")
    sender_context = dict(wf.__dict__, BASE=BASE)
    exec(compile(sender, 'unchanged-finite-video-sender', 'exec'), sender_context)
    MeasuredVideoManager.transmit = sender_context['transmit']
    return MeasuredVideoManager, build
