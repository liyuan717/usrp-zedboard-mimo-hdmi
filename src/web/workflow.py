"""Repeatable fixed photo workflow. All hardware operations live in this worker."""
from pathlib import Path
import base64
import fcntl
import hashlib
import importlib.util
import io
import json
import os
import re
import secrets
import signal
import shutil
import select
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid

from config import (APP, BASE, BOARD, BOARD_APP, BOARD_OUT, BOOT, D, HOST,
    LEASE, RUNS, RUNTIME, SERIAL, STATE, EVIDENCE_NAMES, sha, verify_assets)
from bundle import build_bundle
from transport import Cancelled, TransferServer, UART


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise RuntimeError('拒绝重定向')


def write_json(path, value):
    temporary = path.with_suffix('.pending')
    with temporary.open('w') as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + '\n')
    temporary.replace(path)


class Manager:
    def __init__(self):
        RUNS.mkdir(exist_ok=True)
        STATE.mkdir(exist_ok=True, mode=0o700)
        self.mutex = threading.RLock()
        self.thread = None
        self.stop_event = threading.Event()
        self.state = dict(stage='idle', detail='准备就绪：仅运行 seu.png 新空口照片', active=False)
        self.run = None
        self.tx = None
        self.lock_fd = None
        self.agent_started = False
        self.kernel_ready = False
        self.uart = None
        self.secret = None
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        saved = sorted(RUNS.glob('*/state.json'))
        if saved:
            try:
                previous = json.loads(saved[-1].read_text())
                self.run = saved[-1].parent
                self.state = previous
                if previous.get('active'):
                    self.state = dict(stage='failed', active=False,
                        detail='上轮后台异常退出，硬件状态尚未确认；点击开始将先执行干净 RAM 恢复',
                        previous_run_id=previous.get('run_id'), recovery_required=True)
                    self.run = None
            except (OSError, ValueError):
                self.run = None

    def snapshot(self):
        with self.mutex:
            return json.loads(json.dumps(self.state))

    def update(self, stage=None, detail=None, **fields):
        with self.mutex:
            if stage is not None:
                self.state['stage'] = stage
            if detail is not None:
                self.state['detail'] = detail
            self.state.update(fields, updated_unix_ns=time.time_ns())
            if self.run is not None:
                write_json(self.run / 'state.json', self.state)

    def start(self):
        with self.mutex:
            if self.thread is not None and self.thread.is_alive():
                raise RuntimeError('已有任务正在运行或停止中')
            fd = os.open(STATE / 'operation.lock', os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                raise RuntimeError('另一控制台进程正在操作设备')
            self.lock_fd = fd
            run_id = uuid.uuid4().hex
            self.run = RUNS / (time.strftime('%Y%m%d-%H%M%S') + '-' + run_id[:8])
            self.run.mkdir(exist_ok=False, mode=0o700)
            self.stop_event.clear()
            self.secret = secrets.token_hex(32)
            self.tx = None
            self.uart = None
            self.agent_started = False
            self.kernel_ready = False
            self.state = dict(run_id=run_id, directory=str(self.run), active=True,
                stage='preparing', detail='校验基线与设备连接', mode='new_OTA_photo',
                historical_redisplay=False, visual_confirmation=None, started_unix_ns=time.time_ns())
            self.update()
            self.thread = threading.Thread(target=self._work, daemon=True)
            self.thread.start()
            return self.snapshot()

    def stop(self):
        with self.mutex:
            if self.thread is not None and self.thread.is_alive():
                self.stop_event.set()
                self.update(stage='stopping', detail='正在停止发射，随后关闭接收与显示并保存日志')
            return self.snapshot()

    def check_stop(self):
        if self.stop_event.is_set():
            raise Cancelled('用户停止本轮任务')

    def pause(self, seconds):
        if self.stop_event.wait(seconds):
            raise Cancelled('用户停止本轮任务')

    def command(self, text, name, timeout=20, uboot=False, ignore_stop=False):
        return self.uart.command(text, name, timeout, uboot, ignore_stop)

    def preflight(self):
        verify_assets()
        if shutil.disk_usage(APP).free < 128 << 20:
            raise RuntimeError('本机剩余空间不足 128 MiB；请先保留证据并释放空间')
        if not SERIAL.exists():
            raise RuntimeError('未找到指定 ZedBoard USB 串口，请连接串口线')
        if not os.access(SERIAL, os.R_OK | os.W_OK):
            raise RuntimeError('串口权限不足；请确认当前账号属于 dialout 后重新登录')
        used = subprocess.run(['fuser', str(SERIAL.resolve())], capture_output=True, text=True, timeout=3)
        if used.returncode == 0 and used.stdout.strip():
            raise RuntimeError('串口被其他进程占用（PID ' + used.stdout.strip() + '）；请退出 picocom 等程序')
        if subprocess.run(['systemctl', 'is-active', '--quiet', 'mimo-photo-tftp.socket']).returncode:
            raise RuntimeError('首次配置未完成：请执行页面显示的 setup_once.py 命令')
        self.ensure_host_route()
        routes = {}
        for address in [BOARD, '192.168.10.2']:
            routes[address] = json.loads(subprocess.check_output(['ip', '-j', 'route', 'get', address], timeout=3))
        if routes[BOARD][0].get('prefsrc', routes[BOARD][0].get('src')) != HOST or routes['192.168.10.2'][0]['dev'] != 'enp12s0':
            raise RuntimeError('板卡或 USRP 直连路由不符合已验证配置')
        subprocess.run(['ping', '-c', '1', '-W', '1', '192.168.10.2'], check=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
        write_json(self.run / 'preflight.json', dict(valid=True, routes=routes,
            source_photo_SHA=sha('/home/leeeo/Downloads/seu.png'), SD_written=False))

    def ensure_host_route(self):
        route = json.loads(subprocess.check_output(['ip', '-j', 'route', 'get', BOARD], timeout=3))
        if route[0].get('prefsrc', route[0].get('src')) != HOST:
            subprocess.run(['nmcli', '--wait', '10', 'connection', 'up',
                'codex-zedboard-integration', 'ifname', 'enxec1ac33f53f0'],
                capture_output=True, check=True, timeout=15)
        route = json.loads(subprocess.check_output(['ip', '-j', 'route', 'get', BOARD], timeout=3))
        if route[0].get('prefsrc', route[0].get('src')) != HOST:
            raise RuntimeError('板卡直连网口地址未恢复')

    def boot(self):
        self.uart = UART(self.run / 'serial', self.stop_event)
        self.uart.reboot_to_uboot(lambda text: self.update('preparing', text, hardware_action='PS_RESET'))
        self.update('preparing', 'U-Boot 已接管，载入固定 RAM 启动资产', hardware_action=None)
        self.ensure_host_route()
        self.pause(2)
        manifest = json.loads((BOOT / 'BOOT_MANIFEST.json').read_text())
        network_attempts = []
        for attempt in (1, 2):
            network = self.command('setenv ipaddr 169.254.209.84; setenv serverip 169.254.209.83; '
                'setenv netmask 255.255.0.0; setenv tftpdstp; setenv tftptimeout 1000; '
                'setenv tftptimeoutcountmax 2; ping 169.254.209.83', 'uboot-network-' + str(attempt), 60, uboot=True)
            network_attempts.append(dict(attempt=attempt, host_reachable=b'is alive' in network,
                no_RF_started=True))
            write_json(self.run / 'network-preflight.json', network_attempts)
            if b'is alive' in network:
                break
            if attempt == 2:
                raise RuntimeError('两次有界 U-Boot 网络预检失败，未启动发射')
            self.update('preparing', 'PHY 网络预检未通过，等待后仅重试一次连接检查；尚未发射')
            self.ensure_host_route()
            self.pause(3)
        write_json(LEASE, dict(run_id=self.state['run_id'], expires_monotonic=time.monotonic() + 170))
        LEASE.chmod(0o600)
        addresses = {'hdmi_pipeline.bit': 0x04000000, 'hdmi.dtb': 0x10000000, 'zImage': 0x02000000}
        for row in manifest['files']:
            self.check_stop()
            address = addresses[row['file']]
            data = self.command(f"tftpboot 0x{address:08x} {row['file']}; echo TRANSFER_RC=$?; "
                f"crc32 0x{address:08x} 0x{row['bytes']:x}", 'tftp-' + row['file'], 35, uboot=True)
            if b'TRANSFER_RC=0' not in data or not re.search(rb'==>\s*' + row['crc32'].encode(), data):
                raise RuntimeError('TFTP 下载或板端 CRC 校验失败：' + row['file'])
        LEASE.unlink(missing_ok=True)
        data = self.command('fpga loadb 0 0x04000000 0x3dbb86; echo FPGA_LOAD_RC=$?; '
            'md.l 0x7c430000 8; md.l 0x44a20000 8', 'fpga-verified-baseline', 20, uboot=True)
        if b'FPGA_LOAD_RC=0' not in data or b'494d4243' not in data or b'48504f31' not in data:
            raise RuntimeError('固定 FPGA 资产加载或寄存器身份核验失败')
        self.command('setenv bootargs console=ttyPS0,115200 root=/dev/mmcblk0p2 rootwait '
            'ro rootflags=noload rootfstype=ext4 cma=64M init=/bin/sh', 'ram-readonly-bootargs', uboot=True)
        self.uart.boot_kernel()
        self.kernel_ready = True
        self.command('mount -t proc proc /proc; mount -t sysfs sysfs /sys; '
            'mount -t tmpfs -o size=128m tmpfs /tmp; ip link set eth0 up; '
            'ip addr add 169.254.209.84/16 dev eth0; mkdir -p /tmp/link-share', 'ram-mounts')
        self.command('echo 1 > /sys/bus/iio/devices/iio:device0/out_altvoltage0_RX_LO_powerdown; '
            'echo 1 > /sys/bus/iio/devices/iio:device0/out_altvoltage1_TX_LO_powerdown', 'initial-LO-off')
        data = self.command('uname -r; cat /proc/sys/kernel/random/boot_id; '
            'cat /proc/cmdline; head -1 /proc/mounts', 'ram-identity')
        matches = re.findall(rb'\n([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})\r?\n', data)
        if len(matches) != 1 or b'6.12.0-xilinx-usrp-hdmi' not in data or b'rootflags=noload' not in data or b'/dev/root / ext4 ro,' not in data:
            raise RuntimeError('RAM 内核、boot_id 或只读根核验失败')
        boot_id = matches[0].decode()
        for previous in RUNS.glob('*/result.json'):
            if previous.parent != self.run and json.loads(previous.read_text()).get('boot_id') == boot_id:
                raise RuntimeError('发现旧 boot_id，拒绝复用旧显示环境')
        self.update(boot_id=boot_id)
        write_json(self.run / 'ram-boot.json', dict(boot_id=boot_id, SD_written=False,
            root_readonly=True, saveenv=False, assets=manifest['files']))
        return boot_id

    def transfer(self, bundle, boot_id):
        server = TransferServer({'/runtime': RUNTIME, '/app': bundle}, self.run / 'transfers.json')
        try:
            data = self.command('wget -O /tmp/runtime.tar.gz http://169.254.209.83:8002/runtime; '
                'sha256sum /tmp/runtime.tar.gz', 'download-runtime', 30)
            if sha(RUNTIME).encode() not in data:
                raise RuntimeError('运行包下载 SHA 不符')
            data = self.command("/usr/bin/python3.12 - <<'PY'\nfrom pathlib import Path\n"
                "import hashlib,tarfile,json,stat\np=Path('/tmp/runtime.tar.gz');r=Path('/tmp/link-live')\n"
                "assert hashlib.sha256(p.read_bytes()).hexdigest()=='6d4214aecdb892ed3c25840ecaeff6e580ef3d7a3d6d2a3f14867a16e69d4294'\n"
                "r.mkdir()\nwith tarfile.open(p) as t:t.extractall(r,filter='data')\n"
                "for v in json.loads((r/'MANIFEST.json').read_text())['files']:\n"
                " q=r/v['path'];assert q.stat().st_size==v['bytes']\n"
                " assert hashlib.sha256(q.read_bytes()).hexdigest()==v['sha256']\n"
                " assert stat.S_IMODE(q.stat().st_mode)==v['mode']\n"
                "(r/'EXPECTED_BOOT_ID').write_text(Path('/proc/sys/kernel/random/boot_id').read_text().strip())\n"
                "p.unlink();print('RUNTIME_VERIFIED')\nPY", 'extract-verified-runtime', 35)
            if not re.search(rb'\nRUNTIME_VERIFIED\r?\n', data):
                raise RuntimeError('运行包完整解包校验未通过')
            data = self.command('wget -O /tmp/app.tar.gz http://169.254.209.83:8002/app; '
                'sha256sum /tmp/app.tar.gz', 'download-app', 20)
            if sha(bundle).encode() not in data:
                raise RuntimeError('本轮控制包下载 SHA 不符')
            data = self.command("/usr/bin/python3.12 - <<'PY'\nfrom pathlib import Path\n"
                "import hashlib,tarfile,json\np=Path('/tmp/app.tar.gz');r=Path('/tmp/link-live/photo-console');r.mkdir()\n"
                f"assert hashlib.sha256(p.read_bytes()).hexdigest()=='{sha(bundle)}'\n"
                "with tarfile.open(p) as t:t.extractall(r,filter='data')\n"
                "for v in json.loads((r/'FILES.json').read_text()):\n"
                " q=r/v['name'];assert q.stat().st_size==v['bytes']\n"
                " assert hashlib.sha256(q.read_bytes()).hexdigest()==v['sha256']\n"
                "p.unlink();print('APP_VERIFIED')\nPY", 'extract-verified-app', 20)
            if not re.search(rb'\nAPP_VERIFIED\r?\n', data):
                raise RuntimeError('本轮控制包内容校验未通过')
        finally:
            server.close()
        for script, marker in [('hdmi-dmac-r2/bind_hdmi_provider.py', b'HDMI_ONLY_PROVIDER_BOUND'),
            ('prepare_board_live.py', b'LIVE_RXTH_IMBC_READY_NO_CAPTURE')]:
            data = self.command('/usr/bin/python3.12 /tmp/link-live/' + script + ' ' + boot_id,
                Path(script).stem, 40)
            if marker not in data:
                raise RuntimeError('板端配置未通过：' + script)
        self.command('/usr/bin/python3.12 /tmp/link-live/photo-console/board_agent.py '
            '> /tmp/link-share/photo-console-agent.log 2>&1 &', 'launch-agent')
        self.agent_started = True

    def request(self, path, post=False, timeout=4):
        req = urllib.request.Request('http://' + BOARD + ':8001' + path,
            data=b'{}' if post else None,
            headers={'X-Photo-Key': self.secret, 'Content-Type': 'application/json'},
            method='POST' if post else 'GET')
        with self.opener.open(req, timeout=timeout) as stream:
            raw = stream.read((32 << 20) + 1)
            if len(raw) > 32 << 20:
                raise RuntimeError('板端响应超过固定上限')
            return raw

    def status(self):
        result = json.loads(self.request('/status'))
        if result.get('run_id') != self.state['run_id'] or result.get('boot_id') != self.state['boot_id']:
            raise RuntimeError('板端回包属于另一轮任务')
        return result

    def transmit(self):
        spec = importlib.util.spec_from_file_location('frozen_photo_sender', BASE / 'send_gain18_12f.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        stream = module.build_iq_stream()
        out = self.run / 'tx';out.mkdir()
        # Exact frozen transmitter argv, IQB and existing 0.65 s capture timing.
        argv = [str(BASE / 'tx-gain18/continuous_tx'), '--stats', str(out / 'transport.json'),
            '--actual', str(out / 'actual-readback.json'), '--freq', '5500000000',
            '--bandwidth', '40000000', '--gain', '18.0', '--scale', '1', '--queue', '8',
            '--prefill', '4', '--max-send-samples', '65536', '--clock', 'internal',
            '--time', 'internal', '--addr', '192.168.10.2', '--serial', '3095BA5',
            '--enable-tx', 'YES', '--tx-zero-preroll-samples', '400000']
        write_json(out / 'command.json', argv)
        with (out / 'stdout.log').open('xb') as stdout, (out / 'stderr.log').open('xb') as stderr:
            self.tx = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=stdout,
                stderr=stderr, start_new_session=True)
            os.set_blocking(self.tx.stdin.fileno(), False)
            pending = memoryview(stream)
            input_deadline = time.monotonic() + 15
            while pending:
                self.check_stop()
                if self.tx.poll() is not None or time.monotonic() > input_deadline:
                    raise RuntimeError('USRP 发送进程未能接收有限 IQ 数据')
                if select.select([], [self.tx.stdin], [], .05)[1]:
                    try:
                        count = os.write(self.tx.stdin.fileno(), pending[:65536])
                        pending = pending[count:]
                    except BlockingIOError:
                        pass
            self.tx.stdin.close()
            end = time.monotonic() + 15
            while True:
                self.check_stop()
                try:
                    actual = json.loads((out / 'actual-readback.json').read_text())
                    break
                except (OSError, ValueError):
                    if self.tx.poll() is not None or time.monotonic() > end:
                        raise RuntimeError('USRP 未返回本轮实际配置')
                self.pause(.005)
            if not actual['configuration_matches_requested'] or len(actual['channels']) != 2 or any(abs(x['gain_db'] - 18) > .01 for x in actual['channels']):
                raise RuntimeError('USRP 实际配置与固定照片参数不同')
            configured = time.monotonic()
            self.pause(.65)
            self.command('touch /tmp/link-live/photo-console.trigger', 'trigger-new-ADC', 10)
            write_json(out / 'timing.json', dict(configured_host_monotonic=configured,
                triggered_host_monotonic=time.monotonic(), delay_seconds=.65))
            end = time.monotonic() + 25
            while self.tx.poll() is None:
                self.check_stop()
                if time.monotonic() > end:
                    raise TimeoutError('USRP 有限发射超时')
                self.pause(.05)
        stats = json.loads((out / 'transport.json').read_text())
        if not (self.tx.returncode == 0 and stats['transport_pass'] and
            stats['input_samples_per_channel'] == stats['accepted_samples_per_channel'] == 161280
            and all(stats[k] == 0 for k in ('underflows', 'late_errors', 'other_async_errors'))):
            raise RuntimeError('USRP 有限发射检查失败')
        write_json(out / 'result.json', dict(valid=True, new_RF=True, frames=42,
            unique_frames=41, statistics=stats, IQB_sha256=hashlib.sha256(stream).hexdigest()))

    def fetch(self, status):
        out = self.run / 'evidence';out.mkdir(exist_ok=True)
        rows = []
        for row in status.get('files', []):
            name = row['name']
            if name not in EVIDENCE_NAMES or row['bytes'] > 32 << 20:
                raise RuntimeError('板端证据文件不在固定范围')
            data = self.request('/evidence/' + name, timeout=20)
            if len(data) != row['bytes'] or hashlib.sha256(data).hexdigest() != row['sha256']:
                raise RuntimeError('证据传输 SHA 不符：' + name)
            destination = out / name
            if destination.exists():
                if sha(destination) != row['sha256']:
                    raise RuntimeError('拒绝覆盖已有不同证据：' + name)
            else:
                destination.write_bytes(data)
            rows.append(row)
        write_json(self.run / 'evidence-manifest.json', rows)
        return out

    def grade(self, evidence):
        code = 'import sys,json;sys.path.insert(0,sys.argv[1]);from grade_evidence import grade;from pathlib import Path;print(json.dumps(grade(Path(sys.argv[2]))))'
        done = subprocess.run(['/usr/bin/python3', '-c', code, str(BASE), str(evidence)],
            capture_output=True, text=True, timeout=20)
        (self.run / 'grade.stdout').write_text(done.stdout)
        (self.run / 'grade.stderr').write_text(done.stderr)
        if done.returncode:
            raise RuntimeError('独立照片核验失败：' + done.stderr[-1800:])
        result = json.loads(done.stdout)
        board = json.loads((evidence / 'result.json').read_text())
        if board['run_id'] != self.state['run_id'] or board['boot_id'] != self.state['boot_id']:
            raise RuntimeError('接收结果不是本轮 RAM 运行')
        if board['rx_lo_powerdown'] != '1' or board['tx_lo_powerdown'] != '1' or board['adc_buffer_enable'] != '0' or board['kernel_error_lines']:
            raise RuntimeError('板端收尾状态异常')
        for previous in RUNS.glob('*/result.json'):
            if previous.parent != self.run and json.loads(previous.read_text()).get('ADC_sha256') == result['ADC']['sha256']:
                raise RuntimeError('ADC 与旧轮完全相同，拒绝旧结果重用')
        received = evidence / 'received-live.rfd'
        # Reconstruction runs in its own interpreter to isolate pinned imports.
        recover_code = 'import sys;sys.path.insert(0,sys.argv[1]);import grade_evidence as g;from pathlib import Path;from read_rxonly_delivery import iter_delivery;f=Path(sys.argv[2]).open("rb");records=list(iter_delivery(f));g.recover_au(records);data=bytearray(8200)\nfor r in records:\n for p in r.packets:\n  import struct\n  h=p[20:148];_,_,_,size,_,_,_,offset,_=struct.unpack_from("<4sBBHIIIII",h);data[offset:offset+size]=h[28:28+size]\nPath(sys.argv[3]).write_bytes(data[36:])'
        au = self.run / 'received-photo.h264'
        subprocess.run(['/usr/bin/python3', '-c', recover_code, str(BASE), str(received), str(au)], check=True, timeout=10)
        subprocess.run(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-i', str(au),
            '-frames:v', '1', str(self.run / 'received-photo.png')], check=True, timeout=15)
        frame_hashes = []
        for source in (au, BASE / 'tx-photo-offline/encoded.h264'):
            frame_hashes.append(subprocess.check_output(['ffmpeg', '-nostdin', '-v', 'error', '-i', str(source),
                '-f', 'framemd5', '-'], timeout=15))
        if frame_hashes[0] != frame_hashes[1]:
            raise RuntimeError('接收照片解码像素与发送编码不同')
        write_json(self.run / 'independent-grade.json', result)
        return dict(technical_pass=True, new_RF=True, new_ADC=True, historical_redisplay=False,
            run_id=self.state['run_id'], boot_id=self.state['boot_id'], ADC_blocks=result['ADC']['blocks'],
            ADC_sha256=result['ADC']['sha256'], RF_frames=len(board['received_frame_ids']),
            unique_RF_frames=result['unique_frames'], CRC_correct_packets=result['CRC_correct_byte_identical_packets'],
            AU=result['AU'], reconstructed_photo_pixels_match_TX=True, original_PNG_bit_exact_transfer=False,
            HDMI_frames=board['hdmi_proof']['presented_at_hdmi_boundary'],
            same_buffer_scanout_repeats=board['hdmi_proof']['scanout_repeats'],
            LO_closed=True, ADC_closed=True, kernel_errors=[], visual_confirmation=None)

    def terminate_tx(self):
        if self.tx is not None and self.tx.poll() is None:
            os.killpg(self.tx.pid, signal.SIGINT)
            try:
                self.tx.wait(timeout=4)
            except subprocess.TimeoutExpired:
                os.killpg(self.tx.pid, signal.SIGKILL)
                self.tx.wait(timeout=3)

    def _work(self):
        error = None
        cleanup = dict(TX_stopped=True, board_cleanup_confirmed=False)
        result = None
        try:
            self.preflight()
            self.check_stop()
            boot_id = self.boot()
            self.update('preparing', 'RAM 启动完成，校验运行包并配置原接收链')
            bundle = self.run / 'app.tar.gz'
            write_json(self.run / 'bundle-files.json', build_bundle(bundle, self.state['run_id'], boot_id, self.secret))
            self.transfer(bundle, boot_id)
            deadline = time.monotonic() + 45
            while True:
                self.check_stop()
                try:
                    status = self.status()
                except (OSError, urllib.error.URLError):
                    self.pause(.25)
                    if time.monotonic() > deadline:
                        raise TimeoutError('板端照片接收监督器未就绪')
                    continue
                if status['state'] == 'ready':
                    break
                if status['done']:
                    self.fetch(status)
                    raise RuntimeError('板端接收监督器提前结束：' + json.dumps(status.get('result', status), ensure_ascii=False)[-1500:])
                if time.monotonic() > deadline:
                    raise TimeoutError('原接收程序启动超时')
                self.pause(.2)
            self.update('transmitting', '正在发送固定照片的 42 个双流 RF 突发')
            self.transmit()
            self.update('processing', '有限发射已结束，等待本轮新 ADC 的 MIMO／PL／CRC 处理')
            deadline = time.monotonic() + 245
            while True:
                self.check_stop()
                status = self.status()
                if status['done']:
                    break
                if status['state'] == 'displaying':
                    self.update('displaying', 'HDMI 正在显示本轮实收照片，保持约 60 秒；请观察画面')
                elif status['state'] == 'cleanup':
                    self.update('verifying', '照片显示已结束，正在观察收尾内核状态并保存证据')
                if time.monotonic() > deadline:
                    raise TimeoutError('板端有限照片流程超时')
                self.pause(2)
            self.update('verifying', '读取本轮原始证据，逐字节核对照片包和 HDMI 回执')
            evidence = self.fetch(status)
            result = self.grade(evidence)
            cleanup['board_cleanup_confirmed'] = True
        except BaseException as failure:
            error = type(failure).__name__ + ': ' + str(failure)
        finally:
            try:
                self.terminate_tx()
            except BaseException as failure:
                cleanup.update(TX_stopped=False, TX_stop_error=str(failure))
            if self.agent_started:
                try:
                    if result is None:
                        self.request('/stop', post=True)
                        end = time.monotonic() + 70
                        while time.monotonic() < end:
                            status = self.status()
                            if status['done']:
                                self.fetch(status)
                                board = status.get('result', {})
                                cleanup['board_cleanup_confirmed'] = (board.get('rx_lo_powerdown') == '1'
                                    and board.get('tx_lo_powerdown') == '1' and board.get('adc_buffer_enable') == '0')
                                break
                            time.sleep(.25)
                    self.request('/shutdown', post=True)
                except BaseException as failure:
                    cleanup['board_cleanup_error'] = str(failure)
                    if self.uart is not None:
                        try:
                            self.command('touch /tmp/link-live/photo-console.stop; '
                                'echo 1 > /sys/bus/iio/devices/iio:device0/out_altvoltage0_RX_LO_powerdown; '
                                'echo 1 > /sys/bus/iio/devices/iio:device0/out_altvoltage1_TX_LO_powerdown',
                                'fallback-stop-request', 5, ignore_stop=True)
                        except BaseException as fallback:
                            cleanup['fallback_stop_error'] = str(fallback)
            LEASE.unlink(missing_ok=True)
            if self.uart is not None and self.kernel_ready:
                try:
                    # Post-display check only; never UART polling during hold.
                    if self.state.get('boot_id'):
                        self.command('echo 1 > /sys/bus/iio/devices/iio:device0/out_altvoltage0_RX_LO_powerdown; '
                            'echo 1 > /sys/bus/iio/devices/iio:device0/out_altvoltage1_TX_LO_powerdown',
                            'final-LO-off', 5, ignore_stop=True)
                    wire = self.command("echo IDLE; cat /sys/bus/iio/devices/iio:device0/out_altvoltage0_RX_LO_powerdown; "
                        "cat /sys/bus/iio/devices/iio:device0/out_altvoltage1_TX_LO_powerdown; "
                        "cat /sys/bus/iio/devices/iio:device3/buffer/enable", 'final-idle', 5, ignore_stop=True)
                    cleanup['final_idle_UART_reply'] = wire.decode(errors='replace')[-1500:]
                except BaseException as failure:
                    cleanup['final_idle_query_error'] = str(failure)
            if self.uart is not None:
                self.uart.close()
            if result is None:
                result = dict(technical_pass=False, new_RF=None if self.tx is not None else False,
                    transmit_attempted=self.tx is not None,
                    new_ADC=None, run_id=self.state['run_id'], boot_id=self.state.get('boot_id'), error=error)
            result['cleanup'] = cleanup
            result['finished_unix_ns'] = time.time_ns()
            if not cleanup['TX_stopped'] or (self.agent_started and not cleanup['board_cleanup_confirmed']):
                result['technical_pass'] = False
                result['error'] = (error or '') + '；收尾状态未确认，请检查日志后从干净 RAM 恢复'
            write_json(self.run / 'result.json', result)
            safe_to_stop = cleanup['TX_stopped'] and (not self.agent_started or cleanup['board_cleanup_confirmed'])
            stage = 'stopped' if self.stop_event.is_set() and safe_to_stop else ('complete' if result['technical_pass'] else 'failed')
            detail = ('本轮已停止，日志已保存' if stage == 'stopped' else
                ('照片空口、恢复与 HDMI 技术核验通过；请单独确认视觉稳定性' if stage == 'complete' else result.get('error', error)))
            self.update(stage, detail, active=False, result=result, hardware_action=None)
            if self.lock_fd is not None:
                os.close(self.lock_fd)
                self.lock_fd = None

    def visual(self, run_id, choice):
        if choice not in ('stable', 'accepted_minor_flicker', 'unstable', 'not_seen'):
            raise ValueError('视觉反馈值不在固定范围')
        with self.mutex:
            if self.state.get('run_id') != run_id or self.state.get('active') or not self.state.get('result', {}).get('technical_pass'):
                raise ValueError('只能确认本轮已完成且技术通过的结果')
            result = self.state['result']
            result['visual_confirmation'] = dict(choice=choice, confirmed_unix_ns=time.time_ns())
            result['visual_pass_under_user_criteria'] = choice in ('stable', 'accepted_minor_flicker')
            write_json(self.run / 'result.json', result)
            self.update(result=result)
            return self.snapshot()
