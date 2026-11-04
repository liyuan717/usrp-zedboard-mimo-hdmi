"""Persistent RF-packet -> real codec process -> complete-frame sink wiring.

The caller owns RF capture and the sink lifecycle. Start and call this session
from one packet worker; call tick periodically even when no packets arrive.
No hardware is opened here, and close is cancellation, not successful EOS.
"""
from __future__ import annotations

import threading

from .rf_encoded_bridge import RFEncodedVideoBridge
from .rf_encoded_dispatch import DecoderReferenceLost
from .video_codec_display import DecodedFramePresenter
from .video_codec_ipc import DECODE_ERROR, INPUT_ERROR
from .video_codec_process import CodecReferenceRequired, VideoCodecProcess


class RFEncodedVideoSession:
    def __init__(self, *, session_id, codec_argv, sink=None, decoded_output=None, timeout_s=2.0,
                 process_factory=VideoCodecProcess, **bridge_limits):
        """Create one packet-worker-owned codec session.

        ``sink`` retains the historical complete-frame presentation adapter.
        A native output owner may instead pass ``decoded_output``; it receives
        only a tracker-accepted FRAME header and its CRC-validated RGB565
        payload on the codec reader.  It must not call this session back from
        that callback.  Exactly one output boundary is permitted so a frame
        cannot be silently sent to both a legacy latest-frame sink and a
        bounded HDMI pipe.
        """
        if (sink is None) == (decoded_output is None):
            raise ValueError("supply exactly one of sink or decoded_output")
        if decoded_output is not None and not callable(decoded_output):
            raise TypeError("decoded_output must be callable")
        self.timeout_s = VideoCodecProcess._positive_timeout(timeout_s)
        self.presenter = (DecodedFramePresenter(session_id=session_id, sink=sink)
                          if sink is not None else None)
        self._recovery = threading.Event()
        self._owner = None
        self.state = "new"
        self.failure = None
        self.cleanup_failure = None
        self.codec_recovery_resets = 0
        self.codec_recovery_already_admitted = 0
        frame_output = self.presenter.submit_output if self.presenter is not None else decoded_output
        self.process = process_factory(
            codec_argv, frame_output,
            on_status=self._on_status, response_timeout_s=self.timeout_s)
        self.bridge = RFEncodedVideoBridge(
            session_id=session_id, width=1024, height=600,
            decoder_submit=self._submit,
            decoder_reset=self._reset,
            **bridge_limits)

    def _on_status(self, header):
        # Executed on the codec reader. Never call bridge/flush here: the
        # reader itself must remain available to receive the flush ACK.
        if header.status in (INPUT_ERROR, DECODE_ERROR):
            self._recovery.set()

    def start(self):
        if self.state != "new":
            raise RuntimeError("encoded session already started or closed")
        self._owner = threading.get_ident()
        try:
            self.process.start()
            self.state = "running"
        except BaseException as exc:
            self.state = "failed"
            self.failure = f"{type(exc).__name__}: {exc}"
            try:
                self.close()
            except BaseException as cleanup:
                exc.add_note(f"codec cleanup failed: {cleanup}")
            raise

    def _require_owner(self):
        if threading.get_ident() != self._owner:
            raise RuntimeError("encoded session requires its single packet worker")

    def _submit(self, au):
        # Bounded backpressure: predictive AUs are never latest-wins dropped.
        try:
            self.process.submit_wait(au, self.timeout_s)
        except CodecReferenceRequired as exc:
            # Unlike general submission failure, this guarantees no reserve
            # or pipe write took place. Retire the reference chain, not session.
            raise DecoderReferenceLost(str(exc)) from exc
        return True

    def _reset(self, reason):
        self._recovery.clear()
        self.process.flush(self.timeout_s)

    def tick(self, *, now=None):
        self._require_owner()
        if self.state != "running":
            raise RuntimeError(f"encoded session is {self.state}")
        try:
            status = self.process.snapshot()
            if status.state != "running":
                raise RuntimeError(status.failure or f"codec is {status.state}")
            if self._recovery.is_set():
                self._recovery.clear()
                # An IDR may have been admitted after the error but before
                # this owner tick. Re-read the gate after consuming the event:
                # do not flush that recovery IDR just because an old event
                # remains. A later error sets the event again.
                status = self.process.snapshot()
                if status.state != "running":
                    raise RuntimeError(status.failure or f"codec is {status.state}")
                if status.need_idr:
                    self.bridge.dispatcher.discontinuity("codec rejected encoded reference chain")
                    self.codec_recovery_resets += 1
                else:
                    self.codec_recovery_already_admitted += 1
            self.bridge.tick(now=now)
        except BaseException as exc:
            self.state = "failed"
            self.failure = self.failure or f"{type(exc).__name__}: {exc}"
            raise

    def consume_packet(self, stream, packet, *, now=None):
        self.tick(now=now)
        try:
            self.bridge.consume_packet(stream, packet, now=now)
        except BaseException as exc:
            # Unknown submission acceptance must never replay the RF packet.
            self.state = "failed"
            self.failure = self.failure or f"{type(exc).__name__}: {exc}"
            raise

    def transport_discontinuity(self, reason):
        """Called by the packet worker, never by a capture/detector callback."""
        self._require_owner()
        if self.state != "running":
            raise RuntimeError(f"encoded session is {self.state}")
        try:
            self.bridge.transport_discontinuity(reason)
        except BaseException as exc:
            self.state = "failed"
            self.failure = self.failure or f"{type(exc).__name__}: {exc}"
            raise

    def close(self):
        if self._owner is not None:
            self._require_owner()
        if self.state == "closed":
            return
        prior = self.process.snapshot()
        if prior.state == "failed":
            self.failure = self.failure or prior.failure or "codec failed before cleanup"
            self.state = "failed"
        try:
            self.process.close()
        except BaseException as exc:
            self.cleanup_failure = f"{type(exc).__name__}: {exc}"
            self.state = "failed"
            raise
        status = self.process.snapshot()
        allowed_terminal = status.state == "stopped" or (
            prior.state in ("failed", "new") and status.state == prior.state)
        if not allowed_terminal or not status.child_reaped or status.workers_alive:
            self.cleanup_failure = status.failure or f"codec cleanup ended in {status.state}"
            self.state = "failed"
            raise RuntimeError(self.cleanup_failure)
        if self.state != "failed":
            self.state = "closed"

