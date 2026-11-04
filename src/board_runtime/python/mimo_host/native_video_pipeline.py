"""Private, native-owned RGB565 delivery pipeline.

This module deliberately does not replace :mod:`video_codec_process`.  It is
the opt-in production path used when the decoded RGB565 bytes must never enter
Python.  Its fixed topology is::

    Python AU writer -> codec helper stdin
    codec helper stdout -> private kernel pipe -> native presenter --codec-output-fd
    native presenter --codec-feedback-fd -> Python metadata reader

The only record read by Python on the feedback pipe is the existing 72-byte
``VCDO`` header.  A ``FRAME`` header is an *accepted-for-presenter* record:
the native presenter has already checked the RGB payload CRC and queued the
frame.  It is not an HDMI scan-out acknowledgement.  The feedback pipe is an
inherited local pipe, never a socket or an externally supplied descriptor.

The presenter is the payload-integrity owner.  Consequently this owner never
tries to recompute an RGB CRC that it cannot see.  It still strictly maps every
header identity to one locally reserved token, preserves the 16-AU window, and
retires both children on every unknown-acceptance, timeout, or cancellation
path.
"""
from __future__ import annotations

import os
import queue
import select
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from .video_codec_ipc import (
    DECODE_ERROR,
    FATAL,
    FLUSH_ACK,
    FLUSHED,
    FRAME,
    HEADER,
    INPUT_ERROR,
    STOP_ACK,
    CodecIdentity,
    CodecOutputHeader,
    encode_control,
    encode_decode,
    parse_output_header,
)
from .video_codec_process import CodecProcessError, CodecReferenceRequired


class NativePipelineError(CodecProcessError):
    """A private native delivery pipeline failed or lost acceptance proof."""


@dataclass(frozen=True)
class NativeVideoPipelineSnapshot:
    state: str
    pending: int
    frames_accepted_for_presenter: int
    frames_callback_accepted: int
    frames_callback_rejected: int
    frames_suppressed: int
    need_idr: bool
    helper_reaped: bool
    presenter_reaped: bool
    workers_alive: tuple[str, ...]
    helper_stderr_bytes_retained: int
    helper_stderr_bytes_dropped: int
    presenter_stderr_bytes_retained: int
    presenter_stderr_bytes_dropped: int
    helper_returncode: int | None
    presenter_returncode: int | None
    stop_ack: bool
    failure: str | None


MetadataFrameCallback = Callable[[CodecOutputHeader], bool | None]
StatusCallback = Callable[[CodecOutputHeader], None]


class _BoundedStderr:
    def __init__(self, stream, limit: int) -> None:
        self._stream = stream
        self._limit = limit
        self._lock = threading.Lock()
        self.parts: deque[bytes] = deque()
        self.size = 0
        self.dropped = 0

    def run(self) -> None:
        try:
            while True:
                part = self._stream.read(4096)
                if not part:
                    return
                self._append(part)
        except BaseException:
            # Child lifecycle state reports the material error.  stderr is
            # diagnostic only and must never become an unbounded failure path.
            return

    def _append(self, part: bytes) -> None:
        with self._lock:
            if len(part) >= self._limit:
                self.parts.clear()
                self.dropped += self.size + len(part) - self._limit
                self.parts.append(part[-self._limit :])
                self.size = self._limit
                return
            self.parts.append(part)
            self.size += len(part)
            while self.size > self._limit:
                removed = self.parts.popleft()
                excess = self.size - self._limit
                if len(removed) <= excess:
                    self.size -= len(removed)
                    self.dropped += len(removed)
                else:
                    self.parts.appendleft(removed[excess:])
                    self.size -= excess
                    self.dropped += excess

    def snapshot(self) -> tuple[bytes, int, int]:
        with self._lock:
            return b"".join(self.parts), self.size, self.dropped


