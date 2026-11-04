"""Independent ZVID2/RVF2 wire protocol for H.264 Annex-B access units.

This module deliberately does *not* change ZVID/RVF1 or invoke a codec.
NAL scanning is a structural admission check only.  It can establish that an
AU contains AUD/SPS/PPS/IDR NAL types, but cannot prove constrained-baseline,
absence of B pictures, progressive coding, or that the bytes decode to exactly
one picture.  A real codec/parser remains the final gate before display.
"""

from __future__ import annotations

import math
import struct
import time
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field

from . import waveform

ZVID2_MAGIC = b"ZV2D"
ZVID2_VERSION = 2
H264_ANNEXB_BASELINE = 2
ZVID2_FLAGS_IDR = 1 << 0
ZVID2_FLAGS_SPS = 1 << 1
ZVID2_FLAGS_PPS = 1 << 2
ZVID2_FLAGS_MASK = ZVID2_FLAGS_IDR | ZVID2_FLAGS_SPS | ZVID2_FLAGS_PPS
ZVID2_HEADER = struct.Struct("<4sBBHIHHIIIQ")

RVF2_MAGIC = b"RVF2"
RVF2_VERSION = 2
RVF2_HEADER = struct.Struct("<4sBBHIIIII")
DEFAULT_RF_PAYLOAD_BYTES = 128
MAX_RF_PAYLOAD_BYTES = 4096
DEFAULT_MAX_AU_BYTES = 4 * 1024 * 1024
DEFAULT_MAX_FRAGMENTS = 16_384
MAX_DIMENSION = 1920


class EncodedVideoProtocolError(ValueError):
    """Malformed or out-of-contract ZVID2/RVF2 input."""


@dataclass(frozen=True, order=True)
class EncodedVideoIdentity:
    """Identity scope for one encoded AU; radio frame IDs are not video IDs."""

    session_id: int
    sequence: int


@dataclass(frozen=True)
class AnnexBInspection:
    """Structural NAL result; ``codec_verified`` is always false here."""

    nal_types: tuple[int, ...]
    has_aud: bool
    has_sps: bool
    has_pps: bool
    has_idr: bool
    has_vcl: bool
    structural_self_contained_idr: bool
    codec_verified: bool = False


@dataclass(frozen=True)
class EncodedAccessUnit:
    """One immutable H.264 Annex-B AU carried by a ZVID2 envelope."""

    session_id: int
    sequence: int
    width: int
    height: int
    source_timestamp_ms: int
    annexb: bytes
    flags: int

    @property
    def inspection(self) -> AnnexBInspection:
        return inspect_annexb(self.annexb)

    @property
    def nal_inspection(self) -> AnnexBInspection:
        """Explicit dispatcher-facing name for the structural scan result."""
        return self.inspection

    @property
    def self_contained_idr(self) -> bool:
        """A structural candidate only; a real codec/parser must confirm it."""
        return self.inspection.structural_self_contained_idr


@dataclass(frozen=True)
class ReassembledAccessUnit:
    identity: EncodedVideoIdentity
    metadata: EncodedAccessUnit
    annexb: bytes


@dataclass(frozen=True)
class EncodedReassemblyOutcome:
    status: str
    reason: str | None = None
    access_unit: ReassembledAccessUnit | None = None
    expired_identities: tuple[EncodedVideoIdentity, ...] = ()


@dataclass(frozen=True)
class EncodedReassemblyStats:
    accepted_fragments: int
    duplicate_fragments: int
    rejected_fragments: int
    expired_access_units: int
    completed_access_units: int
    active_access_units: int
    active_bytes: int
    completed_identities: int
    bound_session_id: int
    fixed_width: int | None
    fixed_height: int | None


@dataclass
class _ActiveAU:
    identity: EncodedVideoIdentity
    total_len: int
    crc32: int
    created_at: float
    updated_at: float
    fragments: dict[int, bytes] = field(default_factory=dict)
    received_bytes: int = 0


@dataclass(frozen=True)
class _CompletedAU:
    total_len: int
    crc32: int
    completed_at: float


