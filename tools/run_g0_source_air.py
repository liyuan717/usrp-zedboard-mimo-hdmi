#!/usr/bin/env python3
"""Finite dual-TX forward G0 into the actual PL SOURCE DMA; synthetic data, no video signoff."""
import argparse
import json
from pathlib import Path
import queue
import re
import socket
import subprocess
import struct
import threading
import time
import zlib

from board_command import command
from dma_batch_stream_probe import decode_batch
from hardware_lease import HardwareLease
from workspace_storage_guard import check, GIB

SERIAL = '/dev/serial/by-id/usb-2012_Cypress_Semiconductor_Cypress-USB2UART-Ver1.0G_8344058D1E27-if00'
HOST, BOARD = '169.254.209.83', '169.254.209.84'

RX_SETUP = (Path(__file__).resolve().parents[1] / 'board/g0_forward_video_rx_setup.sh').read_text().rstrip('\n')


def rx_setup_for_bandwidth(bandwidth_hz):
    if bandwidth_hz == 4_000_000:
        return RX_SETUP
    if bandwidth_hz != 2_000_000:
        raise ValueError('Only the bounded 2 MHz receive-bandwidth diagnostic is supported')
    default = 'echo 4000000 > "$phy/in_voltage_rf_bandwidth"'
    if RX_SETUP.count(default) != 1:
        raise ValueError('Unexpected default receive setup; refuse bandwidth override')
    return RX_SETUP.replace(default, 'echo 2000000 > "$phy/in_voltage_rf_bandwidth"\n'
                           'test "$(cat "$phy/in_voltage_rf_bandwidth")" = 2000000')


