"""Original-file video queue and bounded USRP adapter interface; no media encoder."""
from pathlib import Path
import copy
import fcntl
import hashlib
import json
import math
import os
import re
import secrets
import shutil
import socket
import stat
import struct
import subprocess
import threading
import time

APP = Path(__file__).resolve().parent
ROOT = APP / 'video-data'
ADAPTER = APP / 'usrp-adapter.sock'
MAX_FILE = 2 * 1024 ** 3
DEFAULTS = dict(gain_db=18.0, frequency_hz=5500000000, sample_rate_hz=4000000,
                bandwidth_hz=40000000, channels=[0, 1], antenna='TX/RX',
                device_address='192.168.10.2')


def validate_settings(data):
    if not isinstance(data, dict) or set(data) != set(DEFAULTS):
        raise ValueError('发射参数字段不完整或含未知字段')
    for key, lo, hi in [('gain_db', 0, 18), ('frequency_hz', 1200000000, 6000000000),
                        ('bandwidth_hz', 1, 40000000)]:
        x = data[key]
        if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) or not lo <= x <= hi:
            raise ValueError(f'{key} 超出当前接口范围 [{lo}, {hi}]')
    for key in ['sample_rate_hz', 'channels', 'antenna', 'device_address']:
        if data[key] != DEFAULTS[key]:
            raise ValueError(key + ' 在当前已验证配置中固定')
    return copy.deepcopy(data)


def inspect_media(path):
    # Read metadata only. No decoding, stream extraction, resizing, or output media.
    try:
        result = subprocess.run(['/usr/bin/ffprobe', '-v', 'error', '-select_streams', 'v:0',
            '-show_entries', 'stream=width,height,codec_name,profile,has_b_frames,pix_fmt,avg_frame_rate,bit_rate:stream_tags=rotate:stream_side_data=rotation:format=format_name,duration',
            '-of', 'json', str(path)], capture_output=True, timeout=20, check=False)
    except subprocess.TimeoutExpired as error:
        raise ValueError('视频元信息检查超时；没有转换原视频') from error
    if result.returncode or len(result.stdout) > 65536:
        raise ValueError('无法读取视频元信息；原文件未做转换')
    value = json.loads(result.stdout)
    streams = value.get('streams', [])
    if len(streams) != 1:
        raise ValueError('未找到可播放的视频流')
    stream = streams[0]
    rotations = [float(stream.get('tags', {}).get('rotate', 0))]
    rotations += [float(x.get('rotation', 0)) for x in stream.get('side_data_list', [])]
    if (stream.get('width'), stream.get('height')) != (1024, 600) or any(x % 360 for x in rotations):
        raise ValueError('仅接受原始分辨率 1024 × 600、无旋转的视频；请自行准备文件，平台不会缩放或转码')
    if stream.get('codec_name') != 'h264' or stream.get('profile') != 'Constrained Baseline' or stream.get('has_b_frames') != 0 or stream.get('pix_fmt') != 'yuv420p':
        raise ValueError('板端要求 H.264 Constrained Baseline、YUV420P、无 B 帧；请自行准备兼容文件，平台不转码')
    fmt = value.get('format', {}).get('format_name', '')
    if 'mp4' in fmt or 'mov' in fmt:
        mime = 'video/mp4'
    else:
        raise ValueError('当前板端兼容候选仅接受 MP4 容器；不自动转换其他格式')
    return dict(width=1024, height=600, codec=stream.get('codec_name'), profile=stream.get('profile'), has_b_frames=stream.get('has_b_frames'), pix_fmt=stream.get('pix_fmt'), avg_frame_rate=stream.get('avg_frame_rate'), bit_rate=stream.get('bit_rate'), board_metadata_compatible=True, full_board_bitstream_validation=False, mime=mime,
                duration=value.get('format', {}).get('duration'))


