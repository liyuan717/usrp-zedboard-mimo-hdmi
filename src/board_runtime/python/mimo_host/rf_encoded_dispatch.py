"""Bounded complete-AU ordering before the decoder (not a decoder itself).

Single owner only. Call tick even when no radio packets arrive. The reset
callback must synchronously retire the old decoder epoch and its output
tokens; submission must not use a latest-wins encoded queue. Structural IDR
inspection only admits an AU to the codec validator, never proves H.264
profile or output identity. Only the decoder may produce display frames.
"""
from __future__ import annotations

import math
import time

from .rf_encoded_video import EncodedAccessUnit, validate_access_unit


class DecoderReferenceLost(RuntimeError):
    """Decoder rejected this AU BEFORE acceptance; a fresh IDR is required.

    Never use for a pipe/write failure or any uncertain acceptance state.
    """


class EncodedAUDispatcher:
    def __init__(self, *, session_id, width, height, submit, reset_decoder,
                 capacity=8, gap_timeout_s=0.1, clock=time.monotonic,
                 max_au_bytes=4*1024*1024, max_pending_bytes=8*1024*1024):
        if type(session_id) is not int or not 0 <= session_id <= 0xFFFFFFFF:
            raise ValueError("session_id must be uint32")
        if any(type(v) is not int or v <= 0 for v in
               (width, height, capacity, max_au_bytes, max_pending_bytes)):
            raise ValueError("positive dimensions/capacity required")
        if width > 1920 or height > 1920:
            raise ValueError("dimensions exceed protocol limits")
        if not math.isfinite(gap_timeout_s) or gap_timeout_s <= 0:
            raise ValueError("positive finite gap timeout required")
        if not all(callable(f) for f in (submit, reset_decoder, clock)):
            raise TypeError("callbacks must be callable")
        self.session_id, self.width, self.height = session_id, width, height
        self.submit, self.reset_decoder, self.clock = submit, reset_decoder, clock
        self.capacity, self.gap_timeout_s = capacity, gap_timeout_s
        self.max_au_bytes, self.max_pending_bytes = max_au_bytes, max_pending_bytes
        self.state = "WAIT_IDR"
        self.next_sequence = None
        self.high_watermark = -1
        self.pending = {}
        self._pending_idrs = set()
        self.gap_since = None
        self.gap_sequence = None
        self.last_clock = None
        self.cleanup_error = None
        self.sequence_gap_timeouts = 0
        self.submitted = self.discarded = self.resets = self.overflows = 0
        self.last_error = None
        self._reference_base_sequence = None

    @property
    def reference_base_sequence(self):
        """Latest submitted structural IDR base, not codec/display success."""
        return self._reference_base_sequence

    def _fatal(self, exc, *, retire=True):
        self.state = "FATAL"
        self.last_error = f"{type(exc).__name__}: {exc}"
        self.pending.clear()
        self._pending_idrs.clear()
        self._reference_base_sequence = None
        if retire:
            try:
                if self.reset_decoder("fatal: retire possibly accepted decoder epoch") is False:
                    raise RuntimeError("decoder retirement not acknowledged")
            except BaseException as cleanup:
                self.cleanup_error = f"{type(cleanup).__name__}: {cleanup}"
                exc.add_note(f"Decoder retirement failed: {self.cleanup_error}")
        raise exc

    def _now(self):
        try:
            now = self.clock()
            if (type(now) not in (int, float) or not math.isfinite(now)
                    or (self.last_clock is not None and now < self.last_clock)):
                raise ValueError("clock must be finite and monotonic")
            self.last_clock = now
            return now
        except BaseException as exc:
            self._fatal(exc)

    def _send(self, au, *, is_idr):
        try:
            if self.submit(au) is False:
                raise RuntimeError("decoder rejected AU submission")
        except DecoderReferenceLost:
            self.discarded += 1
            self.discontinuity("decoder reference lost before AU acceptance")
            return
        except BaseException as exc:
            self._fatal(exc)
        self.submitted += 1
        self.high_watermark = au.sequence
        self.next_sequence = au.sequence + 1  # no uint32 wrap
        if is_idr:
            self._reference_base_sequence = au.sequence
        self.state = "RUNNING"

    def discontinuity(self, reason):
        """Call on fragment expiry, loss, or decoder recovery request.

        The caller must discard queued outputs from the retired decoder epoch.
        A reset failure is fatal; continuing with stale references is unsafe.
        """
        if self.state == "FATAL":
            raise RuntimeError("dispatcher is fatal")
        self.discarded += len(self.pending)
        self.pending.clear()
        self._pending_idrs.clear()
        self.gap_since = None
        self.gap_sequence = None
        self.next_sequence = None
        self._reference_base_sequence = None
        self.state = "WAIT_IDR"
        try:
            if self.reset_decoder(reason) is False:
                raise RuntimeError("decoder did not acknowledge reset")
        except BaseException as exc:
            self._fatal(exc, retire=False)  # no retry of an uncertain reset
        self.resets += 1

    def _drain(self):
        while self.next_sequence in self.pending:
            is_idr = self.next_sequence in self._pending_idrs
            self._pending_idrs.discard(self.next_sequence)
            self._send(self.pending.pop(self.next_sequence), is_idr=is_idr)
        if not self.pending:
            self.gap_since = None
            self.gap_sequence = None
        elif self.gap_sequence != self.next_sequence:
            self.gap_sequence = self.next_sequence
            self.gap_since = self._now()

    def tick(self):
        if self.state == "FATAL":
            raise RuntimeError("dispatcher is fatal")
        now = self._now()
        if self.gap_since is not None and now - self.gap_since >= self.gap_timeout_s:
            self.sequence_gap_timeouts += 1
            candidates = [au for seq, au in sorted(self.pending.items())
                          if seq in self._pending_idrs]
            # Keep the independently decodable suffix, including subsequent
            # reference frames. Throwing it away would create a new gap even
            # though all its bytes have already arrived.
            retained = ({key: au for key, au in self.pending.items()
                         if key >= candidates[0].sequence} if candidates else {})
            retained_idrs = self._pending_idrs.intersection(retained)
            self.discontinuity("complete AU sequence gap timeout")
            if candidates:
                self.discarded -= len(retained)
                self.pending = retained
                self._pending_idrs = retained_idrs
                self._pending_idrs.discard(candidates[0].sequence)
                self._send(self.pending.pop(candidates[0].sequence), is_idr=True)
                self._drain()

    def push(self, au):
        if self.state == "FATAL":
            raise RuntimeError("dispatcher is fatal")
        try:
            if type(au) is not EncodedAccessUnit:
                raise TypeError("immutable EncodedAccessUnit required")
            if type(au.annexb) is not bytes or not 0 < len(au.annexb) <= self.max_au_bytes:
                raise ValueError("AU must contain bounded immutable Annex-B bytes")
            inspection = validate_access_unit(au, max_au_bytes=self.max_au_bytes)
            is_idr = inspection.structural_self_contained_idr
            metadata = (au.session_id, au.width, au.height, au.sequence)
            if any(type(value) is not int for value in metadata):
                raise TypeError("AU metadata must contain integers")
        except BaseException as exc:
            self._fatal(exc)
        if (au.session_id, au.width, au.height) != (self.session_id, self.width, self.height):
            self._fatal(ValueError("session or dimensions changed"))
        if type(au.sequence) is not int or not 0 <= au.sequence <= 0xFFFFFFFF:
            self._fatal(ValueError("AU sequence must be uint32"))
        self.tick()
        if au.sequence <= self.high_watermark:
            self.discarded += 1
            return
        if self.state == "WAIT_IDR":
            if is_idr:
                self._send(au, is_idr=True)
            else:
                self.discarded += 1
            return
        if au.sequence == self.next_sequence:
            self._send(au, is_idr=is_idr)
            self._drain()
            return
        old = self.pending.get(au.sequence)
        if old is not None:
            if old != au:
                self._fatal(ValueError("conflicting complete AU identity"))
            self.discarded += 1
            return
        if (len(self.pending) >= self.capacity or
                sum(len(item.annexb) for item in self.pending.values()) + len(au.annexb)
                > self.max_pending_bytes):
            self.overflows += 1
            self.discontinuity("encoded reorder capacity exceeded")
            if is_idr:
                self._send(au, is_idr=True)
            else:
                self.discarded += 1
            return
        self.pending[au.sequence] = au
        if is_idr:
            self._pending_idrs.add(au.sequence)
        if self.gap_since is None:
            self.gap_sequence = self.next_sequence
            self.gap_since = self._now()

    def finish(self):
        """Report ordering completion, NOT decoder/display completion."""
        self.tick()
        return {"ordering_complete": (self.state == "RUNNING" and
                                      self.next_sequence is not None and not self.pending
                                      and self.gap_since is None and self.gap_sequence is None),
                "state": self.state, "submitted_aus": self.submitted,
                "reference_chain_valid": self.state == "RUNNING",
                "ended_waiting_for_idr": self.state == "WAIT_IDR",
                "overflows": self.overflows,
                "sequence_gap_timeouts": self.sequence_gap_timeouts,
                "discarded_aus": self.discarded, "decoder_resets": self.resets,
                "pending_bytes": sum(len(item.annexb) for item in self.pending.values()),
                "reorder_pending": len(self.pending), "decoder_outputs_verified": False}
