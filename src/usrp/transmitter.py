#!/usr/bin/env python3
"""Pinned MRF1-v1 video link; dry-run is the default, RF always explicit."""
import argparse
from collections import Counter
from fractions import Fraction
import json
import math
import os
from pathlib import Path
import queue
import resource
import select
import signal
import subprocess
import sys
import threading
import time

import numpy as np
import air_phy as phy
from annexb import access_units, validate_au

HERE = Path(__file__).resolve().parent
HEADER = phy.struct.Struct('<4sQI')


def save(path, value):
    with Path(path).open('x', encoding='utf-8') as f:
        json.dump(value, f, ensure_ascii=False, indent=2, allow_nan=False)
        f.write('\n')


def command(cmd, timeout=30):
    p = subprocess.run([str(x) for x in cmd], capture_output=True, text=True, timeout=timeout,
                       stdin=subprocess.DEVNULL)
    if p.returncode:
        raise ValueError(p.stderr[-2000:] or 'command failed: '+str(cmd[0]))
    return p.stdout


def config(args):
    defaults = json.loads((HERE/'config.json').read_text())
    cfg = dict(defaults)
    if args.config:
        given = json.loads(args.config.read_text())
        if set(given)-set(defaults):
            raise ValueError('unknown configuration keys: '+str(set(given)-set(defaults)))
        cfg.update(given)
    for arg, field in [('freq', 'center_frequency_hz'), ('bandwidth', 'tx_analog_bandwidth_hz'),
                       ('fps', 'video_fps'), ('bitrate', 'video_bitrate_bps')]:
        if getattr(args, arg) is not None:
            cfg[field] = getattr(args, arg)
    frozen = {k: defaults[k] for k in ('schema', 'protocol_commit', 'profile_id', 'sample_rate_hz',
              'symbol_rate_hz', 'samples_per_symbol', 'rrc_rolloff', 'rrc_taps',
              'payload_bytes_per_stream', 'frame_symbols', 'frame_samples', 'iq_peak_component',
              'tx_channels', 'tx_antenna')}
    if any(cfg[k] != v for k, v in frozen.items()):
        raise ValueError('frozen PHY mismatch; a new version/profile is required')
    for key, low, high in [('queue_au', 1, 32), ('queue_payload', 1, 32), ('queue_iq', 1, 64),
                            ('transport_queue_blocks', 1, 64), ('prefill_blocks', 1, 64),
                            ('iq_block_samples', 1, 65536), ('rf_batch_frames', 1, 16),
                            ('max_au_bytes', 128, 1638364), ('max_capture_iq_bytes', 0, 1<<30),
                            ('max_wall_seconds', 1, 3600), ('video_fps', 1, 60),
                            ('video_bitrate_bps', 10000, 2000000), ('video_gop_frames', 1, 600),
                            ('video_vbv_bits', 10000, 4000000), ('session_id', 0, 0xffffffff),
                            ('starting_frame_id', 0, 0xffffffff), ('max_rx_seconds', 1, 120)]:
        if type(cfg[key]) is not int or not low <= cfg[key] <= high:
            raise ValueError('invalid bounded integer config: '+key)
    if cfg['prefill_blocks'] > cfg['transport_queue_blocks']:
        raise ValueError('prefill exceeds transport queue capacity')
    for key, low, high in [('center_frequency_hz', 1.2e9, 6e9), ('tx_analog_bandwidth_hz', 1e6, 40e6),
                            ('rx_analog_bandwidth_hz', 1e6, 40e6), ('rx_gain_db', 0, 30),
                            ('tx_gain_db', 0, 12), ('digital_scale', 1e-6, 1)]:
        if type(cfg[key]) not in (int, float) or not math.isfinite(cfg[key]) or not low <= cfg[key] <= high:
            raise ValueError('invalid RF config: '+key)
    if cfg['hardware_profile_id'] != 'CBX40-5500-v1' or cfg['tx_analog_bandwidth_hz'] != 40000000:
        raise ValueError('CBX hardware profile requires actual TX bandwidth 40000000 Hz')
    receivers = {'same_usrp_cbx40': 40000000, 'fmcomms3_ad9361': 3000000}
    if receivers.get(cfg['peer_receiver_profile']) != cfg['rx_analog_bandwidth_hz']:
        raise ValueError('receiver hardware profile/bandwidth mismatch')
    if args.action == 'loopback' and cfg['peer_receiver_profile'] != 'same_usrp_cbx40':
        raise ValueError('loopback requires the same-USRP receiver profile')
    if type(cfg['source_realtime']) is not bool or cfg['clock_source'] not in ('internal', 'external', 'gpsdo') or cfg['time_source'] not in ('internal', 'external', 'gpsdo'):
        raise ValueError('invalid source pacing or clock/time source')
    if len(cfg['antenna_band_hz']) != 2 or not cfg['antenna_band_hz'][0] <= cfg['center_frequency_hz'] <= cfg['antenna_band_hz'][1]:
        raise ValueError('frequency outside declared antenna band')
    if not .05 <= args.seconds <= 3600 or not 1 <= args.frames <= 1000000:
        raise ValueError('finite duration/frame bound required')
    return cfg


