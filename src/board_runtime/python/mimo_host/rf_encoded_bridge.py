"""Single-owner CRC-valid MRF1 -> RVF2 -> ordered encoded-AU bridge.

No RF device, codec helper, subprocess, framebuffer, or decoder process is
opened here.  Decoder submission/reset are explicit callbacks owned by the
caller.  Callback execution is never protected by a bridge lock: use exactly
one owner thread, and do not call :meth:`consume_packet` re-entrantly.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass

from .rf_encoded_dispatch import EncodedAUDispatcher
from .rf_encoded_video import ReassembledAccessUnit, RFEncodedVideoReassembler


@dataclass(frozen=True)
class EncodedBridgeStats:
    received_mrf_packets: int
    completed_access_units: int
    recovery_events: int
    expired_access_units: int
    non_disruptive_expired_access_units: int
    rejected_packets: int
    fatal_errors: int
    state: str
    last_recovery_reason: str | None
    last_error: str | None


class RFEncodedVideoBridge:
    """Connect bounded transport reassembly to ordered decoder submission.

    Recovery-worthy corruption (MRF/RVF CRC and malformed encoded bytes) resets
    the reference chain and returns the dispatcher to ``WAIT_IDR``.  Session
    and identity/metadata conflicts are fatal because their origin is
    ambiguous; callers must construct a fresh bridge rather than silently
    mixing streams.
    """

    _FATAL_REJECTS = frozenset(
        {
            "session_mismatch",
            "identity_metadata_conflict",
            "conflicting_duplicate",
            "completed_identity_conflict",
            "session_dimension_change",
            "zvid2_sequence_identity_mismatch",
        }
    )

    def __init__(
        self,
        *,
        session_id: int,
        width: int,
        height: int,
        decoder_submit: Callable,
        decoder_reset: Callable,
        reassembler: RFEncodedVideoReassembler | None = None,
        dispatcher: EncodedAUDispatcher | None = None,
        **limits,
    ) -> None:
        if reassembler is not None and reassembler.expected_session_id != session_id:
            raise ValueError("reassembler expected session differs from bridge session")
        if dispatcher is not None and (
            dispatcher.session_id != session_id
            or dispatcher.width != width
            or dispatcher.height != height
        ):
            raise ValueError("dispatcher session/dimensions differ from bridge")
        if not callable(decoder_submit) or not callable(decoder_reset):
            raise TypeError("decoder callbacks must be callable")
        # Keep limits split by their owner.  This avoids an unbounded kwargs
        # tunnel and makes every capacity choice visible to the caller.
        reassembly_keys = {
            "rf_payload_bytes", "fragment_timeout_s", "max_frames", "max_bytes",
            "max_au_bytes", "max_fragments", "max_sessions", "clock",
        }
        dispatch_keys = {"capacity", "gap_timeout_s", "max_pending_bytes"}
        unknown = set(limits) - reassembly_keys - dispatch_keys
        if unknown:
            raise ValueError(f"unknown encoded bridge limits: {sorted(unknown)!r}")
        reassembly_limits = {key: value for key, value in limits.items() if key in reassembly_keys}
        dispatch_limits = {key: value for key, value in limits.items() if key in dispatch_keys}
        if reassembler is None:
            reassembler = RFEncodedVideoReassembler(
                expected_session_id=session_id, **reassembly_limits
            )
        elif reassembly_limits:
            raise ValueError("explicit reassembler cannot be combined with reassembly limits")
        if dispatcher is None:
            # A caller injecting a clock for deterministic reassembly expiry
            # expects the companion ordering timeout to use that same clock.
            # (``tick(now=...)`` is only an RVF2 API; the dispatcher owns its
            # own clock callback.)
            if "clock" in reassembly_limits:
                dispatch_limits["clock"] = reassembly_limits["clock"]
            dispatcher = EncodedAUDispatcher(
                session_id=session_id,
                width=width,
                height=height,
                submit=decoder_submit,
                reset_decoder=decoder_reset,
                max_au_bytes=reassembler.max_au_bytes,
                **dispatch_limits,
            )
        elif dispatch_limits:
            raise ValueError("explicit dispatcher cannot be combined with dispatch limits")
        self.session_id, self.width, self.height = session_id, width, height
        self.reassembler, self.dispatcher = reassembler, dispatcher
        self._state = "RUNNING"
        self._received = self._completed = self._recoveries = self._expired = self._rejected = 0
        self._non_disruptive_expired = 0
        self._fatal_errors = 0
        self._last_recovery: str | None = None
        self._last_error: str | None = None

    def _require_running(self) -> None:
        if self._state != "RUNNING":
            raise RuntimeError(f"encoded bridge is {self._state.lower()}")

    def _fatal(self, exc: BaseException) -> None:
        if self._state == "FATAL":
            raise RuntimeError("encoded bridge is fatal") from exc
        self._state = "FATAL"
        self._fatal_errors += 1
        self._last_error = f"{type(exc).__name__}: {exc}"
        try:
            if self.dispatcher.state != "FATAL":
                self.dispatcher.discontinuity("encoded bridge fatal transport/identity conflict")
        except BaseException as cleanup:
            self._last_error += f"; decoder reset failure: {type(cleanup).__name__}: {cleanup}"
        raise exc

    def _recover(self, reason: str) -> None:
        self._require_running()
        try:
            self.dispatcher.discontinuity(reason)
        except BaseException as exc:
            self._fatal(exc)
        self._recoveries += 1
        self._last_recovery = reason

    def transport_discontinuity(self, reason: str) -> None:
        """Explicit RF loss notification from the separate packet consumer.

        Retire predictive decoding immediately rather than waiting for an AU
        expiry. Bounded partial fragments retain their own identities/CRC and
        expiry; only a later complete independent IDR may restore references.
        """
        if type(reason) is not str or not 1 <= len(reason) <= 256:
            raise ValueError("bounded RF discontinuity reason required")
        self._recover(reason)

    def _expiry_risk_identities(self, expired) -> tuple:
        """Return incomplete AUs that can invalidate the live reference chain.

        Reassembly can retain an old partial AU while a newer self-contained
        IDR has already been submitted.  Expiring that old identity must not
        reset the healthy decoder epoch.  The dispatcher records the sequence
        of the structural self-contained IDR that started its *current*
        ordering epoch, including an IDR it submits internally after a gap
        timeout.  A partial AU at or after that boundary may be predictive
        input for the live reference chain, so this bridge conservatively
        recovers.  A lower sequence belongs to a retired epoch and is only
        accounted for.

        ``reference_base_sequence`` means only successful handoff to the
        decoder callback, not that a codec decoded or displayed the AU.  The
        bridge still never makes a codec-success claim from it.
        """
        reference_base = self.dispatcher.reference_base_sequence
        if self.dispatcher.state != "RUNNING" or reference_base is None:
            return ()
        return tuple(
            identity
            for identity in expired
            if identity.sequence >= reference_base
        )

    def _handle_expiry(self, expired) -> None:
        if not expired:
            return
        self._expired += len(expired)
        risky = self._expiry_risk_identities(expired)
        self._non_disruptive_expired += len(expired) - len(risky)
        if risky:
            self._recover("RVF2 fragment expiry affecting current reference chain")

    def _dispatch(self, complete: ReassembledAccessUnit) -> None:
        try:
            self.dispatcher.push(complete.metadata)
        except BaseException as exc:
            self._fatal(exc)
        self._completed += 1

    def consume_packet(self, stream: int, packet: bytes, *, now: float | None = None) -> None:
        """Consume one whole MRF1 packet; decoder callbacks remain synchronous."""
        self._require_running()
        try:
            complete = self.reassembler.push_rf_packet(packet, expected_stream=stream, now=now)
            expired = self.reassembler.last_expired_identities
        except BaseException as exc:
            self._fatal(exc)
        self._received += 1
        self._handle_expiry(expired)
        outcome = self.reassembler.last_outcome
        if outcome.status == "rejected":
            self._rejected += 1
            reason = outcome.reason or "unspecified RVF2 rejection"
            if reason in self._FATAL_REJECTS:
                self._fatal(ValueError(f"fatal encoded transport rejection: {reason}"))
            self._recover(f"encoded transport rejected packet: {reason}")
            return
        if complete is not None:
            self._dispatch(complete)

    def tick(self, *, now: float | None = None) -> None:
        """Process RVF2 expiry and ordered-AU gap deadlines without new RF input."""
        self._require_running()
        try:
            expired = self.reassembler.expire(now=now)
            self._handle_expiry(expired)
            self.dispatcher.tick()
        except BaseException as exc:
            self._fatal(exc)

    def finish(self, *, now: float | None = None) -> dict[str, object]:
        """Actively expire transport and report only transport/ordering state."""
        self._require_running()
        try:
            reassembly = self.reassembler.finish(now=now)
            self._handle_expiry(reassembly["expired_identities"])
            ordering = self.dispatcher.finish()
        except BaseException as exc:
            self._fatal(exc)
        self._state = "FINISHED"
        return {
            "schema": "rf-encoded-video-bridge-finish-v1",
            "state": self._state,
            "stats": asdict(self.stats),
            "reassembly": reassembly,
            "ordering": ordering,
            "decoder_output_verified": False,
            "decoded_frame_verified": False,
            "display_verified": False,
        }

    @property
    def stats(self) -> EncodedBridgeStats:
        return EncodedBridgeStats(
            self._received,
            self._completed,
            self._recoveries,
            self._expired,
            self._non_disruptive_expired,
            self._rejected,
            self._fatal_errors,
            self._state,
            self._last_recovery,
            self._last_error,
        )
