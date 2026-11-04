"""Fixed photo-only operation. Nothing here is supplied by HTTP clients."""
from pathlib import Path
import hashlib
import json
import os

APP = Path(__file__).resolve().parent
WORK = APP.parent
INTEGRATION = WORK / 'deliverables/usrp-zedboard-integration-20261026'
D = INTEGRATION / 'realtime-hdmi'
BASE = D / 'photo-seu-reproduce-r2-20261030'
REFERENCE = D / 'photo-seu-reobserve-r1-20261030'
BOOT = D / 'boot'
RUNTIME = D / 'resume6-candidate/live-runtime.tar.gz'
SERIAL = Path('/dev/serial/by-id/usb-2012_Cypress_Semiconductor_Cypress-USB2UART-Ver1.0G_8344058D1E27-if00')
HOST = '169.254.209.83'
BOARD = '169.254.209.84'
WEB_PORT = 8098
RUNS = APP / 'runs'
STATE = Path('/run/user') / str(os.getuid()) / 'mimo-photo-console'
LEASE = STATE / 'tftp.json'
BOARD_APP = '/tmp/link-live/photo-console'
BOARD_OUT = '/tmp/link-share/photo-console'
EVIDENCE_NAMES = frozenset(('new-adc-output.rxth', 'new-adc-output.rxth.failure.rxth',
    'received-live.rfd', 'result.json', 'native.log', 'video.log', 'video-report.json',
    'hdmi-proof.json', 'kernel-before.log', 'kernel-after.log', 'rxth-before.json',
    'interrupts-after.log', 'supervisor.log'))


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        while raw := stream.read(1 << 20):
            h.update(raw)
    return h.hexdigest()


def verify_assets():
    pins = json.loads((APP / 'baseline-pins.json').read_text())
    for name, digest in pins['files'].items():
        if sha(name) != digest:
            raise RuntimeError('已验证基线文件被修改：' + name)
    manifest = json.loads((BOOT / 'BOOT_MANIFEST.json').read_text())
    for row in manifest['files']:
        if sha(BOOT / row['file']) != row['sha256']:
            raise RuntimeError('RAM 启动文件哈希不符：' + row['file'])
    return pins
