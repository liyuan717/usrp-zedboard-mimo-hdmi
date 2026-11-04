"""Bounded latest-frame display consumer for decoded RGB565 video frames.

The RF/decoder side calls :meth:`LatestFrameVideoSink.submit` without waiting
for a framebuffer write.  A dedicated worker is the sole owner of an optional
Linux framebuffer and presents only complete :class:`ReceivedFrame` objects.
Its ``target_fps`` is a software write-rate target, not a measurement or proof
of HDMI scanout, Vsync, or end-to-end RF video rate.
"""
from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .video_stream import Framebuffer, ReceivedFrame

DisplayCallable = Callable[[ReceivedFrame], Any]
FramebufferFactory = Callable[[], Framebuffer]


@dataclass(frozen=True)
class _PendingFrame:
    frame: ReceivedFrame
    submitted_monotonic_ns: int


class LatestFrameVideoSink:
    """Asynchronously present the newest complete frame from a bounded queue.

    Queue pressure drops older complete frames before it rejects a new frame.
    A display callback can be injected for PC tests or another display target.
    With the default callback, ``Framebuffer`` is opened only by ``start``'s
    worker and closed only by that same worker after it has stopped.
    """

    def __init__(
        self,
        *,
        target_fps: float = 60.0,
        capacity: int = 2,
        display: DisplayCallable | None = None,
        framebuffer_factory: FramebufferFactory = Framebuffer,
    ) -> None:
        if type(target_fps) not in (int, float) or not 0 < target_fps <= 60:
            raise ValueError("target_fps must be in (0, 60]")
        if type(capacity) is not int or capacity < 1:
            raise ValueError("capacity must be a positive integer")
        if display is not None and not callable(display):
            raise TypeError("display must be callable when supplied")
        if not callable(framebuffer_factory):
            raise TypeError("framebuffer_factory must be callable")
        self.target_fps = float(target_fps)
        self.capacity = capacity
        self._display = display
        self._framebuffer_factory = framebuffer_factory
        self._queue: queue.Queue[_PendingFrame] = queue.Queue(maxsize=capacity)
        self._lock = threading.RLock()
        # The submission gate serializes the acceptance decision with every
        # terminal transition.  When both locks are required, always acquire
        # this gate before _lock.  In particular, a stop cannot return while a
        # submitter that already observed ``accepting`` can still enqueue.
        self._submit_lock = threading.Lock()
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._started = False
        self._stopped = False
        self._accepting = False
        self._framebuffer_open = False
        self._framebuffer_close_failed = False
        self._display_error: str | None = None
        self._accepted = 0
        self._presented = 0
        self._new_sequence_frames = 0
        self._nonincreasing_sequence_writes = 0
        self._sequence_high_watermark: int | None = None
        self._first_new_frame_ns: int | None = None
        self._last_new_frame_ns: int | None = None
        self._dropped = 0
        self._rejected = 0
        self._queue_hwm = 0
        self._last_accepted_sequence: int | None = None
        self._last_presented_sequence: int | None = None
        self._last_dropped_sequence: int | None = None
        self._last_presented_latency_ns: int | None = None
        self._latency_total_ns = 0
        self._latency_min_ns: int | None = None
        self._latency_max_ns: int | None = None
        self._missed_software_deadlines = 0
        self._inflight_sequence: int | None = None

    def start(self) -> None:
        """Start the single display worker; importing or constructing opens no FB."""
        # Do not release the lifecycle gate between publishing the worker and
        # Thread.start(): otherwise stop() can attempt to join an unstarted
        # thread, raising RuntimeError and leaving a half-started sink.
        with self._submit_lock:
            with self._lock:
                if self._started:
                    raise RuntimeError("video sink instances are single-start")
                if self._stopped:
                    raise RuntimeError("video sink cannot start after stop")
                self._started = True
                self._accepting = True
                self._worker = threading.Thread(
                    target=self._run,
                    name="mimo-video-display",
                    daemon=True,
                )
                worker = self._worker
            try:
                worker.start()
            except Exception as exc:
                with self._lock:
                    self._accepting = False
                    self._stopped = True
                    self._display_error = type(exc).__name__ + ": " + str(exc)
                raise

    def submit(self, frame: ReceivedFrame) -> bool:
        """Non-blockingly enqueue a complete frame, evicting stale work first."""
        if not isinstance(frame, ReceivedFrame):
            raise TypeError("submit requires a ReceivedFrame")
        pending = _PendingFrame(frame=frame, submitted_monotonic_ns=time.monotonic_ns())
        with self._submit_lock:
            with self._lock:
                if not self._accepting:
                    self._rejected += 1
                    return False
            # Keep the submission gate through the put/evict operation as
            # well.  Worker finalization and stop() acquire the same gate
            # before draining, so a terminal state cannot acquire a new frame.
            while True:
                try:
                    self._queue.put_nowait(pending)
                except queue.Full:
                    try:
                        discarded = self._queue.get_nowait()
                    except queue.Empty:
                        continue
                    self._record_dropped(discarded.frame.header.sequence)
                    continue
                with self._lock:
                    self._accepted += 1
                    self._last_accepted_sequence = frame.header.sequence
                    self._queue_hwm = max(self._queue_hwm, self._queue.qsize())
                return True

    def _record_dropped(self, sequence: int, *, inflight: bool = False) -> None:
        with self._lock:
            self._dropped += 1
            self._last_dropped_sequence = sequence
            if inflight:
                self._inflight_sequence = None

    def _replace_inflight(self, dropped_sequence: int, next_sequence: int) -> None:
        """Account for a stale in-flight frame while promoting a newer one."""
        with self._lock:
            self._dropped += 1
            self._last_dropped_sequence = dropped_sequence
            self._inflight_sequence = next_sequence

    def _set_inflight(self, sequence: int) -> None:
        with self._lock:
            self._inflight_sequence = sequence

    def _drop_inflight_if_present(self) -> None:
        with self._lock:
            sequence = self._inflight_sequence
            if sequence is None:
                return
            self._dropped += 1
            self._last_dropped_sequence = sequence
            self._inflight_sequence = None

    def _discard_pending(self) -> None:
        while True:
            try:
                pending = self._queue.get_nowait()
            except queue.Empty:
                return
            self._record_dropped(pending.frame.header.sequence)

    def _make_display(self) -> tuple[DisplayCallable, Framebuffer | None]:
        if self._display is not None:
            return self._display, None
        framebuffer = self._framebuffer_factory()
        with self._lock:
            self._framebuffer_open = True

        def write(frame: ReceivedFrame) -> Any:
            return framebuffer.display_rgb565(
                frame.rgb565, frame.header.width, frame.header.height
            )

        return write, framebuffer

    def _take_latest(self, pending: _PendingFrame) -> _PendingFrame:
        latest = pending
        while True:
            try:
                older = latest
                latest = self._queue.get_nowait()
            except queue.Empty:
                return latest
            self._replace_inflight(
                older.frame.header.sequence, latest.frame.header.sequence
            )

    def _run(self) -> None:
        framebuffer: Framebuffer | None = None
        try:
            display, framebuffer = self._make_display()
            deadline = time.monotonic()
            period_s = 1.0 / self.target_fps
            while not self._stop.is_set():
                try:
                    pending = self._queue.get(timeout=0.050)
                except queue.Empty:
                    continue
                self._set_inflight(pending.frame.header.sequence)
                delay_s = deadline - time.monotonic()
                if delay_s > 0 and self._stop.wait(delay_s):
                    self._record_dropped(pending.frame.header.sequence, inflight=True)
                    break
                if self._stop.is_set():
                    self._record_dropped(pending.frame.header.sequence, inflight=True)
                    break
                pending = self._take_latest(pending)
                try:
                    display(pending.frame)
                except Exception as exc:  # noqa: BLE001 - worker must publish display failure
                    # Error-to-terminal transition uses the same gate as
                    # submit(), preventing a late accepted queue entry.
                    with self._submit_lock:
                        with self._lock:
                            self._display_error = type(exc).__name__ + ": " + str(exc)
                            self._accepting = False
                            self._stopped = True
                        self._stop.set()
                        self._record_dropped(
                            pending.frame.header.sequence, inflight=True
                        )
                    break
                finished = time.monotonic_ns()
                latency = finished - pending.submitted_monotonic_ns
                with self._lock:
                    self._presented += 1
                    sequence = pending.frame.header.sequence
                    if self._sequence_high_watermark is None or sequence > self._sequence_high_watermark:
                        self._new_sequence_frames += 1
                        self._sequence_high_watermark = sequence
                        if self._first_new_frame_ns is None:
                            self._first_new_frame_ns = finished
                        self._last_new_frame_ns = finished
                    else:
                        self._nonincreasing_sequence_writes += 1
                    self._last_presented_sequence = pending.frame.header.sequence
                    self._last_presented_latency_ns = latency
                    self._latency_total_ns += latency
                    self._latency_min_ns = (
                        latency
                        if self._latency_min_ns is None
                        else min(self._latency_min_ns, latency)
                    )
                    self._latency_max_ns = (
                        latency
                        if self._latency_max_ns is None
                        else max(self._latency_max_ns, latency)
                    )
                    self._inflight_sequence = None
                deadline += period_s
                if deadline < time.monotonic():
                    with self._lock:
                        self._missed_software_deadlines += 1
                    deadline = time.monotonic()
        except Exception as exc:  # noqa: BLE001 - framebuffer setup failure is display failure
            with self._submit_lock:
                with self._lock:
                    self._display_error = type(exc).__name__ + ": " + str(exc)
                    self._accepting = False
                    self._stopped = True
                self._stop.set()
        finally:
            # Serialize final drain with submit().  This makes the terminal
            # invariant explicit: accepted == presented + dropped once the
            # worker is terminated and its queue is empty.
            with self._submit_lock:
                with self._lock:
                    self._accepting = False
                    self._stopped = True
                self._drop_inflight_if_present()
                self._discard_pending()
            if framebuffer is not None:
                try:
                    framebuffer.close()
                except Exception as exc:  # noqa: BLE001 - preserve close error for caller
                    with self._lock:
                        self._framebuffer_close_failed = True
                        if self._display_error is None:
                            self._display_error = type(exc).__name__ + ": " + str(exc)
                finally:
                    with self._lock:
                        self._framebuffer_open = False

    def stop(self, *, join_timeout_s: float = 2.0) -> dict:
        """Stop accepting frames and wait a bounded time for the FB owner."""
        if type(join_timeout_s) not in (int, float) or not 0 < join_timeout_s <= 60:
            raise ValueError("join_timeout_s must be in (0, 60]")
        # Acquire the same gate as submit() before closing the acceptance
        # window.  The worker can then finish/flush its already accepted work,
        # but no orphan frame can be put after this transition.
        with self._submit_lock:
            with self._lock:
                worker = self._worker
                self._accepting = False
                self._stopped = True
            self._stop.set()
        if worker is not None and worker is not threading.current_thread():
            worker.join(float(join_timeout_s))
        return self.status()

    def status(self) -> dict:
        with self._lock:
            worker = self._worker
            alive = worker is not None and worker.is_alive()
            queue_depth = self._queue.qsize()
            inflight = 0 if self._inflight_sequence is None else 1
            accounted = self._presented + self._dropped + queue_depth + inflight
            span_ns = (0 if self._first_new_frame_ns is None else
                       self._last_new_frame_ns - self._first_new_frame_ns)
            return {
                "schema": "mimo-video-latest-frame-sink-v1",
                "target_fps": self.target_fps,
                "target_scope": (
                    "software framebuffer write cadence; not HDMI Vsync or scanout proof"
                ),
                "queue_capacity": self.capacity,
                "queue_depth": queue_depth,
                "queue_high_watermark": self._queue_hwm,
                "accepted_frames": self._accepted,
                "presented_frames": self._presented,
                "new_sequence_frames": self._new_sequence_frames,
                "nonincreasing_sequence_writes": self._nonincreasing_sequence_writes,
                "first_new_frame_monotonic_ns": self._first_new_frame_ns,
                "last_new_frame_monotonic_ns": self._last_new_frame_ns,
                "new_sequence_interval_fps": (
                    (self._new_sequence_frames - 1) * 1e9 / span_ns
                    if span_ns > 0 else None
                ),
                "rate_measurement_scope": (
                    "increasing sequence framebuffer-write completions between first and last; "
                    "excludes startup/final idle; not RF provenance, pixel uniqueness, Vsync or sustained-rate proof"
                ),
                "dropped_frames": self._dropped,
                "rejected_frames": self._rejected,
                "display_errors": 0 if self._display_error is None else 1,
                "display_error": self._display_error,
                "last_accepted_sequence": self._last_accepted_sequence,
                "last_presented_sequence": self._last_presented_sequence,
                "last_dropped_sequence": self._last_dropped_sequence,
                "last_presented_latency_ns": self._last_presented_latency_ns,
                "mean_presented_latency_ns": (
                    None if self._presented == 0 else self._latency_total_ns // self._presented
                ),
                "min_presented_latency_ns": self._latency_min_ns,
                "max_presented_latency_ns": self._latency_max_ns,
                "missed_software_deadlines": self._missed_software_deadlines,
                "started": self._started,
                "stopped": self._stopped,
                "accepting": self._accepting,
                "worker_terminated": not alive,
                "framebuffer_open": self._framebuffer_open,
                "framebuffer_close_failed": self._framebuffer_close_failed,
                "inflight_frames": inflight,
                "accounted_accepted_frames": accounted,
                "accounting_consistent": self._accepted == accounted,
                "safe_to_release_framebuffer": (
                    not alive
                    and not self._framebuffer_open
                    and not self._framebuffer_close_failed
                ),
            }
