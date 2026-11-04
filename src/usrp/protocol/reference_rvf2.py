"""Standard-library ZVID2/RVF2 fragmentation reference; no codec or RF IO."""

import argparse
import hashlib
import json
import struct
import unittest
import zlib
from pathlib import Path

ZVID = struct.Struct("<4sBBHIHHIIIQ")
RVF = struct.Struct("<4sBBHIIIII")


def encode(annexb, *, session_id, sequence, timestamp_ms, flags):
    # Caller owns real codec/AU validation. This reference checks byte framing,
    # not that arbitrary supplied bytes decode into a constrained-baseline AU.
    for value in (session_id, sequence):
        if type(value) is not int or not 0 <= value <= 0xFFFFFFFF:
            raise ValueError("session/sequence must be uint32")
    if type(timestamp_ms) is not int or not 0 <= timestamp_ms <= 0x7FFFFFFFFFFFFFFF:
        raise ValueError("nonnegative signed-64 source timestamp required")
    if type(flags) is not int or flags & ~7:
        raise ValueError("invalid AU flags")
    if type(annexb) is not bytes or not 1 <= len(annexb) <= 4 * 1024 * 1024:
        raise ValueError("bounded immutable Annex-B required")
    envelope = (
        ZVID.pack(
            b"ZV2D",
            2,
            2,
            flags,
            sequence,
            1024,
            600,
            1228800,
            len(annexb),
            zlib.crc32(annexb),
            timestamp_ms,
        )
        + annexb
    )
    if (len(envelope) + 99) // 100 > 16384:
        raise ValueError("AU exceeds default fragment-count capacity")
    crc = zlib.crc32(envelope)
    fragments = []
    for offset in range(0, len(envelope), 100):
        part = envelope[offset : offset + 100]
        fragments.append(
            RVF.pack(
                b"RVF2",
                2,
                0,
                len(part),
                session_id,
                sequence,
                len(envelope),
                offset,
                crc,
            )
            + part
            + bytes(100 - len(part))
        )
    return envelope, tuple(fragments)


def reconstruct(fragments):
    if not fragments:
        raise ValueError("empty fragment set")
    parts = {}
    identity = None
    for fragment in fragments:
        if len(fragment) != 128:
            raise ValueError("RVF2 payload must be 128 bytes")
        magic, version, flags, size, session, seq, total, offset, crc = RVF.unpack_from(
            fragment
        )
        if (magic, version, flags) != (b"RVF2", 2, 0) or not 1 <= size <= 100:
            raise ValueError("bad RVF2 header")
        if offset % 100 or offset + size > total or size != min(100, total - offset):
            raise ValueError("bad RVF2 fragment geometry")
        if total <= 36 or total > min(4 * 1024 * 1024 + 36, 16384 * 100):
            raise ValueError("invalid AU envelope size")
        if any(fragment[28 + size :]):
            raise ValueError("nonzero RVF2 padding")
        key = session, seq, total, crc
        if identity is None:
            identity = key
        if identity != key:
            raise ValueError("mixed RVF2 identities")
        part = fragment[28 : 28 + size]
        if offset in parts and parts[offset] != part:
            raise ValueError("conflicting duplicate")
        parts[offset] = part
    if set(parts) != set(range(0, identity[2], 100)):
        raise ValueError("incomplete AU")
    envelope = b"".join(parts[offset] for offset in sorted(parts))
    if zlib.crc32(envelope) != identity[3]:
        raise ValueError("envelope CRC mismatch")
    fields = ZVID.unpack_from(envelope)
    if fields[:3] != (b"ZV2D", 2, 2) or fields[4] != identity[1]:
        raise ValueError("bad ZVID2 identity")
    if fields[3] & ~7 or fields[5:8] != (1024, 600, 1228800):
        raise ValueError("bad ZVID2 flags/geometry")
    if fields[10] > 0x7FFFFFFFFFFFFFFF:
        raise ValueError("nonnegative signed-64 source timestamp required")
    annexb = envelope[36:]
    if len(annexb) != fields[8] or zlib.crc32(annexb) != fields[9]:
        raise ValueError("Annex-B CRC/length mismatch")
    return envelope