def admit(args, cfg):
    """Operator/peer declarations are evidence inputs, never hardware readbacks."""
    if not args.enable_tx or not args.admission:
        raise ValueError('send requires --enable-tx and --admission FILE; dry-run needs neither')
    a = json.loads(args.admission.read_text())
    for field in ('operator_confirmed_connections', 'power_budget_reviewed',
                  'peer_configuration_confirmed', 'phase_relationship_reviewed'):
        if a.get(field) is not True:
            raise ValueError('missing explicit admission: '+field)
    for field in ('connection_description', 'phase_relationship_evidence', 'peer_identity'):
        if not isinstance(a.get(field), str) or not a[field].strip() or a[field].startswith('FILL'):
            raise ValueError('missing admission evidence: '+field)
    for field in ('protocol_commit', 'profile_id', 'hardware_profile_id', 'sample_rate_hz',
                  'center_frequency_hz', 'tx_analog_bandwidth_hz', 'rx_analog_bandwidth_hz', 'peer_receiver_profile'):
        if a.get(field) != cfg[field]:
            raise ValueError('peer agreement mismatch: '+field)
    for field in ('symbol_rate_hz', 'samples_per_symbol', 'rrc_rolloff', 'rrc_taps',
                  'payload_bytes_per_stream', 'frame_symbols', 'frame_samples', 'iq_peak_component'):
        if a.get('peer_phy', {}).get(field) != cfg[field]:
            raise ValueError('peer PHY agreement mismatch: '+field)
    route = json.loads(command(['ip', '-json', 'route', 'get', cfg['usrp_addr']]))
    carrier = Path('/sys/class/net')/cfg['dedicated_interface']/'carrier'
    if not route or route[0].get('dev') != cfg['dedicated_interface'] or carrier.read_text().strip() != '1':
        raise ValueError('dedicated USRP Ethernet link/route is not ready')
    command(['ping', '-c', '1', '-W', '1', cfg['usrp_addr']], timeout=3)
    return a


class Cancelled(Exception):
    pass


