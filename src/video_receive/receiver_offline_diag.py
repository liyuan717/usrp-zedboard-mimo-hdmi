#!/usr/bin/env python3
"""Opt-in RFD1/RFE1 receiver wired to the metadata-only native video path.

This process consumes already-produced *local* RFD1/RFE1 records.  It never
opens IIO, RF DMA, an SDR, a framebuffer, or an HDMI device.  Its deliberate
topology is::

    RFD1/RFE1 -> NativeDeliveryConsumer -> RVF2/ZVID2 reassembly
      -> RFNativeEncodedVideoSession -> helper stdout -> native presenter
      -> 72-byte VCDO feedback only

Python therefore sees RFD1/RFE1 transport bytes and VCDO headers, never a
decoded RGB565 frame.  A local fixture can be supplied with ``--input FILE``
or ``--input -`` (a pipe).  The tool is also suitable as the consumer end of
an independently-owned RF delivery relay, but completing it only proves the
bounded software handoff; it does *not* prove RF reception or physical HDMI
scanout.

The existing ``rf_live_video.py`` remains the legacy full-RGB Python path.
It is intentionally not modified by this opt-in entry point.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import select
import socket
import stat
import struct
import sys
import time
from typing import Any, BinaryIO, Iterator

from read_rxonly_delivery import HEADER as DELIVERY_HEADER
from read_rxonly_delivery import MAX_RECORD_BYTES, DeliveryRecord, iter_delivery, parse_delivery_datagram
from rf_native_delivery_consumer import DeliveryConsumerError, NativeDeliveryConsumer


class RFNativeReceiverError(RuntimeError):
    """The native receive handoff cannot retain unambiguous ownership."""


_SOCKET_OWNERS: dict[int, tuple[int, int]] = {}
_EPOCH_PREFIX = b"\0mrf1-"
_EPOCH_NAME_BYTES = len(_EPOCH_PREFIX) + 32


class _ProducerEpochGate:
    """Bind local records to one producer incarnation without changing RFD1."""

    def __init__(self, *, maximum_retired: int) -> None:
        if not 1 <= maximum_retired <= 4096:
            raise ValueError("bounded producer epoch history required")
        self._maximum_retired = maximum_retired
        self.current: bytes | None = None
        self.retired: set[bytes] = set()
        self.changes = 0
        self.rejected_old = 0

    def accept(self, identity: bytes) -> str:
        if self.current is None:
            self.current = identity
            return "initial"
        if identity == self.current:
            return "current"
        if identity in self.retired:
            self.rejected_old += 1
            return "old"
        if len(self.retired) >= self._maximum_retired:
            raise RFNativeReceiverError("producer epoch history exhausted; refusing ambiguous restart")
        self.retired.add(self.current)
        self.current = identity
        self.changes += 1
        return "changed"


def _uint32(value: str) -> int:
    try:
        result = int(value, 0)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("uint32 required") from exc
    if not 0 <= result <= 0xFFFFFFFF:
        raise argparse.ArgumentTypeError("uint32 required")
    return result


def _positive(value: str) -> float:
    try:
        result = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("positive seconds required") from exc
    if not 0.01 <= result <= 60.0:
        raise argparse.ArgumentTypeError("seconds must be 0.01..60")
    return result


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load_runtime_closure(path: Path):
    """Lazy import keeps record-parser tooling free of codec/NumPy imports."""
    from run_native_video_pipeline import _load_runtime_closure as load
    return load(path)


def _native_session_type():
    """Separate import seam for record-only tests and deployment packaging."""
    from mimo_host.native_video_pipeline import RFNativeEncodedVideoSession
    return RFNativeEncodedVideoSession


def _open_input(value: str) -> tuple[BinaryIO, bool, str]:
    if value == "-":
        return sys.stdin.buffer, False, "stdin"
    path = Path(value).resolve(strict=True)
    if not path.is_file() and not path.is_fifo():
        raise RFNativeReceiverError("input must be a regular file, FIFO, or -")
    return path.open("rb", buffering=0), True, os.fspath(path)


def _bind_delivery_socket(path: Path) -> socket.socket:
    """Own one new filesystem AF_UNIX endpoint without unlinking another run."""
    if os.name != "posix" or not hasattr(socket, "AF_UNIX"):
        raise RFNativeReceiverError("live native delivery requires POSIX AF_UNIX")
    if not path.is_absolute() or "\x00" in os.fspath(path) or len(os.fsencode(path)) >= 108:
        raise RFNativeReceiverError("delivery socket must be a short absolute filesystem path")
    if path.exists() or path.is_symlink() or not path.parent.is_dir():
        raise RFNativeReceiverError("delivery socket path must be absent and have an existing parent")
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM | socket.SOCK_NONBLOCK)
    bound = False
    identity: tuple[int, int] | None = None
    try:
        listener.bind(os.fspath(path)); bound = True
        observed = path.stat()
        identity = (observed.st_dev, observed.st_ino)
        os.chmod(path, 0o600)
        if (path.stat().st_dev, path.stat().st_ino) != identity or stat.S_IMODE(path.stat().st_mode) != 0o600:
            raise RFNativeReceiverError("delivery socket ownership or mode changed during bind")
        if not hasattr(socket, "SO_PASSCRED"):
            raise RFNativeReceiverError("live native delivery requires SO_PASSCRED")
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
        _SOCKET_OWNERS[listener.fileno()] = identity
        return listener
    except BaseException:
        listener.close()
        if bound and identity is not None and path.exists():
            current = path.lstat()
            if stat.S_ISSOCK(current.st_mode) and (current.st_dev, current.st_ino) == identity:
                path.unlink()
        raise


def _close_delivery_socket(listener: socket.socket, path: Path) -> None:
    fd = listener.fileno()
    expected = _SOCKET_OWNERS.pop(fd, None)
    listener.close()
    try:
        current = path.lstat()
    except FileNotFoundError:
        return
    if (not stat.S_ISSOCK(current.st_mode) or expected is None or
            (current.st_dev, current.st_ino) != expected):
        raise RFNativeReceiverError("delivery socket ownership changed; refusing unlink")
    path.unlink()


def _peer_epoch(address: object, ancillary: list[tuple[int, int, bytes]]) -> bytes:
    if not isinstance(address, (str, bytes)):
        raise RFNativeReceiverError("native delivery sender address is absent")
    raw = address if isinstance(address, bytes) else os.fsencode(address)
    if (len(raw) != _EPOCH_NAME_BYTES or not raw.startswith(_EPOCH_PREFIX) or
            any(byte not in b"0123456789abcdef" for byte in raw[len(_EPOCH_PREFIX):])):
        raise RFNativeReceiverError("native delivery sender lacks required random abstract epoch")
    credential_type = getattr(socket, "SCM_CREDENTIALS", None)
    for level, kind, data in ancillary:
        if level == socket.SOL_SOCKET and kind == credential_type and len(data) >= struct.calcsize("3i"):
            pid, uid, gid = struct.unpack_from("3i", data)
            if pid <= 0 or uid < 0 or gid < 0:
                break
            return raw + b"|" + struct.pack("3i", pid, uid, gid)
    raise RFNativeReceiverError("native delivery sender has no kernel credentials")


def _iter_pipe_delivery(stream: BinaryIO, deadline: float) -> Iterator[DeliveryRecord]:
    """Bounded stream/FIFO parser for explicit offline fixtures only."""
    fd = stream.fileno()
    os.set_blocking(fd, False)
    buffered = bytearray()
    expected: int | None = None
    while True:
        while expected is None or len(buffered) < expected:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RFNativeReceiverError("offline fixture pipe deadline expired")
            readable, _, _ = select.select([fd], [], [], remaining)
            if not readable:
                raise RFNativeReceiverError("offline fixture pipe deadline expired")
            piece = os.read(fd, MAX_RECORD_BYTES)
            if not piece:
                if buffered:
                    raise RFNativeReceiverError("partial offline fixture record at EOF")
                return
            buffered.extend(piece)
            if expected is None and len(buffered) >= DELIVERY_HEADER.size:
                magic, _frame_id, _build_id, size, _crc = DELIVERY_HEADER.unpack_from(buffered)
                if magic not in (b"RFD1", b"RFE1") or size > 2 * 152:
                    raise RFNativeReceiverError("offline fixture stream header is invalid")
                expected = DELIVERY_HEADER.size + size
            if len(buffered) > MAX_RECORD_BYTES:
                raise RFNativeReceiverError("offline fixture stream record exceeds bound")
        raw = bytes(buffered[:expected])
        del buffered[:expected]
        expected = None
        yield parse_delivery_datagram(raw)


class _NativeSessionTarget:
    """Packet-target adapter which never exposes native RGB payload bytes."""

    def __init__(self, session: Any) -> None:
        self._session = session

    def consume_packet(self, stream: int, packet: bytes, *, now: float | None = None) -> None:
        self._session.consume_packet(stream, packet, now=now)

    def transport_discontinuity(self, reason: str) -> None:
        self._session.transport_discontinuity(reason)

    def tick(self, *, now: float | None = None) -> None:
        self._session.tick(now=now)

    def finish(self, *, now: float | None = None) -> dict[str, Any]:
        # Finish transport first.  This lets the existing bridge prove no
        # partial RVF2 access unit remains before waiting only for identities
        # it has already submitted.  STOP is owned by outer cleanup.
        transport = self._session.bridge.finish(now=now)
        self._session.process.wait_idle(self._session.timeout_s)
        return {"transport": transport, "codec": asdict(self._session.process.snapshot())}


def _read_proof(path: Path, backend: str) -> dict[str, object]:
    if not path.is_file():
        raise RFNativeReceiverError("presenter did not create required fresh proof")
    raw = path.read_bytes()
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RFNativeReceiverError("presenter proof is not JSON") from exc
    if not isinstance(parsed, dict):
        raise RFNativeReceiverError("presenter proof is not an object")
    # A native presentation receipt belongs to the presenter.  We bind the
    # bytes and report its declared backend, but do not elevate it to a radio
    # or HDMI-success claim here.
    return {"path": os.fspath(path), "bytes": len(raw), "sha256": _sha256(raw),
            "backend": backend, "reported_physical": parsed.get("physical"),
            "reported_capture_status": parsed.get("capture_status")}


def _write_fresh_json(path: Path, payload: dict[str, object]) -> None:
    encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        view = memoryview(encoded)
        while view:
            count = os.write(fd, view)
            if count <= 0:
                raise OSError("short report write")
            view = view[count:]
    finally:
        os.close(fd)


def _validate_video_finish(delivery: dict[str, Any], accepted: list[dict[str, object]], expected_aus: int) -> None:
    """Keep a syntactically clean transport EOF from becoming a video pass."""
    if expected_aus < 1 or len(accepted) != expected_aus:
        raise RFNativeReceiverError("accepted AU count does not match required video budget")
    target = delivery.get("target")
    if not isinstance(target, dict) or not isinstance(target.get("transport"), dict):
        raise RFNativeReceiverError("native delivery has no transport finish report")
    transport = target["transport"]
    reassembly, ordering = transport.get("reassembly"), transport.get("ordering")
    if not isinstance(reassembly, dict) or reassembly.get("reassembly_complete") is not True:
        raise RFNativeReceiverError("video finish has incomplete RVF2 reassembly")
    if (not isinstance(ordering, dict) or ordering.get("ordering_complete") is not True or
            ordering.get("reference_chain_valid") is not True or ordering.get("ended_waiting_for_idr") is not False or
            ordering.get("submitted_aus") != expected_aus):
        raise RFNativeReceiverError("video finish is not an ordered accepted IDR/reference chain")


def run(args: argparse.Namespace) -> dict[str, object]:
    pacer = None
    report_path = args.report.resolve()
    proof_path = args.proof_output.resolve()
    if report_path == proof_path or report_path.exists() or proof_path.exists():
        raise RFNativeReceiverError("report and proof-output must be distinct fresh paths")
    helper_argv, presenter_argv, helper_env, runtime = _load_runtime_closure(args.runtime_closure)
    backend = runtime["presenter"]["backend"]
    accepted: list[dict[str, object]] = []

    def on_frame(header: Any) -> None:
        # The callback receives only a 72-byte-derived header.  Keep the
        # receipt bounded and retain source identity/token/CRC, never pixels.
        if len(accepted) >= args.max_records:
            raise RFNativeReceiverError("metadata receipt bound exceeded")
        identity = header.identity
        accepted.append({"session_id": identity.session_id, "sequence": identity.sequence,
                         "token": identity.token, "source_pts_ms": identity.source_pts,
                         "accepted_payload_crc32": f"{header.payload_crc:08x}", "accepted_monotonic_ns": time.monotonic_ns()})

    stop_file = getattr(args, "stop_file", None)
    if stop_file is not None:
        stop_file = Path(stop_file)
        if (args.delivery_socket is None or not stop_file.is_absolute()
                or not stop_file.parent.resolve().is_relative_to(Path('/tmp').resolve())
                or stop_file.exists() or stop_file.is_symlink()):
            raise RFNativeReceiverError("owner stop file must be fresh RAM control for live input")

    session = _native_session_type()(
        session_id=args.session_id,
        codec_argv=helper_argv,
        presenter_argv=presenter_argv,
        presenter_extra_argv=("--backend", backend, "--proof-json", os.fspath(proof_path)),
        helper_env=helper_env,
        on_frame=on_frame,
        timeout_s=args.timeout_s,
        max_pending=args.max_pending,
    )
    target = _NativeSessionTarget(session)
    consumer = NativeDeliveryConsumer(target, expected_build_id=args.imbc_build_id)
    started_ns = time.monotonic_ns()
    stream: BinaryIO | None = None
    close_stream = False
    source_label = ""
    listener: socket.socket | None = None
    epoch_gate: _ProducerEpochGate | None = None
    received_records = 0
    record_log = None
    delivered_report: dict[str, Any] | None = None
    failure: str | None = None
    try:
        record_path = getattr(args, "live_record_output", None)
        if record_path is not None:
            if args.delivery_socket is None or not str(record_path).startswith("/tmp/link-share/"):
                raise RFNativeReceiverError("live record evidence must be RAM output only")
            record_log = record_path.open("xb", buffering=0)
        if args.delivery_socket is not None:
            listener = _bind_delivery_socket(args.delivery_socket)
            source_label = os.fspath(args.delivery_socket)
            epoch_gate = _ProducerEpochGate(maximum_retired=args.max_records)
        else:
            stream, close_stream, source_label = _open_input(args.input)
        # Validate/acquire the source before creating the two children.  A bad
        # endpoint must not start a display process merely to discover that no
        # RF-delivery bytes can be consumed.
        session.start()
        if listener is not None:
            idle_deadline = time.monotonic() + args.idle_timeout_s
            credential_space = socket.CMSG_SPACE(struct.calcsize("3i"))
            while received_records < args.records:
                if getattr(args, "finish_after_accepted", False) and len(accepted) >= args.expected_access_units:
                    break
                if stop_file is not None and stop_file.exists():
                    raise RFNativeReceiverError("owner requested live receive cancellation")
                remaining = idle_deadline - time.monotonic()
                if remaining <= 0:
                    raise RFNativeReceiverError("live native delivery idle deadline expired")
                readable, _, _ = select.select([listener], [], [], min(0.050, remaining))
                if not readable:
                    target.tick(now=time.monotonic())
                    continue
                raw, ancillary, flags, address = listener.recvmsg(MAX_RECORD_BYTES + 1, credential_space)
                if flags & socket.MSG_TRUNC or len(raw) > MAX_RECORD_BYTES:
                    raise RFNativeReceiverError("live native delivery datagram is truncated or oversize")
                epoch = _peer_epoch(address, ancillary)
                assert epoch_gate is not None
                epoch_action = epoch_gate.accept(epoch)
                if epoch_action == "old":
                    # Never replay an old producer into a reset frame-ID
                    # tracker, even if that old sender is still draining.
                    continue
                if epoch_action == "changed":
                    consumer.producer_epoch_changed("verified native producer epoch changed")
                record = parse_delivery_datagram(raw)
                if record_log is not None:
                    if record_log.write(raw) != len(raw):
                        raise RFNativeReceiverError("short live evidence output")
                consumer.consume(record, now=time.monotonic())
                received_records += 1
                idle_deadline = time.monotonic() + args.idle_timeout_s
        else:
            assert stream is not None
            if not args.offline:
                raise RFNativeReceiverError("file/FIFO/stdin is offline fixture mode; pass --offline explicitly")
            deadline = time.monotonic() + args.offline_deadline_s
            iterator = (iter_delivery(stream) if source_label != "stdin" and not Path(source_label).is_fifo()
                        else _iter_pipe_delivery(stream, deadline))
            pacer=_make_diagnostic_pacer(args)
            if pacer is not None:
                from diagnostic_pacer import PacketPacedTarget
                consumer = NativeDeliveryConsumer(PacketPacedTarget(target, pacer, deadline), expected_build_id=args.imbc_build_id)
            for record in iterator:
                received_records += 1
                if received_records > args.max_records:
                    raise RFNativeReceiverError("offline input record count exceeds bounded max-records")
                if time.monotonic() > deadline:
                    raise RFNativeReceiverError("offline fixture deadline expired")
                consumer.consume(record, now=time.monotonic())
        delivered_report = consumer.finish(now=time.monotonic())
        _validate_video_finish(delivered_report, accepted, args.expected_access_units)
        hold_ms = getattr(args, "hold_final_frame_ms", 0)
        if hold_ms:
            print("FINITE_VIDEO_HOLD_BEGIN ms=" + str(hold_ms), flush=True)
            hold_started_ns = time.monotonic_ns()
            hold_until = time.monotonic() + hold_ms / 1000.0
            while time.monotonic() < hold_until:
                if stop_file is not None and stop_file.exists():
                    raise RFNativeReceiverError("owner requested cancellation during finite frame hold")
                # The bridge is already finished; its tick rejects further use.
                # Codec/presenter workers own progress independently during hold.
                status = session.process.snapshot()
                if (status.state != "running" or status.failure is not None or
                        status.helper_returncode is not None or status.presenter_returncode is not None):
                    raise RFNativeReceiverError("native children failed during finite frame hold")
                time.sleep(min(.02, max(0, hold_until-time.monotonic())))

            print("FINITE_VIDEO_HOLD_END elapsed_ns=" + str(time.monotonic_ns()-hold_started_ns), flush=True)

    except BaseException as exc:
        failure = f"{type(exc).__name__}: {exc}"[:1024]
    finally:
        if record_log is not None:
            record_log.close()
        if close_stream and stream is not None:
            stream.close()
        if listener is not None:
            try:
                _close_delivery_socket(listener, args.delivery_socket)
            except BaseException as exc:
                failure = failure or f"socket cleanup {type(exc).__name__}: {exc}"[:1024]
        try:
            session.close()
        except BaseException as exc:
            failure = failure or f"cleanup {type(exc).__name__}: {exc}"[:1024]
    ended_ns = time.monotonic_ns()
    snapshot = session.process.snapshot()
    helper_stderr, presenter_stderr = session.process.diagnostics()
    result: dict[str, object] = {
        "schema": "rf-native-video-receiver-v1",
        "valid": False,
        "input": {"mode": "live_dgram" if args.delivery_socket is not None else "offline_fixture",
                  "source": source_label, "max_records": args.max_records, "received_records": received_records,
                  "expected_access_units": args.expected_access_units, "read_once_per_record": True,
                  "record_budget": args.records if args.delivery_socket is not None else None,
                  "idle_timeout_s": args.idle_timeout_s if args.delivery_socket is not None else None,
                  "offline_deadline_s": args.offline_deadline_s if args.delivery_socket is None else None},
        "diagnostic_source_pacing": None if pacer is None else {"scale":pacer.scale,"schedule":pacer.schedule},
        "runtime": runtime,
        "source_session_id": args.session_id,
        "monotonic_start_ns": started_ns,
        "monotonic_end_ns": ended_ns,
        "duration_ns": ended_ns - started_ns,
        "accepted": accepted,
        "delivery": delivered_report,
        "producer_epoch": (None if epoch_gate is None else {"changes": epoch_gate.changes,
                              "rejected_old": epoch_gate.rejected_old, "retired_bound": args.max_records}),
        "terminal": {"session_state": session.state, "session_failure": session.failure,
                     "cleanup_failure": session.cleanup_failure, "pipeline_state": snapshot.state,
                     "pending": snapshot.pending, "workers_alive": list(snapshot.workers_alive),
                     "stop_ack": snapshot.stop_ack, "helper_returncode": snapshot.helper_returncode,
                     "presenter_returncode": snapshot.presenter_returncode, "failure": snapshot.failure},
        "diagnostics": {"helper_stderr": helper_stderr.decode("utf-8", errors="replace"),
                        "presenter_stderr": presenter_stderr.decode("utf-8", errors="replace"),
                        "stderr_limit_bytes_per_child": 65536},
        "physical_rf_verified": False,
        "hdmi_scanout_verified": False,
        "note": "accepted means presenter CRC-valid payload queued, never HDMI scanout ACK",
    }
    if failure is not None:
        result["failure"] = failure
    else:
        try:
            result["proof"] = _read_proof(proof_path, backend)
        except BaseException as exc:
            result["failure"] = f"{type(exc).__name__}: {exc}"[:1024]
    result["valid"] = bool(
        "failure" not in result and delivered_report is not None and len(accepted) == args.expected_access_units and
        session.state == "closed" and
        snapshot.state == "stopped" and snapshot.pending == 0 and not snapshot.workers_alive and
        snapshot.stop_ack and snapshot.helper_returncode == 0 and snapshot.presenter_returncode == 0
    )
    _write_fresh_json(report_path, result)
    return result


def _make_diagnostic_pacer(args, **clock_options):
    from diagnostic_pacer import DiagnosticPacer
    scale = getattr(args, "diagnostic_time_scale", 1)
    enabled = getattr(args, "diagnostic_pace_source_pts", False)
    if enabled and (not args.offline or args.delivery_socket is not None):
        raise RFNativeReceiverError("source PTS pacing is offline-only")
    return DiagnosticPacer(scale, args.session_id, **clock_options) if enabled or scale != 1 else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--delivery-socket", type=Path,
                        help="new absolute AF_UNIX datagram endpoint for the native RF producer")
    source.add_argument("--input", help="offline RFD1/RFE1 regular-file, FIFO, or - fixture")
    parser.add_argument("--stop-file", type=Path, help="optional fresh RAM owner cancellation control; never a media input")
    parser.add_argument("--live-record-output", type=Path, help="optional RAM-only output evidence; never an RX input")
    parser.add_argument("--offline", action="store_true",
                        help="explicitly permit file/FIFO/stdin fixture mode; never a live-RF claim")
    parser.add_argument("--runtime-closure", type=Path, required=True)
    parser.add_argument("--proof-output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--session-id", type=_uint32, required=True)
    parser.add_argument("--imbc-build-id", type=_uint32)
    parser.add_argument("--timeout-s", type=_positive, default=8.0)
    parser.add_argument("--idle-timeout-s", type=_positive, default=2.0)
    parser.add_argument("--offline-deadline-s", type=_positive, default=8.0)
    parser.add_argument("--records", type=int,
                        help="exact finite live datagram budget; live EOF is not a success signal")
    parser.add_argument("--expected-access-units", type=int, required=True,
                        help="exact decoder/presenter-accepted AU count required for valid video finish")
    parser.add_argument("--finish-after-accepted", action="store_true",
                        help="finite live mode: finish after expected decoded AU acceptance; physical proof remains separately required")
    parser.add_argument("--hold-final-frame-ms", type=int, default=0,
                        help="finite live mode: retain final decoded frame for 0..10000ms before normal shutdown")
    parser.add_argument("--max-records", type=int, default=4096)
    parser.add_argument("--max-pending", type=int, default=16)
    parser.add_argument("--diagnostic-offline-hold-ms", type=int, default=0, help="display-only offline fixture hold 0..60000ms; never a new RF claim")
    parser.add_argument("--diagnostic-time-scale",type=int,default=1,help="offline only: scale public source PTS by1..10 for visual inspection")
    parser.add_argument("--diagnostic-pace-source-pts", action="store_true", help="offline only: explicitly pace source PTS, including scale1")
    args = parser.parse_args()
    if args.diagnostic_pace_source_pts and (not args.offline or args.delivery_socket is not None):
        parser.error("source PTS pacing requires explicit offline fixture")
    if not 1<=args.diagnostic_time_scale<=10 or (args.diagnostic_time_scale!=1 and (not args.offline or args.delivery_socket is not None)):
        parser.error("diagnostic time scale requires explicit offline fixture and1..10")
    if (not 1 <= args.max_records <= 4096 or not 1 <= args.max_pending <= 16 or
            not 1 <= args.expected_access_units <= args.max_records):
        parser.error("max-records must be 1..4096; max-pending 1..16; expected-access-units 1..max-records")
    if not 0 <= args.hold_final_frame_ms <= 10000 or (args.hold_final_frame_ms and args.delivery_socket is None):
        parser.error("hold-final-frame-ms requires live input and 0..10000ms")
    if args.finish_after_accepted and args.delivery_socket is None:
        parser.error("finish-after-accepted requires live delivery socket")
    if args.delivery_socket is not None:
        if args.offline or args.records is None or not 1 <= args.records <= args.max_records:
            parser.error("live --delivery-socket requires --records 1..max-records and forbids --offline")
        if args.imbc_build_id is None:
            parser.error("live --delivery-socket requires an explicit --imbc-build-id")
    elif args.records is not None:
        parser.error("--records is valid only with --delivery-socket")
    if args.diagnostic_offline_hold_ms:
        if not args.offline or args.delivery_socket is not None or not 0 <= args.diagnostic_offline_hold_ms <= 60000:
            parser.error("diagnostic-offline-hold-ms requires explicit offline fixture and 0..60000ms")
        args.hold_final_frame_ms = args.diagnostic_offline_hold_ms
    try:
        result = run(args)
    except (OSError, ValueError, RuntimeError, DeliveryConsumerError, RFNativeReceiverError) as exc:
        parser.exit(2, f"rf native video receiver failed: {exc}\n")
    print(json.dumps({"schema": result["schema"], "report": os.fspath(args.report),
                      "valid": result["valid"], "accepted": len(result["accepted"])}, sort_keys=True))
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