def generate(
    annexb_path, output, *, session_id=714, sequence=0, timestamp_ms=0, flags=7
):
    annexb = annexb_path.read_bytes()
    envelope, fragments = encode(
        annexb,
        session_id=session_id,
        sequence=sequence,
        timestamp_ms=timestamp_ms,
        flags=flags,
    )
    assert reconstruct(tuple(reversed(fragments)) + fragments[-1:]) == envelope
    output.mkdir(parents=True, exist_ok=False)
    files = {
        "au.h264": annexb,
        "au.zv2d": envelope,
        "fragments-128b.bin": b"".join(fragments),
    }
    entries = []
    for name, data in files.items():
        (output / name).write_bytes(data)
        entries.append(
            {
                "path": name,
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    manifest = {
        "schema": "rvf2-zvid2-reference-v1",
        "session_id": session_id,
        "video_sequence": sequence,
        "source_timestamp_ms": timestamp_ms,
        "flags": flags,
        "width": 1024,
        "height": 600,
        "fragments": len(fragments),
        "fragment_bytes": 128,
        "paired_rf_frames": (len(fragments) + 1) // 2,
        "odd_fragment_pairing": "repeat final immutable fragment on stream1",
        "scope": "fragmentation/CRC reference, not a codec or radio test",
        "artifacts": entries,
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )


def verify_example(output):
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    if manifest["schema"] != "rvf2-zvid2-reference-v1":
        raise ValueError("unexpected video manifest schema")
    expected = {"au.h264", "au.zv2d", "fragments-128b.bin"}
    if (
        len(manifest["artifacts"]) != 3
        or {row["path"] for row in manifest["artifacts"]} != expected
    ):
        raise ValueError("unexpected video artifacts")
    files = {}
    for row in manifest["artifacts"]:
        data = (output / row["path"]).read_bytes()
        if (
            len(data) != row["bytes"]
            or hashlib.sha256(data).hexdigest() != row["sha256"]
        ):
            raise ValueError("video artifact hash/length mismatch")
        files[row["path"]] = data
    envelope, fragments = encode(
        files["au.h264"],
        session_id=manifest["session_id"],
        sequence=manifest["video_sequence"],
        timestamp_ms=manifest["source_timestamp_ms"],
        flags=manifest["flags"],
    )
    if (
        envelope != files["au.zv2d"]
        or b"".join(fragments) != files["fragments-128b.bin"]
        or manifest["fragments"] != len(fragments)
        or manifest["fragment_bytes"] != 128
        or manifest["paired_rf_frames"] != (len(fragments) + 1) // 2
        or (manifest["width"], manifest["height"]) != (1024, 600)
    ):
        raise ValueError("video regeneration/metadata mismatch")
    if reconstruct(tuple(reversed(fragments)) + fragments[-1:]) != envelope:
        raise ValueError("video reassembly mismatch")
    print(
        "RVF2_VIDEO_EXAMPLE_VERIFY_PASS: hashes + regeneration + reassembly; no codec/RF"
    )


class FragmentTests(unittest.TestCase):
    def test_high_bit_timestamp_rejected_even_with_valid_crc(self):
        envelope, fragments = encode(
            b"format-only-test", session_id=1, sequence=0, timestamp_ms=0, flags=0
        )
        bad = bytearray(envelope)
        struct.pack_into("<Q", bad, 28, 1 << 63)
        fragment = bytearray(fragments[0])
        fragment[28 : 28 + len(bad)] = bad
        struct.pack_into("<I", fragment, 24, zlib.crc32(bad))
        with self.assertRaisesRegex(ValueError, "timestamp"):
            reconstruct((bytes(fragment),))

    def test_crc_identity_padding_and_idempotent_duplicate(self):
        data = b"\0\0\0\1\x09\xf0" + b"format-only-test" * 13
        envelope, fragments = encode(
            data, session_id=714, sequence=9, timestamp_ms=123, flags=7
        )
        self.assertEqual(
            reconstruct(tuple(reversed(fragments)) + fragments[-1:]), envelope
        )
        self.assertEqual(len(envelope), 36 + len(data))
        self.assertTrue(all(len(part) == 128 for part in fragments))

    def test_missing_corrupt_and_mixed_fragments_rejected(self):
        _, fragments = encode(
            bytes(range(256)), session_id=1, sequence=0, timestamp_ms=0, flags=0
        )
        with self.assertRaisesRegex(ValueError, "incomplete"):
            reconstruct(fragments[:-1])
        broken = bytearray(fragments[0])
        broken[29] ^= 1
        with self.assertRaises(ValueError):
            reconstruct((bytes(broken),) + fragments[1:])
        mixed = bytearray(fragments[1])
        mixed[8] ^= 1
        with self.assertRaisesRegex(ValueError, "identities"):
            reconstruct((fragments[0], bytes(mixed)) + fragments[2:])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--verify-example", type=Path)
    parser.add_argument("--annexb", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.verify_example is not None:
        verify_example(args.verify_example)
        raise SystemExit(0)
    if args.self_test:
        result = unittest.TextTestRunner(verbosity=2).run(
            unittest.defaultTestLoader.loadTestsFromTestCase(FragmentTests)
        )
        raise SystemExit(0 if result.wasSuccessful() else 1)
    if args.annexb is None or args.output is None:
        parser.error("--annexb and --output are required unless --self-test")
    generate(args.annexb, args.output)
