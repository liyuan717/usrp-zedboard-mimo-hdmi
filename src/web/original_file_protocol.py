"""Opaque original-file payloads inside unchanged 128-byte MRF1 payloads.
No video decoder/encoder. Receiver reconstructs only received bytes.
"""
import hashlib
import os
from pathlib import Path
import struct
import zlib

SIZE = 128
HEADER = struct.Struct('<4s16sQI')
DATA = SIZE - HEADER.size - 4
MAX_FILE = 16 * 1024 * 1024


def manifest(path, session):
    path = Path(path)
    if len(session) != 16 or not 0 < path.stat().st_size <= MAX_FILE:
        raise ValueError('invalid file size or session')
    digest = hashlib.sha256()
    with path.open('rb') as source:
        while chunk := source.read(1 << 20):digest.update(chunk)
    body = struct.pack('<4s16sQ32s', b'OFM1', session, path.stat().st_size, digest.digest())
    return body.ljust(124, b'\0') + struct.pack('<I', zlib.crc32(body.ljust(124, b'\0')))


def chunks(path, session):
    offset = 0
    with Path(path).open('rb') as source:
        while raw := source.read(DATA):
            body = HEADER.pack(b'OFC1', session, offset, len(raw)) + raw.ljust(DATA, b'\0')
            yield body + struct.pack('<I', zlib.crc32(body))
            offset += len(raw)


def checked(payload):
    if len(payload) != SIZE or zlib.crc32(payload[:124]) != struct.unpack_from('<I', payload, 124)[0]:
        raise ValueError('original-file payload CRC or length mismatch')
    return payload[:124]


class Assembler:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=False)
        self.path = self.directory / 'received.partial'
        self.file = self.path.open('x+b')
        self.meta = None
        self.offset = 0
        self.completed = False

    def accept(self, payload):
        body = checked(payload)
        if body[:4] == b'OFM1':
            magic, session, length, digest = struct.unpack_from('<4s16sQ32s', body)
            if not 0 < length <= MAX_FILE or any(body[60:]):raise ValueError('invalid manifest')
            meta = session, length, digest
            if self.meta is not None and meta != self.meta:raise ValueError('different transfer manifest')
            self.meta = meta
            return
        if body[:4] != b'OFC1' or self.meta is None:raise ValueError('missing manifest')
        magic, session, offset, size = HEADER.unpack_from(body)
        if session != self.meta[0] or not 0 < size <= DATA or offset + size > self.meta[1] or offset % DATA:
            raise ValueError('chunk bounds or session mismatch')
        if size != min(DATA, self.meta[1] - offset) or any(body[HEADER.size+size:]):
            raise ValueError('chunk length/padding mismatch')
        raw = body[HEADER.size:HEADER.size+size]
        if offset < self.offset:
            self.file.seek(offset)
            if offset+size > self.offset or self.file.read(size) != raw:raise ValueError('conflicting duplicate')
            return
        if offset != self.offset:raise ValueError('missing or out-of-order chunk')
        self.file.seek(offset)
        self.file.write(raw)
        self.offset += size

    def finish(self):
        if self.completed or self.meta is None or self.offset != self.meta[1]:raise ValueError('incomplete file')
        self.file.flush()
        self.file.seek(0)
        h = hashlib.sha256()
        while raw := self.file.read(1<<20):h.update(raw)
        if h.digest() != self.meta[2]:raise ValueError('final original-file SHA256 mismatch')
        self.file.close()
        target = self.directory / 'received-original'
        self.path.rename(target)
        target.chmod(0o400)
        self.completed = True
        return dict(bytes=self.offset, sha256=h.hexdigest(), path=str(target), original_file_verified=True,
                    RF_verified=False, HDMI_verified=False)

    def close(self):
        if not self.file.closed:self.file.close()