class Pipeline:
    def __init__(self, args, cfg, out):
        self.args, self.cfg, self.out = args, cfg, out
        self.stop = threading.Event()
        self.queues = {n: queue.Queue(cfg['queue_'+n]) for n in ('au', 'payload', 'iq')}
        self.stats = dict(au_emitted=0, rf_frames=0, mrf_packets=0, iq_samples_per_channel=0,
                          iq_blocks=0, final_padding_samples=0, captured_iq_bytes=0,
                          encoded_bytes=0, source_input_passes=1, full_frame_drops=0,
                          queue_policy='bounded_backpressure_or_fail_stop_no_hidden_drop',
                          high_water={}, queue_block_seconds={}, stage_timing={}, source={}, errors=[])
        self.stats_lock = threading.Lock()
        self.encoder = None
        self.transport = None
        self.start = time.monotonic()

    def put(self, name, value):
        start = time.monotonic()
        while not self.stop.is_set():
            try:
                self.queues[name].put(value, timeout=.1)
                with self.stats_lock:
                    self.stats['high_water'][name] = max(self.stats['high_water'].get(name, 0), self.queues[name].qsize())
                    self.stats['queue_block_seconds'][name] = self.stats['queue_block_seconds'].get(name, 0)+time.monotonic()-start
                return
            except queue.Full:
                pass
        raise Cancelled()

    def get(self, name):
        while not self.stop.is_set():
            try:
                return self.queues[name].get(timeout=.1)
            except queue.Empty:
                pass
        raise Cancelled()

    def guarded(self, method):
        start, cpu = time.monotonic(), time.thread_time()
        try:
            method()
        except Cancelled:
            pass
        except Exception as e:
            with self.stats_lock:
                self.stats['errors'].append({'stage': method.__name__, 'error': str(e)})
            self.stop.set()
        finally:
            with self.stats_lock:
                self.stats['stage_timing'][method.__name__] = {
                    'wall_seconds_including_backpressure': time.monotonic()-start,
                    'thread_cpu_seconds': time.thread_time()-cpu}

    def encode(self):
        cfg = self.cfg
        if self.args.mode != 'video':
            self.put('au', None)
            return
        cmd = ['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-filter_threads', '1']
        if cfg['source_realtime']:
            cmd += ['-re']
        if self.args.input:
            source = self.args.input.resolve()
            if not source.is_file():
                raise ValueError('input video is missing')
            info = json.loads(command(['ffprobe', '-v', 'error', '-select_streams', 'v:0',
                '-show_entries', 'stream=width,height,avg_frame_rate,r_frame_rate:format=duration', '-of', 'json', source]))
            video = info['streams'][0]
            rate = Fraction(video.get('avg_frame_rate', '0/1'))
            if rate <= 0:
                rate = Fraction(video['r_frame_rate'])
            if rate < cfg['video_fps']:
                raise ValueError(f'input {float(rate):g} fps < requested {cfg["video_fps"]}; refusing duplicate-frame upsampling; use --fps {math.floor(rate)}')
            self.stats['source'] = {'path': str(source), 'probe': info,
                'original_geometry_matches_goal': (video['width'], video['height']) == (1024, 600),
                'timestamps': 'normalized CFR source timeline, seq*1000/fps; not host arrival time'}
            cmd += ['-i', str(source)]
        else:
            self.stats['source'] = {'kind': 'generated_dynamic_testsrc2', 'original_geometry_matches_goal': True,
                                    'timestamps': 'generated source CFR timeline'}
            cmd += ['-f', 'lavfi', '-i', f'testsrc2=size=1024x600:rate={cfg["video_fps"]}']
        cmd += ['-t', str(self.args.seconds), '-map', '0:v:0', '-an', '-vf',
                f'fps={cfg["video_fps"]},scale=1024:600:force_original_aspect_ratio=decrease,pad=1024:600:(ow-iw)/2:(oh-ih)/2,setsar=1',
                '-c:v', 'libx264', '-threads', '2', '-preset', 'veryfast', '-tune', 'zerolatency',
                '-profile:v', 'baseline', '-pix_fmt', 'yuv420p', '-bf', '0', '-g', str(cfg['video_gop_frames']),
                '-keyint_min', str(cfg['video_gop_frames']), '-sc_threshold', '0',
                '-b:v', str(cfg['video_bitrate_bps']), '-maxrate', str(cfg['video_bitrate_bps']),
                '-bufsize', str(cfg['video_vbv_bits']), '-x264-params',
                'aud=1:repeat-headers=1:slices=1:sliced-threads=0:interlaced=0:open-gop=0',
                '-flush_packets', '1', '-f', 'h264', 'pipe:1']
        save(self.out/'encoder-command.json', cmd)
        with (self.out/'encoder.stderr.log').open('xb') as log, (self.out/'encoded.h264').open('xb') as encoded, (self.out/'source-ledger.jsonl').open('x') as ledger:
            self.encoder = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=log,
                                            stdin=subprocess.DEVNULL, start_new_session=True)
            for seq, au in enumerate(access_units(self.encoder.stdout, self.stop, cfg['max_au_bytes'])):
                flags = validate_au(au)
                if seq == 0 and flags != 7:
                    raise ValueError('first AU must be an independent SPS/PPS IDR')
                timestamp = seq*1000//cfg['video_fps']
                event = {'sequence': seq, 'source_timestamp_ms': timestamp, 'annexb_bytes': len(au),
                         'flags': flags, 'encoded_ready_seconds': time.monotonic()-self.start,
                         'annexb_sha256': phy.hashlib.sha256(au).hexdigest()}
                encoded.write(au);ledger.write(json.dumps(event)+'\n')
                self.stats['au_emitted'] += 1;self.stats['encoded_bytes'] += len(au)
                self.put('au', (event, au))
            code = self.encoder.wait(timeout=10)
            if code:
                raise ValueError('FFmpeg encoder failed; see encoder.stderr.log')
        if not self.stats['au_emitted']:
            raise ValueError('source produced no complete AU')
        self.put('au', None)

    def packetize(self):
        frame_id = self.cfg['starting_frame_id']
        with (self.out/'rf-ledger.jsonl').open('x') as ledger:
            if self.args.mode == 'video':
                while True:
                    item = self.get('au')
                    if item is None:
                        break
                    event, au = item
                    envelope, fragments = phy.rvf2.encode(au, session_id=self.cfg['session_id'],
                        sequence=event['sequence'], timestamp_ms=event['source_timestamp_ms'], flags=event['flags'])
                    first = frame_id
                    for start in range(0, len(fragments), 2*self.cfg['rf_batch_frames']):
                        batch = fragments[start:start+2*self.cfg['rf_batch_frames']]
                        if len(batch) & 1:
                            batch += (batch[-1],)
                        rows = []
                        for i in range(0, len(batch), 2):
                            if frame_id > 0xffffffff:
                                raise ValueError('RF frame_id wrap requires explicit new session')
                            rows.append((frame_id, (batch[i], batch[i+1])));frame_id += 1
                        self.put('payload', rows)
                    ledger.write(json.dumps(dict(event, first_rf_frame=first, last_rf_frame=frame_id-1,
                        fragments=len(fragments), odd_fragment_repeated=bool(len(fragments)&1),
                        envelope_bytes=len(envelope), packetized_seconds=time.monotonic()-self.start))+'\n')
            else:
                self.get('au')
                source = self.args.input.open('rb') if self.args.input else None
                try:
                    rows = []
                    if self.args.mode == 'vectors':
                        manifest = json.loads((HERE/'protocol/vectors-manifest.json').read_text())
                        iterable = [(x['frame_id'], tuple(bytes.fromhex(p) for p in x['payload_hex'])) for x in manifest['vectors']]
                    else:
                        iterable = range(self.args.frames)
                    for item in iterable:
                        if self.args.mode == 'vectors':
                            fid, payloads = item
                        else:
                            fid = frame_id;frame_id += 1
                            if fid > 0xffffffff:
                                raise ValueError('RF frame_id wrap requires new session')
                            if source:
                                data = source.read(256)
                                if len(data) != 256:
                                    raise ValueError('payload file must supply exactly 256 bytes per selected RF frame')
                                payloads = data[:128], data[128:]
                            else:
                                payloads = phy.deterministic_payloads(fid, self.args.pattern)
                        rows.append((fid, payloads))
                        ledger.write(json.dumps({'frame_id': fid, 'kind': 'functional_payload_test'})+'\n')
                        if len(rows) == self.cfg['rf_batch_frames']:
                            self.put('payload', rows);rows = []
                    if rows:
                        self.put('payload', rows)
                    if source and source.read(1):
                        raise ValueError('payload file has unused bytes; specify an exact frame count')
                finally:
                    if source:
                        source.close()
        self.put('payload', None)

    def modulate(self):
        pending = np.empty((0, 2), dtype='<c8')
        size = self.cfg['iq_block_samples']
        capture = (self.out/'tx-reference.fc32le').open('xb') if self.args.save_iq else None
        with (self.out/'tx-packets.jsonl').open('x') as packets_file:
            try:
                while True:
                    rows = self.get('payload')
                    if rows is None:
                        break
                    samples, packets = phy.build_frames(rows)
                    self.stats['rf_frames'] += len(rows);self.stats['mrf_packets'] += 2*len(rows)
                    for row, pair in zip(rows, packets):
                        for stream, packet in enumerate(pair):
                            packets_file.write(json.dumps({'frame_id': row[0], 'stream': stream,
                                'packet_hex': packet.hex(), 'scope': 'TX_expected_not_RX_evidence'})+'\n')
                    pending = np.concatenate((pending, samples))
                    while len(pending) >= size:
                        self.emit(pending[:size].copy(), capture)
                        pending = pending[size:]
                # No artificial RF-frame/API alignment gaps. Only the finite
                # session tail is padded; the exact count is recorded.
                if len(pending):
                    padding = size-len(pending)
                    self.stats['final_padding_samples'] = padding
                    self.emit(np.pad(pending, ((0, padding), (0, 0))), capture)
            finally:
                if capture:
                    capture.close()
        self.put('iq', None)

    def emit(self, samples, capture):
        if capture:
            remaining = self.cfg['max_capture_iq_bytes']-self.stats['captured_iq_bytes']
            data = samples[:max(0, remaining//16)].tobytes()
            capture.write(data);self.stats['captured_iq_bytes'] += len(data)
        self.stats['iq_blocks'] += 1;self.stats['iq_samples_per_channel'] += len(samples)
        self.put('iq', samples)

    def write_pipe(self, data):
        view = memoryview(data);at = 0;deadline = time.monotonic()+10
        while at < len(view):
            if self.stop.is_set():
                raise Cancelled()
            if self.transport.poll() is not None:
                raise RuntimeError('transport exited; see transport.stderr.log/transport.json')
            fd = self.transport.stdin.fileno()
            if not select.select([], [fd], [], .1)[1]:
                if time.monotonic() > deadline:
                    raise RuntimeError('bounded transport pipe write timeout')
                continue
            try:
                wrote = os.write(fd, view[at:at+65536])
            except BlockingIOError:
                continue
            if wrote <= 0:
                raise RuntimeError('transport pipe zero write')
            at += wrote;deadline = time.monotonic()+10

    def transmit(self):
        sequence = 0
        try:
            while True:
                samples = self.get('iq')
                if samples is None:
                    break
                data = samples.astype('<c8', copy=False).tobytes()
                self.write_pipe(HEADER.pack(b'IQB1', sequence, len(samples)))
                self.write_pipe(data);sequence += 1
            self.write_pipe(HEADER.pack(b'IQB1', sequence, 0))
        finally:
            self.transport.stdin.close()
        if self.transport.wait(timeout=15):
            raise RuntimeError('continuous TX transport failed; inspect transport.json')

    def run(self):
        cfg = self.cfg
        cmd = [str(HERE/'continuous_tx'), '--stats', str(self.out/'transport.json'),
               '--actual', str(self.out/'actual-readback.json'), '--freq', str(cfg['center_frequency_hz']),
               '--bandwidth', str(cfg['tx_analog_bandwidth_hz']), '--gain', str(cfg['tx_gain_db']),
               '--scale', str(cfg['digital_scale']), '--queue', str(cfg['transport_queue_blocks']),
               '--prefill', str(cfg['prefill_blocks']), '--clock', cfg['clock_source'],
               '--time', cfg['time_source'], '--addr', cfg['usrp_addr'], '--serial', cfg['expected_serial']]
        if self.args.action in ('send', 'loopback'):
            cmd += ['--enable-tx', 'YES']
            if self.args.action == 'send':
                cmd += ['--tx-zero-preroll-samples', '400000']
            if self.args.action == 'loopback':
                cmd += ['--rx-out', str(self.out/'capture'), '--rx-bandwidth', str(cfg['rx_analog_bandwidth_hz']),
                        '--rx-gain', str(cfg['rx_gain_db']), '--max-rx-seconds', str(cfg['max_rx_seconds'])]
        else:
            cmd += ['--simulate', '--max-send-samples', str(self.args.simulate_max_send)]
            if self.args.simulate_realtime:
                cmd += ['--simulate-realtime']
        save(self.out/'transport-command.json', cmd)
        with (self.out/'transport.stdout.log').open('xb') as stdout, (self.out/'transport.stderr.log').open('xb') as stderr:
            self.transport = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=stdout, stderr=stderr,
                                              start_new_session=True, bufsize=0)
            os.set_blocking(self.transport.stdin.fileno(), False)
            methods = (self.encode, self.packetize, self.modulate, self.transmit)
            workers = [threading.Thread(target=self.guarded, args=(m,), name=m.__name__) for m in methods]
            for worker in workers:
                worker.start()
            last = 0
            while any(w.is_alive() for w in workers):
                elapsed = time.monotonic()-self.start
                if elapsed > cfg['max_wall_seconds'] and not self.stop.is_set():
                    self.stats['errors'].append({'stage': 'watchdog', 'error': 'wall-time bound exceeded'})
                    self.stop.set()
                if self.stop.is_set():
                    self.stop_children()
                for worker in workers:
                    worker.join(.05)
                if elapsed-last >= 2:
                    print(f'AU={self.stats["au_emitted"]} RF={self.stats["rf_frames"]} blocks={self.stats["iq_blocks"]}', flush=True)
                    last = elapsed
            self.stop_children()
        return self.stats

    def stop_children(self):
        for process in (self.encoder, self.transport):
            if process is not None and process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL);process.wait(timeout=3)
                except ProcessLookupError:
                    pass