class NativeVideoPipeline:
    """Persistent helper + native-presenter owner with a metadata-only API.

    ``helper_argv`` is supplied through the legacy ``argv`` positional
    argument so this class can be used as an explicit ``process_factory``.
    The mandatory ``presenter_argv`` is kept separate to prevent a caller from
    accidentally treating the native presenter as part of the helper command.
    No caller-controlled file descriptor is accepted for codec data or
    feedback: both are created privately here.
    """

    _STOP = object()
    _VALID_STATES = {"new", "running", "flushing", "stopping", "stopping_ack", "stopped", "failed"}

    def __init__(
        self,
        argv: Sequence[str | os.PathLike[str]],
        on_frame: MetadataFrameCallback,
        *,
        presenter_argv: Sequence[str | os.PathLike[str]],
        presenter_extra_argv: Sequence[str | os.PathLike[str]] = (),
        helper_env: Mapping[str, str] | None = None,
        max_pending: int = 16,
        stderr_limit: int = 64 * 1024,
        response_timeout_s: float = 2.0,
        on_status: StatusCallback | None = None,
        feedback_timeout_ms: int = 50,
        presenter_drain_timeout_ms: int = 3000,
    ) -> None:
        self._argv = self._validate_argv(argv, "helper")
        presenter = self._validate_argv(presenter_argv, "presenter")
        if any(item in {"--codec-output-fd", "--codec-feedback-fd", "--proof-json"} for item in presenter):
            raise ValueError("presenter argv must not supply private codec/feedback/proof fd options")
        if not callable(on_frame):
            raise TypeError("on_frame must be a metadata callback")
        if on_status is not None and not callable(on_status):
            raise TypeError("on_status must be callable or None")
        if type(max_pending) is not int or not 1 <= max_pending <= 16:
            raise ValueError("max_pending must be 1..16")
        if type(stderr_limit) is not int or not 1024 <= stderr_limit <= 1024 * 1024:
            raise ValueError("stderr_limit must be 1 KiB..1 MiB")
        if type(feedback_timeout_ms) is not int or not 1 <= feedback_timeout_ms <= 1000:
            raise ValueError("feedback_timeout_ms must be 1..1000")
        if type(presenter_drain_timeout_ms) is not int or not 1 <= presenter_drain_timeout_ms <= 30000:
            raise ValueError("presenter_drain_timeout_ms must be 1..30000")
        self._presenter_argv = presenter
        self._presenter_extra_argv = tuple(os.fspath(item) for item in presenter_extra_argv)
        self._helper_env = self._validate_helper_env(helper_env)
        self._on_frame = on_frame
        self._on_status = on_status
        self._max_pending = max_pending
        self._stderr_limit = stderr_limit
        self._response_timeout_s = self._positive_timeout(response_timeout_s)
        self._feedback_timeout_ms = feedback_timeout_ms
        self._presenter_drain_timeout_ms = presenter_drain_timeout_ms
        self._lock = threading.RLock()
        self._capacity = threading.Condition(self._lock)
        self._state = "new"
        self._failure: str | None = None
        self._next_token = 1
        self._pending: dict[int, CodecIdentity] = {}
        self._pending_deadlines: dict[int, float] = {}
        self._need_idr = True
        self._control_pending: int | None = None
        self._frames_accepted = 0
        self._frames_callback_accepted = 0
        self._frames_callback_rejected = 0
        self._frames_suppressed = 0
        self._writes: queue.Queue[bytes | object] = queue.Queue()
        self._helper: subprocess.Popen[bytes] | None = None
        self._presenter: subprocess.Popen[bytes] | None = None
        self._threads: list[threading.Thread] = []
        self._flush_ack = threading.Event()
        self._stop_ack = threading.Event()
        self._callback_thread_ident: int | None = None
        self._helper_stderr: _BoundedStderr | None = None
        self._presenter_stderr: _BoundedStderr | None = None
        self._feedback_read_fd: int | None = None
        self._closing = threading.Event()

    @staticmethod
    def _validate_argv(argv, name: str) -> tuple[str, ...]:
        if not argv or any(not isinstance(item, (str, os.PathLike)) or not os.fspath(item) for item in argv):
            raise ValueError(f"{name} argv must be a non-empty executable argument sequence")
        return tuple(os.fspath(item) for item in argv)

    @staticmethod
    def _validate_helper_env(helper_env: Mapping[str, str] | None) -> dict[str, str]:
        """Permit only audited helper selectors; never serialize ambient env."""
        if helper_env is None:
            return {}
        if not isinstance(helper_env, Mapping):
            raise TypeError("helper_env must be a mapping")
        allowed = {
            "MIMO_CODEC_CONVERTER": {"sws", "neon601"},
            "MIMO_CODEC_CRC": {"slice8"},
            "MIMO_CODEC_PROFILE": {"0", "1"},
        }
        result: dict[str, str] = {}
        for name, value in helper_env.items():
            if name not in allowed or not isinstance(value, str) or value not in allowed[name]:
                raise ValueError(f"unapproved helper environment selector: {name}")
            result[name] = value
        return result

    @staticmethod
    def _positive_timeout(timeout_s: float) -> float:
        if type(timeout_s) not in (int, float) or not 0.01 <= float(timeout_s) <= 60.0:
            raise ValueError("timeout_s must be 0.01..60 seconds")
        return float(timeout_s)

    def start(self) -> None:
        """Spawn both children and private pipes; no hardware is opened here."""
        if os.name != "posix":
            raise NativePipelineError("native video pipeline requires POSIX pass_fds")
        with self._lock:
            if self._state != "new":
                raise NativePipelineError(f"cannot start from {self._state}")
        codec_read = codec_write = feedback_read = feedback_write = None
        try:
            codec_read, codec_write = os.pipe()
            feedback_read, feedback_write = os.pipe()
            presenter_argv = self._presenter_argv + (
                "--codec-output-fd", str(codec_read),
                "--codec-feedback-fd", str(feedback_write),
                "--feedback-timeout-ms", str(self._feedback_timeout_ms),
                "--drain-timeout-ms", str(self._presenter_drain_timeout_ms),
            ) + self._presenter_extra_argv
            presenter = subprocess.Popen(
                presenter_argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                close_fds=True,
                pass_fds=(codec_read, feedback_write),
                bufsize=0,
                env={key: value for key, value in os.environ.items()
                     if key not in {"LD_PRELOAD", "LD_AUDIT", "LD_LIBRARY_PATH"}},
            )
            # Its stdout is the private kernel pipe, not a Python PIPE.
            helper_environment = dict(os.environ, **self._helper_env)
            # Closure hashes describe the intended dynamic dependencies. Do
            # not allow ambient loader injection to substitute unpinned code.
            for injected_name in ("LD_PRELOAD", "LD_AUDIT", "LD_LIBRARY_PATH"):
                helper_environment.pop(injected_name, None)
            helper = subprocess.Popen(
                self._argv,
                stdin=subprocess.PIPE,
                stdout=codec_write,
                stderr=subprocess.PIPE,
                close_fds=True,
                bufsize=0,
                env=helper_environment,
            )
            if helper.stdin is None or helper.stderr is None or presenter.stderr is None:
                raise OSError("child standard streams unavailable")
            # A dead/misbehaving helper must not pin the writer inside a
            # blocking >PIPE_BUF write.  The writer uses select+deadline.
            os.set_blocking(helper.stdin.fileno(), False)
        except BaseException as exc:
            for fd in (codec_read, codec_write, feedback_read, feedback_write):
                self._close_fd(fd)
            for child in (locals().get("helper"), locals().get("presenter")):
                if child is not None:
                    self._terminate_child(child, 0.2)
            with self._lock:
                self._state = "failed"
                self._failure = f"spawn: {type(exc).__name__}: {exc}"
            raise NativePipelineError(self._failure) from exc
        # Parent must not retain either end it does not own.  Retaining a
        # write end would make EOF/STOP diagnostics ambiguous.
        self._close_fd(codec_read)
        self._close_fd(codec_write)
        self._close_fd(feedback_write)
        with self._lock:
            self._helper = helper
            self._presenter = presenter
            self._feedback_read_fd = feedback_read
            self._helper_stderr = _BoundedStderr(helper.stderr, self._stderr_limit)
            self._presenter_stderr = _BoundedStderr(presenter.stderr, self._stderr_limit)
            self._state = "running"
            self._threads = [
                threading.Thread(target=self._writer_loop, name="native-video-writer", daemon=True),
                threading.Thread(target=self._feedback_loop, name="native-video-feedback", daemon=True),
                threading.Thread(target=self._helper_stderr.run, name="native-video-helper-stderr", daemon=True),
                threading.Thread(target=self._presenter_stderr.run, name="native-video-presenter-stderr", daemon=True),
                threading.Thread(target=self._watchdog_loop, name="native-video-watchdog", daemon=True),
            ]
            for thread in self._threads:
                thread.start()

    def submit(self, access_unit) -> int:
        self._reject_callback_reentry("submit")
        with self._lock:
            return self._submit_locked(access_unit)

    def submit_wait(self, access_unit, timeout_s: float) -> int:
        self._reject_callback_reentry("submit_wait")
        deadline = time.monotonic() + self._positive_timeout(timeout_s)
        with self._capacity:
            while len(self._pending) >= self._max_pending:
                self._require_running("submit_wait")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise NativePipelineError("native presenter capacity wait timeout")
                self._capacity.wait(remaining)
            return self._submit_locked(access_unit)

    def _submit_locked(self, access_unit) -> int:
        self._require_running("submit")
        # ``submit`` is public as well as ``submit_wait``. Enforce the 16
        # token bound before validation/reservation/queueing in both paths.
        if len(self._pending) >= self._max_pending:
            raise NativePipelineError("native presenter pending window full")
        token = self._next_token
        identity, wire = encode_decode(access_unit, token)
        is_idr = bool(int.from_bytes(wire[12:16], "little") & 1)
        if self._need_idr and not is_idr:
            raise CodecReferenceRequired("native delivery recovery requires a self-contained IDR AU")
        self._pending[token] = identity
        self._pending_deadlines[token] = time.monotonic() + self._response_timeout_s
        self._next_token += 1
        if is_idr:
            self._need_idr = False
        # One queue entry, one helper write sequence.  A write failure is
        # unknown acceptance and therefore retires both children; never retry.
        self._writes.put(wire)
        return token

    def flush(self, timeout_s: float) -> None:
        self._reject_callback_reentry("flush")
        self._begin_control(2, timeout_s)

    def wait_idle(self, timeout_s: float) -> None:
        """Wait until all submitted identities receive private feedback."""
        self._reject_callback_reentry("wait_idle")
        deadline = time.monotonic() + self._positive_timeout(timeout_s)
        with self._capacity:
            while self._pending:
                self._require_not_failed("wait_idle")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise NativePipelineError("native delivery idle wait timeout")
                self._capacity.wait(remaining)

    def stop(self, timeout_s: float = 2.0) -> None:
        """EOS only after all frame identities are terminal; require STOP_ACK."""
        self._reject_callback_reentry("stop")
        timeout_s = self._positive_timeout(timeout_s)
        self.wait_idle(timeout_s)
        self._begin_control(3, timeout_s)
        self._retire_after_stop(timeout_s, require_clean=True)

    def close(self) -> None:
        """Bounded cancellation.  It does not claim that pending AUs completed."""
        self._reject_callback_reentry("close")
        with self._lock:
            state = self._state
        if state in {"new", "stopped"}:
            return
        if state in {"running", "flushing", "stopping", "stopping_ack"}:
            try:
                self.stop(1.0)
                return
            except BaseException:
                pass
        self._retire_after_stop(1.0, require_clean=False)

    def snapshot(self) -> NativeVideoPipelineSnapshot:
        with self._lock:
            helper_stderr = self._helper_stderr
            presenter_stderr = self._presenter_stderr
            return NativeVideoPipelineSnapshot(
                self._state,
                len(self._pending),
                self._frames_accepted,
                self._frames_callback_accepted,
                self._frames_callback_rejected,
                self._frames_suppressed,
                self._need_idr,
                self._helper is None or self._helper.poll() is not None,
                self._presenter is None or self._presenter.poll() is not None,
                tuple(thread.name for thread in self._threads if thread.is_alive()),
                0 if helper_stderr is None else helper_stderr.snapshot()[1],
                0 if helper_stderr is None else helper_stderr.snapshot()[2],
                0 if presenter_stderr is None else presenter_stderr.snapshot()[1],
                0 if presenter_stderr is None else presenter_stderr.snapshot()[2],
                None if self._helper is None else self._helper.poll(),
                None if self._presenter is None else self._presenter.poll(),
                self._stop_ack.is_set(),
                self._failure,
            )

    def diagnostics(self) -> tuple[bytes, bytes]:
        """Return bounded helper/presenter stderr tails, never RGB data."""
        with self._lock:
            return (
                b"" if self._helper_stderr is None else self._helper_stderr.snapshot()[0],
                b"" if self._presenter_stderr is None else self._presenter_stderr.snapshot()[0],
            )

    def _begin_control(self, command: int, timeout_s: float) -> None:
        timeout_s = self._positive_timeout(timeout_s)
        expected = FLUSH_ACK if command == 2 else STOP_ACK
        ack = self._flush_ack if command == 2 else self._stop_ack
        with self._lock:
            self._require_running("control")
            if self._control_pending is not None:
                raise NativePipelineError("native control already pending")
            self._control_pending = expected
            self._state = "flushing" if command == 2 else "stopping"
            self._need_idr = True
            ack.clear()
            self._writes.put(encode_control(command))
            self._capacity.notify_all()
        if not ack.wait(timeout_s):
            self._fail(f"control {command} acknowledgement timeout")
            raise NativePipelineError(self.snapshot().failure or "native control timeout")
        with self._lock:
            if self._state == "failed":
                raise NativePipelineError(self._failure or "native pipeline failed")

    def _writer_loop(self) -> None:
        try:
            while True:
                item = self._writes.get()
                if item is self._STOP:
                    return
                with self._lock:
                    if self._state == "failed":
                        return
                    helper = self._helper
                if helper is None or helper.stdin is None:
                    raise OSError("helper stdin unavailable")
                self._write_all(helper.stdin, item)
        except BaseException as exc:
            self._fail(f"stdin unknown-acceptance failure: {type(exc).__name__}: {exc}")

    def _write_all(self, stream, payload: bytes) -> None:
        """Bounded nonblocking write; partial bytes are never retried as an AU."""
        view = memoryview(payload)
        fd = stream.fileno()
        deadline = time.monotonic() + self._response_timeout_s
        while view:
            try:
                count = os.write(fd, view)
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not select.select([], [fd], [], remaining)[1]:
                    raise TimeoutError("helper stdin write deadline exceeded")
                continue
            if count <= 0:
                raise OSError("stdin short write")
            view = view[count:]

    def _feedback_loop(self) -> None:
        try:
            fd = self._feedback_read_fd
            if fd is None:
                raise EOFError("feedback pipe unavailable")
            while True:
                raw = self._read_exact_fd(fd, HEADER.size)
                header = parse_output_header(raw)
                self._consume_feedback(raw, header)
        except EOFError as exc:
            with self._lock:
                clean = self._stop_ack.is_set()
            if not clean and not self._closing.is_set():
                self._fail(f"feedback EOF before STOP_ACK: {exc}")
        except BaseException as exc:
            if not self._closing.is_set():
                self._fail(f"feedback framing failure: {type(exc).__name__}: {exc}")

    @staticmethod
    def _read_exact_fd(fd: int, size: int) -> bytes:
        chunks: list[bytes] = []
        remaining = size
        while remaining:
            part = os.read(fd, remaining)
            if not part:
                raise EOFError(f"short feedback record ({size - remaining}/{size} bytes)")
            chunks.append(part)
            remaining -= len(part)
        return b"".join(chunks)

    def _consume_feedback(self, raw: bytes, header: CodecOutputHeader) -> None:
        callback = None
        with self._capacity:
            if self._state == "failed":
                return
            if header.status == FATAL:
                raise NativePipelineError("native presenter reported FATAL")
            if header.status in (FLUSH_ACK, STOP_ACK):
                if header.status != self._control_pending:
                    raise NativePipelineError("unexpected native control acknowledgement")
                if self._pending:
                    raise NativePipelineError("native control ACK before pending tokens retired")
                self._control_pending = None
                if header.status == FLUSH_ACK:
                    self._state = "running"
                    self._flush_ack.set()
                else:
                    self._state = "stopping_ack"
                    self._stop_ack.set()
                self._notify_status(header)
                self._capacity.notify_all()
                return
            expected = self._pending.get(header.identity.token)
            if expected != header.identity:
                raise NativePipelineError("unknown/duplicate/mismatched native feedback identity")
            del self._pending[header.identity.token]
            self._pending_deadlines.pop(header.identity.token, None)
            if header.status == FRAME:
                # The raw header says a full RGB payload existed upstream, but
                # it intentionally does not follow on this metadata pipe.
                if self._control_pending is not None:
                    self._frames_suppressed += 1
                else:
                    self._frames_accepted += 1
                    callback = self._on_frame
            elif header.status not in (INPUT_ERROR, DECODE_ERROR, FLUSHED):
                raise NativePipelineError("unknown native feedback status")
            else:
                self._need_idr = True
                self._notify_status(header)
            self._capacity.notify_all()
        if callback is not None:
            self._callback_thread_ident = threading.get_ident()
            try:
                result = callback(header)
            finally:
                self._callback_thread_ident = None
            with self._lock:
                if result is False:
                    self._frames_callback_rejected += 1
                else:
                    self._frames_callback_accepted += 1

    def _notify_status(self, header: CodecOutputHeader) -> None:
        if self._on_status is not None:
            self._callback_thread_ident = threading.get_ident()
            try:
                self._on_status(header)
            finally:
                self._callback_thread_ident = None

    def _watchdog_loop(self) -> None:
        while True:
            time.sleep(min(0.02, self._response_timeout_s / 4.0))
            with self._lock:
                if self._state in {"failed", "stopped"}:
                    return
                # STOP_ACK changes responsibility from protocol watchdog to
                # bounded reaping below.  Keeping this worker alive through
                # presenter drain would make a successful stop look unclean.
                if self._stop_ack.is_set():
                    return
                now = time.monotonic()
                expired = [token for token, deadline in self._pending_deadlines.items() if deadline <= now]
            if expired:
                self._fail(f"native feedback timeout for token {min(expired)}")
                return
            with self._lock:
                helper = self._helper
                presenter = self._presenter
                if (helper is not None and helper.poll() is not None and not self._stop_ack.is_set()):
                    self._fail(f"helper exited {helper.returncode} before STOP_ACK")
                    return
                if (presenter is not None and presenter.poll() is not None and not self._stop_ack.is_set()):
                    self._fail(f"native presenter exited {presenter.returncode} before STOP_ACK")
                    return

    def _require_running(self, operation: str) -> None:
        if self._state != "running" or self._helper is None or self._helper.poll() is not None:
            raise NativePipelineError(f"cannot {operation}; state={self._state}")

    def _require_not_failed(self, operation: str) -> None:
        if self._state == "failed":
            raise NativePipelineError(f"cannot {operation}; {self._failure or 'pipeline failed'}")

    def _reject_callback_reentry(self, operation: str) -> None:
        if self._callback_thread_ident == threading.get_ident():
            raise NativePipelineError(f"cannot {operation} from native feedback callback")

    def _fail(self, detail: str) -> None:
        with self._capacity:
            if self._state == "failed":
                return
            self._state = "failed"
            self._failure = detail
            self._need_idr = True
            self._capacity.notify_all()
        self._retire_after_stop(0.5, require_clean=False)

    def _retire_after_stop(self, timeout_s: float, *, require_clean: bool) -> None:
        deadline = time.monotonic() + self._positive_timeout(timeout_s)
        self._writes.put(self._STOP)
        self._closing.set()
        with self._lock:
            helper, presenter = self._helper, self._presenter
            feedback_fd = self._feedback_read_fd
            self._feedback_read_fd = None
        # Closing stdin tells the helper cancellation occurred if STOP could
        # not be sent/acknowledged.  The native codec pipe then reaches EOF.
        if helper is not None and helper.stdin is not None:
            try:
                helper.stdin.close()
            except OSError:
                pass
        self._close_fd(feedback_fd)
        for child in (helper, presenter):
            if child is None:
                continue
            remaining = max(0.0, deadline - time.monotonic())
            if require_clean:
                # STOP_ACK is only a protocol acknowledgement.  r4 may still
                # be draining a bounded native queue, so first give the child
                # its advertised graceful exit interval before escalating.
                try:
                    child.wait(remaining)
                    continue
                except subprocess.TimeoutExpired:
                    pass
            self._terminate_child(child, remaining)
        for thread in list(self._threads):
            if thread is threading.current_thread():
                continue
            thread.join(max(0.0, deadline - time.monotonic()))
        with self._capacity:
            helper_ok = helper is None or (helper.poll() is not None and helper.returncode == 0)
            presenter_ok = presenter is None or (presenter.poll() is not None and presenter.returncode == 0)
            workers = [thread.name for thread in self._threads if thread.is_alive()]
            clean = self._stop_ack.is_set() and helper_ok and presenter_ok and not workers
            if require_clean and not clean:
                self._state = "failed"
                self._failure = self._failure or "native children did not stop cleanly"
                self._capacity.notify_all()
                raise NativePipelineError(self._failure)
            # Cancellation is intentionally not a successful EOS. Never let
            # unresolved AUs become a false ``stopped``/all-delivered result.
            if not require_clean and (self._pending or not self._stop_ack.is_set()):
                self._state = "failed"
                self._failure = self._failure or "native pipeline cancelled before clean STOP_ACK"
            elif self._state != "failed":
                self._state = "stopped"
            self._capacity.notify_all()

    @staticmethod
    def _close_fd(fd: int | None) -> None:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

    @staticmethod
    def _terminate_child(child: subprocess.Popen[bytes], timeout_s: float) -> None:
        if child.poll() is not None:
            return
        child.terminate()
        try:
            child.wait(max(0.05, min(timeout_s, 0.25)))
            return
        except subprocess.TimeoutExpired:
            child.kill()
            try:
                child.wait(0.25)
            except subprocess.TimeoutExpired:
                return


