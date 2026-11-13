#!/usr/bin/env python3
"""Observe X310 time against both host clocks under the shared hardware lease."""
import argparse
import json
import subprocess
from pathlib import Path

from g0_forward_timebase import clock_rate_bounds
from hardware_lease import HardwareLease
from workspace_storage_guard import check


def main(args):
    out = args.output.resolve()
    out.mkdir(exist_ok=False)
    (out / 'disk-before.json').write_text(json.dumps(check('before-clock-pairs'), indent=2)+'\n')
    try:
        with HardwareLease():
            with (out / 'observations.log').open('x') as log:
                subprocess.run([str(args.binary.resolve())], stdout=log, stderr=subprocess.STDOUT,
                               check=True, timeout=35)
        lines = (out / 'observations.log').read_text().splitlines()
        assert 'CLOCK_PAIRS_DONE RF_STREAMS_CREATED=0' in lines
        rows = [list(map(int, line.split()[1:])) for line in lines if line.startswith('CLOCK_PAIR ')]
        assert len(rows) == 33 and all(len(row) == 5 for row in rows)
        assert all(row[0] <= row[4] and row[1] <= row[3] for row in rows)
        assert all(b[2] > a[2] for a,b in zip(rows, rows[1:]))
        mono = [(row[0], row[4], row[2]) for row in rows]
        raw = [(row[1], row[3], row[2]) for row in rows]
        result = dict(scope=__doc__, completed=True, samples=rows,
                      MONOTONIC=clock_rate_bounds(mono[0], mono[-1]),
                      MONOTONIC_RAW=clock_rate_bounds(raw[0], raw[-1]),
                      RF_streams_created=0, device_time_set=False, board_accessed=False,
                      source_capture_or_production_age_verified=False)
        result['UHD_reported_error_lines'] = [line for line in lines if line.startswith('[ERROR]')]
        result['UHD_error_free_observed'] = not result['UHD_reported_error_lines']
        (out / 'receipt.json').write_text(json.dumps(result, indent=2)+'\n')
        print(json.dumps({key:value for key,value in result.items() if key!='samples'}))
    finally:
        (out / 'disk-after.json').write_text(json.dumps(check('after-clock-pairs'), indent=2)+'\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    main(parser.parse_args())
