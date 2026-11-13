#!/usr/bin/env python3
"""Stream the verified original R22 G6 waveform through a bounded FIFO."""
import argparse
from collections import defaultdict
import errno
from hashlib import sha256
import json
import os
from pathlib import Path
import select
import sys
import time
import zlib

from g0_camera_live_stream import LiveSchedule, PROFILE_REPEAT_PERIODS
from g0_video_air_stream import K, S, inspect_block, modulate_packets, public_prefix, rrc, swap_coded_lanes


ROOT = Path(__file__).resolve().parents[1]
REVIEW = ROOT / 'docs/evidence/g0-R22-G6-exact-waveform-review-20261106.json'
NATIVE_REVIEW = ROOT / 'docs/evidence/g0-R22-short-camera1200-20261106.json'
LIVE_FAILURE_REVIEW = ROOT / 'docs/evidence/g0-R22-native-AU-live-and-known-IQ-HDMI-20261107.json'
PERIOD_SECONDS = .020
SAMPLES_PER_PERIOD = 80_000


def read(path):
    return json.loads(path.read_text())


def load_replay(archive):
    archive = archive.resolve(strict=True)
    failure = read(LIVE_FAILURE_REVIEW)['replay_qualification']
    if archive == ROOT / failure['archive']:
        expected_iq = failure['waveform']
        expected_hashes = failure['file_SHA256']
    else:
        review = read(REVIEW)
        expected_iq = review['waveform']
        expected_hashes = {**read(NATIVE_REVIEW)['files_sha256'], **review['file_SHA256']}
    if not expected_iq['exact_IQ_match']:
        raise ValueError('Exact waveform was not independently verified')
    files = ['receipt.json', 'wave/receipt.json', 'wave/admissions.json',
             'wave/camera/availability.json', 'wave/camera/source-symbols.bin',
             'tx-clock.json', 'clean-native.json']
    for filename in files:
        path = archive / filename
        key = str(path.relative_to(ROOT))
        if sha256(path.read_bytes()).hexdigest() != expected_hashes.get(key):
            raise ValueError(f'Unverified archived replay input: {key}')
    original = read(archive / 'wave/receipt.json')
    session = read(archive / 'receipt.json')['session']
    clean = read(archive / 'clean-native.json')
    pixels = {row['capture_id'] & 0xffffffff: row['rgb565_crc32'] for row in clean['pixels']}
    if (clean['machine'] != 'armv7l' or clean['decoded_frames'] != 1200
            or clean['reader_error'] or clean['dropped'] or set(pixels) != set(range(1200))
            or len(clean['pixels']) != len(pixels)
            or any(row['capture_id'] >> 32 != session for row in clean['pixels'])):
        raise ValueError('Expected the complete original Cortex-A9 pixel reference')
    periods = original['periods']
    if (not 8 <= periods <= 3200 or periods != expected_iq['periods']
            or original['IQ_bytes'] != periods * SAMPLES_PER_PERIOD * 16):
        raise ValueError('Unexpected archived sample count')
    if (original['IQ_bytes'] != expected_iq['bytes']
            or any(original[key] != expected_iq[key] for key in ('IQ_sha256', 'IQ_crc32'))):
        raise ValueError('Archived IQ identity differs from the independent replay proof')
    plan = {'video_frames': 1200, 'periods': periods, 'TX_samples': periods * SAMPLES_PER_PERIOD,
            'TX_duration_s': periods * PERIOD_SECONDS}
    return plan, session, pixels, expected_iq


def load_coded_lane_proof(path, session, original):
    proof = read(path)
    if (not proof['completed'] or not proof['coded_lanes_swapped'] or proof['RF_opened_by_producer']
            or proof['session'] != session or proof['bytes_written'] != original['bytes']
            or proof['original_archive_IQ_sha256'] != original['IQ_sha256']):
        raise ValueError('Missing verified no-RF coded-slot control proof')
    for key, length in (('IQ_sha256', 64), ('IQ_crc32', 8)):
        if len(proof[key]) != length or any(c not in '0123456789abcdef' for c in proof[key]):
            raise ValueError('Invalid coded-slot control digest')
    return {**proof, 'bytes': proof['bytes_written']}


