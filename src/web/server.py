#!/usr/bin/python3
"""Loopback-only photo console. No arbitrary command or upload endpoint."""
import fcntl
import hmac
import http.server
import json
import os
import secrets
import signal
import threading
import time
from urllib.parse import unquote

from config import APP, STATE, WEB_PORT
from workflow import Manager
from video_control import VideoManager, MAX_FILE


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, manager=None, video=None):
        self.manager = manager or Manager()
        self.video = video or VideoManager()
        self.token = secrets.token_hex(32)
        super().__init__(address, Handler)


class Handler(http.server.BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(60)

    def allowed_host(self):
        return self.headers.get('Host') in ('127.0.0.1:' + str(self.server.server_port),
            'localhost:' + str(self.server.server_port)) and self.client_address[0] == '127.0.0.1'

    def reply(self, value, code=200):
        self.raw(json.dumps(value, ensure_ascii=False).encode(), 'application/json; charset=utf-8', code)

    def raw(self, raw, kind, code=200):
        self.send_response(code)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; media-src 'self' blob:; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if not self.allowed_host():
            self.reply(dict(error='仅允许本机固定地址'), 403)
            return
        static = {'/': ('index.html', 'text/html; charset=utf-8'),
            '/photo': ('photo.html', 'text/html; charset=utf-8'),
            '/photo.js': ('photo.js', 'text/javascript; charset=utf-8'),
            '/video.css': ('video.css', 'text/css; charset=utf-8'),
            '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
            '/style.css': ('style.css', 'text/css; charset=utf-8')}
        if self.path in static:
            name, kind = static[self.path]
            self.raw((APP / 'dist' / name).read_bytes(), kind)
        elif self.path == '/api/state':
            self.reply(dict(state=self.server.manager.snapshot(), csrf=self.server.token))
        elif self.path == '/api/video/library':
            root = __import__('pathlib').Path('/home/leeeo/Downloads/videos')
            self.reply(dict(files=[p.name for p in sorted(root.glob('*.mp4')) if p.is_file() and not p.is_symlink()]))
        elif self.path == '/api/video/state':
            self.reply(dict(state=self.server.video.snapshot(), csrf=self.server.token))
        elif self.path.startswith('/api/video/media/'):
            self.serve_video(self.path.removeprefix('/api/video/media/'))
        elif self.path.startswith('/api/photo/'):
            manager = self.server.manager
            with manager.mutex:
                p = None if manager.run is None else manager.run / 'received-photo.png'
                if self.path != '/api/photo/' + manager.state.get('run_id', '') or p is None or not p.is_file():
                    self.reply(dict(error='本轮照片尚未恢复'), 404)
                else:
                    self.raw(p.read_bytes(), 'image/png')
        else:
            self.reply(dict(error='未知操作'), 404)

    def do_POST(self):
        port = self.server.server_port
        if not self.allowed_host() or self.headers.get('Origin') not in (f'http://127.0.0.1:{port}', f'http://localhost:{port}') or not hmac.compare_digest(self.headers.get('X-Photo-CSRF', ''), self.server.token):
            self.reply(dict(error='请求来源或会话校验失败'), 403)
            return
        try:
            size = int(self.headers.get('Content-Length', '-1'))
            if self.path == '/api/video/media':
                if self.headers.get('Content-Type') != 'application/octet-stream' or not 0 < size <= MAX_FILE:
                    raise ValueError('仅接受不超过 2 GiB 的完整原视频文件')
                self.reply(dict(state=self.server.video.upload(self.rfile, size,
                    unquote(self.headers.get('X-Video-Name', 'video')))))
                return
            if not 0 <= size <= 2048 or self.headers.get('Content-Type') != 'application/json':
                raise ValueError('只接受固定 JSON 操作')
            data = json.loads(self.rfile.read(size))
            if self.path == '/api/video/select' and isinstance(data,dict) and set(data)=={'name'}:
                from pathlib import Path
                root=Path('/home/leeeo/Downloads/videos')
                name=data['name']
                if not isinstance(name,str) or Path(name).name != name or not name.endswith('.mp4'):
                    raise ValueError('无效视频名称')
                path=root/name
                if path.is_symlink() or not path.is_file():raise ValueError('视频不存在或为链接')
                with path.open('rb') as stream:
                    state=self.server.video.upload(stream,path.stat().st_size,name)
            elif self.path == '/api/video/visual' and isinstance(data,dict) and set(data)=={'run_id','choice'}:
                state=self.server.video.visual(data['run_id'],data['choice'])
            elif self.path == '/api/video/settings':
                state = self.server.video.settings(data)
            elif self.path == '/api/video/start' and isinstance(data, dict) and set(data) == {'media_id'}:
                state = self.server.video.start(data['media_id'])
            elif self.path == '/api/video/stop' and data == {}:
                state = self.server.video.stop()
            elif self.path == '/api/start' and data == {}:
                state = self.server.manager.start()
            elif self.path == '/api/stop' and data == {}:
                state = self.server.manager.stop()
            elif self.path == '/api/visual' and isinstance(data, dict) and set(data) == {'run_id', 'choice'}:
                state = self.server.manager.visual(data['run_id'], data['choice'])
            else:
                raise ValueError('未知操作或多余参数；不能执行任意命令')
            self.reply(dict(state=state))
        except (ValueError, TypeError) as error:
            self.reply(dict(error=str(error)), 400)
        except RuntimeError as error:
            self.reply(dict(error=str(error)), 409)
        except (OSError, TimeoutError) as error:
            self.reply(dict(error='本机文件或后端操作失败：' + str(error)), 500)

    def serve_video(self, ident):
        try:
            path, meta = self.server.video.media_path(ident)
        except ValueError as error:
            self.reply(dict(error=str(error)), 404)
            return
        size = meta['bytes']
        start, end, code = 0, size - 1, 200
        value = self.headers.get('Range')
        if value:
            try:
                if not value.startswith('bytes=') or ',' in value:
                    raise ValueError()
                left, right = value[6:].split('-')
                if left:
                    start = int(left)
                    end = min(int(right), size - 1) if right else size - 1
                else:
                    suffix = int(right)
                    if suffix <= 0:
                        raise ValueError()
                    start = max(0, size - suffix)
                if not 0 <= start <= end < size:
                    raise ValueError()
                code = 206
            except ValueError:
                self.send_response(416)
                self.send_header('Content-Range', f'bytes */{size}')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
        self.send_response(code)
        self.send_header('Content-Type', meta['mime'])
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Content-Length', str(end - start + 1))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        if code == 206:
            self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
        self.end_headers()
        with path.open('rb') as source:
            source.seek(start)
            remaining = end - start + 1
            while remaining:
                chunk = source.read(min(256 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def log_message(self, format, *args):
        print(json.dumps(dict(time=time.time(), path=self.path, message=format % args)), flush=True)


def main():
    STATE.mkdir(exist_ok=True, mode=0o700, parents=True)
    lock = open(STATE / 'server.lock', 'a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit('控制台已经运行：http://127.0.0.1:8098')
    server = Server(('127.0.0.1', WEB_PORT))
    def stop(*args):
        server.manager.stop()
        server.video.stop()
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    print('PHOTO_CONSOLE_READY http://127.0.0.1:' + str(WEB_PORT), flush=True)
    try:
        server.serve_forever()
    finally:
        server.manager.stop()
        server.video.stop()
        if server.video.thread:
            server.video.thread.join(90 if server.video.builtin() else 12)
        if server.manager.thread:
            server.manager.thread.join(90)
        server.server_close()
        lock.close()


if __name__ == '__main__':
    main()