def review_capture(out, wave=None, remote_prefix=None):
    """Review an already terminated finite run; never starts or configures RF."""
    v = Path(__file__).resolve().parents[1]
    saved = json.loads((out/'receipt.json').read_text()) if (out/'receipt.json').exists() else {}
    wave = wave or v/saved.get('TX_waveform_directory', 'build/g0-fec-session-wave-r1')
    remote_prefix = remote_prefix or saved.get('board_capture_prefix', '/tmp/v3-source/air')
    data = (out / 'air.batches').read_bytes()
    log = (out / 'RX-result.log').read_text()
    crc = f'{zlib.crc32(data):08x}'
    assert len(data) == 1024 * 32768
    assert re.search(r'(?m)^' + crc + ' ' + re.escape(remote_prefix+'.batches') + r'\r?$', log)
    txlog = (out / 'TX.log').read_text()
    assert 'TX_BURST_ACK channel_mask=3' in txlog and 'TX completed: 2000000 dual-stream samples' in txlog
    records, sources, restored, record_crc = 0, [], 0, 0
    kinds, batch_flags = {}, {}
    waveform = json.loads((wave/'transmitted-packets.json').read_text())
    direct_tx, direct_counts = {}, {0: 0, 1: 0}
    for frame in waveform[:25]:  # 2000000 samples / 80000 samples per period.
        for packet in frame['packets']:
            if packet['kind'] == 1 and packet['symbol'] < 20 and not packet['intentional_CRC_error']:
                direct_tx.setdefault((packet['block'], packet['symbol']), set()).add(packet['TX'])
    for sequence in range(1024):
        decoded, meta = decode_batch(data[sequence*32768:(sequence+1)*32768], sequence)
        # Bit1 is the normal bounded low-rate flush, not a damaged prefix.
        assert not (meta['flags'] & ~2) and meta['total_rejected'] == 0
        batch_flags[meta['flags']] = batch_flags.get(meta['flags'], 0) + 1
        for r in decoded:
            kind, flags = struct.unpack_from('<HH', r, 12)
            kinds[kind] = kinds.get(kind, 0) + 1
            records += 1
            record_crc = zlib.crc32(r, record_crc)
            if kind == 1:
                session, = struct.unpack_from('<I', r, 16)
                block, index = struct.unpack_from('<IH', r, 48)
                assert session == 1446200833 and index < 20 and len(r) == 576
                expected = bytes((block*71+index*29+o*17+o//11+(block+1)*(o%13)) & 255 for o in range(512))
                assert r[64:] == expected, (block, index)
                sources.append((block, index))
                restored += 512 * (flags & 1)
                if not (flags & 1):
                    tx_channels = direct_tx[(block, index)]
                    assert len(tx_channels) == 1
                    direct_counts[next(iter(tx_channels))] += 1
    assert sources and len(sources) == len(set(sources)), 'No valid SOURCE or duplicate delivery'
    assert all(direct_counts.values()), 'Need independently transmitted direct data from both TX channels'
    summary = next(line for line in log.splitlines() if line.startswith('SOURCE_DMA '))
    values = {k: int(value, 16 if k == 'crc' else 10) for k, value in re.findall(r'(\w+)=([0-9a-f]+)', summary)}
    assert values['records'] == records and values['source'] == len(sources)
    assert values['exact_bytes'] == 512*len(sources) and values['restored_bytes'] == restored
    assert values['crc'] == record_crc
    for key in ('bad_source', 'bad_metadata', 'bad_record', 'missing_batch', 'duplicate', 'late',
                'running', 'queued', 'user', 'poisoned', 'pinned', 'exit'):
        assert values[key] == 0, key
    counters = {address: int(value, 16) for address, value in re.findall(r'(?m)^CSR_([0-9a-f]+)\r?\n0x([0-9A-F]+)\r?$', log)}
    assert counters['7c430008'] == 0 and counters['7c430040'] == len(sources)
    result = {'scope': 'Post-run offline review of actual forward PL/physical DMA/ARM bytes. Synthetic FEC data only.',
              'transport_bytes': len(data), 'transport_crc32': crc, 'batches': 1024,
              'records': records, 'record_kinds': kinds, 'batch_flags': batch_flags, 'SOURCE_records': len(sources),
              'SOURCE_bytes_exact': 512*len(sources), 'restored_bytes_exact': restored,
              'direct_not_FEC_SOURCE_records_by_unique_TX': direct_counts,
              'record_crc32_matches_native': f'{record_crc:08x}', 'unique_SOURCE_identities': sources,
              'CSR_stopped_counters': counters, 'EOB_acknowledged_channels': [0, 1],
              'actual_forward_SOURCE_byte_join_passed': True,
              'finite_native_control_and_byte_review_passed': True,
              'all_transmitted_data_recovered': False,
              'reliable_throughput_margin_or_video_or_fullG0_passed': False}
    (out / 'capture-review.json').write_text(json.dumps(result, indent=2) + '\n')
    return result


def main(a):
    v = Path(__file__).resolve().parents[1]
    out = a.output.resolve()
    assert re.fullmatch(r'[A-Za-z0-9_-]{1,72}', out.name), 'Use a simple bounded capture name'
    remote_prefix = '/tmp/v3-source/'+out.name
    wave = (a.waveform_directory or v/'build/g0-fec-session-wave-r1').resolve()
    profile = json.loads((wave/'waveform.json').read_text())
    assert profile['profile_id']==240 and profile['session']==1446200833
    assert profile['sample_rate_hz']==4000000 and profile['FEC']=={'banks':8,'k':20,'n':32,'symbol_bytes':512}
    assert profile['total_samples']>=2000000 and (wave/'iq.fc32').stat().st_size==profile['total_samples']*16
    out.mkdir(parents=True, exist_ok=False)
    before = check('before-finite-SOURCE-RF')
    (out / 'disk-before.json').write_text(json.dumps(before, indent=2) + '\n')
    assert before['disk']['available_bytes'] >= 5 * GIB
    result = {'scope': __doc__, 'frequency_hz': 5500000000, 'TX_gain_db': a.gain_db,
              'RX_gain_db': 60, 'sample_rate_hz': 4000000, 'RX_bandwidth_hz': 4000000,
              'TX_LO_offset_hz': 1000000, 'TX_max_samples': 2000000,
              'TX_duration_s': .5, 'SOURCE_DMA_max_batches': 1024,
              'ports': 'X310 TX/RX0 vertical, TX/RX1 horizontal; FMCOMMS3 RX1A vertical, RX2A horizontal',
              'FMCOMMS3_TX_enabled': False, 'feedback_enabled': False, 'TDD_enabled': False,
              'fixed_FEC_banks': 8, 'SD_written': False, 'completed': False,
              'g0_or_video_passed': False, 'RF_started': False, 'cleanup_LOs_off': False,
              'TX_waveform_directory': str(wave.relative_to(v)), 'board_capture_prefix': remote_prefix,
              'intentional_CRC_bad_sources_per_block': profile['intentional_CRC_bad_sources_per_block']}
    result['host_monotonic_command_intervals'] = {}
    result['TX_host_monotonic_events'] = []
    tx = None
    uart_ready = True
    try:
        with HardwareLease() as lease:
            def run(name, text, timeout=30):
                nonlocal uart_ready
                text = text.replace('/tmp/v3-source/air.', remote_prefix+'.')
                subprocess.run(['sh', '-n'], input=text, text=True, check=True)
                (out / (name + '.sh')).write_text(text + '\n')
                started = time.monotonic()
                try:
                    return command(SERIAL, text, out / (name + '.log'), timeout, lease=lease).decode(errors='replace')
                except TimeoutError:
                    uart_ready = False
                    result['UART_timeout_requires_inspection'] = True
                    raise
                finally:
                    result['host_monotonic_command_intervals'][name] = [started, time.monotonic()]

            identity = run('preflight', '''test -c /dev/v3-g0-source-ring
test "$(uname -r)" = 6.12.0-xilinx
grep ' / ext4 ro,' /proc/mounts
cat /proc/sys/kernel/random/boot_id
test "$(cat /sys/bus/iio/devices/iio:device0/out_altvoltage1_TX_LO_powerdown)" = 1
test "$(cat /sys/kernel/debug/iio/iio:device0/bist_prbs)" = 0
test "$(devmem 0x7c430008)" = 0x00000000
python3 -c "import os; s=os.statvfs('/tmp'); assert s.f_bavail*s.f_frsize>=33554432"''')
            ids = re.findall(r'(?m)^([a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12})\r?$', identity)
            assert len(ids) == 1
            result['boot_id'] = ids[0]
            try:
                run('RX-setup', RX_SETUP)
                args = [str(v / 'build/g0-forward/g0_uhd_probe'), '--iq',
                        str(wave/'iq.fc32'), '--freq-hz', '5500000000',
                        '--gain-db', str(a.gain_db), '--tx-lo-offset-hz', '1000000',
                        '--max-samples', '2000000', '--enable-tx']
                (out / 'TX-command.json').write_text(json.dumps(args, indent=2) + '\n')
                lines = queue.Queue()
                tx = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.STDOUT, text=True, bufsize=1)
                def read_tx():
                    with (out / 'TX.log').open('w') as log:
                        for line in tx.stdout:
                            if line.startswith(('G0_ARMED ', 'TX_FIRST_SAMPLES_ACCEPTED ', 'TX_BURST_ACK ', 'TX completed:')):
                                result['TX_host_monotonic_events'].append({'time': time.monotonic(), 'line': line.strip()})
                            log.write(line)
                            log.flush()
                            lines.put(line)
                    lines.put(None)
                reader = threading.Thread(target=read_tx, daemon=True)
                reader.start()
                for _ in range(100):
                    line = lines.get(timeout=30)
                    if line is None:
                        raise RuntimeError('TX initialization ended before READY; no GO')
                    if line.strip() == 'G0_READY WAIT_GO':
                        break
                else:
                    raise RuntimeError('TX READY absent; no GO')
                run('RX-arm', '''test ! -e /tmp/v3-source/air.rc
echo G0_RX_START_UPTIME
cat /proc/uptime
(/tmp/v3-source/probe 1024 5000 /tmp/v3-source/air.batches > /tmp/v3-source/air.log 2>&1; echo $? > /tmp/v3-source/air.rc) &
sleep 0.05
test "$(devmem 0x7c430008)" = 0x00000001
devmem 0x7c43000c
echo G0_PRE_GO_BATCHES
devmem 0x7c430010
echo G0_PRE_GO_UPTIME
cat /proc/uptime''')
                result['GO_host_monotonic'] = time.monotonic()
                tx.stdin.write('GO\n')
                tx.stdin.flush()
                result['RF_started'] = True
                rc = tx.wait(timeout=20)
                reader.join(timeout=2)
                result['TX_exit'] = rc
                assert rc == 0, 'Inspect TX.log, do not treat host admission as RF completion'
                txlog = (out / 'TX.log').read_text()
                assert 'TX_BURST_ACK channel_mask=3' in txlog
                assert 'TX completed: 2000000 dual-stream samples' in txlog
                result['EOB_acknowledged_channels'] = [0, 1]
                raw = run('RX-result', '''for i in $(seq 1 70); do if test -e /tmp/v3-source/air.rc; then break; fi; sleep 0.1; done
test -e /tmp/v3-source/air.rc
cat /tmp/v3-source/air.log
test "$(cat /tmp/v3-source/air.rc)" = 0
busybox crc32 /tmp/v3-source/air.batches
for a in 7c430008 7c43000c 7c430010 7c430040 7c430044 7c430048 7c43004c 7c430050 7c430054 7c430058 7c43005c 7c430060 7c430064 7c430068 7c43006c 7c430070 7c430074; do echo CSR_$a; devmem 0x$a; done''', timeout=15)
                summary = [line.strip() for line in raw.splitlines() if line.startswith('SOURCE_DMA ')]
                assert len(summary) == 1
                result['native_summary'] = summary[0]
                size = 1024 * 32768
                with socket.socket() as server:
                    server.bind((HOST, 0))
                    server.listen(1)
                    server.settimeout(15)
                    port = server.getsockname()[1]
                    transfer = {}
                    def receive():
                        try:
                            conn, peer = server.accept()
                            assert peer[0] == BOARD
                            with conn, (out / 'air.batches').open('wb') as f:
                                conn.settimeout(15)
                                count, crc = 0, 0
                                while True:
                                    data = conn.recv(min(65536, size + 1 - count))
                                    if not data:
                                        break
                                    count += len(data)
                                    assert count <= size
                                    crc = zlib.crc32(data, crc)
                                    f.write(data)
                            assert count == size
                            transfer.update(bytes=count, crc32=f'{crc:08x}')
                        except BaseException as error:
                            transfer['error'] = repr(error)
                    receiver = threading.Thread(target=receive, daemon=True)
                    receiver.start()
                    script = f'''python3 -c "import socket,pathlib; s=socket.create_connection(('{HOST}',{port}),5); s.sendall(pathlib.Path('/tmp/v3-source/air.batches').read_bytes()); s.shutdown(socket.SHUT_WR); s.close()"'''
                    run('transfer', script, timeout=25)
                    receiver.join(timeout=20)
                    assert not receiver.is_alive() and 'error' not in transfer, transfer
                    assert re.search(r'(?m)^' + transfer['crc32'] + ' ' + re.escape(remote_prefix+'.batches') + r'\r?$', raw), 'Board/host capture CRC mismatch'
                    result['capture'] = transfer
                result['capture_review'] = review_capture(out, wave, remote_prefix)
                result['completed'] = True
            finally:
                if tx is not None and tx.poll() is None:
                    tx.stdin.close()
                    try:
                        tx.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        tx.terminate()
                        tx.wait(timeout=5)
                if uart_ready:
                    run('RX-stop', '''echo 1 > /sys/bus/iio/devices/iio:device0/out_altvoltage0_RX_LO_powerdown
echo 1 > /sys/bus/iio/devices/iio:device0/out_altvoltage1_TX_LO_powerdown
test "$(cat /sys/bus/iio/devices/iio:device0/out_altvoltage0_RX_LO_powerdown)" = 1
test "$(cat /sys/bus/iio/devices/iio:device0/out_altvoltage1_TX_LO_powerdown)" = 1
test "$(cat /sys/kernel/debug/iio/iio:device0/bist_prbs)" = 0
devmem 0x7c430008
devmem 0x7c43000c
dmesg | tail -12''')
                    result['cleanup_LOs_off'] = True
    finally:
        (out / 'disk-after.json').write_text(json.dumps(check('after-finite-SOURCE-RF'), indent=2) + '\n')
        (out / 'receipt.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result), flush=True)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--review-only', action='store_true', help='Review an existing capture; no hardware access')
    p.add_argument('--waveform-directory', type=Path, help='Verified finite f0 waveform; defaults to the original four-CRC-erasure probe')
    p.add_argument('--gain-db', type=int, choices=(12, 18), default=12, help='Finite comparison within the user-authorized18dB ceiling')
    a = p.parse_args()
    if a.review_only:
        r = review_capture(a.output, a.waveform_directory)
        print(json.dumps({k: value for k, value in r.items() if k != 'unique_SOURCE_identities'}))
    else:
        main(a)
