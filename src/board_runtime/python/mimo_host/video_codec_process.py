"""Lifecycle owner for the persistent tagged video codec helper.

The owner deliberately has dedicated pipe workers: only the writer touches
stdin, only the reader touches stdout, and only the diagnostic worker touches
stderr.  They collectively own one child process; callers never touch a pipe
or infer identity from submission order.  This avoids the classic deadlock in
which a decoder emits a 1.2 MiB RGB frame while the controller waits for the
next AU to make a blocking read complete.
"""
from __future__ import annotations

import os
import queue
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from .video_codec_ipc import (
    FLUSH_ACK,
    FRAME,
    HEADER,
    STOP_ACK,
    CodecOutputHeader,
    CodecOutputTracker,
    encode_decode,
    parse_output_header,
)


class CodecProcessError(RuntimeError):
    """The helper has an unknown-acceptance or framing/lifecycle failure."""


class CodecReferenceRequired(CodecProcessError):
    """A P AU was never accepted because the helper is waiting for an IDR."""


@dataclass(frozen=True)
class CodecProcessSnapshot:
    state: str
    pending: int
    frames_decoded: int
    frames_callback_accepted: int
    frames_callback_rejected: int
    frames_suppressed: int
    need_idr: bool
    stderr_bytes_retained: int
    stderr_bytes_dropped: int
    child_reaped: bool
    workers_alive: tuple[str, ...]
    failure: str | None


FrameCallback = Callable[[CodecOutputHeader, bytes], bool | None]
StatusCallback = Callable[[CodecOutputHeader], None]