def _uint(value: int, name: str, bits: int) -> int:
    if type(value) is not int or not 0 <= value < 1 << bits:
        raise EncodedVideoProtocolError(f"{name} must be an unsigned {bits}-bit integer")
    return value


def _payload_limit(rf_payload_bytes: int) -> int:
    if type(rf_payload_bytes) is not int or not 1 <= rf_payload_bytes <= MAX_RF_PAYLOAD_BYTES:
        raise EncodedVideoProtocolError("rf_payload_bytes must be in 1..4096")
    result = rf_payload_bytes - RVF2_HEADER.size
    if result < 1:
        raise EncodedVideoProtocolError("RF payload has no RVF2 fragment data room")
    return result


def _validate_dimensions(width: int, height: int, raw_size: int) -> None:
    if (
        type(width) is not int
        or type(height) is not int
        or not (1 <= width <= MAX_DIMENSION and 1 <= height <= MAX_DIMENSION)
    ):
        raise EncodedVideoProtocolError("invalid encoded frame dimensions")
    expected = width * height * 2
    if expected > DEFAULT_MAX_AU_BYTES or raw_size != expected:
        raise EncodedVideoProtocolError("ZVID2 RGB565 source size/dimensions mismatch")


def _start_codes(data: bytes) -> list[tuple[int, int]]:
    positions: list[tuple[int, int]] = []
    index = 0
    while True:
        # bytes.find scans in native code instead of allocating two Python
        # slices per payload byte. Preserve the old greedy 3/4-byte grammar:
        # in a run of >=3 zeros the last three zeros belong to the 4-byte
        # prefix, and any earlier zeros remain outside the marker.
        found = data.find(b"\0\0\1", index)
        if found < 0:
            break
        if found > index and data[found - 1] == 0:
            positions.append((found - 1, 4))
        else:
            positions.append((found, 3))
        index = found + 3
    return positions


def inspect_annexb(annexb: bytes) -> AnnexBInspection:
    """Scan Annex-B boundaries and NAL headers without pretending to parse H.264."""
    if not isinstance(annexb, bytes) or not annexb:
        raise EncodedVideoProtocolError("Annex-B access unit must be non-empty immutable bytes")
    starts = _start_codes(annexb)
    if not starts or starts[0][0] != 0:
        raise EncodedVideoProtocolError("H.264 AU must begin with an Annex-B start code")
    nal_types: list[int] = []
    for number, (offset, marker_size) in enumerate(starts):
        begin = offset + marker_size
        end = starts[number + 1][0] if number + 1 < len(starts) else len(annexb)
        if begin >= end:
            raise EncodedVideoProtocolError("empty Annex-B NAL")
        header = annexb[begin]
        if header & 0x80:
            raise EncodedVideoProtocolError("forbidden_zero_bit set in H.264 NAL")
        nal_type = header & 0x1F
        if nal_type == 0 or nal_type > 23:
            raise EncodedVideoProtocolError("unsupported/reserved H.264 NAL type")
        nal_types.append(nal_type)
    if nal_types.count(9) != 1 or nal_types[0] != 9:
        raise EncodedVideoProtocolError("AU must contain exactly one leading AUD NAL")
    has_vcl = any(1 <= item <= 5 for item in nal_types)
    if not has_vcl:
        raise EncodedVideoProtocolError("AU has no H.264 VCL NAL")
    has_sps, has_pps, has_idr = 7 in nal_types, 8 in nal_types, 5 in nal_types
    return AnnexBInspection(
        nal_types=tuple(nal_types),
        has_aud=True,
        has_sps=has_sps,
        has_pps=has_pps,
        has_idr=has_idr,
        has_vcl=has_vcl,
        structural_self_contained_idr=has_sps and has_pps and has_idr,
    )


