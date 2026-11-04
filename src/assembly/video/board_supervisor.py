"""Bounded fresh RF -> native/real PL -> this-run RFD -> paced HDMI. No retry."""
from pathlib import Path
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import traceback

R = Path('/tmp/link-live')
B = R / 'short-video-staged-r2'
O = Path('/tmp/link-share/short-video-staged-r2-au-hdmi')
sys.path[:0] = [str(B), str(R / 'python'), str(R / 'python/tools')]
from receive_gate import validate_received
from acceptance_gate import check
from read_rxonly_delivery import iter_delivery

P = Path('/sys/bus/iio/devices/iio:device0')
LO = [P / 'out_altvoltage0_RX_LO_powerdown', P / 'out_altvoltage1_TX_LO_powerdown']
ADC = Path('/sys/bus/iio/devices/iio:device3/buffer/enable')
RING = Path('/sys/class/misc/zed-rxth/device')
STOP = R / 'short-video-staged-r2.stop'
TRIGGER = R / 'short-video-staged-r2.trigger'
DISPLAY = R / 'short-video-staged-r2.display'


def save(name, value):
    (O / name).write_text(json.dumps(value, indent=2, allow_nan=False) + '\n')


def counters():
    return {k: int((RING / k).read_text()) for k in ('ring_halts', 'ready_blocks', 'ring_capacity')}


