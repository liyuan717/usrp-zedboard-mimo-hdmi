"""Offline tests only: fake processes, loopback HTTP/UDP and pseudo-terminal."""
from pathlib import Path
import copy
import http.client
import io
import json
import os
import pty
import socket
import struct
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
import bundle
import config
import server
import tftp_service
import transport
import workflow


class SecurityAndFlow(unittest.TestCase):
    def setUp(self):
        # A mock omission must never fall through to the real board.
        self.no_real_uart = patch.object(workflow, 'UART', side_effect=AssertionError('offline tests cannot open board UART'))
        self.no_real_uart.start()
        self.addCleanup(self.no_real_uart.stop)

    def test_frozen_assets_and_no_media_on_receiver(self):
        config.verify_assets()
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'app.tar.gz'
            rows = bundle.build_bundle(p, 'a' * 32, 'fresh-boot', 'secret')
            with tarfile.open(p) as archive:
                names = archive.getnames()
                self.assertFalse(any(n.endswith(('.rfd', '.iqb', '.png', '.h264', '.rxth')) for n in names))
                auth = json.load(archive.extractfile('AUTHORIZATION.json'))
                self.assertEqual(auth['expected_boot_id'], 'fresh-boot')
                self.assertTrue(auth['visual_only_no_scanout_sampler'])
                self.assertEqual(archive.extractfile('live-rx-arm').read(),
                    (config.D / 'candidates/finite-video-long-r2/live-rx-arm').read_bytes())
                self.assertEqual(archive.extractfile('hdmi-presenter').read(),
                    (config.D / 'candidates/hdmi-lazy-r6-idle-repeat/build-arm/hdmi-presenter').read_bytes())
                source = archive.extractfile('board_supervisor.py').read().decode()
                self.assertIn('"60000",', source)
                self.assertNotIn('93ddbf22', source)
                self.assertIn('HDMI_SINGLE_ACTIVATION_CLAIM', source)
                compile(source, 'adapted-worker', 'exec')

    def test_baseline_independent_grader(self):
        code = 'import sys,json;sys.path.insert(0,sys.argv[1]);from grade_evidence import grade;from pathlib import Path;print(json.dumps(grade(Path(sys.argv[2]))))'
        done = subprocess.run(['/usr/bin/python3', '-c', code, str(config.BASE),
            str(config.BASE / 'board-evidence')], capture_output=True, text=True, timeout=15)
        self.assertEqual(done.returncode, 0, done.stderr)
        result = json.loads(done.stdout)
        self.assertTrue(result['valid'])
        self.assertEqual(result['ADC']['blocks'], 2048)
        self.assertEqual(result['CRC_correct_byte_identical_packets'], 84)

    def test_actual_IPC_counter_namespace_photo_and_pixel_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'runs').mkdir();(root/'state').mkdir();run=root/'runs/grading';run.mkdir();e=run/'evidence';e.mkdir()
            for source in (config.BASE/'board-evidence').iterdir():
                if source.is_file() and source.name!='result.json':os.link(source,e/source.name)
            board=json.loads((config.BASE/'board-evidence/result.json').read_text());board.update(run_id='test-run',boot_id='test-boot')
            self.assertEqual(board['native_summary']['delivered_frames'],0)
            (e/'result.json').write_text(json.dumps(board))
            with patch.object(workflow,'RUNS',root/'runs'),patch.object(workflow,'STATE',root/'state'):
                manager=workflow.Manager();manager.run=run;manager.state=dict(run_id='test-run',boot_id='test-boot')
                result=manager.grade(e)
                self.assertEqual(result['RF_frames'],42)
                self.assertEqual(result['CRC_correct_packets'],84)
                self.assertTrue(result['reconstructed_photo_pixels_match_TX'])
                self.assertTrue((run/'received-photo.png').is_file())

    def test_same_manager_duplicate_start_and_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp);(root / 'runs').mkdir();(root / 'state').mkdir()
            entered = threading.Event();release = threading.Event()
            def preflight(manager):
                entered.set();release.wait(2);manager.check_stop()
                raise RuntimeError('offline preflight fixture')
            with patch.object(workflow, 'RUNS', root / 'runs'), patch.object(workflow, 'STATE', root / 'state'), patch.object(workflow.Manager, 'preflight', preflight):
                manager = workflow.Manager()
                first = manager.start();self.assertTrue(entered.wait(1))
                with self.assertRaises(RuntimeError):manager.start()
                manager.stop();release.set();manager.thread.join(3)
                self.assertEqual(manager.snapshot()['stage'], 'stopped')
                self.assertFalse(manager.snapshot()['active'])
                self.assertIsNone(manager.lock_fd)
                second = manager.start();manager.thread.join(3)
                self.assertNotEqual(first['run_id'], second['run_id'])
                self.assertEqual(len(list((root / 'runs').glob('*'))), 2)

    def test_visual_feedback_bound_to_completed_current_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'runs').mkdir();(root/'state').mkdir();run=root/'runs/feedback';run.mkdir()
            with patch.object(workflow,'RUNS',root/'runs'),patch.object(workflow,'STATE',root/'state'):
                manager=workflow.Manager();manager.run=run;manager.state=dict(run_id='current',active=False,result=dict(technical_pass=True))
                manager.visual('current','accepted_minor_flicker')
                self.assertTrue(json.loads((run/'result.json').read_text())['visual_pass_under_user_criteria'])
                for run_id,choice in [('old','stable'),('current','shell command')]:
                    with self.assertRaises(ValueError):manager.visual(run_id,choice)
                manager.state['active']=True
                with self.assertRaises(ValueError):manager.visual('current','stable')

    def test_stop_orders_TX_before_board_stop_and_keeps_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp);(root / 'runs').mkdir();(root / 'state').mkdir()
            events = []
            def boot(manager):return 'fresh-boot'
            def transfer(manager, *args):
                manager.agent_started = True
                raise transport.Cancelled('cancel fixture')
            def request(manager, path, **kwargs):
                events.append(path)
                return b'{}'
            def status(manager):return dict(done=True, result=dict(rx_lo_powerdown='1', tx_lo_powerdown='1', adc_buffer_enable='0'))
            with patch.object(workflow, 'RUNS', root / 'runs'), patch.object(workflow, 'STATE', root / 'state'), patch.object(workflow.Manager, 'preflight'), patch.object(workflow.Manager, 'boot', boot), patch.object(workflow.Manager, 'transfer', transfer), patch.object(workflow.Manager, 'terminate_tx', lambda manager: events.append('TX_STOP')), patch.object(workflow.Manager, 'request', request), patch.object(workflow.Manager, 'status', status), patch.object(workflow.Manager, 'fetch', lambda manager, status: events.append('EVIDENCE')):
                manager = workflow.Manager();manager.start();manager.thread.join(3)
                self.assertLess(events.index('TX_STOP'), events.index('/stop'))
                self.assertIn('EVIDENCE', events)
                result = json.loads((manager.run / 'result.json').read_text())
                self.assertTrue(result['cleanup']['board_cleanup_confirmed'])
                self.assertFalse(result['technical_pass'])

    def test_RAM_default_LO_enabled_is_closed_before_provider_preparation(self):
        levels = dict(RX='0', TX='0');network_calls=[];loaded=[];fail_forever=False
        class FakeUART:
            def __init__(self, *args):pass
            def reboot_to_uboot(self, notify):pass
            def boot_kernel(self):levels.update(RX='0', TX='0')
            def command(self, text, *args):
                if 'echo 1 > /sys/bus/iio/devices/iio:device0/out_altvoltage0_RX_LO_powerdown' in text:levels['RX']='1'
                if 'echo 1 > /sys/bus/iio/devices/iio:device0/out_altvoltage1_TX_LO_powerdown' in text:levels['TX']='1'
                if text.startswith('setenv ipaddr'):
                    network_calls.append(text)
                    return b'not reachable' if fail_forever or len(network_calls)==1 else b'is alive'
                if text.startswith('tftpboot'):
                    name=text.split()[2].rstrip(';')
                    row=next(r for r in json.loads((config.BOOT/'BOOT_MANIFEST.json').read_text())['files'] if r['file']==name)
                    return b'TRANSFER_RC=0\n==> '+row['crc32'].encode()
                if text.startswith('fpga loadb'):
                    loaded.append(True)
                    return b'FPGA_LOAD_RC=0 494d4243 48504f31'
                if text.startswith('uname -r'):
                    return b'\n6.12.0-xilinx-usrp-hdmi\n11111111-2222-3333-4444-555555555555\nrootflags=noload\n/dev/root / ext4 ro,relatime\n'
                return b''
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'runs').mkdir();(root/'state').mkdir();run=root/'runs/test';run.mkdir()
            with patch.object(workflow,'RUNS',root/'runs'),patch.object(workflow,'STATE',root/'state'),patch.object(workflow,'LEASE',root/'lease.json'),patch.object(workflow,'UART',FakeUART),patch.object(workflow.Manager,'ensure_host_route'),patch.object(workflow.Manager,'pause'):
                manager=workflow.Manager();manager.run=run;manager.state=dict(run_id='a'*32)
                boot_id=manager.boot()
                self.assertEqual(boot_id,'11111111-2222-3333-4444-555555555555')
                self.assertEqual(levels,dict(RX='1',TX='1'))
                self.assertEqual(len(network_calls),2)
                self.assertEqual([x['host_reachable'] for x in json.loads((run/'network-preflight.json').read_text())],[False,True])
                fail_forever=True;network_calls.clear();loaded.clear()
                run2=root/'runs/network-fail';run2.mkdir();manager.run=run2
                with self.assertRaises(RuntimeError):manager.boot()
                self.assertEqual(len(network_calls),2)
                self.assertEqual(loaded,[])

    def test_UART_already_UBoot_does_not_reboot_and_timeout_aborts(self):
        master,slave=pty.openpty()
        try:
            with tempfile.TemporaryDirectory() as tmp,patch.object(transport,'SERIAL',Path(os.ttyname(slave))):
                u=transport.UART(Path(tmp)/'tty',threading.Event());seen=[]
                def board():
                    seen.append(os.read(master,4096));os.write(master,b'\r\nZynq> ')
                thread=threading.Thread(target=board);thread.start()
                u.reboot_to_uboot(lambda text:None);thread.join(2)
                self.assertNotIn(b'reboot',b''.join(seen))
                with self.assertRaises(TimeoutError):u.command('ping fixture','timeout',.02)
                self.assertIn(b'\x03',os.read(master,4096))
                u.close()
        finally:os.close(master);os.close(slave)

    def test_UART_exclusive_ownership_and_release(self):
        master, slave = pty.openpty()
        try:
            with tempfile.TemporaryDirectory() as tmp, patch.object(transport, 'SERIAL', Path(os.ttyname(slave))):
                first = transport.UART(Path(tmp) / 'one', threading.Event())
                with self.assertRaises(OSError):transport.UART(Path(tmp) / 'two', threading.Event())
                first.close()
                second = transport.UART(Path(tmp) / 'three', threading.Event())
                second.close()
        finally:
            os.close(master);os.close(slave)

    def test_HTTP_fixed_operations_csrf_origin_host_and_upload_rejected(self):
        class FakeManager:
            def __init__(self):self.calls = [];self.mutex = threading.RLock();self.run = None
            def snapshot(self):return dict(stage='idle', active=False)
            def start(self):self.calls.append('start');return self.snapshot()
            def stop(self):self.calls.append('stop');return self.snapshot()
        manager = FakeManager()
        http = server.Server(('127.0.0.1', 0), manager)
        thread = threading.Thread(target=http.serve_forever, daemon=True);thread.start()
        port = http.server_port
        def call(path, body=b'{}', **headers):
            connection = __import__('http.client', fromlist=['HTTPConnection']).HTTPConnection('127.0.0.1', port, timeout=2)
            h = {'Origin': f'http://127.0.0.1:{port}', 'Content-Type': 'application/json', 'X-Photo-CSRF': http.token}
            h.update(headers);connection.request('POST', path, body, h);response=connection.getresponse();status=response.status;response.read();connection.close();return status
        try:
            self.assertEqual(call('/api/start'), 200)
            self.assertEqual(call('/api/start', b'{"command":"reboot"}'), 400)
            self.assertEqual(call('/api/shell'), 400)
            self.assertEqual(call('/api/upload'), 400)
            self.assertEqual(call('/api/start', **{'Origin':'http://evil.example'}), 403)
            self.assertEqual(call('/api/start', **{'X-Photo-CSRF':'bad'}), 403)
            self.assertEqual(call('/api/start', **{'Host':'rebind.example'}), 403)
            self.assertEqual(call('/api/stop'), 200)
            self.assertEqual(manager.calls, ['start', 'stop'])
        finally:
            http.shutdown();http.server_close();thread.join(2)

    def test_TFTP_fixed_RRQ_parser(self):
        assets = {'zImage': b'kernel'}
        self.assertEqual(tftp_service.parse_request(b'\0\1zImage\0octet\0', assets), ('zImage', {}))
        options = b'\0\1' + b'\0'.join([b'zImage', b'octet', b'blksize', b'1468', b'tsize', b'0', b''])
        self.assertEqual(tftp_service.parse_request(options, assets), ('zImage', dict(blksize='1468', tsize='6')))
        for raw in [b'\0\2zImage\0octet\0', b'\0\1../../etc/passwd\0octet\0', b'\0\1zImage\0netascii\0', b'\0\1unknown\0octet\0']:
            with self.assertRaises(ValueError):tftp_service.parse_request(raw, assets)

    def test_TFTP_actual_loopback_download_and_stop_gate(self):
        payload = b'fixed-boot-data' * 100
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
            client.bind(('127.0.0.1', 0));client.settimeout(2)
            with patch.object(tftp_service, 'HOST', '127.0.0.1'), patch.object(tftp_service, 'BOARD', '127.0.0.1'), patch.object(tftp_service, 'active', return_value=True):
                thread = threading.Thread(target=tftp_service.transfer, args=(client.getsockname(), b'\0\1zImage\0octet\0', {'zImage':payload}));thread.start()
                received=bytearray()
                while True:
                    raw,peer=client.recvfrom(2048);opcode,block=struct.unpack('!HH',raw[:4]);self.assertEqual(opcode,3)
                    received.extend(raw[4:]);client.sendto(struct.pack('!HH',4,block),peer)
                    if len(raw)<516:break
                thread.join(2);self.assertEqual(bytes(received),payload)
            with patch.object(tftp_service, 'HOST', '127.0.0.1'), patch.object(tftp_service, 'active', return_value=False):
                tftp_service.transfer(client.getsockname(), b'\0\1zImage\0octet\0', {'zImage':payload})
                raw,peer=client.recvfrom(2048);self.assertEqual(raw[:2],b'\0\5')


if __name__ == '__main__':
    unittest.main()