def validate_access_unit(
    access_unit: EncodedAccessUnit,
    *,
    max_au_bytes: int = DEFAULT_MAX_AU_BYTES,
) -> AnnexBInspection:
    """Validate immutable AU metadata/Annex-B structure without encoding it.

    This is intentionally public for the ordered dispatcher.  It performs no
    envelope concatenation or codec subprocess call, so validating a large AU
    does not make an additional 4 MiB copy before later transport work.
    """
    if not isinstance(access_unit, EncodedAccessUnit):
        raise TypeError("access_unit must be EncodedAccessUnit")
    if type(max_au_bytes) is not int or not 1 <= max_au_bytes <= DEFAULT_MAX_AU_BYTES:
        raise ValueError("max_au_bytes out of range")
    _uint(access_unit.session_id, "session_id", 32)
    _uint(access_unit.sequence, "sequence", 32)
    _uint(access_unit.source_timestamp_ms, "source_timestamp_ms", 64)
    _validate_dimensions(
        access_unit.width, access_unit.height, access_unit.width * access_unit.height * 2
    )
    if type(access_unit.flags) is not int or access_unit.flags & ~ZVID2_FLAGS_MASK:
        raise EncodedVideoProtocolError("unsupported ZVID2 flags")
    if not isinstance(access_unit.annexb, bytes) or not (
        1 <= len(access_unit.annexb) <= max_au_bytes
    ):
        raise EncodedVideoProtocolError("AU byte payload is outside configured bounds")
    inspection = inspect_annexb(access_unit.annexb)
    flags = (
        (ZVID2_FLAGS_IDR if inspection.has_idr else 0)
        | (ZVID2_FLAGS_SPS if inspection.has_sps else 0)
        | (ZVID2_FLAGS_PPS if inspection.has_pps else 0)
    )
    if access_unit.flags != flags:
        raise EncodedVideoProtocolError("ZVID2 flags disagree with scanned H.264 NAL types")
    if inspection.has_idr and not inspection.structural_self_contained_idr:
        raise EncodedVideoProtocolError("IDR AU must repeat SPS and PPS")
    return inspection


def encode_access_unit(access_unit: EncodedAccessUnit) -> bytes:
    """Encode a single ZVID2 AU envelope; no decoding or subprocess is used."""
    validate_access_unit(access_unit)
    return (
        ZVID2_HEADER.pack(
            ZVID2_MAGIC,
            ZVID2_VERSION,
            H264_ANNEXB_BASELINE,
            access_unit.flags,
            access_unit.sequence,
            access_unit.width,
            access_unit.height,
            access_unit.width * access_unit.height * 2,
            len(access_unit.annexb),
            zlib.crc32(access_unit.annexb) & 0xFFFFFFFF,
            access_unit.source_timestamp_ms,
        )
        + access_unit.annexb
    )


def decode_access_unit(
    packet: bytes, *, session_id: int, max_au_bytes: int = DEFAULT_MAX_AU_BYTES
) -> EncodedAccessUnit:
    """Validate one ZVID2 packet and return an immutable AU, never RGB pixels."""
    _uint(session_id, "session_id", 32)
    if type(max_au_bytes) is not int or not 1 <= max_au_bytes <= DEFAULT_MAX_AU_BYTES:
        raise ValueError("max_au_bytes out of range")
    if not isinstance(packet, bytes) or len(packet) <= ZVID2_HEADER.size:
        raise EncodedVideoProtocolError("truncated ZVID2 access unit")
    (
        magic,
        version,
        codec,
        flags,
        sequence,
        width,
        height,
        raw_size,
        payload_size,
        crc,
        timestamp,
    ) = ZVID2_HEADER.unpack_from(packet)
    if magic != ZVID2_MAGIC or version != ZVID2_VERSION or codec != H264_ANNEXB_BASELINE:
        raise EncodedVideoProtocolError("ZVID2 magic/version/codec mismatch")
    _validate_dimensions(width, height, raw_size)
    if (
        payload_size < 1
        or payload_size > max_au_bytes
        or len(packet) != ZVID2_HEADER.size + payload_size
    ):
        raise EncodedVideoProtocolError("ZVID2 AU length mismatch")
    annexb = packet[ZVID2_HEADER.size :]
    if zlib.crc32(annexb) & 0xFFFFFFFF != crc:
        raise EncodedVideoProtocolError("ZVID2 AU CRC mismatch")
    result = EncodedAccessUnit(session_id, sequence, width, height, timestamp, annexb, flags)
    validate_access_unit(result, max_au_bytes=max_au_bytes)
    return result


