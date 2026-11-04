"""Adapt only one-shot orchestration, retaining frozen native/PL/CRC/display."""
import io
import json
import tarfile

from config import APP, BASE, D, sha


SCOPE = 'finite RF42 seu photo AU and HDMI with idle same-buffer scanout refresh; fresh ADC buffered, real PL; no SD/FPGA/reload'


def supervisor_source():
    source = (BASE / 'board_supervisor.py').read_text()
    source = source.replace('photo-seu-reproduce-r2-au-hdmi', 'photo-console')
    source = source.replace('photo-seu-reproduce-r2', 'photo-console')
    source = source.replace('"10000",', '"60000",')
    source = source.replace('overall = time.monotonic() + 150', 'overall = time.monotonic() + 210')
    source = source.replace('        "valid": False,', '        "valid": False,\n        "run_id": auth["run_id"],\n        "boot_id": auth["expected_boot_id"],\n        "new_RF": True,\n        "new_ADC": True,')
    source = source.replace('        python = os.readlink("/proc/self/exe")',
        '        with (R / "HDMI_SINGLE_ACTIVATION_CLAIM").open("x") as claim: claim.write(auth["run_id"])\n        python = os.readlink("/proc/self/exe")')
    source = source.replace('        with (O / "kernel-after.log").open("xb") as stream:',
        '        time.sleep(15)  # Preserve delayed DRM-error observation after teardown.\n        with (O / "kernel-after.log").open("xb") as stream:')
    compile(source, 'photo-console-supervisor.py', 'exec')
    return source.encode()


def build_bundle(path, run_id, boot_id, secret):
    files = {
        'board_supervisor.py': supervisor_source(),
        'board_agent.py': (APP / 'board_agent.py').read_bytes(),
        'acceptance_gate.py': (BASE / 'acceptance_gate.py').read_bytes(),
        'scanout_health.py': (BASE / 'scanout_health.py').read_bytes(),
        'make_runtime_closure.py': (BASE / 'make_runtime_closure.py').read_bytes(),
        'hdmi-presenter': (D / 'candidates/hdmi-lazy-r6-idle-repeat/build-arm/hdmi-presenter').read_bytes(),
        'live-rx-arm': (D / 'candidates/finite-video-long-r2/live-rx-arm').read_bytes(),
        'rf_native_video_receiver.py': (D / 'candidates/finite-video-long-r2/runtime-tools/rf_native_video_receiver.py').read_bytes(),
    }
    auth = dict(authorized=True, scope=SCOPE, expected_boot_id=boot_id,
        run_id=run_id, api_key=secret, visual_only_no_scanout_sampler=True,
        unbind_rx_dma_before_hold=False,
        native_sha256=sha(D / 'candidates/finite-video-long-r2/live-rx-arm'))
    files['AUTHORIZATION.json'] = json.dumps(auth).encode()
    import hashlib
    rows = [dict(name=name, bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest()) for name, raw in files.items()]
    files['FILES.json'] = json.dumps(rows).encode()
    with tarfile.open(path, 'x:gz') as archive:
        for name, raw in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(raw)
            info.mode = 0o755 if name in ('hdmi-presenter', 'live-rx-arm') else 0o600
            archive.addfile(info, io.BytesIO(raw))
    # No IQ, RFD, source photo, encoded media, or TX ledger goes to the receiver.
    return rows
