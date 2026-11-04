#!/usr/bin/env python3
"""Read bounded RFD1/RFE1 native RX records; no radio or DMA is opened.

Local record header is LE <4sIIII>: magic, RF frame ID, IMBC build ID,
payload bytes, CRC32(header first 16 bytes + payload). RFD1 has exactly two
152-byte MRF1 packets; RFE1 has no payload and invalidates video assembly.
These storage headers are not part of the over-air protocol.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import struct
from typing import BinaryIO, Iterator
import zlib


HEADER = struct.Struct("<4sIIII")
PACKET_BYTES = 152
MAX_RECORD_BYTES = HEADER.size + 2 * PACKET_BYTES


@dataclass(frozen=True)
class DeliveryRecord:
    frame_id: int
    build_id: int
    rejected: bool
    packets: tuple[bytes, ...]


def _read_exact(stream: BinaryIO, size: int) -> bytes:
    body = bytearray()
    while len(body) < size:
        piece = stream.read(size - len(body))
        if not piece:
            raise ValueError("partial native delivery record")
        body.extend(piece)
    return bytes(body)


def _check_packet(packet: bytes, stream_id: int, frame_id: int) -> None:
    if packet[:8] != b"MRF1" + bytes((1, stream_id, 1, 0)):
        raise ValueError("MRF1 packet identity mismatch")
    if struct.unpack_from("<II", packet, 8) != (frame_id, 128):
        raise ValueError("MRF1 frame/length differs from delivery record")
    if struct.unpack_from("<I", packet, 16)[0] != zlib.crc32(packet[:16]):
        raise ValueError("MRF1 header CRC mismatch")
    if struct.unpack_from("<I", packet, 148)[0] != zlib.crc32(packet[20:148]):
        raise ValueError("MRF1 payload CRC mismatch")


def parse_delivery_datagram(raw: bytes) -> DeliveryRecord:
    """Validate exactly one complete native AF_UNIX datagram.

    Datagram boundaries are part of the local IPC contract.  This parser does
    not buffer, concatenate, or recover a partial record: doing so would turn
    a truncated datagram into ambiguous RF delivery ownership.
    """
    if type(raw) is not bytes or not HEADER.size <= len(raw) <= MAX_RECORD_BYTES:
        raise ValueError("native delivery datagram length is outside the fixed bound")
    magic, frame_id, build_id, size, crc = HEADER.unpack_from(raw)
    if magic not in (b"RFD1", b"RFE1") or build_id == 0:
        raise ValueError("unknown native delivery identity/build")
    rejected = magic == b"RFE1"
    expected = 0 if rejected else 2 * PACKET_BYTES
    if size != expected or len(raw) != HEADER.size + expected:
        raise ValueError("native delivery datagram has invalid exact payload length")
    body = raw[HEADER.size:]
    if zlib.crc32(body, zlib.crc32(raw[:16])) != crc:
        raise ValueError("native delivery record CRC mismatch")
    packets = () if rejected else (body[:PACKET_BYTES], body[PACKET_BYTES:])
    for stream_id, packet in enumerate(packets):
        _check_packet(packet, stream_id, frame_id)
    return DeliveryRecord(frame_id, build_id, rejected, packets)


def iter_delivery(stream: BinaryIO) -> Iterator[DeliveryRecord]:
    while True:
        first = stream.read(1)
        if first == b"":
            return
        head = first + _read_exact(stream, HEADER.size - 1)
        # Bound the file-mode allocation before reading.  ``parse_delivery_datagram``
        # then applies the shared identity/CRC/packet validation.
        magic, _frame_id, _build_id, size, _crc = HEADER.unpack(head)
        if magic not in (b"RFD1", b"RFE1") or size > 2 * PACKET_BYTES:
            raise ValueError("native delivery file record length is invalid")
        yield parse_delivery_datagram(head + _read_exact(stream, size))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    args = parser.parse_args()
    valid = rejected = payload_bytes = 0
    try:
        with args.input.open("rb") as stream:
            for record in iter_delivery(stream):
                if record.rejected:
                    rejected += 1
                else:
                    valid += 1
                    payload_bytes += 256
    except (OSError, ValueError) as error:
        parser.exit(2, f"invalid RF delivery capture: {error}\n")
    print(json.dumps({
        "schema": "rxonly-local-delivery-audit-v1",
        "valid_rf_frames": valid,
        "crc_rejected_rf_frames": rejected,
        "mrf_payload_bytes": payload_bytes,
        "video_frame_rate_verified": False,
        "hardware_verified": False,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