class VideoManager:
    def __init__(self, root=ROOT, adapter=ADAPTER):
        self.root, self.adapter = Path(root), Path(adapter)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.thread = None
        self.cancel = threading.Event()
        self.uploading = False
        self.state = dict(stage='idle', active=False, detail='选择 1024 × 600 的原视频',
                          media=None, settings=copy.deepcopy(DEFAULTS), run_id=None,
                          error=None, device_applied=False, rf_verified=False, cleanup_unconfirmed=False)
        try:
            ident = (self.root / 'selected-media').read_text().strip()
            if re.fullmatch('[0-9a-f]{32}', ident):
                media = json.loads((self.root / 'media' / ident / 'manifest.json').read_text())
                if media.get('board_metadata_compatible') is True and media['id'] == ident and (media.get('width'), media.get('height')) == (1024, 600) and (self.root / 'media' / ident / 'original').stat().st_size == media['bytes']:
                    self.state.update(media=media, stage='ready', detail='已载入此前选择的原视频；尚未发送')
        except (OSError, ValueError, KeyError):
            pass
        settings = self.root / 'settings.json'
        if settings.is_file():
            try:
                self.state['settings'] = validate_settings(json.loads(settings.read_text()))
            except (ValueError, OSError):
                pass

    def builtin(self):
        return self.root.resolve() == ROOT.resolve() and self.adapter == ADAPTER

    def adapter_available(self):
        if self.builtin():
            return (APP / "original_backend.py").is_file()
        try:
            s = self.adapter.lstat()
            return stat.S_ISSOCK(s.st_mode) and s.st_uid == os.getuid()
        except FileNotFoundError:
            return False

    def snapshot(self):
        with self.lock:
            value = copy.deepcopy(self.state)
            value.update(adapter_available=self.adapter_available(), uploading=self.uploading)
            return value

    def settings(self, value):
        value = validate_settings(value)
        if self.builtin() and (value['frequency_hz'] != 5500000000 or value['bandwidth_hz'] != 40000000):
            raise ValueError('当前收发链固定5500 MHz和40 MHz带宽；增益可调0–18 dB')
        with self.lock:
            if self.state['active']:
                raise RuntimeError('发送中不能更改参数，请先停止')
            temp = self.root / 'settings.pending'
            temp.write_text(json.dumps(value))
            temp.replace(self.root / 'settings.json')
            self.state.update(settings=value, device_applied=False)
        return self.snapshot()

    def upload(self, stream, size, name):
        if self.builtin() and size > 180000:
            raise ValueError('当前单窗口版本限180000字节、25帧、5fps；请准备较短或较小的视频')
        if not 0 < size <= MAX_FILE:
            raise ValueError('原视频文件大小必须在 1 字节至 2 GiB 之间')
        with self.lock:
            if self.state['active'] or self.uploading:
                raise RuntimeError('已有传送或文件导入任务')
            self.uploading = True
        directory = None
        try:
            if shutil.disk_usage(self.root).free < size + 64 * 1024 ** 2:
                raise ValueError('磁盘空间不足，无法保存完整原文件')
            ident = secrets.token_hex(16)
            directory = self.root / 'media' / ident
            directory.mkdir(mode=0o700, parents=True)
            path = directory / 'original'
            digest = hashlib.sha256()
            remaining = size
            with path.open('xb') as target:
                while remaining:
                    chunk = stream.read(min(1024 ** 2, remaining))
                    if not chunk:
                        raise ValueError('原视频上传不完整，请重新选择')
                    target.write(chunk)
                    digest.update(chunk)
                    remaining -= len(chunk)
            meta = inspect_media(path)
            path.chmod(0o400)
            meta.update(id=ident, name=Path(name).name[:180] or 'video', bytes=size,
                        sha256=digest.hexdigest(), transformations=[], original_bytes_preserved=True)
            (directory / 'manifest.json').write_text(json.dumps(meta, ensure_ascii=False, indent=2))
            selected = self.root / 'selected-media.pending'
            selected.write_text(ident)
            selected.replace(self.root / 'selected-media')
            with self.lock:
                self.state.update(media=meta, stage='ready', detail='原视频就绪，文件未转码或压缩',
                                  error=None, run_id=None, device_applied=False, rf_verified=False, adapter_result=None, bytes_delivered=0, visual_confirmation=None)
            return self.snapshot()
        except Exception:
            if directory:
                shutil.rmtree(directory)
            raise
        finally:
            with self.lock:
                self.uploading = False

    def media_path(self, ident):
        with self.lock:
            media = self.state['media']
            if not media or ident != media['id']:
                raise ValueError('未知视频编号')
            return self.root / 'media' / ident / 'original', copy.deepcopy(media)

    def start(self, ident):
        with self.lock:
            if self.state['active'] or self.uploading:
                raise RuntimeError('已有任务正在运行')
            if self.state['cleanup_unconfirmed']:
                raise RuntimeError('前轮后端未确认停止，请先检查并关闭设备发射，再重启本机服务')
            path, media = self.media_path(ident)
            if not self.adapter_available():
                raise RuntimeError('USRP 发送后端尚未接入；本地播放可用，当前不会发射')
            # Share the original photo console/hardware lock; no overlapping workflows.
            from config import STATE
            STATE.mkdir(parents=True, exist_ok=True)
            lock = (STATE / 'operation.lock').open('a')
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                lock.close()
                raise RuntimeError('硬件正被其他任务占用')
            ident = secrets.token_hex(16)
            directory = self.root / 'runs' / ident
            try:
                directory.mkdir(mode=0o700, parents=True)
                request = dict(protocol='mimo-video-original-v1', operation='transmit', run_id=ident,
                               media=media, settings=copy.deepcopy(self.state['settings']))
                (directory / 'request.json').write_text(json.dumps(request, ensure_ascii=False, indent=2))
                self.cancel.clear()
                self.state.update(active=True, stage='preparing', detail='正在连接 USRP 发送后端',
                                  run_id=ident, error=None, device_applied=False, rf_verified=False,
                                  bytes_delivered=0, adapter_result=None, visual_confirmation=None)
                self.thread = threading.Thread(target=self._builtin_work if self.builtin() else self._work, args=(path, request, directory, lock), daemon=True)
                self.thread.start()
            except Exception:
                lock.close()
                raise
        return self.snapshot()

    def _builtin_work(self, path, request, directory, lock):
        try:
            from original_backend import run
            def notify(value):
                with self.lock:
                    self.state.update(stage=value.get('stage','preparing'), detail=value.get('detail') or '正在准备原文件发送',
                                      hardware_action=value.get('hardware_action'), native_state=value)
            result = run(path, directory, request['settings'], self.cancel, notify, lock, request['run_id'])
            r = result.get('result', {})
            with self.lock:
                self.state.update(stage=result['stage'], detail=result.get('detail'), error=None if result['stage']=='stopped' and r.get('cleanup',{}).get('board_cleanup_confirmed') else r.get('error'),
                    adapter_result=r, rf_verified=bool(r.get('technical_pass') and r.get('new_RF') and r.get('new_ADC')),
                    device_applied=bool(r.get('new_RF')), bytes_delivered=request['media']['bytes'] if r.get('original_file_byte_identical') else 0,
                    cleanup_unconfirmed=bool(r.get('transmit_attempted') or r.get('new_RF')) and not r.get('cleanup',{}).get('board_cleanup_confirmed',False))
        except Exception as error:
            import traceback
            (directory/'backend-error.txt').write_text(traceback.format_exc())
            with self.lock:
                self.state.update(stage='stopped' if self.cancel.is_set() else 'failed', error=None if self.cancel.is_set() and not (directory/'tx').exists() else str(error),detail=str(error),
                    cleanup_unconfirmed=(directory/'tx').exists())
        finally:
            lock.close()
            with self.lock:
                self.state['active']=False
                (directory/'web-result.json').write_text(json.dumps(self.state,ensure_ascii=False,indent=2))

    def visual(self, run_id, choice):
        with self.lock:
            if choice not in ('stable','unstable','not_seen') or self.state['active'] or run_id != self.state.get('run_id') or not self.state.get('rf_verified'):
                raise ValueError('仅能确认当前已完成的新空口测试')
            self.state['visual_confirmation']=dict(choice=choice,confirmed_unix_ns=time.time_ns())
            (self.root/'runs'/run_id/'user-visual.json').write_text(json.dumps(self.state['visual_confirmation']))
        return self.snapshot()

    def _work(self, path, request, directory, lock):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        started = time.monotonic()
        connected = False
        stopped_confirmed = False
        try:
            sock.settimeout(0.5)
            sock.connect(str(self.adapter))
            connected = True
            header = json.dumps(request, ensure_ascii=False).encode()
            def send(raw):
                view = memoryview(raw)
                while view:
                    if self.cancel.is_set():
                        raise InterruptedError('已停止向后端传送；后端须在断开连接时关闭发射')
                    if time.monotonic() - started > 300:
                        raise TimeoutError('后端传送超过 300 秒时限')
                    try:
                        n = sock.send(view)
                        if not n:
                            raise ConnectionError('USRP 后端连接断开')
                        view = view[n:]
                    except socket.timeout:
                        continue
            send(struct.pack('!I', len(header)) + header)
            with self.lock:
                self.state.update(stage='transmitting', detail='将原视频完整字节交给 USRP 后端')
            digest = hashlib.sha256()
            count = 0
            with path.open('rb') as source:
                while chunk := source.read(256 * 1024):
                    digest.update(chunk)
                    send(chunk)
                    count += len(chunk)
                    with self.lock:
                        self.state['bytes_delivered'] = count
            if count != request['media']['bytes'] or digest.hexdigest() != request['media']['sha256']:
                raise RuntimeError('原文件校验失败；已中断后端连接')
            # File bytes end at the declared length. Connection stays bidirectional
            # so a subsequent JSONL stop can be acknowledged by the backend.
            with self.lock:
                self.state.update(stage='waiting', detail='原文件已交付，等待后端设备回执')
            pending = b''
            event_bytes = 0
            stop_sent_at = None
            with (directory / 'events.jsonl').open('w') as events:
                while True:
                    if self.cancel.is_set() and stop_sent_at is None:
                        sock.sendall(b'{"operation":"stop"}\n')
                        stop_sent_at = time.monotonic()
                    if stop_sent_at is not None and time.monotonic() - stop_sent_at > 10:
                        raise TimeoutError('停止回执超时；无法确认设备发射关闭，请检查后端')
                    if time.monotonic() - started > 300:
                        raise TimeoutError('USRP 后端未在 300 秒内完成')
                    try:
                        chunk = sock.recv(4096)
                    except socket.timeout:
                        continue
                    if not chunk:
                        raise RuntimeError('后端未提供完成回执；不视为空口成功')
                    event_bytes += len(chunk)
                    if event_bytes > 1024 * 1024:
                        raise RuntimeError('后端回执累计超过 1 MiB')
                    pending += chunk
                    if len(pending) > 65536:
                        raise RuntimeError('后端回执超长')
                    while b'\n' in pending:
                        raw, pending = pending.split(b'\n', 1)
                        event = json.loads(raw)
                        if not isinstance(event, dict):
                            raise RuntimeError('后端回执必须是 JSON 对象')
                        events.write(json.dumps(event, ensure_ascii=False) + '\n')
                        events.flush()
                        if event.get('event') == 'error':
                            stopped_confirmed = event.get('tx_stopped') is True
                            raise RuntimeError(str(event.get('message', 'USRP 后端报告失败'))[:500])
                        if event.get('event') == 'stopped' and self.cancel.is_set() and event.get('tx_stopped') is True:
                            stopped_confirmed = True
                            with self.lock:
                                self.state.update(stage='stopped', detail='后端已确认停止发射', adapter_result=event)
                            return
                        if event.get('event') == 'complete':
                            if event.get('input_sha256') != request['media']['sha256'] or event.get('input_bytes') != count or event.get('tx_stopped') is not True:
                                raise RuntimeError('后端回执缺少原文件一致性或 TX 停止确认')
                            stopped_confirmed = True
                            with self.lock:
                                self.state.update(stage='complete', detail='后端任务完成；接收与 HDMI 效果须独立验收',
                                                  adapter_result=event, device_applied=event.get('applied_settings') == request['settings'])
                            return
        except Exception as error:
            with self.lock:
                self.state.update(stage='stopped' if self.cancel.is_set() else 'failed', detail=str(error), error=str(error))
        finally:
            sock.close()
            lock.close()
            with self.lock:
                self.state['active'] = False
                self.state['cleanup_unconfirmed'] = connected and not stopped_confirmed
                (directory / 'result.json').write_text(json.dumps(self.state, ensure_ascii=False, indent=2))

    def stop(self):
        with self.lock:
            if self.state['active']:
                self.cancel.set()
                self.state.update(stage='stopping', detail='正在停止本轮流程并清理发射、采集和显示' if self.builtin() else '正在中断后端连接；未取得设备关闭回执前不宣称安全关闭')
        return self.snapshot()
