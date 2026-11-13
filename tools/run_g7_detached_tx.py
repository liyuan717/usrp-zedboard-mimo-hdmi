#!/usr/bin/env python3
"""Run two bounded R22 G7 witness transmissions without a receiver control link."""
import argparse
from contextlib import nullcontext
from hashlib import file_digest
import json
from pathlib import Path
import re
import signal
import subprocess
import time

from g0_video_air_stream import preflight
from hardware_lease import HardwareLease
from workspace_storage_guard import GIB, check


ROOT = Path(__file__).resolve().parents[1]
RECEIVER_UART = Path('/dev/serial/by-id/usb-2012_Cypress_Semiconductor_Cypress-USB2UART-Ver1.0G_8344058D1E27-if00')
RECEIVER_ETHERNET = Path('/sys/class/net/enxec1ac33f53f0/carrier')
X310_INTERFACE = 'enp12s0'
X310_ETHERNET = Path(f'/sys/class/net/{X310_INTERFACE}/carrier')
X310_ADDRESS = '192.168.10.2'
SESSIONS = {'A': 0x47375241, 'B': 0x47375242}
TX_READER_SHA256 = '1c863c4f056350c3aedd96e49fa1eb201b8fd7ba84a23e4b753eda927ef65533'
SD_EVIDENCE = ROOT / 'docs/evidence/g0-R22-offline-SD-virtual-boot-20261106.json'


def digest(path):
    with path.open('rb') as stream:
        return file_digest(stream, 'sha256').hexdigest()


def detached_links():
    return {
        'receiver_UART_absent': not RECEIVER_UART.exists(),
        'receiver_ethernet_carrier': RECEIVER_ETHERNET.read_text().strip() if RECEIVER_ETHERNET.exists() else None,
        'X310_ethernet_carrier': X310_ETHERNET.read_text().strip(),
    }


def require_detached_links():
    links = detached_links()
    if (not links['receiver_UART_absent'] or links['receiver_ethernet_carrier'] not in (None, '0')
            or links['X310_ethernet_carrier'] != '1'):
        raise RuntimeError(f'Receiver development links must be detached and X310 connected: {links}')
    route, = json.loads(subprocess.check_output(['ip', '-j', 'route', 'get', X310_ADDRESS], text=True))
    if route.get('dev') != X310_INTERFACE or route.get('prefsrc') != '192.168.10.1' or 'gateway' in route:
        raise RuntimeError(f'X310 must use its directly attached interface: {route}')
    links['X310_route'] = route
    return links


def interrupt(_signum, _frame):
    raise KeyboardInterrupt


def event(run, name):
    run[name] = {'monotonic_ns': time.monotonic_ns(), 'unix_ns': time.time_ns()}
    print(f'{name} at {run[name]["unix_ns"]}', flush=True)


