#!/usr/bin/env python3
"""Bracket SOURCE recovery for unequal coded-slot protection on physical captures."""
import argparse
from hashlib import file_digest, sha256
import json
from pathlib import Path
import struct

from dma_batch_stream_probe import decode_batch
from g0_source_compact import batches
from g0_video_air_stream import HEADER, K, N, S
from review_g0_camera_source_losses import review


def read_capture(path, source, session):
    direct = set()
    restored = set()
    for sequence, (_, batch) in enumerate(batches(path)):
        if struct.unpack_from('<Q', batch, 16)[0] != sequence:
            raise ValueError('Capture has a missing, late, or duplicate DMA batch')
        records, meta = decode_batch(batch, sequence)
        if meta['flags'] & 1 or meta['total_rejected']:
            raise ValueError('Damaged or rejected DMA records in capture')
        for record in records:
            kind, flags, sid = struct.unpack_from('<HHI', record, 12)
            if kind != 1:
                continue
            block, index = struct.unpack_from('<IH', record, 48)
            ordinal = block * K + index
            if (sid != session or index >= K or ordinal >= len(source)
                    or flags & ~0xf or len(record) != S + 64
                    or ordinal in direct or ordinal in restored or record[64:] != source[ordinal]):
                raise ValueError('Invalid or duplicate physical SOURCE record')
            (restored if flags & 1 else direct).add(ordinal)
    return direct, restored


def protected_order(source, strong_sources):
    """Map each physical coded slot to one original RS row, within one block."""
    def priority(index):
        kind = HEADER.unpack_from(source[index])[8]
        return ({7: 0, 8: 0, 5: 1, 1: 2, 6: 3, 9: 4}.get(kind, 5),
                index % 2, index)

    strong = set(sorted(range(K), key=priority)[:strong_sources])
    even = [index for index in range(K) if index in strong]
    odd = [index for index in range(K) if index not in strong]
    parity = list(range(K, N))
    even += parity[:N // 2 - len(even)]
    odd += parity[N // 2 - strong_sources:]
    return [index for pair in zip(even, odd, strict=True) for index in pair]


def metrics(source, found):
    offered = source.stat().st_size // S
    missing = sorted(set(range(offered)) - found)
    result = review({
        'offered_SOURCE_symbols': offered,
        'not_delivered_SOURCE_symbols': len(missing),
        'video_missing_SOURCE_ordinals': missing,
    }, source)
    return {
        'byteexact_SOURCE': len(found),
        'missing_nonpadding_SOURCE': result['missing_nonpadding_SOURCE'],
        'complete_refresh_frames': result['fully_delivered_SPS_PPS_and_all_IDR_slices'],
        'max_complete_refresh_source_PTS_gap_us':
            result['max_source_pts_gap_between_complete_recovery_us'],
        'missing_fragments_by_NAL_kind': result['missing_fragments_by_NAL_kind'],
    }


def analyze(path, source_path, session):
    raw = source_path.read_bytes()
    if not 0 < len(raw) <= 1200 * K * S or len(raw) % (K * S):
        raise ValueError('Expected bounded complete SOURCE blocks')
    source = [raw[i:i + S] for i in range(0, len(raw), S)]
    direct, restored = read_capture(path, source, session)
    baseline = direct | restored
    blocks = len(source) // K
    complete = {
        b for b in range(blocks)
        if all(b * K + i in baseline for i in range(K))
    }
    assumptions = (
        'Same recorded systematic-slot delivery and same success of full original blocks are carried over; actual changed IQ can change either.',
        'For incomplete blocks the lower case credits only known direct systematic slots. The upper case additionally credits the unobserved RF0 parity slots when reassigned to systematic SOURCE.',
        'The upper case assumes those RF0 slots arrive, without adding any hypothetical FEC recovery or changing the number of coded slots.',
        'SOURCE counts and source PTS gaps are offline estimates. They do not establish HDMI performance or RF causality.',
    )
    result = {
        'assumptions': assumptions,
        'air_slots_per_block': N,
        'RS_SOURCE_per_block': K,
        'fixed_FEC_banks': 8,
        'added_air_slots': 0,
        'direct_SYSTEMATIC_RF0_RF1': [sum(i % 2 == lane for i in direct) for lane in range(2)],
        'restored_SOURCE': len(restored),
        'fully_delivered_original_blocks': len(complete),
        'partially_delivered_original_blocks_with_any_restored_SOURCE': len({i // K for i in restored} - complete),
        'baseline': metrics(source_path, baseline),
        'candidates': [],
    }
    for strong_sources in (10, 12, 14, 16):
        lower = set()
        upper = set()
        for b in range(blocks):
            base = b * K
            order = protected_order(source[base:base + K], strong_sources)
            if sorted(order) != list(range(N)):
                raise ValueError('Coded-slot mapping must be a permutation')
            if b in complete:
                lower.update(range(base, base + K))
                upper.update(range(base, base + K))
                continue
            for slot, row in enumerate(order):
                if row >= K:
                    continue
                if slot < K and base + slot in direct:
                    lower.add(base + row)
                    upper.add(base + row)
                elif slot >= K and slot % 2 == 0:
                    upper.add(base + row)
        result['candidates'].append({
            'systematic_slots_RF0_RF1': [strong_sources, K - strong_sources],
            'known_systematic_slot_model': metrics(source_path, lower),
            'also_credit_unobserved_RF0_parity_slots_model': metrics(source_path, upper),
        })
    with path.open('rb') as stream:
        result['capture_sha256'] = file_digest(stream, 'sha256').hexdigest()
    result['SOURCE_sha256'] = sha256(raw).hexdigest()
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--capture', type=Path, required=True)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--session', type=int, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    report = analyze(args.capture, args.source, args.session)
    with args.output.open('x') as output:
        json.dump(report, output, indent=2)
        output.write('\n')
