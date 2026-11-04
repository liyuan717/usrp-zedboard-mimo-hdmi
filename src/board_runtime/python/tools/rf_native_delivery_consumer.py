#!/usr/bin/env python3
"""Bounded RFD1/RFE1-to-RVF2 handoff; RF and HDMI remain outside this tool.

The native RX runtime writes only complete local records.  This module is the
single business-data consumer: it validates record continuity and a fixed
IMBC build identity before handing the two already-validated MRF1 packets to
the existing RVF2/ZVID2/codec session.  It deliberately does not retry after
an uncertain decoder submission and never opens IIO, DMA, a framebuffer, or
an RF device.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import sys
import time
from typing import Any, Protocol

from read_rxonly_delivery import DeliveryRecord, iter_delivery


class DeliveryConsumerError(RuntimeError):
    """A local delivery stream cannot safely continue."""


class PacketTarget(Protocol):
    """Single-owner boundary to the existing encoded-video implementation."""
    def consume_packet(self, stream: int, packet: bytes, *, now: float | None = None) -> None: ...
    def transport_discontinuity(self, reason: str) -> None: ...
    def tick(self, *, now: float | None = None) -> None: ...
    def finish(self, *, now: float | None = None) -> dict[str, Any]: ...


@dataclass(frozen=True)
class NativeDeliveryStats:
    expected_build_id: int | None
    last_rf_frame_id: int | None
    delivered_records: int
    rejected_records: int
    packets_forwarded: int
    forward_gaps: int
    duplicate_records: int
    replay_records: int
    discontinuities: int
    producer_epoch_resets: int
    state: str
    last_error: str | None


class NativeDeliveryConsumer:
    """Consume finite ordered RFD1/RFE1 records through exactly one target.

    A forward frame-ID gap retires the old reference chain and then permits the
    current record to supply a new IDR.  Duplicate/replay records are never
    handed to the target: they still retire references because their prior
    acceptance cannot be proven.  RFE1 likewise retires references but does
    not turn a later valid IDR into a fatal process failure.
    """

    def __init__(self, target: PacketTarget, *, expected_build_id: int | None = None) -> None:
        if not all(callable(getattr(target, name, None)) for name in
                   ("consume_packet", "transport_discontinuity", "tick", "finish")):
            raise TypeError("target must implement the bounded packet-target interface")
        if expected_build_id is not None and (type(expected_build_id) is not int or not 1 <= expected_build_id <= 0xFFFFFFFF):
            raise ValueError("expected_build_id must be a nonzero uint32 or None")
        self._target = target
        self._expected_build_id = expected_build_id
        self._last_frame_id: int | None = None
        self._delivered = self._rejected = self._packets = 0
        self._gaps = self._duplicates = self._replays = self._discontinuities = 0
        self._producer_epoch_resets = 0
        self._state = "ACTIVE"
        self._last_error: str | None = None

    @property
    def stats(self) -> NativeDeliveryStats:
        return NativeDeliveryStats(
            self._expected_build_id, self._last_frame_id, self._delivered,
            self._rejected, self._packets, self._gaps, self._duplicates,
            self._replays, self._discontinuities, self._producer_epoch_resets,
            self._state, self._last_error,
        )

    def _fail(self, message: str, cause: BaseException | None = None) -> None:
        self._state = "FAILED"
        self._last_error = message
        if cause is None:
            raise DeliveryConsumerError(message)
        raise DeliveryConsumerError(message) from cause

    def _require_active(self) -> None:
        if self._state != "ACTIVE":
            raise DeliveryConsumerError(f"native delivery consumer is {self._state.lower()}")

    def _bind_build(self, record: DeliveryRecord) -> None:
        if type(record.build_id) is not int or not 1 <= record.build_id <= 0xFFFFFFFF:
            self._fail("delivery record has no valid nonzero build ID")
        if self._expected_build_id is None:
            self._expected_build_id = record.build_id
        elif record.build_id != self._expected_build_id:
            self._fail("delivery record IMBC build ID changed within one run")

    def _classify_frame(self, frame_id: int) -> str:
        if type(frame_id) is not int or not 0 <= frame_id <= 0xFFFFFFFF:
            self._fail("delivery record frame ID is not uint32")
        if self._last_frame_id is None:
            self._last_frame_id = frame_id
            return "initial"
        delta = (frame_id - self._last_frame_id) & 0xFFFFFFFF
        if delta == 1:
            self._last_frame_id = frame_id
            return "contiguous"
        if delta == 0:
            self._duplicates += 1
            return "duplicate"
        if delta < 0x80000000:
            self._gaps += 1
            self._last_frame_id = frame_id
            return "gap"
        self._replays += 1
        return "replay"

    def _discontinuity(self, reason: str) -> None:
        try:
            self._target.transport_discontinuity(reason[:256])
        except BaseException as exc:
            self._fail("target discontinuity acceptance is uncertain", exc)
        self._discontinuities += 1

    def producer_epoch_changed(self, reason: str) -> None:
        """Retire replay tracking after a verified native producer restart.

        RFD1 deliberately has no producer-instance field.  The live AF_UNIX
        receiver therefore authenticates a producer epoch out of band from a
        per-process random abstract sender address (and kernel credentials
        when available), then calls this method before it hands the first
        packet of the new epoch to the existing bridge.  Frame IDs may safely
        restart only after this explicit decoder discontinuity.
        """
        self._require_active()
        if type(reason) is not str or not 1 <= len(reason) <= 256:
            self._fail("bounded producer-epoch reason required")
        self._discontinuity(reason)
        self._last_frame_id = None
        self._producer_epoch_resets += 1

    def consume(self, record: DeliveryRecord, *, now: float | None = None) -> None:
        self._require_active()
        if type(record) is not DeliveryRecord:
            self._fail("validated DeliveryRecord required")
        self._bind_build(record)
        sequence = self._classify_frame(record.frame_id)
        if record.rejected:
            self._rejected += 1
            self._discontinuity(f"RFE1 RF frame {record.frame_id} rejected after {sequence}")
            return
        if not isinstance(record.packets, tuple) or len(record.packets) != 2 or not all(type(packet) is bytes for packet in record.packets):
            self._fail("RFD1 record lacks exactly two immutable MRF1 packets")
        if sequence in ("duplicate", "replay"):
            self._discontinuity(f"RFD1 RF frame {record.frame_id} {sequence}; packets not replayed")
            return
        if sequence == "gap":
            self._discontinuity(f"RFD1 RF frame-ID forward gap before {record.frame_id}")
        try:
            self._target.tick(now=now)
            for stream, packet in enumerate(record.packets):
                self._target.consume_packet(stream, packet, now=now)
                self._packets += 1
        except BaseException as exc:
            # Packet 0 may already have been accepted by a codec when packet 1
            # fails.  Do not reset and replay the local record: acceptance is
            # unknowable and the caller must terminate this finite run.
            self._fail("target packet acceptance is uncertain; record is not retried", exc)
        self._delivered += 1

    def finish(self, *, now: float | None = None) -> dict[str, Any]:
        self._require_active()
        try:
            self._target.tick(now=now)
            target_report = self._target.finish(now=now)
        except BaseException as exc:
            self._fail("target finish is uncertain", exc)
        if not isinstance(target_report, dict):
            self._fail("target finish did not return a report")
        transport = target_report.get("transport", target_report)
        if not isinstance(transport, dict):
            self._fail("target finish transport report is malformed")
        reassembly = transport.get("reassembly")
        if not isinstance(reassembly, dict) or reassembly.get("reassembly_complete") is not True:
            self._fail("finite delivery ended with an incomplete RVF2 access unit")
        self._state = "FINISHED"
        return {
            "schema": "rxonly-native-delivery-consumer-v1",
            "stats": asdict(self.stats),
            "target": target_report,
            "physical_rf_verified": False,
            "codec_output_verified": False,
            "decoded_frame_verified": False,
            "hdmi_scanout_verified": False,
        }


class EncodedSessionTarget:
    """Explicit adapter over RFEncodedVideoSession; the caller owns close()."""

    def __init__(self, session: Any) -> None:
        self._session = session

    def consume_packet(self, stream: int, packet: bytes, *, now: float | None = None) -> None:
        self._session.consume_packet(stream, packet, now=now)

    def transport_discontinuity(self, reason: str) -> None:
        self._session.transport_discontinuity(reason)

    def tick(self, *, now: float | None = None) -> None:
        self._session.tick(now=now)

    def finish(self, *, now: float | None = None) -> dict[str, Any]:
        transport = self._session.bridge.finish(now=now)
        self._session.process.drain(self._session.timeout_s)
        return {"transport": transport, "codec": asdict(self._session.process.snapshot())}


class _CountingNonHDMISink:
    """Complete decoded-frame counter; intentionally no framebuffer/HDMI owner."""
    def __init__(self) -> None:
        self.frames = 0
        self.last_sequence: int | None = None

    def __call__(self, frame: Any) -> None:
        self.frames += 1
        self.last_sequence = int(frame.header.sequence)


def _run_file(args: argparse.Namespace) -> dict[str, Any]:
    from mimo_host.rf_encoded_session import RFEncodedVideoSession
    from mimo_host.video_sink import LatestFrameVideoSink

    counter = _CountingNonHDMISink()
    sink = LatestFrameVideoSink(display=counter, capacity=2, target_fps=60.0)
    session = RFEncodedVideoSession(
        session_id=args.session_id,
        codec_argv=[str(args.codec_helper), *args.codec_arg], sink=sink,
        timeout_s=args.codec_timeout_s,
    )
    consumer = NativeDeliveryConsumer(EncodedSessionTarget(session), expected_build_id=args.imbc_build_id)
    started = session_started = False
    try:
        sink.start(); started = True
        session.start(); session_started = True
        with args.input.open("rb") as stream:
            for record in iter_delivery(stream):
                consumer.consume(record, now=time.monotonic())
        report = consumer.finish(now=time.monotonic())
        report["non_hdmi_sink"] = {"complete_decoded_frames": counter.frames, "last_sequence": counter.last_sequence,
                                   "status": sink.status()}
        return report
    finally:
        # Closing does not retry RF/decoder input.  It merely retires the
        # already-owned helper and bounded sink after success or failure.
        if session_started and session.state != "closed":
            session.close()
        if started:
            sink.stop()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--session-id", type=lambda value: int(value, 0), required=True)
    parser.add_argument("--imbc-build-id", type=lambda value: int(value, 0), required=True)
    parser.add_argument("--codec-helper", type=Path, required=True)
    parser.add_argument("--codec-arg", action="append", default=[])
    parser.add_argument("--codec-timeout-s", type=float, default=2.0)
    args = parser.parse_args()
    try:
        if type(args.session_id) is not int or not 0 <= args.session_id <= 0xFFFFFFFF:
            raise DeliveryConsumerError("session ID must be uint32")
        result = _run_file(args)
    except (OSError, ValueError, DeliveryConsumerError, RuntimeError) as exc:
        parser.exit(2, f"native delivery consumer failed: {exc}\n")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