def send(out, name, symbols, plan, reader, rf):
    wave = out / name
    fifo = wave / 'iq.fifo'
    run = {'burst': name, 'session': SESSIONS[name], 'plan': plan, 'SOURCE_sha256': digest(symbols)}
    with (out / f'{name}-producer.log').open('w') as log:
        producer = subprocess.Popen(
            ['/usr/bin/python3', str(ROOT / 'tools/g0_video_air_stream.py'),
             '--symbols', str(symbols), '--output', str(wave), '--session', str(SESSIONS[name])],
            stdout=log, stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 20
            while not fifo.exists():
                if producer.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError(f'{name} IQ FIFO did not start')
                time.sleep(.05)
            command = [str(reader), '--iq', str(fifo)]
            if rf:
                run['links_before_TX'] = require_detached_links()
                command += ['--addr', X310_ADDRESS, '--freq-hz', '5500000000', '--gain-db', '18',
                            '--tx-lo-offset-hz', '1000000', '--max-samples', str(plan['TX_samples']),
                            '--observe-clock-end', '--enable-tx']
            (out / f'{name}-command.json').write_text(json.dumps(command, indent=2) + '\n')
            event(run, f'{name}_sender_started')
            with (out / f'{name}-TX.log').open('w') as tx_log:
                result = subprocess.run(command, input='GO\n' if rf else '', text=True,
                                        stdout=tx_log, stderr=subprocess.STDOUT, timeout=45)
            event(run, f'{name}_sender_ended')
            result.check_returncode()
            tx = (out / f'{name}-TX.log').read_text()
            if rf:
                required = ('G0_TX_IQ_MAPPING rf0=0 rf1=1', 'TX_BURST_ACK channel_mask=3',
                            f'TX completed: {plan["TX_samples"]} dual-stream samples')
                if not all(marker in tx for marker in required) or any(
                    marker in tx for marker in ('[ERROR]', 'UHD asynchronous TX failure')
                ):
                    raise RuntimeError(f'{name} failed dual-RF completion checks')
                clock = re.search(r'^G0_TX_CLOCK (\d+) (\d+) (\d+) (\d+)$', tx, re.M)
                if clock is None:
                    raise RuntimeError(f'{name} lacks the scheduled start clock bracket')
                before, after, observed_tick, start_tick = map(int, clock.groups())
                if after < before or start_tick - observed_tick != 1_000_000:
                    raise RuntimeError(f'{name} has an invalid 4 MSPS start clock bracket')
                run['scheduled_start_host_monotonic_ns'] = [before + 250_000_000, after + 250_000_000]
                run['dual_EOB_acknowledged'] = True
            elif f'IQ input valid: {plan["TX_samples"]} dual-stream samples. TX disabled' not in tx:
                raise RuntimeError(f'{name} did not verify the complete dry IQ stream')
            if producer.wait(timeout=5) != 0:
                raise RuntimeError(f'{name} IQ producer failed')
            iq = json.loads((wave / 'receipt.json').read_text())
            if not iq['completed'] or iq['bytes_written'] != plan['TX_samples'] * 16:
                raise RuntimeError(f'{name} incomplete IQ production')
            run['IQ_sha256'] = iq['IQ_sha256']
            return run
        finally:
            if producer.poll() is None:
                producer.terminate()
                try:
                    producer.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    producer.kill()
                    producer.wait()


def main(args):
    if args.enable_rf:
        if args.sd_readback is None:
            raise ValueError('Verified physical R22 SD readback is required before RF')
        sd_readback = json.loads(args.sd_readback.read_text())
        expected = json.loads(SD_EVIDENCE.read_text())['SD_image']
        if sd_readback.get('physical_SD_readback') != expected:
            raise ValueError('Physical SD readback does not match the exact R22 image')
    reader = args.reader.resolve(strict=True)
    if digest(reader) != TX_READER_SHA256:
        raise ValueError('Expected the same UHD probe used in the R22 short-range run')
    witness = args.witness.resolve(strict=True)
    evidence = json.loads((ROOT / 'docs/evidence/g0-G7-detached-picture-witness-20261105.json').read_text())
    bursts = []
    for info in evidence['two_distinct_bursts']:
        name = info['burst']
        symbols = witness / name / 'symbols/video-source-symbols.bin'
        plan = preflight(symbols)
        if (digest(symbols) != info['symbol_fixture_SHA256'] or plan['video_frames'] != 200
                or plan['TX_duration_s'] != info['TX_duration_s']):
            raise ValueError(f'{name} differs from the qualified witness clip')
        bursts.append((name, symbols, plan))

    out = args.output.resolve()
    out.mkdir(exist_ok=False)
    report = {'RF_requested': args.enable_rf, 'completed': False, 'bursts': [], 'G7_passed': False}
    try:
        report['disk_before'] = check('before-G7-detached-witness')
        if report['disk_before']['disk']['available_bytes'] < 5 * GIB:
            raise RuntimeError('Available disk below 5 GiB')
        with HardwareLease() if args.enable_rf else nullcontext():
            if args.enable_rf:
                report['physical_SD_readback_receipt_SHA256'] = digest(args.sd_readback)
                report['initial_links'] = require_detached_links()
                event(report, 'late_start_wait_begins')
                time.sleep(10)
            report['bursts'].append(send(out, *bursts[0], reader, args.enable_rf))
            if args.enable_rf:
                event(report, 'stopped_transmission_wait_begins')
                time.sleep(8)
            report['bursts'].append(send(out, *bursts[1], reader, args.enable_rf))
            if args.enable_rf:
                first, second = report['bursts']
                report['conservative_stop_gap_ns'] = (
                    second['scheduled_start_host_monotonic_ns'][0] - first['A_sender_ended']['monotonic_ns']
                )
            report['completed'] = True
    finally:
        report['disk_after'] = check('after-G7-detached-witness')
        (out / 'receipt.json').write_text(json.dumps(report, indent=2) + '\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reader', type=Path, required=True)
    parser.add_argument('--witness', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--sd-readback', type=Path)
    parser.add_argument('--enable-rf', action='store_true',
                        help='only after verified R22 SD cold boot with development leads detached')
    signal.signal(signal.SIGTERM, interrupt)
    main(parser.parse_args())
