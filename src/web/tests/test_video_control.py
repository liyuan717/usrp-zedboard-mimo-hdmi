"""No hardware: original byte stream, HTTP controls, metadata gates, and cleanup."""
from pathlib import Path
import copy
import fcntl
import hashlib
import http.client
import io
import json
import socket
import struct
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config
import server
import video_control as v

META = dict(width=1024, height=600, board_metadata_compatible=True, codec='h264', mime='video/mp4', duration='1')
RAW = bytes(range(256)) * 8192


def read_exact(sock, n):
    out = bytearray()
    while len(out) < n:
        raw = sock.recv(n - len(out))
        if not raw:
            raise EOFError()
        out.extend(raw)
    return bytes(out)


class VideoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='vtest-', dir='/tmp')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manager = v.VideoManager(self.root / 'data', self.root / 'adapter.sock')
        self.patch = patch.object(config, 'STATE', self.root / 'state')
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def upload(self):
        with patch.object(v, 'inspect_media', return_value=copy.deepcopy(META)):
            return self.manager.upload(io.BytesIO(RAW), len(RAW), '原视频.mp4')

    def adapter(self, mode='complete'):
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(str(self.manager.adapter))
        listener.listen(1)
        listener.settimeout(5)
        self.addCleanup(listener.close)
        result = {}
        ready = threading.Event()
        def worker():
            try:
                conn, _ = listener.accept()
                with conn:
                    conn.settimeout(5)
                    header = json.loads(read_exact(conn, struct.unpack('!I', read_exact(conn, 4))[0]))
                    body = read_exact(conn, header['media']['bytes'])
                    result.update(header=header, body=body)
                    ready.set()
                    if mode == 'close':
                        return
                    if mode == 'stop':
                        cmd = b''
                        while not cmd.endswith(b'\n'):
                            cmd += conn.recv(128)
                        result['command'] = json.loads(cmd)
                        event = dict(event='stopped', tx_stopped=True)
                    else:
                        event = dict(event='complete', input_sha256=hashlib.sha256(body).hexdigest(),
                                     input_bytes=len(body), tx_stopped=True, applied_settings=header['settings'])
                        if mode == 'wrong_hash':
                            event['input_sha256'] = '0' * 64
                    conn.sendall(json.dumps(event).encode() + b'\n')
            except Exception as error:
                result['error'] = repr(error)
                ready.set()
        thread = threading.Thread(target=worker)
        thread.start()
        self.addCleanup(lambda: thread.join(6))
        return result, ready, thread

    def test_builtin_rejects_large_file_before_upload_read(self):
        with patch.object(v,'ROOT',self.root/'builtin'):
            manager=v.VideoManager(self.root/'builtin',v.ADAPTER)
            stream=type('S',(),{'read':lambda *_:(_ for _ in ()).throw(AssertionError('must not read'))})()
            with self.assertRaisesRegex(ValueError,'180000'):
                manager.upload(stream,180001,'large.mp4')

    def test_original_bytes_hash_persistence_no_transform(self):
        state = self.upload()
        path, meta = self.manager.media_path(state['media']['id'])
        self.assertEqual(path.read_bytes(), RAW)
        self.assertEqual(meta['sha256'], hashlib.sha256(RAW).hexdigest())
        self.assertEqual(meta['transformations'], [])
        self.assertEqual(path.stat().st_mode & 0o777, 0o400)
        restarted = v.VideoManager(self.manager.root, self.manager.adapter)
        self.assertEqual(restarted.snapshot()['media'], meta)
        self.assertFalse(restarted.snapshot()['active'])

    def test_truncated_and_oversized_upload_do_not_replace_selected(self):
        selected = self.upload()['media']
        with self.assertRaises(ValueError):
            self.manager.upload(io.BytesIO(b'abc'), 5, 'broken.mp4')
        with self.assertRaises(ValueError):
            self.manager.upload(io.BytesIO(b''), v.MAX_FILE + 1, 'large.mp4')
        self.assertEqual(self.manager.snapshot()['media'], selected)
        self.assertEqual(len(list((self.manager.root / 'media').iterdir())), 1)

    def test_resolution_rejected_without_conversion(self):
        result = type('R', (), dict(returncode=0, stdout=json.dumps(dict(streams=[dict(width=1024, height=720)], format=dict(format_name='mp4'))).encode()))()
        with patch.object(v.subprocess, 'run', return_value=result) as run:
            with self.assertRaisesRegex(ValueError, '1024'):
                self.manager.upload(io.BytesIO(b'unchanged'), 9, 'test.mp4')
            self.assertEqual(run.call_args.args[0][0], '/usr/bin/ffprobe')
        self.assertIsNone(self.manager.snapshot()['media'])

    def test_rotated_rejected(self):
        result = type('R', (), dict(returncode=0, stdout=json.dumps(dict(streams=[dict(width=1280, height=720, side_data_list=[dict(rotation=90)])], format=dict(format_name='mp4'))).encode()))()
        with patch.object(v.subprocess, 'run', return_value=result), self.assertRaises(ValueError):
            v.inspect_media('/not-executed')

    def test_board_codec_metadata_positive_and_rejections(self):
        stream = dict(width=1024, height=600, codec_name='h264', profile='Constrained Baseline',
                      has_b_frames=0, pix_fmt='yuv420p', avg_frame_rate='5/1')
        def inspect(changes):
            data = dict(stream, **changes)
            result = type('R', (), dict(returncode=0, stdout=json.dumps(dict(streams=[data], format=dict(format_name='mov,mp4,m4a,3gp,3g2,mj2'))).encode()))()
            with patch.object(v.subprocess, 'run', return_value=result):
                return v.inspect_media('/not-executed')
        self.assertTrue(inspect({})['board_metadata_compatible'])
        self.assertFalse(inspect({})['full_board_bitstream_validation'])
        for changes in [dict(profile='High'), dict(profile='Baseline'), dict(has_b_frames=2),
                        dict(codec_name='hevc'), dict(pix_fmt='yuv420p10le'), dict(width=1280,height=720)]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                inspect(changes)

    def test_parameter_ranges_injection_and_persistence(self):
        data = copy.deepcopy(v.DEFAULTS)
        data.update(gain_db=12.5, frequency_hz=2400000000, bandwidth_hz=20000000)
        self.manager.settings(data)
        self.assertEqual(v.VideoManager(self.manager.root).snapshot()['settings'], data)
        for change in [dict(gain_db=21), dict(gain_db=float('nan')), dict(gain_db=True),
                       dict(frequency_hz=6000000001), dict(sample_rate_hz=8000000),
                       dict(device_address='; shell'), dict(command='anything')]:
            bad = dict(data, **change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.manager.settings(bad)

    def test_missing_adapter_does_not_start_or_claim_success(self):
        ident = self.upload()['media']['id']
        with self.assertRaisesRegex(RuntimeError, '尚未接入'):
            self.manager.start(ident)
        self.assertFalse(self.manager.snapshot()['active'])
        self.assertFalse(self.manager.snapshot()['rf_verified'])

    def test_adapter_exact_file_parameters_ack_and_repeat(self):
        ident = self.upload()['media']['id']
        data = dict(v.DEFAULTS, gain_db=10)
        self.manager.settings(data)
        result, ready, thread = self.adapter()
        first = self.manager.start(ident)['run_id']
        self.manager.thread.join(5)
        thread.join(5)
        self.assertNotIn('error', result)
        self.assertEqual(result['body'], RAW)
        self.assertEqual(result['header']['settings'], data)
        self.assertEqual(self.manager.snapshot()['stage'], 'complete')
        self.assertTrue(self.manager.snapshot()['device_applied'])
        self.assertFalse(self.manager.snapshot()['rf_verified'])
        self.manager.adapter.unlink()
        result2, _, thread2 = self.adapter()
        second = self.manager.start(ident)['run_id']
        self.manager.thread.join(5)
        thread2.join(5)
        self.assertNotEqual(first, second)
        self.assertEqual(result2['body'], RAW)
        self.assertEqual(len(list((self.manager.root / 'runs').iterdir())), 2)

    def test_bad_ack_blocks_repeat_until_external_cleanup(self):
        ident = self.upload()['media']['id']
        self.adapter('wrong_hash')
        self.manager.start(ident)
        self.manager.thread.join(5)
        self.assertEqual(self.manager.snapshot()['stage'], 'failed')
        self.assertTrue(self.manager.snapshot()['cleanup_unconfirmed'])
        with self.assertRaisesRegex(RuntimeError, '未确认停止'):
            self.manager.start(ident)

    def test_stop_ack_duplicate_settings_and_upload_exclusion(self):
        ident = self.upload()['media']['id']
        result, ready, thread = self.adapter('stop')
        self.manager.start(ident)
        self.assertTrue(ready.wait(3))
        with self.assertRaises(RuntimeError):
            self.manager.start(ident)
        with self.assertRaises(RuntimeError):
            self.manager.settings(v.DEFAULTS)
        with self.assertRaises(RuntimeError):
            self.upload()
        self.manager.stop()
        self.manager.thread.join(5)
        thread.join(5)
        self.assertEqual(result['command'], dict(operation='stop'))
        self.assertEqual(self.manager.snapshot()['stage'], 'stopped')
        self.assertFalse(self.manager.snapshot()['cleanup_unconfirmed'])

    def test_shared_hardware_lock(self):
        ident = self.upload()['media']['id']
        self.adapter()
        config.STATE.mkdir(parents=True)
        with (config.STATE / 'operation.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(RuntimeError, '占用'):
                self.manager.start(ident)
        # Close the listening socket with no accept: this test intentionally sends no bytes.
        self.manager.adapter.unlink()

    def test_media_id_path_traversal_rejected(self):
        self.upload()
        for ident in ['../../etc/passwd', 'old', '']:
            with self.assertRaises(ValueError):
                self.manager.media_path(ident)

    def test_HTTP_original_bytes_ranges_auth_and_settings(self):
        class FakePhoto:
            def snapshot(self):return dict(active=False)
        srv = server.Server(('127.0.0.1', 0), FakePhoto(), self.manager)
        thread = threading.Thread(target=srv.serve_forever)
        thread.start()
        self.addCleanup(srv.server_close)
        self.addCleanup(thread.join, 3)
        self.addCleanup(srv.shutdown)
        port = srv.server_port
        def request(method, path, body=None, extra=None):
            conn = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
            headers = {'Origin': f'http://127.0.0.1:{port}', 'X-Photo-CSRF': srv.token, 'Content-Type': 'application/json'}
            headers.update(extra or {})
            conn.request(method, path, body=body, headers=headers)
            response = conn.getresponse()
            result = response.status, dict(response.getheaders()), response.read()
            conn.close()
            return result
        with patch.object(v, 'inspect_media', return_value=copy.deepcopy(META)):
            status, _, raw = request('POST', '/api/video/media', RAW, {'Content-Type':'application/octet-stream'})
        self.assertEqual(status, 200)
        ident = json.loads(raw)['state']['media']['id']
        url = '/api/video/media/' + ident
        self.assertEqual(request('GET', url)[2], RAW)
        status, headers, part = request('GET', url, extra={'Range':'bytes=256-1023'})
        self.assertEqual(status, 206)
        self.assertEqual(part, RAW[256:1024])
        self.assertEqual(request('GET', url, extra={'Range':'bytes=-32'})[2], RAW[-32:])
        self.assertEqual(request('GET', url, extra={'Range':'bytes=999999999-'})[0], 416)
        self.assertEqual(request('GET', url, extra={'Range':'bytes=-0'})[0], 416)
        self.assertEqual(request('POST', '/api/video/settings', json.dumps(v.DEFAULTS), {'X-Photo-CSRF':'wrong'})[0], 403)
        self.assertEqual(request('POST', '/api/video/settings', json.dumps(v.DEFAULTS), {'Origin':'http://evil.test'})[0], 403)
        self.assertEqual(request('GET', '/api/video/state', extra={'Host':'evil.test'})[0], 403)
        self.assertEqual(request('POST', '/api/video/settings', json.dumps(dict(v.DEFAULTS, command='sh')))[0], 400)
        self.assertEqual(request('POST', '/api/video/settings', json.dumps(v.DEFAULTS))[0], 200)
        self.assertEqual(request('GET', '/photo')[0], 200)
        self.assertIn(b'media-src', request('GET', '/')[1]['Content-Security-Policy'].encode())


if __name__ == '__main__':
    unittest.main()