class VideoCodecProcess:
    """Bounded asynchronous owner for one `mimo-video-codec-helper` child.

    ``submit`` reserves a token before queuing bytes.  If a later pipe write
    fails, the child is retired because acceptance is unknowable.  A callback
    is invoked only for a tracker-accepted current-epoch FRAME; errors from it
    also retire the child because presentation acceptance is then unknown.
    ``on_frame`` and ``on_status`` run synchronously on the reader thread
    while the lifecycle lock is held: they must be bounded, non-blocking and
    must not call this owner's submit/control/close methods (those reentries
    are rejected explicitly).  A presenter should only enqueue/notify here.
    """

    _STOP = object()

    def __init__(
        self,
        argv: Sequence[str | os.PathLike[str]],
        on_frame: FrameCallback,
        *,
        max_pending: int = 16,
        stderr_limit: int = 64 * 1024,
        response_timeout_s: float = 2.0,
        on_status: StatusCallback | None = None,
    ) -> None:
        invalid_argv = not argv or any(
            not isinstance(item, (str, os.PathLike)) or not os.fspath(item) for item in argv
        )
        if invalid_argv:
            raise ValueError("argv must be a non-empty executable argument sequence")
        if not callable(on_frame):
            raise TypeError("on_frame must be callable")
        if on_status is not None and not callable(on_status):
            raise TypeError("on_status must be callable or None")
        if type(stderr_limit) is not int or not 1024 <= stderr_limit <= 1024 * 1024:
            raise ValueError("stderr_limit must be 1 KiB..1 MiB")
        response_timeout_s = self._positive_timeout(response_timeout_s)
        self._argv = tuple(os.fspath(item) for item in argv)
        self._on_frame = on_frame
        self._on_status = on_status
        self._tracker = CodecOutputTracker(max_pending=max_pending)
        self._stderr_limit = stderr_limit
        self._stderr = deque[bytes]()
        self._stderr_size = 0
        self._stderr_dropped = 0
        self._response_timeout_s = response_timeout_s
        self._pending_deadlines: dict[int, float] = {}
        self._need_idr = True
        self._frames_callback_accepted = 0
        self._frames_callback_rejected = 0
        self._lock = threading.RLock()
        self._capacity = threading.Condition(self._lock)
        self._state = "new"
        self._failure: str | None = None
        self._next_token = 1
        self._process: subprocess.Popen[bytes] | None = None
        self._writes: queue.Queue[bytes | object] = queue.Queue()
        self._threads: list[threading.Thread] = []
        self._flush_ack = threading.Event()
        self._stop_ack = threading.Event()
        self._saw_stop_ack = False
        self._callback_thread_ident: int | None = None

    def start(self) -> None:
        with self._lock:
            if self._state != "new":
                raise CodecProcessError(f"cannot start from {self._state}")
            try:
                process = subprocess.Popen(
                    self._argv,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    shell=False,
                    bufsize=0,
                    close_fds=True,
                )
            except OSError as exc:
                self._state = "failed"
                self._failure = f"spawn: {exc}"
                self._tracker.poison()
                raise CodecProcessError(self._failure) from exc
            if process.stdin is None or process.stdout is None or process.stderr is None:
                process.kill()
                process.wait()
                self._state = "failed"
                self._failure = "subprocess pipes unavailable"
                self._tracker.poison()
                raise CodecProcessError(self._failure)
            self._process = process
            self._state = "running"
            self._threads = [
                threading.Thread(target=self._writer_loop, name="video-codec-writer", daemon=True),
                threading.Thread(target=self._reader_loop, name="video-codec-reader", daemon=True),
                threading.Thread(target=self._stderr_loop, name="video-codec-stderr", daemon=True),
                threading.Thread(
                    target=self._watchdog_loop, name="video-codec-watchdog", daemon=True
                ),
            ]
            for thread in self._threads:
                thread.start()

    def submit(self, access_unit) -> int:
        """Queue one AU without waiting for a decoded output frame."""
        self._reject_callback_reentry("submit")
        with self._lock:
            return self._submit_locked(access_unit)

    def submit_wait(self, access_unit, timeout_s: float) -> int:
        """Wait for bounded helper capacity without dropping or polling an AU.

        This is for the dedicated RF bridge worker, never for the reader
        callback.  It returns only after the exact AU is reserved and queued;
        a timeout means that the caller still owns the AU and must recover or
        end the session explicitly.
        """
        self._reject_callback_reentry("submit_wait")
        timeout_s = self._positive_timeout(timeout_s)
        deadline = time.monotonic() + timeout_s
        with self._capacity:
            while len(self._tracker.pending) >= self._tracker.max_pending:
                self._require_running("submit_wait")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CodecProcessError("decoder capacity wait timeout")
                self._capacity.wait(remaining)
            return self._submit_locked(access_unit)

    def _submit_locked(self, access_unit) -> int:
        self._require_running("submit")
        token = self._next_token
        identity, wire = encode_decode(access_unit, token)
        is_idr = bool(HEADER.unpack(wire[: HEADER.size])[4] & 1)
        if self._need_idr and not is_idr:
            # This is a known non-acceptance: no tracker reservation or pipe
            # write has happened, so the dispatcher may discard only this P
            # AU and transition to its explicit WAIT_IDR recovery state.
            raise CodecReferenceRequired("decoder recovery requires a self-contained IDR AU")
        self._tracker.reserve(identity)
        if is_idr:
            # The helper validates the real SPS/slice contract before it
            # accepts this packet; later status errors re-arm the gate.
            self._need_idr = False
        self._next_token += 1
        self._pending_deadlines[token] = time.monotonic() + self._response_timeout_s
        self._writes.put(wire)
        return token

    def flush(self, timeout_s: float) -> None:
        self._reject_callback_reentry("flush")
        self._begin_control(2, timeout_s)

    def drain(self, timeout_s: float) -> None:
        """Wait for already submitted tokens; this does not issue codec EOS/flush.

        The present helper ABI deliberately has no drain command: ``FLUSH``
        invalidates outstanding tokens and is therefore unsuitable at the end
        of a finite video.  This method only waits for normal FRAME/error
        terminal records.  A timeout leaves the session running and reports
        that an ABI-level EOS/drain command would be required for this codec.
        """
        self._reject_callback_reentry("drain")
        timeout_s = self._positive_timeout(timeout_s)
        deadline = time.monotonic() + timeout_s
        with self._capacity:
            self._require_running("drain")
            while self._tracker.pending:
                self._require_running("drain")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CodecProcessError(
                        "decoder drain wait timeout; helper ABI has no EOS command"
                    )
                self._capacity.wait(remaining)

    def stop(self, timeout_s: float = 2.0) -> None:
        self._reject_callback_reentry("stop")
        retired = False
        try:
            self._begin_control(3, timeout_s)
        finally:
            retired = self._retire_after_stop(timeout_s)
        if not retired:
            raise CodecProcessError(self.snapshot().failure or "helper did not stop cleanly")

    def close(self) -> None:
        """Best-effort, bounded teardown; it never replays an AU."""
        self._reject_callback_reentry("close")
        with self._lock:
            state = self._state
        if state in {"running", "flushing", "stopping"}:
            try:
                self.stop(1.0)
                return
            except CodecProcessError:
                pass
        self._retire_after_stop(1.0)

    def diagnostics(self) -> bytes:
        with self._lock:
            return b"".join(self._stderr)

    def snapshot(self) -> CodecProcessSnapshot:
        with self._lock:
            process = self._process
            workers_alive = tuple(thread.name for thread in self._threads if thread.is_alive())
            return CodecProcessSnapshot(
                self._state,
                len(self._tracker.pending),
                self._tracker.frames,
                self._frames_callback_accepted,
                self._frames_callback_rejected,
                self._tracker.suppressed_frames,
                self._need_idr,
                self._stderr_size,
                self._stderr_dropped,
                process is None or process.poll() is not None,
                workers_alive,
                self._failure,
            )

    def _require_running(self, operation: str) -> None:
        if self._state != "running" or self._process is None or self._process.poll() is not None:
            raise CodecProcessError(f"cannot {operation}; state={self._state}")

    def _reject_callback_reentry(self, operation: str) -> None:
        """Do not wait on the reader thread from one of its callbacks."""
        if self._callback_thread_ident == threading.get_ident():
            raise CodecProcessError(f"cannot {operation} from codec callback")

    @staticmethod
    def _positive_timeout(timeout_s: float) -> float:
        if type(timeout_s) not in (int, float) or not 0.01 <= float(timeout_s) <= 60.0:
            raise ValueError("timeout_s must be 0.01..60 seconds")
        return float(timeout_s)

    def _begin_control(self, command: int, timeout_s: float) -> None:
        timeout_s = self._positive_timeout(timeout_s)
        ack = self._flush_ack if command == 2 else self._stop_ack
        with self._lock:
            self._require_running("control")
            wire = self._tracker.begin_control(command)
            self._state = "flushing" if command == 2 else "stopping"
            self._need_idr = True
            ack.clear()
            if command == 3:
                self._saw_stop_ack = False
            self._writes.put(wire)
            self._capacity.notify_all()
        if not ack.wait(timeout_s):
            self._fail(f"control {command} acknowledgement timeout")
            raise CodecProcessError(self.snapshot().failure or "codec control timeout")
        with self._lock:
            if self._state == "failed":
                raise CodecProcessError(self._failure or "codec control failed")

    @staticmethod
    def _write_all(stream, payload: bytes) -> None:
        view = memoryview(payload)
        while view:
            written = stream.write(view)
            if written is None:
                # Raw FileIO normally returns the byte count.  Treat an
                # implementation that cannot prove it as unknown acceptance.
                raise OSError("stdin write returned None")
            if written <= 0:
                raise OSError("stdin short write")
            view = view[written:]
        stream.flush()

    @staticmethod
    def _read_exact(stream, size: int) -> bytes:
        parts: list[bytes] = []
        remaining = size
        while remaining:
            part = stream.read(remaining)
            if not part:
                raise EOFError(f"short stdout record ({size - remaining}/{size} bytes)")
            parts.append(part)
            remaining -= len(part)
        return b"".join(parts)

    def _writer_loop(self) -> None:
        try:
            while True:
                item = self._writes.get()
                if item is self._STOP:
                    return
                with self._lock:
                    if self._state == "failed":
                        return
                process = self._process
                if process is None or process.stdin is None:
                    raise OSError("helper stdin disappeared")
                # Any exception after the first byte may mean the helper has
                # accepted a prefix, so all pending identities become unsafe.
                self._write_all(process.stdin, item)
        except BaseException as exc:
            self._fail(f"stdin unknown-acceptance failure: {type(exc).__name__}: {exc}")

    def _reader_loop(self) -> None:
        try:
            while True:
                process = self._process
                if process is None or process.stdout is None:
                    raise EOFError("helper stdout disappeared")
                header_bytes = self._read_exact(process.stdout, HEADER.size)
                header = parse_output_header(header_bytes)
                payload = (
                    self._read_exact(process.stdout, header.payload_bytes)
                    if header.payload_bytes
                    else b""
                )
                with self._lock:
                    accepted = self._tracker.consume(header_bytes, payload)
                    if header.status not in {FLUSH_ACK, STOP_ACK}:
                        self._pending_deadlines.pop(header.identity.token, None)
                    if header.status != FRAME:
                        self._need_idr = True
                        if self._on_status is not None:
                            self._call_status_callback(header)
                    if header.status == FLUSH_ACK:
                        self._state = "running"
                        self._flush_ack.set()
                    elif header.status == STOP_ACK:
                        # STOP_ACK is an acknowledgement, not proof that the
                        # child and pipe workers have exited. `_retire_after_stop`
                        # performs the reap and is the only stopped transition.
                        self._state = "stopping_ack"
                        self._saw_stop_ack = True
                        self._stop_ack.set()
                    self._capacity.notify_all()
                    # Hold the lifecycle lock through admission: a concurrent
                    # FLUSH cannot begin between tracker acceptance and sink
                    # admission. Callbacks must not re-enter this owner.
                    if accepted is not None and accepted.status == FRAME:
                        try:
                            result = self._call_frame_callback(accepted, payload)
                        except BaseException as exc:
                            detail = f"{type(exc).__name__}: {exc}"
                            self._fail(
                                f"frame callback unknown-acceptance failure: {detail}"
                            )
                            return
                        if result is False:
                            self._frames_callback_rejected += 1
                        else:
                            self._frames_callback_accepted += 1
        except EOFError as exc:
            # A clean helper normally closes stdout immediately after STOP_ACK.
            # It is still the reaper's job to prove process exit; without the
            # ACK this remains a framing failure.
            with self._lock:
                acknowledged_stop = self._stop_ack.is_set()
            if not acknowledged_stop:
                self._fail(f"stdout framing/decoder failure: {type(exc).__name__}: {exc}")
        except BaseException as exc:
            self._fail(f"stdout framing/decoder failure: {type(exc).__name__}: {exc}")

    def _stderr_loop(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        try:
            while True:
                data = process.stderr.read(4096)
                if not data:
                    return
                with self._lock:
                    retained = max(0, self._stderr_limit - self._stderr_size)
                    if retained:
                        chunk = data[:retained]
                        self._stderr.append(chunk)
                        self._stderr_size += len(chunk)
                    self._stderr_dropped += len(data) - retained
        except BaseException as exc:
            self._fail(f"stderr drain failure: {type(exc).__name__}: {exc}")

    def _watchdog_loop(self) -> None:
        while True:
            time.sleep(min(0.05, self._response_timeout_s / 4.0))
            with self._lock:
                active = self._state in {"running", "flushing", "stopping"}
                now = time.monotonic()
                oldest = min(self._pending_deadlines.values(), default=None)
                # Control timeout is owned by the caller's Event.wait in
                # _begin_control. Duplicating it here races a valid ACK that
                # is buffered immediately before the deadline.
                expired = active and oldest is not None and now > oldest
            if expired:
                self._fail("codec response progress timeout")
                return
            if not active:
                return

    def _call_status_callback(self, header: CodecOutputHeader) -> None:
        self._callback_thread_ident = threading.get_ident()
        try:
            self._on_status(header)  # type: ignore[misc]
        finally:
            self._callback_thread_ident = None

    def _call_frame_callback(self, header: CodecOutputHeader, payload: bytes) -> bool | None:
        self._callback_thread_ident = threading.get_ident()
        try:
            return self._on_frame(header, payload)
        finally:
            self._callback_thread_ident = None

    def _fail(self, reason: str) -> None:
        with self._lock:
            if self._state in {"failed", "stopped"}:
                return
            self._state = "failed"
            self._failure = reason
            self._tracker.poison()
            self._flush_ack.set()
            self._stop_ack.set()
            process = self._process
            self._writes.put(self._STOP)
            self._capacity.notify_all()
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass

    @staticmethod
    def _close_pipes(process: subprocess.Popen[bytes]) -> None:
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass

    def _retire_after_stop(self, timeout_s: float) -> bool:
        """Reap the child and workers; never report stopped on a live helper."""
        process = self._process
        if process is None:
            return self._state == "stopped"
        timeout_s = self._positive_timeout(timeout_s)
        self._writes.put(self._STOP)
        survived_stop_ack = False
        try:
            process.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            with self._lock:
                survived_stop_ack = self._saw_stop_ack
            try:
                process.kill()
            except OSError:
                pass
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                pass
        self._close_pipes(process)
        for thread in self._threads:
            if thread is not threading.current_thread():
                thread.join(timeout=1.0)
        live_workers = [
            thread.name
            for thread in self._threads
            if thread is not threading.current_thread() and thread.is_alive()
        ]
        reaped = process.poll() is not None
        with self._lock:
            if survived_stop_ack:
                self._state = "failed"
                self._failure = "helper acknowledged STOP but did not exit before timeout"
                self._tracker.poison()
            elif not reaped or live_workers:
                self._state = "failed"
                detail = (
                    "helper unreaped"
                    if not reaped
                    else "live workers: " + ", ".join(live_workers)
                )
                self._failure = f"stop teardown incomplete: {detail}"
                self._tracker.poison()
            elif self._state == "stopping_ack" and process.returncode != 0:
                self._state = "failed"
                self._failure = (
                    f"helper exited after STOP_ACK with return code {process.returncode}"
                )
                self._tracker.poison()
            elif self._state not in {"failed", "stopped"}:
                self._state = "stopped"
            self._capacity.notify_all()
            return self._state == "stopped"

    def __enter__(self) -> VideoCodecProcess:
        self.start()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()
