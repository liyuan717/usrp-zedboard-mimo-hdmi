"""RAM-only board supervisor control. Fixed operations; no shell/media input."""
from pathlib import Path
import hashlib
import hmac
import http.server
import json
import os
import subprocess
import time

B = Path('/tmp/link-live/photo-console')
O = Path('/tmp/link-share/photo-console')
AUTH = json.loads((B / 'AUTHORIZATION.json').read_text())
NAMES = frozenset(('new-adc-output.rxth', 'new-adc-output.rxth.failure.rxth',
    'received-live.rfd', 'result.json', 'native.log', 'video.log', 'video-report.json',
    'hdmi-proof.json', 'kernel-before.log', 'kernel-after.log', 'rxth-before.json',
    'interrupts-after.log', 'supervisor.log'))
child = None
shutdown = False


def state():
    value = dict(run_id=AUTH['run_id'], boot_id=AUTH['expected_boot_id'], state='preparing', done=False)
    if (O / 'result.json').is_file() and child is not None and child.poll() is not None:
        value.update(done=True, state='complete', result=json.loads((O / 'result.json').read_text()))
    elif child is not None and child.poll() is not None:
        value.update(done=True, state='failed', error='接收监督进程提前退出', returncode=child.returncode)
    else:
        native = (O / 'native.log').read_text(errors='replace') if (O / 'native.log').exists() else ''
        video = (O / 'video.log').read_text(errors='replace') if (O / 'video.log').exists() else ''
        if 'FINITE_VIDEO_HOLD_END' in video:
            value['state'] = 'cleanup'
        elif 'FINITE_VIDEO_HOLD_BEGIN' in video:
            value['state'] = 'displaying'
        elif 'LIVE_FINITE_ADC_CLOSED' in native:
            value['state'] = 'processing'
        elif (O / 'READY').exists():
            value['state'] = 'ready' if not native else 'receiving'
    if value['done']:
        value['files'] = [dict(name=p.name, bytes=p.stat().st_size,
            sha256=hashlib.sha256(p.read_bytes()).hexdigest())
            for p in (O.iterdir() if O.exists() else []) if p.name in NAMES and p.is_file() and not p.is_symlink()]
        log = Path('/tmp/link-share/photo-console-agent-supervisor.log')
        if log.is_file():
            value['files'].append(dict(name='supervisor.log', bytes=log.stat().st_size,
                sha256=hashlib.sha256(log.read_bytes()).hexdigest()))
    return value


class Handler(http.server.BaseHTTPRequestHandler):
    def authorized(self):
        return (self.client_address[0] == '169.254.209.83'
            and hmac.compare_digest(self.headers.get('X-Photo-Key', ''), AUTH['api_key']))

    def reply(self, value):
        raw = json.dumps(value).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if not self.authorized():
            self.send_error(403)
            return
        if self.path == '/status':
            self.reply(state())
        elif self.path.startswith('/evidence/') and state()['done']:
            name = self.path[len('/evidence/'):]
            path = (Path('/tmp/link-share/photo-console-agent-supervisor.log')
                if name == 'supervisor.log' else O / name)
            if name not in NAMES or path.is_symlink() or not path.is_file() or path.stat().st_size > 32 << 20:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header('Content-Length', str(path.stat().st_size))
            self.end_headers()
            with path.open('rb') as stream:
                while raw := stream.read(65536):
                    self.wfile.write(raw)
        else:
            self.send_error(404)

    def do_POST(self):
        global shutdown
        if not self.authorized() or self.headers.get('Content-Length', '0') not in ('0', '2'):
            self.send_error(403)
            return
        self.rfile.read(int(self.headers.get('Content-Length', '0')))
        if self.path == '/stop':
            Path('/tmp/link-live/photo-console.stop').touch()
            self.reply(dict(stop_requested=True, run_id=AUTH['run_id']))
        elif self.path == '/shutdown' and state()['done']:
            shutdown = True
            self.reply(dict(shutdown=True))
        else:
            self.send_error(409)

    def log_message(self, *args):
        pass


def main():
    global child
    assert Path('/proc/sys/kernel/random/boot_id').read_text().strip() == AUTH['expected_boot_id']
    log = Path('/tmp/link-share/photo-console-agent-supervisor.log').open('xb')
    with http.server.HTTPServer(('169.254.209.84', 8001), Handler) as server:
        child = subprocess.Popen([os.readlink('/proc/self/exe'), str(B / 'board_supervisor.py')],
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        server.timeout = .25
        end = time.monotonic() + 420
        while not shutdown and time.monotonic() < end:
            server.handle_request()
        if child.poll() is None:
            Path('/tmp/link-live/photo-console.stop').touch()
            try:
                child.wait(timeout=35)
            except subprocess.TimeoutExpired:
                child.terminate()
                child.wait(timeout=5)
        log.close()


if __name__ == '__main__':
    main()
