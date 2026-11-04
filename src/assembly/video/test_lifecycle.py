"""Fake-device lifecycle tests: failed/new incomplete RF must never start HDMI."""
from pathlib import Path
import contextlib
import hashlib
import importlib.util
import io
import json
import sys
import tempfile
from unittest.mock import patch

B = Path(__file__).resolve().parent
D = B.parent
sys.path[:0] = [str(B), str(D / 'resume6-candidate/runtime/python/tools'), str(D / 'resume6-candidate/runtime/python')]
old = D / 'finite-video-multi-r1-20261027/board-evidence'
rfd = (old / 'received-live.rfd').read_bytes()
video = json.loads((old / 'video-report.json').read_text())
proof = json.loads((old / 'hdmi-proof.json').read_text())
proof['scanout_repeats'] = 1800
video['diagnostic_source_pacing'] = {'scale': 5, 'schedule': [dict(sequence=i, released_monotonic_s=float(i)) for i in range(10)]}
native = dict(schema='mrf1-native-rx-backend-v1', captured_blocks=2048, detected_frames=129,
              delivered_frames=129, crc_rejected_frames=0, elapsed_ns=1000,
              delivery=dict(mode='file', ipc_dropped_records=0))
outcomes = []
for scenario in ('native_failure', 'incomplete_RF', 'good'):
    spec = importlib.util.spec_from_file_location('board_lifecycle_' + scenario, B / 'board_supervisor.py')
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    with tempfile.TemporaryDirectory(prefix='staged-lifecycle-') as tmp:
        root = Path(tmp)
        m.R = root / 'runtime'
        m.B = m.R / 'short-video-staged-r2'
        m.B.mkdir(parents=True)
        m.O = root / 'evidence'
        m.LO = [root / 'rx_lo', root / 'tx_lo']
        m.ADC = root / 'adc'
        m.RING = root / 'ring'
        m.RING.mkdir()
        for p in m.LO:
            p.write_text('1\n')
        m.ADC.write_text('0\n')
        for name, value in [('ring_halts', 0), ('ready_blocks', 0), ('ring_capacity', 64)]:
            (m.RING / name).write_text(str(value))
        for attr, suffix in [('STOP', 'stop'), ('TRIGGER', 'trigger'), ('DISPLAY', 'display')]:
            setattr(m, attr, m.R / suffix)
        (m.R / 'EXPECTED_BOOT_ID').write_text('test-boot')
        for name in ('live-rx-arm', 'hdmi-presenter'):
            (m.B / name).write_bytes(b'never executed')
        auth = json.loads((B / 'AUTHORIZATION.json').read_text())
        auth.update(expected_boot_id='test-boot', native_sha256=hashlib.sha256(b'never executed').hexdigest(),
                    presenter_sha256=hashlib.sha256(b'never executed').hexdigest())
        (m.B / 'AUTHORIZATION.json').write_text(json.dumps(auth))
        proc = root / 'proc'
        for name, contents in [('sys/kernel/random/boot_id', 'test-boot'), ('sys/kernel/tainted', '4096'),
                               ('mounts', '/dev/root / ext4 ro,relatime 0 0\n'), ('meminfo', 'MemAvailable: 200000 kB\n')]:
            p = proc / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(contents)
        real_path = Path
        m.Path = lambda value: proc / str(value)[6:] if str(value).startswith('/proc/') else real_path(value)
        m.wait_signal = lambda path, seconds: path.touch()
        stages = []

        class Child:
            def __init__(self, command, **options):
                self.pid = 99999999
                if '--delivery-path' in command:
                    stages.append('new_RF_native')
                    assert stages == ['new_RF_native']
                    options['stdout'].write((json.dumps(native) + '\n').encode())
                    options['stdout'].flush()
                    (m.O / 'received-live.rfd').write_bytes(rfd[:-324] if scenario == 'incomplete_RF' else rfd)
                    self.returncode = 4 if scenario == 'native_failure' else 0
                else:
                    stages.append('HDMI')
                    assert stages == ['new_RF_native', 'HDMI']
                    assert all(p.read_text().strip() == '1' for p in m.LO) and m.ADC.read_text().strip() == '0'
                    assert command[command.index('--input') + 1] == str(m.O / 'received-live.rfd')
                    assert (m.O / 'RF_READY.json').exists()
                    (m.O / 'video-report.json').write_text(json.dumps(video))
                    (m.O / 'hdmi-proof.json').write_text(json.dumps(proof))
                    options['stdout'].write(b'FINITE_VIDEO_HOLD_BEGIN\nFINITE_VIDEO_HOLD_END\n')
                    self.returncode = 0

            def poll(self):
                return self.returncode

            def wait(self, timeout):
                return self.returncode

        with patch.object(m.subprocess, 'Popen', Child), patch.object(m.subprocess, 'check_output', return_value=b'clean kernel\n'), \
             patch.object(m.subprocess, 'run'), patch.object(m.time, 'sleep'), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            m.main()
        result = json.loads((m.O / 'result.json').read_text())
        assert result['valid'] is (scenario == 'good')
        assert ('HDMI' in stages) is (scenario == 'good')
        assert all(p.read_text().strip() == '1' for p in m.LO) and m.ADC.read_text().strip() == '0'
        outcomes.append(dict(scenario=scenario, valid=result['valid'], stages=stages, LO_closed=True))
(B / 'LIFECYCLE-CHECK.json').write_text(json.dumps(dict(valid=True, fake_devices_only=True, cases=outcomes), indent=2) + '\n')
print(json.dumps(outcomes))