def fragment_access_unit(
    access_unit: EncodedAccessUnit | bytes,
    *,
    session_id: int,
    video_frame_id: int,
    rf_payload_bytes: int = DEFAULT_RF_PAYLOAD_BYTES,
    max_fragments: int = DEFAULT_MAX_FRAGMENTS,
) -> tuple[bytes, ...]:
    """Create fixed-size RVF2 payloads from exactly one validated ZVID2 AU."""
    _uint(session_id, "session_id", 32)
    _uint(video_frame_id, "video_frame_id", 32)
    if isinstance(access_unit, EncodedAccessUnit):
        if access_unit.session_id != session_id:
            raise EncodedVideoProtocolError("AU session differs from RVF2 session")
        packet = encode_access_unit(access_unit)
    elif isinstance(access_unit, bytes):
        packet = access_unit
        decoded = decode_access_unit(packet, session_id=session_id)
        if decoded.sequence != video_frame_id:
            raise EncodedVideoProtocolError("RVF2 identity differs from ZVID2 sequence")
    else:
        raise TypeError("access_unit must be EncodedAccessUnit or encoded bytes")
    decoded = decode_access_unit(packet, session_id=session_id)
    if decoded.sequence != video_frame_id:
        raise EncodedVideoProtocolError("RVF2 identity differs from ZVID2 sequence")
    data_size = _payload_limit(rf_payload_bytes)
    count = (len(packet) + data_size - 1) // data_size
    if type(max_fragments) is not int or not 1 <= count <= max_fragments:
        raise EncodedVideoProtocolError("AU exceeds configured RVF2 fragment limit")
    crc = zlib.crc32(packet) & 0xFFFFFFFF
    result = []
    for offset in range(0, len(packet), data_size):
        data = packet[offset : offset + data_size]
        result.append(
            RVF2_HEADER.pack(
                RVF2_MAGIC,
                RVF2_VERSION,
                0,
                len(data),
                session_id,
                video_frame_id,
                len(packet),
                offset,
                crc,
            )
            + data
            + b"\0" * (data_size - len(data))
        )
    return tuple(result)