class RFNativeEncodedVideoSession:
    """RF packet bridge adapter for :class:`NativeVideoPipeline`.

    This intentionally lives beside the native pipeline rather than changing
    ``RFEncodedVideoSession``.  Existing sinks receive decoded RGB565 bytes;
    this adapter's ``on_frame`` receives *only* a tracked ``CodecOutputHeader``
    after the native presenter has validated the private RGB payload and
    queued it.  Code using an old two-argument pixel callback must not select
    this adapter accidentally.
    """

    def __init__(
        self,
        *,
        session_id: int,
        codec_argv: Sequence[str | os.PathLike[str]],
        presenter_argv: Sequence[str | os.PathLike[str]],
        on_frame: MetadataFrameCallback,
        timeout_s: float = 2.0,
        process_factory=NativeVideoPipeline,
        helper_env: Mapping[str, str] | None = None,
        presenter_extra_argv: Sequence[str | os.PathLike[str]] = (),
        max_pending: int = 16,
        stderr_limit: int = 64 * 1024,
        feedback_timeout_ms: int = 50,
        presenter_drain_timeout_ms: int = 3000,
        **bridge_limits,
    ) -> None:
        from .rf_encoded_bridge import RFEncodedVideoBridge
        from .rf_encoded_dispatch import DecoderReferenceLost

        if not callable(on_frame):
            raise TypeError("on_frame must accept metadata only")
        self._decoder_reference_lost = DecoderReferenceLost
        self.timeout_s = NativeVideoPipeline._positive_timeout(timeout_s)
        self._recovery = threading.Event()
        self._owner: int | None = None
        self.state = "new"
        self.failure: str | None = None
        self.cleanup_failure: str | None = None
        self.codec_recovery_resets = 0
        self.codec_recovery_already_admitted = 0
        self.process = process_factory(
            codec_argv,
            on_frame,
            presenter_argv=presenter_argv,
            on_status=self._on_status,
            response_timeout_s=self.timeout_s,
            helper_env=helper_env,
            presenter_extra_argv=presenter_extra_argv,
            max_pending=max_pending,
            stderr_limit=stderr_limit,
            feedback_timeout_ms=feedback_timeout_ms,
            presenter_drain_timeout_ms=presenter_drain_timeout_ms,
        )
        self.bridge = RFEncodedVideoBridge(
            session_id=session_id,
            width=1024,
            height=600,
            decoder_submit=self._submit,
            decoder_reset=self._reset,
            **bridge_limits,
        )

    def _on_status(self, header: CodecOutputHeader) -> None:
        if header.status in (INPUT_ERROR, DECODE_ERROR):
            self._recovery.set()

    def start(self) -> None:
        if self.state != "new":
            raise RuntimeError("native encoded session already started or closed")
        self._owner = threading.get_ident()
        try:
            self.process.start()
            self.state = "running"
        except BaseException as exc:
            self.state = "failed"
            self.failure = f"{type(exc).__name__}: {exc}"
            self.close()
            raise

    def _require_owner(self) -> None:
        if threading.get_ident() != self._owner:
            raise RuntimeError("native encoded session requires its single packet worker")

    def _submit(self, au) -> bool:
        try:
            self.process.submit_wait(au, self.timeout_s)
        except CodecReferenceRequired as exc:
            raise self._decoder_reference_lost(str(exc)) from exc
        return True

    def _reset(self, reason) -> None:
        del reason
        self._recovery.clear()
        self.process.flush(self.timeout_s)

    def tick(self, *, now=None) -> None:
        self._require_owner()
        if self.state != "running":
            raise RuntimeError(f"native encoded session is {self.state}")
        try:
            status = self.process.snapshot()
            if status.state != "running":
                raise RuntimeError(status.failure or f"native codec is {status.state}")
            if self._recovery.is_set():
                self._recovery.clear()
                status = self.process.snapshot()
                if status.state != "running":
                    raise RuntimeError(status.failure or f"native codec is {status.state}")
                if status.need_idr:
                    self.bridge.dispatcher.discontinuity("native codec rejected encoded reference chain")
                    self.codec_recovery_resets += 1
                else:
                    self.codec_recovery_already_admitted += 1
            self.bridge.tick(now=now)
        except BaseException as exc:
            self.state = "failed"
            self.failure = self.failure or f"{type(exc).__name__}: {exc}"
            raise

    def consume_packet(self, stream, packet, *, now=None) -> None:
        self.tick(now=now)
        try:
            self.bridge.consume_packet(stream, packet, now=now)
        except BaseException as exc:
            self.state = "failed"
            self.failure = self.failure or f"{type(exc).__name__}: {exc}"
            raise

    def transport_discontinuity(self, reason) -> None:
        self._require_owner()
        if self.state != "running":
            raise RuntimeError(f"native encoded session is {self.state}")
        try:
            self.bridge.transport_discontinuity(reason)
        except BaseException as exc:
            self.state = "failed"
            self.failure = self.failure or f"{type(exc).__name__}: {exc}"
            raise

    def close(self) -> None:
        if self._owner is not None:
            self._require_owner()
        if self.state == "closed":
            return
        prior = self.process.snapshot()
        if prior.state == "failed":
            self.failure = self.failure or prior.failure or "native codec failed before cleanup"
            self.state = "failed"
        try:
            self.process.close()
        except BaseException as exc:
            self.cleanup_failure = f"{type(exc).__name__}: {exc}"
            self.state = "failed"
            raise
        status = self.process.snapshot()
        allowed_terminal = status.state == "stopped" or (
            prior.state in {"failed", "new"} and status.state == prior.state
        )
        if not allowed_terminal or not (status.helper_reaped and status.presenter_reaped) or status.workers_alive:
            self.cleanup_failure = status.failure or f"native cleanup ended in {status.state}"
            self.state = "failed"
            raise RuntimeError(self.cleanup_failure)
        if self.state != "failed":
            self.state = "closed"