class ArchivedSchedule(LiveSchedule):
    def __init__(self, archive, plan, session):
        wave = archive / 'wave'
        original = read(wave / 'receipt.json')
        rows = read(wave / 'admissions.json')
        if (not len(rows) == original['produced_SOURCE_blocks'] == original['transmitted_SOURCE_blocks']
                or original['overflow_dropped_unadvertised_blocks'] or original['expired_unadvertised_blocks']):
            raise ValueError('Replay requires complete ordered original SOURCE')
        self.source_blocks = len(rows)
        self.capture_times = dict(enumerate(read(wave / 'camera/availability.json')['capture_times_us']))
        active = [count for count in range(1, plan['periods'] + 1)
                  if count - (count + PROFILE_REPEAT_PERIODS - 1) // PROFILE_REPEAT_PERIODS
                  == original['forward_timebases_transmitted']]
        first_active, = active
        first_period = plan['periods'] - first_active
        first_us = rows[0]['admitted_monotonic_us']
        self.by_period = defaultdict(list)
        with (wave / 'camera/source-symbols.bin').open('rb') as stream:
            for index, row in enumerate(rows):
                data = stream.read(K * S)
                if (index != row['wire_block'] or index != row['source_ordinal']
                        or sha256(data).hexdigest() != row['source_sha256']):
                    raise ValueError('Archived SOURCE bytes or ownership differ')
                period = first_period + round((row['admitted_monotonic_us'] - first_us) / 20_000)
                self.by_period[period].append((data, row))
            if stream.read(1):
                raise ValueError('Unexpected archived SOURCE tail')
        super().__init__(None, session, plan['periods'], archive / 'tx-clock.json')

    def fill_pending(self):
        for data, row in self.by_period.pop(self.last_period, ()):
            if len(self.pending) >= 2 or row['wire_block'] != self.block + len(self.pending):
                raise ValueError('Archived period exceeds two pending SOURCE blocks')
            if self.capture_epoch_us is None:
                self.capture_epoch_us = row['capture_epoch_us']
            if row['capture_epoch_us'] != self.capture_epoch_us:
                raise ValueError('Archived SOURCE changed capture epoch')
            self.pending.append((data, inspect_block(data, capture_times=self.capture_times)))

    def is_ready(self, _meta, _period):
        return True


def open_fifo_writer(fifo):
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            return os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
        except OSError as error:
            if error.errno != errno.ENXIO:
                raise
            time.sleep(.02)
    raise TimeoutError('Replay IQ reader did not connect')


def main(args):
    plan, session, _, expected = load_replay(args.archive)
    schedule = ArchivedSchedule(args.archive, plan, session)
    args.output.mkdir(exist_ok=False)
    fifo = args.output / 'iq.fifo'
    os.mkfifo(fifo, 0o600)
    result = {'completed': False, 'session': session, 'paced': args.paced, 'RF_opened_by_producer': False,
              'coded_lanes_swapped': args.swap_coded_lanes,
              'original_archive_IQ_sha256': expected['IQ_sha256']}
    fd = None
    count = 0
    crc = 0
    digest = sha256()
    try:
        fd = open_fifo_writer(fifo)
        if args.wait_go:
            if not select.select([sys.stdin], [], [], 60)[0] or sys.stdin.buffer.readline(4) != b'GO\n':
                raise RuntimeError('Archived replay requires a timely GO after receiver arm')
        prefix, coefficients = public_prefix(), rrc()
        start = time.monotonic()
        deadline = start + plan['TX_duration_s'] + 60
        for period in range(plan['periods']):
            if args.paced:
                time.sleep(max(0, start + period * PERIOD_SECONDS - time.monotonic()))
            wires, metadata = schedule.period(period)
            if args.swap_coded_lanes:
                swap_coded_lanes(wires, metadata)
            packed, _ = modulate_packets(session, period, lambda slot: (wires[slot], metadata[slot]),
                                         prefix, coefficients)
            data = packed.tobytes()
            crc = zlib.crc32(data, crc)
            digest.update(data)
            remaining = memoryview(data)
            while remaining:
                if time.monotonic() >= deadline:
                    raise TimeoutError('Replay IQ sink exceeded finite deadline')
                if not select.select([], [fd], [], .2)[1]:
                    continue
                try:
                    n = os.write(fd, remaining)
                except BlockingIOError:
                    continue
                count += n
                remaining = remaining[n:]
        if (schedule.by_period or schedule.pending or schedule.sent != schedule.source_blocks * 32
                or schedule.timebases != expected['timebases']
                or count != expected['bytes']):
            raise ValueError('Archived IQ dimensions or packet census differ')
        if not args.swap_coded_lanes and (digest.hexdigest() != expected['IQ_sha256']
                                          or f'{crc:08x}' != expected['IQ_crc32']):
            raise ValueError('Streamed IQ differs from the original RF run')
        result['completed'] = True
    finally:
        if fd is not None:
            os.close(fd)
        result.update(bytes_written=count, IQ_sha256=digest.hexdigest(), IQ_crc32=f'{crc:08x}')
        (args.output / 'receipt.json').write_text(json.dumps(result, indent=2) + '\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--paced', action='store_true')
    parser.add_argument('--wait-go', action='store_true')
    parser.add_argument('--swap-coded-lanes', action='store_true')
    main(parser.parse_args())
