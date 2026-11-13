#!/usr/bin/env python3
"""Join physical archived-video SOURCE losses to the original NAL and recovery frames."""
import argparse
from collections import Counter
from hashlib import sha256
import json
from pathlib import Path

from g0_video_air_stream import HEADER, S


def review(capture, source):
    raw = source.read_bytes()
    offered = capture['offered_SOURCE_symbols']
    missing = set(capture['video_missing_SOURCE_ordinals'])
    if (not 0 < offered <= 1200 * 20 or len(raw) != offered * S
            or len(missing) != capture['not_delivered_SOURCE_symbols']
            or any(not 0 <= item < offered for item in missing)):
        raise ValueError('SOURCE size or missing identity census differs')
    nals = {}
    for ordinal in range(offered):
        symbol = raw[ordinal * S:(ordinal + 1) * S]
        if not any(symbol):
            continue
        frame, pts, nal, _, _, _, index, count, kind, *_ = HEADER.unpack_from(symbol)
        key = (frame, nal, kind)
        unit = nals.setdefault(key, {'count': count, 'pts_us': pts, 'seen': set(), 'missing': []})
        if (unit['count'] != count or unit['pts_us'] != pts or index >= count or index in unit['seen']):
            raise ValueError('Original NAL identities or fragment counts differ')
        unit['seen'].add(index)
        if ordinal in missing:
            unit['missing'].append(ordinal)
    if any(len(unit['seen']) != unit['count'] for unit in nals.values()):
        raise ValueError('Incomplete original NAL fixture')
    totals = Counter()
    affected = Counter()
    fragments = Counter()
    recovery = {}
    for (frame, _, kind), unit in sorted(nals.items()):
        totals[kind] += 1
        affected[kind] += bool(unit['missing'])
        fragments[kind] += len(unit['missing'])
        if kind in (5, 7, 8):
            row = recovery.setdefault(frame, {'frame': frame, 'pts_us': unit['pts_us'],
                                               'offered_by_NAL_kind': Counter(), 'damaged_by_NAL_kind': Counter(),
                                               'missing_SOURCE_ordinals': []})
            row['offered_by_NAL_kind'][kind] += 1
            row['damaged_by_NAL_kind'][kind] += bool(unit['missing'])
            row['missing_SOURCE_ordinals'].extend(unit['missing'])
    refresh = [row for row in recovery.values() if row['offered_by_NAL_kind'][5]]
    complete = [row for row in refresh if {5, 7, 8} <= row['offered_by_NAL_kind'].keys()
                and not row['missing_SOURCE_ordinals']]
    return {'scope': __doc__, 'original_SOURCE_sha256': sha256(raw).hexdigest(),
            'offered_SOURCE': offered, 'missing_SOURCE': len(missing),
            'missing_nonpadding_SOURCE': sum(fragments.values()),
            'NAL_units_by_kind': dict(totals), 'affected_NAL_units_by_kind': dict(affected),
            'missing_fragments_by_NAL_kind': dict(fragments), 'scheduled_IDR_frames': len(refresh),
            'fully_delivered_SPS_PPS_and_all_IDR_slices': len(complete),
            'complete_recovery_frames': [row['frame'] for row in complete],
            'max_source_pts_gap_between_complete_recovery_us': max(
                (b['pts_us'] - a['pts_us'] for a, b in zip(complete, complete[1:])), default=None),
            'recovery_frame_details': refresh,
            'limits': 'Recovery completeness is a diagnostic count. The receiver still handles partial AUs. '
                      'SOURCE capture does not independently observe HDMI or establish playout causality.'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--capture-review', type=Path, required=True)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = review(json.loads(args.capture_review.read_text()), args.source)
    with args.output.open('x') as output:
        json.dump(result, output, indent=2)
        output.write('\n')