def retire(child):
    if child is None or child.poll() is not None:
        return
    os.killpg(child.pid, signal.SIGINT)
    try:
        child.wait(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(child.pid, signal.SIGTERM)
        try:
            child.wait(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait(timeout=3)


def wait_signal(path, seconds):
    end = time.monotonic() + seconds
    while not path.exists():
        if STOP.exists():
            raise RuntimeError('host stop requested')
        if time.monotonic() >= end:
            raise TimeoutError('bounded wait expired: ' + path.name)
        time.sleep(.02)


def native_summary():
    rows = []
    for line in (O / 'native.log').read_text(errors='replace').splitlines():
        if line.startswith('{'):
            try:
                value = json.loads(line)
                if value.get('schema') == 'mrf1-native-rx-backend-v1':
                    rows.append(value)
            except ValueError:
                pass
    if not rows:
        raise ValueError('native summary absent')
    return rows[-1]


def main():
    auth = json.loads((B / 'AUTHORIZATION.json').read_text())
    assert auth['authorized'] and auth['expected_boot_id'] == (R / 'EXPECTED_BOOT_ID').read_text().strip()
    assert auth['expected_boot_id'] == Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    assert auth['scope'] == 'finite RF131 ten AUs; fresh ADC and real PL, then paced HDMI from this run only'
    assert auth['maximum_frames'] == 131 and auth['ADC_blocks'] == 2048 and auth['display_time_scale'] == 5
    assert auth['HDMI_hold_ms'] == 60000 and auth['SD_write'] is False
    assert '/dev/root / ext4 ro,' in Path('/proc/mounts').read_text()
    assert all(p.read_text().strip() == '1' for p in LO) and ADC.read_text().strip() == '0'
    assert Path('/proc/sys/kernel/tainted').read_text().strip() == '4096'
    assert hashlib.sha256((B / 'live-rx-arm').read_bytes()).hexdigest() == auth['native_sha256']
    assert hashlib.sha256((B / 'hdmi-presenter').read_bytes()).hexdigest() == auth['presenter_sha256']
    assert not any(p.exists() for p in (STOP, TRIGGER, DISPLAY, R / 'HDMI_SINGLE_ACTIVATION_CLAIM'))
    assert shutil.disk_usage('/tmp').free >= 40 * 1024 * 1024
    assert int(next(l.split()[1] for l in Path('/proc/meminfo').read_text().splitlines() if l.startswith('MemAvailable:'))) >= 96 * 1024
    O.mkdir(exist_ok=False)
    before = subprocess.check_output(['/bin/busybox', 'dmesg'])
    (O / 'kernel-before.log').write_bytes(before)
    initial = counters()
    save('rxth-before.json', initial)
    child = None
    result = dict(scope=auth['scope'], valid=False, new_RF=True, new_ADC=True,
                  visual_pass=False, physical_visual_confirmation=None,
                  fresh_RF_stage_completed=False, current_run_RFD_only=True,
                  health={'visual_only_not_sampled': True, 'dev_mem_reads_during_hold': 0})
    python = os.readlink('/proc/self/exe')
    try:
        # Settle the receiver LO before READY, preserving the verified photo timing.
        LO[0].write_text('0\n')
        assert LO[0].read_text().strip() == '0'
        save('READY', {'ready': True})
        wait_signal(TRIGGER, 45)
        env = dict(os.environ, LINK_FINITE_CAPTURE_CFO='1',
                   LINK_DIAGNOSTIC_COARSE_CFO_HZ='7223.213205776218',
                   LINK_DIAGNOSTIC_SFO_PPM='39.98002049598881',
                   LINK_DIAGNOSTIC_RF_FRAME_BUDGET='131', LINK_DIAGNOSTIC_LAST_RF_FRAME_ID='128',
                   LINK_RXTH_EVIDENCE_OUT=str(O / 'new-adc-output.rxth'))
        command = [str(R / 'presenter/lib/ld-linux-armhf.so.3'), '--library-path', str(R / 'presenter/lib'),
                   str(B / 'live-rx-arm'), 'start-rx', '--blocks', '2048', '--imbc-build-id', '1',
                   '--deadline-ms', '120000', '--delivery-path', str(O / 'received-live.rfd')]
        with (O / 'native.log').open('xb') as log:
            child = subprocess.Popen(command, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            end = time.monotonic() + 135
            while child.poll() is None:
                text = (O / 'native.log').read_text(errors='replace')
                if 'LIVE_FINITE_ADC_CLOSED' in text and not result.get('RX_LO_closed_after_capture'):
                    LO[0].write_text('1\n')
                    result['RX_LO_closed_after_capture'] = True
                if STOP.exists() or time.monotonic() > end:
                    raise RuntimeError('native stage stopped or deadline expired')
                if counters()['ring_halts'] != initial['ring_halts'] or Path('/proc/sys/kernel/tainted').read_text().strip() != '4096':
                    raise RuntimeError('native ring halt or new taint')
                time.sleep(.02)
        result['native_returncode'] = child.returncode
        assert child.returncode == 0, 'native receiver failed'
        for p in LO:
            p.write_text('1\n')
        assert ADC.read_text().strip() == '0' and counters()['ready_blocks'] == 0
        result['native_summary'] = native_summary()
        with (O / 'received-live.rfd').open('rb') as stream:
            records = list(iter_delivery(stream))
        result['public_AUs'] = validate_received(result['native_summary'], records)
        result['received_frame_ids'] = [r.frame_id for r in records]
        result['fresh_RF_stage_completed'] = True
        source_hash = hashlib.sha256((O / 'received-live.rfd').read_bytes()).hexdigest()
        save('RF_READY.json', {'RFD_sha256': source_hash, 'public_AUs': result['public_AUs'],
                              'RF_records': len(records), 'native_elapsed_ns': result['native_summary']['elapsed_ns']})
        # The host announces the observation window before this fresh trigger.
        wait_signal(DISPLAY, 180)
        assert hashlib.sha256((O / 'received-live.rfd').read_bytes()).hexdigest() == source_hash
        assert child.poll() == 0 and all(p.read_text().strip() == '1' for p in LO) and ADC.read_text().strip() == '0'
        with (R / 'HDMI_SINGLE_ACTIVATION_CLAIM').open('x') as claim:
            claim.write('short-video-staged-r2\n')
        subprocess.run([python, str(B / 'make_runtime_closure.py')], check=True, timeout=15)
        video_argv = [python, str(B / 'receiver_offline_diag.py'), '--input', str(O / 'received-live.rfd'),
                      '--offline', '--runtime-closure', str(B / 'runtime-closure.json'),
                      '--proof-output', str(O / 'hdmi-proof.json'), '--report', str(O / 'video-report.json'),
                      '--session-id', '714', '--imbc-build-id', '1', '--max-records', '131',
                      '--expected-access-units', '10', '--timeout-s', '30', '--offline-deadline-s', '20',
                      '--max-pending', '2', '--diagnostic-offline-hold-ms', '60000', '--diagnostic-time-scale', '5']
        with (O / 'video.log').open('xb') as log:
            child = subprocess.Popen(video_argv, env=dict(os.environ, PYTHONPATH=str(R / 'python') + ':' + str(R / 'python/tools')),
                                     stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            result['video_returncode'] = child.wait(timeout=105)
        result['video_report'] = json.loads((O / 'video-report.json').read_text())
        result['hdmi_proof'] = json.loads((O / 'hdmi-proof.json').read_text())
        result['hold_completed'] = 'FINITE_VIDEO_HOLD_END' in (O / 'video.log').read_text()
        result['technical_gate_problems'] = check(result['native_summary'], result['video_report'], result['hdmi_proof'], result['received_frame_ids'])
        pace = result['video_report'].get('diagnostic_source_pacing', {})
        schedule = pace.get('schedule', [])
        assert pace.get('scale') == 5 and [x['sequence'] for x in schedule] == list(range(10)), 'missing paced video source'
        assert schedule[-1]['released_monotonic_s'] - schedule[0]['released_monotonic_s'] >= 8.9, 'video compressed into a burst'
        result['valid'] = bool(result['video_returncode'] == 0 and result['hold_completed'] and not result['technical_gate_problems'])
    except BaseException as error:
        result['valid'] = False
        result['failure'] = type(error).__name__ + ': ' + str(error)
        traceback.print_exc()
    finally:
        retire(child)
        for p in LO:
            p.write_text('1\n')
        time.sleep(15)
        after = subprocess.check_output(['/bin/busybox', 'dmesg'])
        (O / 'kernel-after.log').write_bytes(after)
        result.update(rx_lo_powerdown=LO[0].read_text().strip(), tx_lo_powerdown=LO[1].read_text().strip(),
                      adc_buffer_enable=ADC.read_text().strip(), rxth_after=counters(),
                      taint=int(Path('/proc/sys/kernel/tainted').read_text()))
        result['kernel_error_lines'] = [l for l in after[len(before):].decode(errors='replace').splitlines()
                                        if any(w in l for w in ('Oops', 'panic', 'BUG:', 'WARNING:', 'timed out'))]
        if result['kernel_error_lines'] or result['taint'] != 4096 or result['rx_lo_powerdown'] != '1' or result['tx_lo_powerdown'] != '1' or result['adc_buffer_enable'] != '0' or result['rxth_after']['ring_halts'] != initial['ring_halts']:
            result['valid'] = False
        save('result.json', result)
        print('STAGED_VIDEO_DONE', result['valid'], flush=True)


if __name__ == '__main__':
    main()
