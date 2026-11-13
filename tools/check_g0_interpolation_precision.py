#!/usr/bin/env python3
"""Reproduce G0 coordinate/output-precision ablation on an existing ADC capture.

Compare integer NCO/RRC with Q32 coordinates, Q16 coordinates without sample
rounding, and explicit F=0/1/2 integer output. This diagnostic preserves extra
CRC erasures even if FEC recovers them. It does not change a candidate or prove
bounded history, streaming timing, connected RTL, or full G0 acceptance.
"""
import argparse
from functools import partial
import json
from pathlib import Path
import zlib
import numpy as np
from check_g0_causal_model import bounds
from check_g0_fixed_interpolation import accepted_packets, compare_source
from g0_fixed_frontend_model import fixed_frontend
from g0_fixed_interpolation_model import (Q16, Q32, exact_signed20_lanes,
    fixed_sampler, quantized_positions)
from g0_packet_probe import receive
from g0_public_acquisition import interpolate


def coordinate_sampler(mf, peak, step, period, preamble_start, fraction_bits):
    index, weight, positions, p32, info = quantized_positions(peak, step, period, preamble_start)
    if fraction_bits == 32:
        positions = p32.astype(float) / Q32
        # Preserve this software diagnostic's ordinary interpolation, while
        # explicitly avoiding its edge-hold behavior at the capture boundary.
        if np.any(positions < 0) or np.any(np.floor(positions) + 1 >= len(mf)):
            raise ValueError('coordinate ablation needs both endpoints')
        result = interpolate(mf, positions)
    elif fraction_bits == 16:
        if np.any(index < 0) or np.any(index + 1 >= len(mf)):
            raise ValueError('coordinate ablation needs both endpoints')
        x0, x1 = exact_signed20_lanes(mf[index]), exact_signed20_lanes(mf[index + 1])
        value = x0 + (x1 - x0) * weight[:, None] / Q16
        result = (value[:, 0::2] + 1j * value[:, 1::2]) / 2
    else:
        raise ValueError('coordinate ablation fraction bits must be16or32')
    info['precision_ablation'] = f'Q{fraction_bits}_coordinates_float_values'
    info['sample_value_output_rounding'] = False
    return result, positions, info


def main(a):
    out = a.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    if a.capture.stat().st_size % 16:
        raise ValueError('truncated ADC record')
    raw = np.fromfile(a.capture, dtype=np.dtype([('tag', '<u8'), ('iq', '<i2', (4,))]))
    crc = f'{zlib.crc32(raw):08x}'
    if crc != json.loads((a.capture.parent / 'receipt.json').read_text())['capture']['crc32']:
        raise ValueError('capture CRC differs')
    if np.any(np.diff(raw['tag']) != 1):
        raise ValueError('ADC tag gap')
    y = raw['iq'][:, 0::2] + 1j * raw['iq'][:, 1::2]
    baseline, basebytes = receive(y, causal=True, frontend=fixed_frontend)
    bounds(baseline)
    compare_source(basebytes)
    basegood = accepted_packets(baseline)
    (out / 'baseline.json').write_text(json.dumps(baseline, indent=2) + '\n')
    rows = []
    modes = [(f'Q{bits}_coordinates_float_values', partial(coordinate_sampler, fraction_bits=bits))
             for bits in [32, 16]]
    modes += [(f'output_fraction_{bits}', partial(fixed_sampler, output_fraction_bits=bits)) for bits in range(3)]
    for name, sampler in modes:
        report, blocks = receive(y, causal=True, frontend=fixed_frontend, sampler=sampler)
        bounds(report)
        equal = compare_source(blocks)
        good = accepted_packets(report)
        row = {
            'mode': name, 'baseline_CRC_good_packets': baseline['CRC_good_link_packets'],
            'CRC_good_packets': report['CRC_good_link_packets'],
            'additional_erased_packet_sequences': sorted(basegood - good),
            'newly_accepted_packet_sequences': sorted(good - basegood),
            'recovered_blocks': len(blocks), 'eligible_blocks': report['eligible_complete_FEC_blocks'],
            'source_bytes_equal': True, 'source_bytes_compared': equal,
            'same_delivered_blocks': set(blocks) == set(basebytes) and all(blocks[k] == basebytes[k] for k in blocks),
            'same_CRC_set_as_baseline': good == basegood, 'g0_passed': False,
        }
        rows.append(row)
        (out / (name + '.json')).write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps(row), flush=True)
    receipt = {'scope': 'coordinate/output-precision ablation only; no candidate or RTL change',
               'capture_path': str(a.capture.resolve()), 'capture_crc32': crc, 'cases': rows,
               'new_RF_transmissions': False, 'g0_passed': False}
    (out / 'receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--capture', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    main(parser.parse_args())
