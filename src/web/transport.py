"""One UART owner and bounded, fixed-resource RAM transfer server."""
from pathlib import Path
import fcntl
import http.server
import json
import os
import select
import termios
import threading
import time
import uuid

from config import SERIAL, HOST, BOARD, sha


class Cancelled(Exception):
    pass


class UART:
    def __init__(self, logs, stop):
        self.logs = Path(logs)
        self.logs.mkdir()
        self.stop = stop
        self.sequence = 0
        self.fd = os.open(SERIAL, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.ioctl(self.fd, termios.TIOCEXCL)
            attrs = termios.tcgetattr(self.fd)
            attrs[0] = attrs[1] = attrs[3] = 0
            attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
            attrs[4] = attrs[5] = termios.B115200
            termios.tcsetattr(self.fd, termios.TCSANOW, attrs)
        except BaseException:
            os.close(self.fd)
            raise

    def close(self):
        if self.fd is not None:
            try:
                fcntl.ioctl(self.fd, termios.TIOCNXCL)
            finally:
                os.close(self.fd)
                self.fd = None

    def write(self, raw):
        data = memoryview(raw)
        while data:
            try:
                n = os.write(self.fd, data)
                data = data[n:]
            except BlockingIOError:
                select.select([], [self.fd], [], .1)

    def read(self, seconds=.1):
        if select.select([self.fd], [], [], seconds)[0]:
            try:
                return os.read(self.fd, 65536)
            except BlockingIOError:
                pass
        return b''

    def command(self, command, name, timeout=20, uboot=False, ignore_stop=False):
        if self.stop.is_set() and not ignore_stop:
            raise Cancelled('用户停止')
        if any(len(line.encode()) > 480 for line in command.splitlines()):
            raise ValueError('UART 命令行过长')
        marker = 'PHOTO_DONE_' + uuid.uuid4().hex
        wire = command.rstrip('\n') + (('; echo ' + marker + '\n') if uboot else
            "\nprintf '\\n%s%s\\n' '" + marker[:10] + "' '" + marker[10:] + "'\n")
        self.write(wire.encode())
        raw = bytearray()
        deadline = time.monotonic() + timeout
        try:
            while time.monotonic() < deadline:
                if self.stop.is_set() and not ignore_stop:
                    self.write(b'\x03')
                    raise Cancelled('用户停止')
                raw.extend(self.read())
                if b'\n' + marker.encode() + b'\r\n' in raw or b'\n' + marker.encode() + b'\n' in raw:
                    return bytes(raw)
                if b'Kernel panic' in raw:
                    raise RuntimeError('板端内核 panic')
            self.write(b'\x03')
            abort_end = time.monotonic() + .5
            while time.monotonic() < abort_end:
                raw.extend(self.read(.05))
            raise TimeoutError('串口命令超时：' + name)
        finally:
            self.sequence += 1
            (self.logs / f'{self.sequence:03d}-{name}.log').write_bytes(raw)

    def reboot_to_uboot(self, notify):
        # The old standalone SD intentionally has no shell. First entry can
        # need one physical reset; subsequent init=/bin/sh RAM boots reboot.
        raw = bytearray()
        self.write(b'\x03\n')
        probe_end = time.monotonic() + .5
        while time.monotonic() < probe_end:
            raw.extend(self.read(.05))
        if b'Zynq>' in raw:
            (self.logs / 'reset-to-uboot.log').write_bytes(raw)
            return
        self.write(b'reboot -f\n')
        deadline = time.monotonic() + 120
        prompted = False
        while time.monotonic() < deadline:
            if self.stop.is_set():
                raise Cancelled('用户停止启动恢复')
            chunk = self.read()
            raw.extend(chunk)
            if b'Hit any key' in raw:
                self.write(b' ')
            if b'Zynq>' in raw:
                (self.logs / 'reset-to-uboot.log').write_bytes(raw)
                return
            if not prompted and deadline - time.monotonic() < 115:
                notify('当前启动环境没有可用命令入口，请按一次 ZedBoard PS-RST／PS_SRST，勿按 PROG。')
                prompted = True
        (self.logs / 'reset-to-uboot.log').write_bytes(raw)
        raise TimeoutError('未到达 U-Boot；请核对处理器复位和串口连接')

    def boot_kernel(self):
        self.write(b'bootz 0x02000000 - 0x10000000\n')
        raw = bytearray()
        end = time.monotonic() + 60
        try:
            while time.monotonic() < end:
                if self.stop.is_set():
                    raise Cancelled('用户停止 RAM 启动')
                raw.extend(self.read())
                if b'Kernel panic' in raw:
                    raise RuntimeError('RAM 启动 panic')
                if b'job control turned off' in raw or b'/ #' in raw or b'~ #' in raw:
                    return
            raise TimeoutError('未进入干净 RAM 命令环境')
        finally:
            (self.logs / 'ram-boot.log').write_bytes(raw)


class TransferServer:
    def __init__(self, files, log):
        self.files = {name: Path(path) for name, path in files.items()}
        self.rows = []
        self.log = Path(log)
        owner = self
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.client_address[0] != BOARD or self.path not in owner.files:
                    self.send_error(403)
                    return
                p = owner.files[self.path]
                self.send_response(200)
                self.send_header('Content-Length', str(p.stat().st_size))
                self.end_headers()
                with p.open('rb') as stream:
                    while raw := stream.read(65536):
                        self.wfile.write(raw)
                owner.rows.append(dict(resource=self.path, bytes=p.stat().st_size, sha256=sha(p)))
            def log_message(self, *args):
                pass
        class Server(http.server.ThreadingHTTPServer):
            daemon_threads = True
            allow_reuse_address = True
        self.server = Server((HOST, 8002), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)
        self.log.write_text(json.dumps(self.rows, indent=2) + '\n')
