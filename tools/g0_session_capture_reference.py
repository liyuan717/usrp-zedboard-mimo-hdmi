#!/usr/bin/env python3
"""Finite offline VRS3/14-data-slot reference; never RF/native throughput signoff.

The observer independently validates wire CRC, public PHY identities and VRS3
descriptors. Only after receive returns does a separate synthetic oracle check
accepted data and recovered SOURCE. No waveform/golden input enters acquisition.
"""
import argparse
from functools import partial
import hashlib
import json
from pathlib import Path
import struct
import zlib
import numpy as np
from g0_packet_probe import receive
from g0_packet_fec import recover, source_pattern, encode


class Observer:
    def __init__(self):
        self.packets = {}
        self.descriptors = []
        self.counts = dict(wire_packets=0, crc_bad=0, identity_bad=0,
                           descriptor_bad=0, duplicates=0)

    def __call__(self, wire, session, sf, seq):
        c = self.counts
        c['wire_packets'] += 1
        if c['wire_packets'] > 48 * 16:
            raise ValueError('finite diagnostic packet bound exceeded')
        if len(wire) != 548 or zlib.crc32(wire[:-4]) != struct.unpack_from('<I', wire, 544)[0]:
            c['crc_bad'] += 1
            return
        magic, major, kind, flags, sid, frame, packet, hint, block, index, size = struct.unpack_from('<4sBBHIIIIIHH', wire)
        slot = seq - sf * 16
        if not 0 <= sf < 48 or not 0 <= slot < 16:
            raise ValueError('finite diagnostic frame/slot bounds')
        common = (magic, major, flags, sid, frame, packet, hint, size)
        if common != (b'VRL3', 1, 0, session, sf, seq, 0xffffffff, 512):
            c['identity_bad'] += 1
            return
        body = wire[32:544]
        if slot < 2:
            expected = (b'VRS3', 1, 240, 0, 20, 32, 512, 1024, 600, 20, 1,
                        sf * 14 // 32, 8, 0, 0, sf * 14 // 32)
            desc = struct.unpack_from('<4sBBHBBHHHHHIBBHI', body)
            if (kind, block, index) != (3, 0xffffffff, 0) or desc != expected or any(body[32:]):
                c['descriptor_bad'] += 1
                return
            self.descriptors.append({'session': session, 'superframe': sf, 'seq': seq,
                                     'first_block': desc[11], 'block_count': desc[12]})
            return
        expected_block, expected_index = divmod(sf * 14 + slot - 2, 32)
        if (kind, block, index) != (1 if expected_index < 20 else 2, expected_block, expected_index):
            c['identity_bad'] += 1
            return
        key = session, block, index
        if key in self.packets:
            if self.packets[key] != body:
                raise ValueError('conflicting CRC-valid duplicate in finite reference')
            c['duplicates'] += 1
        self.packets[key] = body


def main(a):
    out = a.output.resolve()
    out.mkdir(exist_ok=False)
    raw = np.fromfile(a.capture, dtype=np.dtype([('tag', '<u8'), ('iq', '<i2', (4,))]))
    if not 100000 <= len(raw) <= 2097152 or np.any(np.diff(raw['tag']) != 1):
        raise ValueError('finite contiguous raw capture required')
    if a.records:
        raw = raw[:a.records]
    samples = raw['iq'][:, 0::2] + 1j * raw['iq'][:, 1::2]
    observer = Observer()
    kwargs = {}
    if a.causal_gate:
        from g0_fixed_differential_model import FixedDifferentialSelector
        from g0_fixed_frontend_model import fixed_frontend
        from g0_fixed_interpolation_model import fixed_sampler
        from g0_fixed_training_model import FixedTrainingEstimator
        from g0_fixed_startup_model import FixedStartupEstimator
        # No notch or future-mean adapter: use the same exact startup DC/RNE
        # boundary as PL. Only the model's calibration function is replaced
        # in its local call module; saved receiver defaults are unchanged.
        import g0_causal_acquisition as causal
        def dc_only(y, period):
            lanes = np.empty((len(y), 4))
            lanes[:, 0::2], lanes[:, 1::2] = y.real, y.imag
            mean = lanes[:80000].mean(axis=0)
            lanes = np.rint(lanes - mean)
            calibrated = lanes[:, 0::2] + 1j * lanes[:, 1::2]
            calibrated[:82048] = 0
            return calibrated, {'calibration_commit_sample': 82048,
                                'enabled': False, 'notch_implemented': False,
                                'mean': mean.tolist(), 'exact_PL_DC_RNE': True}
        causal.calibrated_input = dc_only
        kwargs = dict(causal=True, frontend=fixed_frontend, sampler=fixed_sampler,
                      differential_selector=partial(FixedDifferentialSelector, hit_denominator=a.causal_gate),
                      channel_estimator=FixedTrainingEstimator(), startup_estimator=FixedStartupEstimator())
    report, _legacy_blocks = receive(samples, dc_block=False, wire_observer=observer, **kwargs)
    (out / 'PHY-model.json').write_text(json.dumps(report, indent=2) + '\n')
    # Post-decode only: none of these expected bytes are available to acquire.
    sessions = sorted({key[0] for key in observer.packets})
    rows = []
    for sid in sessions:
        for block in sorted({key[1] for key in observer.packets if key[0] == sid}):
            received = {k[2]: v for k, v in observer.packets.items() if k[:2] == (sid, block)}
            expected = encode(source_pattern(block))
            assert all(v == expected[i].tobytes() for i, v in received.items()), 'CRC-accepted coding bytes differ from separate TX oracle'
            recovered = recover(received)
            equal = recovered is not None and np.array_equal(recovered, source_pattern(block))
            if recovered is not None:
                assert equal
                (out / f'{sid:08x}-block-{block}.source.bin').write_bytes(recovered.tobytes())
            rows.append({'session': sid, 'block': block, 'received_indices': sorted(received),
                         'recovered': recovered is not None, 'source_bytes_equal': 10240 if equal else 0})
    result = dict(scope=__doc__, capture=str(a.capture), records=len(raw),
                  capture_sha256=hashlib.sha256(a.capture.read_bytes()).hexdigest(),
                  causal_integer_islands_gate_denominator=a.causal_gate or None,
                  CRC_good_PHY_headers=report['CRC_good_PHY_headers'],
                  sample_rate_offset_ppm=report['sample_rate_offset_ppm'],
                  tracking_NCO_CFO_hz=report['tracking_NCO_CFO_hz'],
                  wire_counts=observer.counts, descriptors=observer.descriptors,
                  accepted_data_packets=len(observer.packets),
                  accepted_data_bytes_exact=len(observer.packets)*512,
                  recovered_SOURCE_bytes_exact=sum(r['source_bytes_equal'] for r in rows),
                  blocks=rows, PL_or_native_or_reliable_throughput_or_G0_passed=False)
    assert observer.packets and observer.descriptors
    (out / 'receipt.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({k:v for k,v in result.items() if k not in ('blocks','descriptors')}), flush=True)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--capture', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--records', type=int, default=0)
    p.add_argument('--causal-gate', type=int, choices=(0,5,10), default=0)
    main(p.parse_args())
