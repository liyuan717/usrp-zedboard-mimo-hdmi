#!/usr/bin/env python3
"""Finite SOURCE-batch DMA control on the explicitly RAM-booted candidate, no RF TX."""
import argparse
import hashlib
import http.server
import json
from pathlib import Path
import re
import socket
import subprocess
import threading
import zlib

from board_command import command, require_boot_id
from hardware_lease import HardwareLease
from g0_source_compact import batches as compact_batches
from dma_batch_stream_probe import decode_batch
from workspace_storage_guard import check as disk_check, GIB

SERIAL = '/dev/serial/by-id/usb-2012_Cypress_Semiconductor_Cypress-USB2UART-Ver1.0G_8344058D1E27-if00'
HOST, BOARD = '169.254.209.83', '169.254.209.84'


def main(a):
    v = Path(__file__).resolve().parents[1]
    out = a.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    driver, client = a.driver.resolve(), a.client.resolve()
    for directory in (driver, client):
        proof = json.loads((directory / 'receipt.json').read_text())
        for name, digest in proof['source_sha256'].items():
            assert hashlib.sha256((v / name).read_bytes()).hexdigest() == digest, name
    assert json.loads((driver / 'receipt.json').read_text())['module_layout_checked']
    files = {'source.ko': driver / 'g0_source_ring.ko', 'probe': client / 'g0_source_ring_probe'}
    checks = {name: subprocess.check_output(['cksum', str(path)], text=True).split()[:2]
              for name, path in files.items()}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            path = files.get(self.path.lstrip('/'))
            if self.client_address[0] != BOARD or path is None:
                self.send_error(403)
                return
            data = path.read_bytes()
            self.send_response(200)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    before = disk_check('before-native-SOURCE-control')
    (out / 'disk-before.json').write_text(json.dumps(before, indent=2) + '\n')
    assert before['disk']['available_bytes'] >= 5 * GIB
    result = {'scope': __doc__, 'native_padding_DMA_control_passed': False,
              'RF_transmission': False, 'SD_written': False,
              'physical_SOURCE_payload_passed': False, 'g0_or_video_passed': False,
              'native_lines': [], 'cleanup_LOs_off': False,
              'long_continuous_padding_control_passed': False}
    server = None
    try:
        with HardwareLease() as lease:
            def run(name, text, timeout=30):
                (out / (name + '.sh')).write_text(text + '\n')
                return command(SERIAL, text, out / (name + '.log'), timeout,
                               lease=lease).decode(errors='replace')

            identity = run('identity', '''uname -a
cat /proc/sys/kernel/random/boot_id
test "$(uname -r)" = 6.12.0-xilinx
grep ' / ext4 ro,' /proc/mounts
test "$(cat /sys/bus/iio/devices/iio:device0/out_altvoltage0_RX_LO_powerdown)" = 1
test "$(cat /sys/bus/iio/devices/iio:device0/out_altvoltage1_TX_LO_powerdown)" = 1
cat /sys/bus/platform/devices/7c430000.rf-receiver-v2/of_node/compatible
ip -4 addr show eth0''')
            assert 'seu,video-radio-v3-g0-source-batch' in identity
            result['boot_id'] = require_boot_id(identity, getattr(a, 'expected_boot_id', None))
            server = http.server.ThreadingHTTPServer((HOST, 0), Handler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            setup = 'mkdir -p /tmp/v3-source\n'
            for name in files:
                setup += f'wget -q -O /tmp/v3-source/{name} http://{HOST}:{server.server_address[1]}/{name}\n'
                setup += f'test "$(cksum /tmp/v3-source/{name} | cut -d " " -f1,2)" = "' + ' '.join(checks[name]) + '"\n'
            setup += '''chmod 700 /tmp/v3-source/probe
grep -q ' /sys/kernel/debug debugfs ' /proc/mounts || mount -t debugfs debugfs /sys/kernel/debug
test "$(cat /sys/kernel/debug/iio/iio:device0/bist_prbs)" = 0
test ! -d /sys/module/g0_adc_capture
test ! -d /sys/module/g0_dma_ring
insmod /tmp/v3-source/source.ko
test -c /dev/v3-g0-source-ring
dmesg | tail -12'''
            try:
                run('setup', setup)
                if a.clock:
                    run('clock-capability','test "$(devmem 0x7c430080)" = 0x43543356')
                    result['clock_controls']=[]
                for n in (5, 32):
                    raw = run('padding-' + str(n),
                              f'/tmp/v3-source/probe {n} 5000 /dev/null'+(' --clock' if a.clock else ''), timeout=15)
                    lines = [line.strip() for line in raw.splitlines() if line.startswith('SOURCE_DMA ')]
                    assert len(lines) == 1
                    values = {k: int(value, 16 if k == 'crc' else 10)
                              for k, value in re.findall(r'(\w+)=([0-9a-f]+)', lines[0])}
                    assert values['batches'] == n and values['hw_batches'] == n
                    for key in ('bad_source', 'bad_metadata', 'bad_record', 'missing_batch',
                                'duplicate', 'late', 'running', 'queued', 'user', 'poisoned', 'pinned', 'exit'):
                        assert values[key] == 0, (key, values)
                    # This no-RF pass checks framing/padding/STOP/reentry, not decoded RF payload.
                    assert values['source'] == 0 and values['records'] == 0
                    result['native_lines'].append(lines[0])
                    if a.clock:
                        line,=[line for line in raw.splitlines() if line.startswith('SOURCE_CLOCK ')]
                        clock={k:int(value) for k,value in re.findall(r'(\w+)=(\d+)',line)}
                        assert clock['before_idle']==clock['after_idle']==1
                        assert clock['valid']+clock['unavailable']==n
                        result['clock_controls'].append(clock)
                result['native_padding_DMA_control_passed'] = True
                if a.long:
                    raw=run('long-padding',
                            '/tmp/v3-source/probe 65536 110000 /tmp/v3-source/long.v3bc --compact\n'
                            'busybox crc32 /tmp/v3-source/long.v3bc',timeout=115)
                    lines=[s for s in raw.splitlines() if s.startswith('SOURCE_DMA ')]
                    compact=[s for s in raw.splitlines() if s.startswith('SOURCE_COMPACT ')]
                    assert len(lines)==len(compact)==1
                    values={k:int(x,16 if k=='crc' else 10) for k,x in re.findall(r'(\w+)=([0-9a-f]+)',lines[0])}
                    detail={k:int(x) for k,x in re.findall(r'(\w+)=(\d+)',compact[0])}
                    assert values['batches']==values['hw_batches']==65536
                    for key in ('source','records','bad_source','bad_metadata','bad_record',
                                'missing_batch','duplicate','late','running','queued','user','poisoned','pinned','exit'):
                        assert values[key]==0,(key,values)
                    assert detail['omitted_batches']==0 and detail['dump_bytes']==16+65536*80
                    span=(detail['last_ns']-detail['first_ns'])/1e9
                    assert 60<=span<110,span
                    transfer={};size=detail['dump_bytes']
                    with socket.socket() as sock:
                        sock.bind((HOST,0));sock.listen(1);sock.settimeout(20)
                        def receive():
                            try:
                                conn,peer=sock.accept();assert peer[0]==BOARD
                                count=crc=0
                                with conn,(out/'long.v3bc').open('wb') as f:
                                    conn.settimeout(15)
                                    while True:
                                        data=conn.recv(min(65536,size+1-count))
                                        if not data:break
                                        count+=len(data);assert count<=size
                                        f.write(data);crc=zlib.crc32(data,crc)
                                assert count==size
                                transfer.update(bytes=count,crc32=f'{crc:08x}')
                            except BaseException as error:transfer['error']=repr(error)
                        receiver=threading.Thread(target=receive,daemon=True);receiver.start()
                        port=sock.getsockname()[1]
                        run('long-transfer',f'''python3 -c "import socket,pathlib; s=socket.create_connection(('{HOST}',{port}),5); s.sendall(pathlib.Path('/tmp/v3-source/long.v3bc').read_bytes()); s.shutdown(socket.SHUT_WR); s.close()"''',timeout=25)
                        receiver.join(timeout=20)
                        assert not receiver.is_alive() and 'error' not in transfer,transfer
                    assert re.search(r'(?m)^'+transfer['crc32']+r' /tmp/v3-source/long\.v3bc\r?$',raw)
                    previous=0;count=0
                    for timestamp,batch in compact_batches(out/'long.v3bc'):
                        records,meta=decode_batch(batch,count)
                        assert not records and not(meta['flags']&~2) and meta['total_rejected']==0
                        assert timestamp>=previous;previous=timestamp;count+=1
                    assert count==65536
                    again=run('post-long-reentry','/tmp/v3-source/probe 5 5000 /dev/null',timeout=15)
                    result['post_long_reentry_summary']=next(s for s in again.splitlines() if s.startswith('SOURCE_DMA '))
                    reentry={k:int(x,16 if k=='crc' else 10) for k,x in re.findall(r'(\w+)=([0-9a-f]+)',result['post_long_reentry_summary'])}
                    assert reentry['batches']==reentry['hw_batches']==5
                    assert all(reentry[k]==0 for k in ('running','queued','user','poisoned','pinned','exit','bad_metadata','bad_record','missing_batch'))
                    result.update(long_continuous_padding_control_passed=True,
                                  long_native_summary=lines[0],long_compact_summary=compact[0],
                                  long_observed_span_seconds=span,long_capture=transfer,
                                  long_all65536_batch_boundaries_independently_checked=True)
            finally:
                run('idle', '''test "$(cat /sys/bus/iio/devices/iio:device0/out_altvoltage0_RX_LO_powerdown)" = 1
test "$(cat /sys/bus/iio/devices/iio:device0/out_altvoltage1_TX_LO_powerdown)" = 1
test "$(cat /sys/kernel/debug/iio/iio:device0/bist_prbs)" = 0
dmesg | tail -18
cat /proc/sys/kernel/random/boot_id''')
                result['cleanup_LOs_off'] = True
    finally:
        if server:
            server.shutdown()
            server.server_close()
        (out / 'disk-after.json').write_text(json.dumps(disk_check('after-native-SOURCE-control'), indent=2) + '\n')
        (out / 'receipt.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result), flush=True)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('output', 'driver', 'client'):
        p.add_argument('--' + name, type=Path, required=True)
    p.add_argument('--long',action='store_true',help='one continuous65536-batch no-RF control, then STOP/reentry; not reliable RF capacity')
    p.add_argument('--clock',action='store_true',help='check native clock capability, bounds and stopped freshness')
    p.add_argument('--expected-boot-id',help='require the qualified RAM boot before loading or starting DMA')
    main(p.parse_args())