def codec_check(out, cfg, expected):
    command(['ffmpeg', '-nostdin', '-v', 'error', '-xerror', '-i', out/'encoded.h264',
             '-map', '0:v:0', '-f', 'null', '-'], timeout=120)
    info = json.loads(command(['ffprobe', '-v', 'error', '-count_frames', '-select_streams', 'v:0',
        '-show_entries', 'stream=codec_name,profile,width,height,pix_fmt,has_b_frames,field_order,nb_read_frames',
        '-of', 'json', out/'encoded.h264'], timeout=120))
    v = info['streams'][0]
    if (v['codec_name'], v['profile'], v['width'], v['height'], v['pix_fmt'], v['has_b_frames'], v['field_order'], int(v['nb_read_frames'])) != ('h264', 'Constrained Baseline', 1024, 600, 'yuv420p', 0, 'progressive', expected):
        raise ValueError('actual full codec decode differs from admitted video profile')
    return info


def main(argv=None, *, tx_only=False):
    parser = argparse.ArgumentParser(description='TX-only MRF1 video sender; local RX is never started.' if tx_only else __doc__)
    actions = ('dry-run', 'send', 'preflight', 'discover', 'selftest') if tx_only else ('dry-run', 'send', 'loopback', 'preflight', 'discover', 'selftest')
    parser.add_argument('action', nargs='?', default='dry-run', choices=actions)
    parser.add_argument('--mode', choices=('payload', 'vectors', 'video'), default='video' if tx_only else 'payload')
    parser.add_argument('--config', type=Path, default=HERE/'tx-only/config.json' if tx_only else None)
    parser.add_argument('--out', type=Path)
    parser.add_argument('--input', type=Path)
    parser.add_argument('--frames', type=int, default=128)
    parser.add_argument('--seconds', type=float, default=24 if tx_only else 2)
    parser.add_argument('--pattern', choices=('deterministic', 'ascending', 'boundary'), default='deterministic')
    parser.add_argument('--freq', type=float)
    parser.add_argument('--bandwidth', type=float)
    parser.add_argument('--fps', type=int, default=25 if tx_only else None)
    parser.add_argument('--bitrate', type=int)
    parser.add_argument('--enable-tx', action='store_true')
    parser.add_argument('--admission', type=Path)
    parser.add_argument('--save-iq', action='store_true')
    parser.add_argument('--simulate-realtime', action='store_true')
    parser.add_argument('--simulate-max-send', type=int, default=65536)
    args = parser.parse_args(argv)
    if tx_only and args.mode == 'video' and args.input is None:
        args.input = Path('/home/leeeo/Downloads/1.mp4')
    phy.verify_contract()
    if args.action == 'selftest':
        import unittest
        suite = unittest.defaultTestLoader.discover(str(HERE/'tests'), pattern='test_*.py')
        return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1
    cfg = config(args)
    if not 1 <= args.simulate_max_send <= 65536:
        raise ValueError('invalid simulated send bound')
    if args.action not in ('send', 'loopback') and args.enable_tx:
        raise ValueError('--enable-tx only belongs to explicit send/loopback actions')
    admission = admit(args, cfg) if args.action in ('send', 'loopback') else None
    if args.out is None:
        raise ValueError('--out NEW_DIRECTORY is required')
    out = args.out.resolve();out.mkdir(parents=True, exist_ok=False)
    save(out/'effective-config.json', cfg)
    save(out/'provenance.json', {'protocol_commit': phy.COMMIT, 'mode': args.mode,
        'tx_only_entrypoint': tx_only,
        'selected_config_sha256': phy.sha_file(args.config) if args.config else None,
        'scope': 'single_USRP_functional_diagnostic' if args.action == 'loopback' else 'TX_only_no_receiver_acceptance', 'command': sys.argv,
        'source_files_sha256': {p.name: phy.sha_file(p) for p in sorted(HERE.iterdir()) if p.suffix in ('.py', '.cpp', '.json') or p.name in ('continuous_tx', 'run', 'build.sh')},
        'admission_operator_declaration': admission, 'operator_declaration_is_not_actual_readback': True})
    if args.action == 'discover':
        text = command([HERE/'continuous_tx', '--discover', '--addr', cfg['usrp_addr']], timeout=20)
        (out/'device.txt').write_text(text)
        save(out/'RESULT.json', {'device_discovery_completed': True, 'RF_transmission_started': False})
        print(text)
        return 0
    if args.action == 'preflight':
        cmd = [HERE/'continuous_tx', '--preflight', '--addr', cfg['usrp_addr'], '--serial', cfg['expected_serial'],
               '--freq', str(cfg['center_frequency_hz']), '--bandwidth', str(cfg['tx_analog_bandwidth_hz']),
               '--gain', str(cfg['tx_gain_db']), '--clock', cfg['clock_source'], '--time', cfg['time_source'],
               '--actual', out/'actual-readback.json', '--stats', out/'transport.json']
        if not tx_only and cfg['peer_receiver_profile'] == 'same_usrp_cbx40':
            cmd += ['--rx-bandwidth', str(cfg['rx_analog_bandwidth_hz']), '--rx-gain', str(cfg['rx_gain_db'])]
        try:
            text = command(cmd, timeout=30)
            (out/'device.txt').write_text(text)
            actual = json.loads((out/'actual-readback.json').read_text())
            save(out/'RESULT.json', {'hardware_configuration_pass': actual['configuration_matches_requested'],
                'RF_transmission_started': False, 'RF_receiver_tested': False,
                'peer_receiver_actual_checked': bool(actual.get('rx_channels')),
                'hardware_profile_id': cfg['hardware_profile_id']})
            print(text)
            print((out/'RESULT.json').read_text())
            return 0
        except Exception as e:
            save(out/'RESULT.json', {'hardware_configuration_pass': False, 'RF_transmission_started': False, 'error': str(e)})
            raise
    if not (HERE/'continuous_tx').is_file():
        raise ValueError('build the transport first: sh build.sh')
    pipeline = Pipeline(args, cfg, out)
    child_cpu_before = resource.getrusage(resource.RUSAGE_CHILDREN)
    old = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        old[sig] = signal.signal(sig, lambda *_: pipeline.stop.set())
    try:
        stats = pipeline.run()
    finally:
        pipeline.stop_children()
        for sig, handler in old.items():
            signal.signal(sig, handler)
    transport = json.loads((out/'transport.json').read_text()) if (out/'transport.json').exists() else {}
    passed = bool(not stats['errors'] and not pipeline.stop.is_set() and transport.get('transport_pass'))
    passed = passed and transport.get('accepted_samples_per_channel') == stats['iq_samples_per_channel']
    passed = passed and stats['iq_samples_per_channel'] == stats['rf_frames']*3840+stats['final_padding_samples']
    decoded = None
    if args.mode == 'video' and passed:
        try:
            decoded = codec_check(out, cfg, stats['au_emitted'])
        except Exception as e:
            stats['errors'].append({'stage': 'codec_validation', 'error': str(e)});passed = False
    elapsed = time.monotonic()-pipeline.start
    child_cpu_after = resource.getrusage(resource.RUSAGE_CHILDREN)
    duration = stats['au_emitted']/cfg['video_fps'] if args.mode == 'video' else None
    receiver_report = None
    if args.action == 'loopback' and passed:
        try:
            import receiver
            receiver_report = receiver.decode_capture(out/'capture', out/'receiver', video=args.mode == 'video')
            receiver_report['all_expected_packets_received'] = receiver_report['unique_packets'] == stats['mrf_packets']
            receiver_report['stream_frame_ids_match_expected'] = receiver.verify_expected_frames(out/'receiver/rx-packets.jsonl',
                cfg['starting_frame_id'], stats['rf_frames'])
            receiver_report['received_packets_match_tx'] = receiver.compare_packets(out/'tx-packets.jsonl', out/'receiver/rx-packets.jsonl')
            if args.mode == 'video':
                receiver_report['received_AnnexB_matches_encoded'] = phy.sha_file(out/'receiver/received.h264') == phy.sha_file(out/'encoded.h264')
                receiver_report['all_expected_AUs_decoded'] = receiver_report['decoded_video_frames'] == stats['au_emitted']
            passed = passed and receiver_report['all_expected_packets_received'] and receiver_report['stream_frame_ids_match_expected']
            passed = passed and receiver_report['received_packets_match_tx'] and receiver_report['duplicate_packets'] == 0
            if args.mode == 'video':
                passed = passed and receiver_report['received_AnnexB_matches_encoded'] and receiver_report['all_expected_AUs_decoded']
            save(out/'receiver/ACCEPTANCE.json', receiver_report)
        except Exception as e:
            stats['errors'].append({'stage': 'receiver_validation', 'error': str(e)});passed = False
    pipeline_elapsed = elapsed
    elapsed = time.monotonic()-pipeline.start
    report = {'schema': 'mrf1-usrp-link-result-v2', 'protocol_commit': phy.COMMIT,
        'tx_only_entrypoint': tx_only, 'local_RX_stream_enabled': args.action == 'loopback',
        'hardware_profile_id': cfg['hardware_profile_id'],
        'offline_pipeline_pass': bool(passed and args.action == 'dry-run'),
        'hardware_tx_transport_pass': bool(transport.get('transport_pass') and args.action in ('send', 'loopback')),
        'hardware_attempted': args.action in ('send', 'loopback'),
        'hardware_tested': bool(passed and args.action in ('send', 'loopback')), 'RF_transmission_finished': True,
        'RF_receiver_tested': bool(args.action == 'loopback' and transport.get('rx_samples_per_channel', 0)),
        'single_USRP_video_loopback_pass': bool(passed and args.action == 'loopback' and args.mode == 'video'),
        'single_USRP_payload_loopback_pass': bool(passed and args.action == 'loopback' and args.mode == 'payload'),
        'RF_receiver_validation_pass': bool(passed and args.action == 'loopback'),
        'receiver': receiver_report, 'ZedBoard_MHGD_tested': False, 'video_30fps_end_to_end_verified': False,
        'mode': args.mode, 'elapsed_seconds': elapsed, 'pipeline_before_RX_decode_seconds': pipeline_elapsed, 'statistics': stats,
        'transport': transport, 'full_encoded_video_decode': decoded,
        'source_timeline_seconds': duration,
        'measured_encoded_bitrate_bps': stats['encoded_bytes']*8/duration if duration else None,
        'planned_RF_occupancy_per_source_second': stats['rf_frames']*.00096/duration if duration else None,
        'required_RF_frames_per_source_second': stats['rf_frames']/duration if duration else None,
        'estimated_average_80_percent_RF_budget_pass': stats['rf_frames']*.00096/duration <= .8 if duration else None,
        'local_child_cpu_seconds_encoder_transport_and_validation':
            child_cpu_after.ru_utime+child_cpu_after.ru_stime-child_cpu_before.ru_utime-child_cpu_before.ru_stime,
        'local_pipeline_AUs_per_wall_second': stats['au_emitted']/pipeline_elapsed,
        'RF_frame_delivery_count': None, 'RVF2_receiver_completed_AUs': None,
        'receiver_new_video_fps': None, 'receiver_latency_p50_p95_ms': None,
        'queue_capacity': {k: cfg['queue_'+k] for k in ('au', 'payload', 'iq')}}
    save(out/'RESULT.json', report)
    print(json.dumps({k: report[k] for k in ('offline_pipeline_pass', 'hardware_tx_transport_pass', 'RF_receiver_tested', 'elapsed_seconds', 'planned_RF_occupancy_per_source_second')}, indent=2))
    if not passed:
        print(json.dumps(stats['errors'], ensure_ascii=False), file=sys.stderr)
    return 0 if passed else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as e:
        print('ERROR: '+str(e), file=sys.stderr)
        raise SystemExit(2)