class RFEncodedVideoReassembler:
    """Bounded RVF2 reassembly. Ordering/WAIT_IDR belongs to the dispatcher."""

    def __init__(
        self,
        *,
        expected_session_id: int,
        rf_payload_bytes: int = DEFAULT_RF_PAYLOAD_BYTES,
        fragment_timeout_s: float = 2.0,
        max_frames: int = 32,
        max_bytes: int = DEFAULT_MAX_AU_BYTES,
        max_au_bytes: int = DEFAULT_MAX_AU_BYTES,
        max_fragments: int = DEFAULT_MAX_FRAGMENTS,
        max_sessions: int = 1,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._data_size = _payload_limit(rf_payload_bytes)
        _uint(expected_session_id, "expected_session_id", 32)
        if (
            type(fragment_timeout_s) not in (int, float)
            or not math.isfinite(fragment_timeout_s)
            or fragment_timeout_s <= 0
        ):
            raise ValueError("fragment_timeout_s must be finite positive")
        if (
            type(max_frames) is not int
            or max_frames < 1
            or type(max_bytes) is not int
            or max_bytes < 1
            or type(max_au_bytes) is not int
            or not 1 <= max_au_bytes <= max_bytes
            or type(max_fragments) is not int
            or max_fragments < 1
            or max_sessions != 1
            or not callable(clock)
        ):
            raise ValueError("invalid bounded RVF2 reassembly configuration")
        self.expected_session_id = expected_session_id
        self.fragment_timeout_s, self.max_frames, self.max_bytes = (
            float(fragment_timeout_s),
            max_frames,
            max_bytes,
        )
        self.max_au_bytes, self.max_fragments, self._clock = max_au_bytes, max_fragments, clock
        self._active: dict[EncodedVideoIdentity, _ActiveAU] = {}
        self._completed: dict[EncodedVideoIdentity, _CompletedAU] = {}
        self._active_bytes = self._accepted = self._duplicates = self._rejected = self._expired = (
            self._completed_count
        ) = 0
        self._fixed_dimensions: tuple[int, int] | None = None
        self._last_expired: tuple[EncodedVideoIdentity, ...] = ()
        self._last_outcome = EncodedReassemblyOutcome("incomplete", "no_input_yet")

    @property
    def stats(self) -> EncodedReassemblyStats:
        width, height = self._fixed_dimensions or (None, None)
        return EncodedReassemblyStats(
            self._accepted,
            self._duplicates,
            self._rejected,
            self._expired,
            self._completed_count,
            len(self._active),
            self._active_bytes,
            len(self._completed),
            self.expected_session_id,
            width,
            height,
        )

    @property
    def last_outcome(self) -> EncodedReassemblyOutcome:
        return self._last_outcome

    @property
    def last_expired_identities(self) -> tuple[EncodedVideoIdentity, ...]:
        """Identities expired by the most recent explicit or implicit expiry pass."""
        return self._last_expired

    def _now(self, now: float | None) -> float:
        value = self._clock() if now is None else now
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError("now must be finite")
        return float(value)

    def _set_outcome(
        self,
        status: str,
        reason: str | None = None,
        access_unit: ReassembledAccessUnit | None = None,
        expired: tuple[EncodedVideoIdentity, ...] = (),
    ) -> ReassembledAccessUnit | None:
        self._last_outcome = EncodedReassemblyOutcome(status, reason, access_unit, expired)
        return access_unit

    def _drop(self, identity: EncodedVideoIdentity) -> None:
        state = self._active.pop(identity, None)
        if state is not None:
            self._active_bytes -= state.received_bytes

    def expire(self, *, now: float | None = None) -> tuple[EncodedVideoIdentity, ...]:
        current = self._now(now)
        expired = tuple(
            identity
            for identity, state in self._active.items()
            if current - state.updated_at >= self.fragment_timeout_s
        )
        for identity in expired:
            self._drop(identity)
            self._expired += 1
        for identity, state in list(self._completed.items()):
            if current - state.completed_at >= self.fragment_timeout_s:
                del self._completed[identity]
        self._last_expired = expired
        if expired:
            self._set_outcome("expired", "fragment_timeout", expired=expired)
        return expired

    def finish(self, *, now: float | None = None) -> dict[str, object]:
        """Actively expire stale input and report whether no partial AU remains.

        This is a protocol bookkeeping report only.  It neither invokes a
        codec nor asserts that a decoder/display consumed completed AUs.
        """
        expired = self.expire(now=now)
        return {
            "schema": "rf-encoded-video-reassembly-finish-v1",
            "expired_identities": expired,
            "active_access_units": len(self._active),
            "active_bytes": self._active_bytes,
            "reassembly_complete": not self._active and self._active_bytes == 0,
        }

    def _reject(self, identity: EncodedVideoIdentity | None, reason: str) -> None:
        self._rejected += 1
        if identity is not None:
            self._drop(identity)
        self._set_outcome("rejected", reason)

    def _parse(self, payload: bytes):
        if not isinstance(payload, bytes) or len(payload) != self._data_size + RVF2_HEADER.size:
            return None, "invalid_rvf2_payload"
        try:
            magic, version, flags, length, session, sequence, total, offset, crc = (
                RVF2_HEADER.unpack_from(payload)
            )
        except struct.error:
            return None, "invalid_rvf2_payload"
        if magic != RVF2_MAGIC or version != RVF2_VERSION or flags != 0:
            return None, "invalid_rvf2_payload"
        if session != self.expected_session_id:
            return None, "session_mismatch"
        if (
            not 1 <= length <= self._data_size
            or not ZVID2_HEADER.size + 1 <= total <= self.max_au_bytes
        ):
            return None, "invalid_rvf2_payload"
        count = (total + self._data_size - 1) // self._data_size
        if (
            count > self.max_fragments
            or offset >= total
            or offset + length > total
            or offset % self._data_size
        ):
            return None, "invalid_rvf2_payload"
        final = offset + length == total
        if (final and (offset != (count - 1) * self._data_size or length != total - offset)) or (
            not final and length != self._data_size
        ):
            return None, "invalid_rvf2_payload"
        if any(payload[RVF2_HEADER.size + length :]):
            return None, "invalid_rvf2_payload"
        return (
            EncodedVideoIdentity(session, sequence),
            total,
            offset,
            crc,
            payload[RVF2_HEADER.size : RVF2_HEADER.size + length],
        ), None

    def push_payload(
        self, payload: bytes, *, now: float | None = None
    ) -> ReassembledAccessUnit | None:
        current = self._now(now)
        self.expire(now=current)
        parsed, reject_reason = self._parse(payload)
        if parsed is None:
            self._reject(None, reject_reason or "invalid_rvf2_payload")
            return None
        identity, total, offset, crc, data = parsed
        done = self._completed.get(identity)
        if done is not None:
            if done.total_len == total and done.crc32 == crc:
                self._duplicates += 1
                return self._set_outcome("duplicate", "completed_identity")
            self._reject(None, "completed_identity_conflict")
            return None
        state = self._active.get(identity)
        if state is None:
            if len(self._active) >= self.max_frames:
                self._reject(None, "active_frame_limit")
                return None
            state = _ActiveAU(identity, total, crc, current, current)
            self._active[identity] = state
        elif state.total_len != total or state.crc32 != crc:
            self._reject(identity, "identity_metadata_conflict")
            return None
        prior = state.fragments.get(offset)
        if prior is not None:
            if prior == data:
                self._duplicates += 1
                return self._set_outcome("duplicate", "exact_active_fragment")
            self._reject(identity, "conflicting_duplicate")
            return None
        if self._active_bytes + len(data) > self.max_bytes:
            self._reject(identity, "active_byte_limit")
            return None
        state.fragments[offset] = data
        state.received_bytes += len(data)
        state.updated_at = current
        self._active_bytes += len(data)
        self._accepted += 1
        if state.received_bytes != state.total_len:
            return self._set_outcome("incomplete", "awaiting_fragments")
        packet = b"".join(
            state.fragments.get(offset, b"") for offset in range(0, total, self._data_size)
        )
        if len(packet) != total:
            return self._set_outcome("incomplete", "canonical_fragment_gap")
        if zlib.crc32(packet) & 0xFFFFFFFF != crc:
            self._reject(identity, "whole_zvid2_crc_mismatch")
            return None
        try:
            metadata = decode_access_unit(
                packet, session_id=identity.session_id, max_au_bytes=self.max_au_bytes
            )
        except EncodedVideoProtocolError:
            self._reject(identity, "invalid_zvid2_access_unit")
            return None
        if metadata.sequence != identity.sequence:
            self._reject(identity, "zvid2_sequence_identity_mismatch")
            return None
        dimensions = (metadata.width, metadata.height)
        if self._fixed_dimensions is None:
            self._fixed_dimensions = dimensions
        elif dimensions != self._fixed_dimensions:
            self._reject(identity, "session_dimension_change")
            return None
        self._drop(identity)
        if len(self._completed) >= self.max_frames:
            oldest = min(self._completed, key=lambda key: self._completed[key].completed_at)
            del self._completed[oldest]
        self._completed[identity] = _CompletedAU(total, crc, current)
        self._completed_count += 1
        result = ReassembledAccessUnit(identity, metadata, metadata.annexb)
        return self._set_outcome("completed", access_unit=result)

    def push_rf_packet(
        self, packet: bytes, *, expected_stream: int, now: float | None = None
    ) -> ReassembledAccessUnit | None:
        try:
            decoded = waveform.decode_packet(packet, expected_stream)
        except (TypeError, ValueError):
            self.expire(now=now)
            self._reject(None, "invalid_mrf1_packet")
            return None
        return self.push_payload(decoded["payload"], now=now)
