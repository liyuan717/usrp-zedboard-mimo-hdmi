"""Bounded wire codec for video_codec_helper's tagged 72-byte ABI.

No subprocess or hardware is opened. A process owner must impose I/O
deadlines, drain stderr, and retire the process on framing/mapping failure.
Outputs carry decoder-returned identities, never inferred FIFO identities.
"""
from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass

from .rf_encoded_video import validate_access_unit

HEADER = struct.Struct("<4sHHIIQQQqIIIIII")
MAX_AU_BYTES = 4 * 1024 * 1024
WIDTH, HEIGHT = 1024, 600
RGB_BYTES = WIDTH * HEIGHT * 2
FRAME, INPUT_ERROR, DECODE_ERROR, FLUSHED, FLUSH_ACK, FATAL, STOP_ACK = range(1, 8)


class CodecIPCError(ValueError):
    pass


@dataclass(frozen=True)
class CodecIdentity:
    session_id: int
    sequence: int
    token: int
    source_pts: int

    def __post_init__(self):
        for name, value, maximum in (
            ("session", self.session_id, 0xFFFFFFFF),
            ("sequence", self.sequence, 0xFFFFFFFF),
            ("token", self.token, 0x7FFFFFFFFFFFFFFF),
            ("source_pts", self.source_pts, 0x7FFFFFFFFFFFFFFF),
        ):
            if type(value) is not int or not 0 <= value <= maximum:
                raise CodecIPCError(f"invalid {name} in codec identity")


@dataclass(frozen=True)
class CodecOutputHeader:
    status: int
    flags: int
    identity: CodecIdentity
    width: int
    height: int
    pixel_format: int
    payload_bytes: int
    payload_crc: int


def encode_decode(au, token):
    inspection = validate_access_unit(au, max_au_bytes=MAX_AU_BYTES)
    if (au.width, au.height) != (WIDTH, HEIGHT):
        raise CodecIPCError("helper requires 1024x600")
    if type(token) is not int or not 1 <= token <= 0x7FFFFFFFFFFFFFFF:
        raise CodecIPCError("token must fit positive AVPacket signed pts")
    if au.source_timestamp_ms > 0x7FFFFFFFFFFFFFFF:
        raise CodecIPCError("source timestamp does not fit helper signed pts")
    identity = CodecIdentity(au.session_id, au.sequence, token, au.source_timestamp_ms)
    header = HEADER.pack(b"VCDI", 1, HEADER.size, 1,
                         int(inspection.structural_self_contained_idr),
                         identity.session_id, identity.sequence, token, identity.source_pts,
                         WIDTH, HEIGHT, 1, len(au.annexb), zlib.crc32(au.annexb), 0)
    return identity, header + au.annexb


def encode_control(command):
    if type(command) is not int or command not in (2, 3):
        raise CodecIPCError("only FLUSH=2 and STOP=3 controls exist")
    return HEADER.pack(b"VCDI", 1, HEADER.size, command, *([0] * 11))


def parse_output_header(raw):
    """Call BEFORE allocating/reading the advertised payload."""
    if type(raw) is not bytes or len(raw) != HEADER.size:
        raise CodecIPCError("short or nonimmutable helper header")
    (magic, version, size, status, flags, session, sequence, token, pts,
     width, height, pixfmt, length, crc, reserved) = HEADER.unpack(raw)
    if (magic, version, size, reserved) != (b"VCDO", 1, HEADER.size, 0):
        raise CodecIPCError("helper header ABI mismatch")
    if status not in range(1, 8) or flags & ~1:
        raise CodecIPCError("unknown helper status/flags")
    if flags != (0 if status == FRAME else 1):
        raise CodecIPCError("helper status and NEED_IDR flags disagree")
    if status == FRAME:
        if flags or (width, height, pixfmt, length) != (WIDTH, HEIGHT, 1, RGB_BYTES):
            raise CodecIPCError("invalid decoded image geometry/length/flags")
    elif any((width, height, pixfmt, length, crc)):
        raise CodecIPCError("status record must not contain image payload")
    if status in (FLUSH_ACK, STOP_ACK):
        if any((session, sequence, token, pts)):
            raise CodecIPCError("control ACK must not impersonate a frame identity")
    elif status != FATAL and not 1 <= token <= 0x7FFFFFFFFFFFFFFF:
        raise CodecIPCError("invalid decoder token")
    return CodecOutputHeader(status, flags, CodecIdentity(session, sequence, token, pts),
                             width, height, pixfmt, length, crc)


class CodecOutputTracker:
    """Map actual returned tokens against bounded, explicitly reserved inputs.

    Reserve before a write to avoid a response racing registration. If that
    write fails, call poison and retire the subprocess; do not replay it.
    Terminal non-frame records retire a token without claiming decoded output.
    """
    def __init__(self, max_pending=16):
        if type(max_pending) is not int or not 1 <= max_pending <= 16:
            raise ValueError("max_pending must be 1..16")
        self.max_pending = max_pending
        self.pending = {}
        self.last_token = 0
        self.failed = False
        self.frames = 0
        self.suppressed_frames = 0
        self.control_pending = None
        self.stopped = False

    def poison(self):
        self.failed = True

    def reserve(self, identity):
        if self.failed:
            raise CodecIPCError("tracker failed; retire process")
        if self.stopped or self.control_pending is not None:
            raise CodecIPCError("cannot submit while stopping/flushing or stopped")
        if (type(identity) is not CodecIdentity or
                not self.last_token < identity.token <= 0x7FFFFFFFFFFFFFFF):
            raise CodecIPCError("tokens must be strictly increasing")
        if len(self.pending) >= self.max_pending:
            raise CodecIPCError("decoder pending token limit reached")
        self.pending[identity.token] = identity
        self.last_token = identity.token

    def begin_control(self, command):
        """Register expected ACK before writing control bytes to the helper.

        Outputs that arrive while control_pending is set belong to the old
        epoch and must not be presented, even if they are valid FRAME records.
        """
        if self.failed or self.stopped or self.control_pending is not None:
            raise CodecIPCError("control unavailable in current state")
        wire = encode_control(command)
        self.control_pending = FLUSH_ACK if command == 2 else STOP_ACK
        return wire

    def consume(self, header_bytes, payload=b""):
        if self.failed:
            raise CodecIPCError("tracker failed; retire process")
        try:
            header = parse_output_header(header_bytes)
            if type(payload) is not bytes or len(payload) != header.payload_bytes:
                raise CodecIPCError("short/overlong helper payload")
            if header.status == FATAL:
                raise CodecIPCError("helper reported FATAL")
            if header.status in (FLUSH_ACK, STOP_ACK):
                if header.status != self.control_pending:
                    raise CodecIPCError("unexpected control acknowledgement")
                if self.pending:
                    raise CodecIPCError("ACK before pending tokens retired")
                self.control_pending = None
                self.stopped = header.status == STOP_ACK
                return header
            if self.pending.get(header.identity.token) != header.identity:
                raise CodecIPCError("unknown/duplicate/mismatched output identity")
            if header.status == FRAME and zlib.crc32(payload) != header.payload_crc:
                raise CodecIPCError("decoded pixel CRC mismatch")
            del self.pending[header.identity.token]
            if header.status == FRAME and self.control_pending is not None:
                # Valid but retired epoch: consume bytes/token, never hand a
                # presentable frame to the owner after reset/stop began.
                self.suppressed_frames += 1
                return None
            self.frames += int(header.status == FRAME)
            return header
        except BaseException:
            self.poison()
            raise
