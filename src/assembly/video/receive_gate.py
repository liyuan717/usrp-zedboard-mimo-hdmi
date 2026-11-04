"""Validate this run's public receive records, without a TX payload or ledger."""
import hashlib
import struct
import zlib


def check_native(native, ids):
    count = len(ids)
    if count not in range(128, 132) or ids != [1] * (count - 127) + list(range(2, 129)):
        raise ValueError('RF records missing, repeated or reordered')
    if native.get('captured_blocks') != 2048 or native.get('crc_rejected_frames') != 0:
        raise ValueError('ADC capture or PL CRC failure')
    if native.get('detected_frames') != count or native.get('delivered_frames') != count:
        raise ValueError('native detected/file delivery count mismatch')
    delivery = native.get('delivery', {})
    if delivery.get('mode') != 'file' or delivery.get('ipc_dropped_records') != 0:
        raise ValueError('wrong delivery mode or drop')


def validate_received(native, records):
    check_native(native, [r.frame_id for r in records])
    groups = {}
    for record in records:
        if record.rejected or record.build_id != 1 or len(record.packets) != 2:
            raise ValueError('rejected record or wrong PL build')
        for lane, packet in enumerate(record.packets):
            if len(packet) != 152:
                raise ValueError('MRF packet size')
            if struct.unpack_from('<4sBBBBII', packet) != (b'MRF1', 1, lane, 1, 0, record.frame_id, 128):
                raise ValueError('MRF header identity')
            if zlib.crc32(packet[:16]) != struct.unpack_from('<I', packet, 16)[0] or zlib.crc32(packet[20:148]) != struct.unpack_from('<I', packet, 148)[0]:
                raise ValueError('MRF packet CRC')
            p = packet[20:148]
            magic, version, flags, size, session, seq, total, offset, crc = struct.unpack_from('<4sBBHIIIII', p)
            if (magic, version, session) != (b'RVF2', 2, 714) or seq not in range(10):
                raise ValueError('RVF2 public identity')
            if not 36 <= total <= 65536 or not 1 <= size <= 100 or offset + size > total:
                raise ValueError('RVF2 bounds')
            if seq not in groups:
                groups[seq] = (bytearray(total), bytearray(total), crc)
            data, seen, group_crc = groups[seq]
            if len(data) != total or group_crc != crc:
                raise ValueError('conflicting envelope')
            for at, value in enumerate(p[28:28 + size], offset):
                if seen[at] and data[at] != value:
                    raise ValueError('conflicting duplicate fragment')
                data[at] = value
                seen[at] = 1
    if sorted(groups) != list(range(10)):
        raise ValueError('missing video AU')
    result = []
    for seq in range(10):
        data, seen, crc = groups[seq]
        if not all(seen) or zlib.crc32(data) != crc:
            raise ValueError('incomplete or corrupt AU')
        magic, version, codec, flags, source_seq, width, height, raw_size, size, crc, pts = struct.unpack_from('<4sBBHIHHIIIQ', data)
        h264 = data[36:]
        if (magic, version, codec, source_seq, width, height, raw_size, pts) != (b'ZV2D', 2, 2, seq, 1024, 600, 1228800, seq * 200):
            raise ValueError('ZVID2 public identity or PTS')
        if size != len(h264) or zlib.crc32(h264) != crc:
            raise ValueError('encoded size or CRC')
        result.append(dict(sequence=seq, PTS_ms=pts, flags=flags, H264_bytes=size,
                           H264_sha256=hashlib.sha256(h264).hexdigest()))
    return result
