#!/usr/bin/env python3
"""Run a verified H.264 fixture through the private native delivery path.

This is a board-side diagnostic runner, not the RF receive loop.  It proves
that one validated fixture can traverse ``helper stdout -> presenter`` without
Python receiving RGB565 pixels.  The persistent RF owner should use
``RFNativeEncodedVideoSession`` instead.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path

from mimo_host.native_video_pipeline import NativeVideoPipeline
from mimo_host.rf_encoded_video import EncodedAccessUnit, ZVID2_FLAGS_IDR, ZVID2_FLAGS_PPS, ZVID2_FLAGS_SPS


_MAX_RECORDS = 4096


class _SourcePacer:
    """Bound fixture submission to its existing millisecond source PTS.

    This is deliberately not a player clock: it never changes an AU's PTS
    and it makes no claim about a presenter's physical vblank timing.  It
    only prevents a file fixture from being submitted as an artificial burst.
    """

    def __init__(self, enabled: bool, access_units: list[EncodedAccessUnit]) -> None:
        self.enabled = enabled
        self._wake = threading.Event()
        self._first_pts_ms = access_units[0].source_timestamp_ms if access_units else None
        self._anchor_ns: int | None = None
        self._first_target_ns: int | None = None
        self._last_target_ns: int | None = None
        self._first_actual_ns: int | None = None
        self._last_actual_ns: int | None = None
        self._max_late_ns = 0
        self._late_frames = 0

    def wait_for(self, access_unit: EncodedAccessUnit) -> None:
        if not self.enabled:
            return
        if self._first_pts_ms is None:
            raise RuntimeError("cannot pace an empty fixture")
        if self._anchor_ns is None:
            self._anchor_ns = time.monotonic_ns()
        target = self._anchor_ns + (access_unit.source_timestamp_ms - self._first_pts_ms) * 1_000_000
        remaining_ns = target - time.monotonic_ns()
        if remaining_ns > 0:
            # Event.wait is interruptible and avoids a busy spin as requested.
            self._wake.wait(remaining_ns / 1_000_000_000)
        actual = time.monotonic_ns()
        late = max(0, actual - target)
        self._first_target_ns = target if self._first_target_ns is None else self._first_target_ns
        self._last_target_ns = target
        self._first_actual_ns = actual if self._first_actual_ns is None else self._first_actual_ns
        self._last_actual_ns = actual
        self._max_late_ns = max(self._max_late_ns, late)
        self._late_frames += int(late > 0)

    def receipt(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "policy": "source_pts_ms_anchored_monotonic_event_wait" if self.enabled else "unpaced_capacity_only",
            "source_first_pts_ms": self._first_pts_ms,
            "anchor_monotonic_ns": self._anchor_ns,
            "first_target_monotonic_ns": self._first_target_ns,
            "last_target_monotonic_ns": self._last_target_ns,
            "first_actual_submit_monotonic_ns": self._first_actual_ns,
            "last_actual_submit_monotonic_ns": self._last_actual_ns,
            "late_frames": self._late_frames,
            "max_late_ns": self._max_late_ns,
        }


def _require_fixture_pacing(backend: str, enabled: bool) -> None:
    """Keep capacity measurements distinct from paced KMS fixture audit."""
    if backend == "kms" and not enabled:
        raise ValueError("KMS fixture audit requires explicit --pace-source; unpaced mode is capacity-only")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_digest(path: Path, *, require_elf: bool) -> dict[str, object]:
    resolved = path.resolve(strict=True)
    if not resolved.is_file():
        raise ValueError(f"runtime path is not a regular file: {path}")
    payload = resolved.read_bytes()
    if require_elf and not payload.startswith(b"\x7fELF"):
        raise ValueError(f"runtime executable is not an ELF: {resolved}")
    return {"path": os.fspath(resolved), "bytes": len(payload), "sha256": _sha256(payload)}


def _scrubbed_environment() -> dict[str, str]:
    return {key: value for key, value in os.environ.items()
            if key not in {"LD_PRELOAD", "LD_AUDIT", "LD_LIBRARY_PATH"}}


def _verify_loader_closure(role: str, item: dict[str, object], declared: list[dict[str, object]], launch_argv: list[str], elf_index: int) -> list[dict[str, object]]:
    """Run the explicitly configured dynamic loader, then hash its real paths."""
    command = item.get("loader_list_argv")
    if not isinstance(command, list) or not 2 <= len(command) <= 32 or any(not isinstance(part, str) or not part for part in command):
        raise ValueError(f"{role} requires explicit loader_list_argv")
    if "--list" not in command:
        raise ValueError(f"{role} loader_list_argv must use --list")
    launch_prefix = launch_argv[:elf_index + 1]
    expected_list = launch_prefix[:elf_index] + ["--list", launch_prefix[elf_index]]
    if command != expected_list:
        raise ValueError(f"{role} loader_list_argv must bind loader options through pinned ELF only")
    loader = _file_digest(Path(command[0]), require_elf=True)
    _same_digest(item.get("loader_sha256"), loader, f"{role} loader")
    try:
        result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=_scrubbed_environment(), timeout=5.0, check=False, text=True)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f"{role} loader --list failed: {exc}") from exc
    if result.returncode != 0 or len(result.stdout) > 256 * 1024 or len(result.stderr) > 64 * 1024:
        raise ValueError(f"{role} loader --list returned {result.returncode}")
    resolved = set(re.findall(r"=>\s+(/[^\s(]+)|^\s*(/[^\s(]+)\s+\(", result.stdout, flags=re.MULTILINE))
    paths = {next(part for part in pair if part) for pair in resolved}
    actual = [_file_digest(Path(path), require_elf=False) for path in sorted(paths)]
    declared_paths = {entry["path"] for entry in declared}
    actual_paths = {entry["path"] for entry in actual}
    if actual_paths != declared_paths:
        raise ValueError(f"{role} loader resolution does not equal declared library closure")
    if {entry["sha256"] for entry in actual} != {entry["sha256"] for entry in declared}:
        raise ValueError(f"{role} loader resolved library hash mismatch")
    return actual


def _same_digest(expected: object, actual: dict[str, object], label: str) -> None:
    if not isinstance(expected, str) or len(expected) != 64 or expected != actual["sha256"]:
        raise ValueError(f"{label} SHA-256 mismatch")


def _load_runtime_closure(path: Path) -> tuple[list[str], list[str], dict[str, str], dict[str, object]]:
    """Load explicitly pinned executable/library closure, never infer it from argv."""
    raw_bytes = path.read_bytes()
    raw = json.loads(raw_bytes.decode("utf-8"))
    if not isinstance(raw, dict) or raw.get("schema") != "native-video-runtime-closure-v1":
        raise ValueError("unsupported runtime closure schema")
    output: dict[str, object] = {"config_path": os.fspath(path.resolve()), "config_sha256": _sha256(raw_bytes)}
    argv_by_role: dict[str, list[str]] = {}
    helper_env: dict[str, str] = {}
    for role in ("helper", "presenter"):
        item = raw.get(role)
        if not isinstance(item, dict):
            raise ValueError(f"runtime closure missing {role}")
        argv, index, elf, libraries = item.get("argv"), item.get("elf_argv_index"), item.get("elf"), item.get("libraries")
        if (not isinstance(argv, list) or not 1 <= len(argv) <= 32 or
                any(not isinstance(part, str) or not part or len(part) > 4096 for part in argv) or
                not isinstance(index, int) or not 0 <= index < len(argv) or not isinstance(elf, dict) or
                not isinstance(libraries, list) or not 1 <= len(libraries) <= 256):
            raise ValueError(f"invalid {role} runtime closure")
        elf_path, elf_hash = elf.get("path"), elf.get("sha256")
        if not isinstance(elf_path, str) or Path(elf_path).is_absolute() is False:
            raise ValueError(f"{role} elf path must be absolute")
        actual_elf = _file_digest(Path(elf_path), require_elf=True)
        _same_digest(elf_hash, actual_elf, f"{role} ELF")
        if os.path.realpath(argv[index]) != actual_elf["path"]:
            raise ValueError(f"{role} elf_argv_index does not name the pinned ELF")
        actual_libraries = []
        for lib in libraries:
            if not isinstance(lib, dict) or not isinstance(lib.get("path"), str) or not Path(lib["path"]).is_absolute():
                raise ValueError(f"invalid {role} library closure entry")
            actual = _file_digest(Path(lib["path"]), require_elf=False)
            _same_digest(lib.get("sha256"), actual, f"{role} library")
            actual_libraries.append(actual)
        actual_libraries = _verify_loader_closure(role, item, actual_libraries, list(argv), index)
        output[role] = {"elf": actual_elf, "elf_argv_index": index, "loader": _file_digest(Path(item["loader_list_argv"][0]), require_elf=True), "libraries": actual_libraries}
        if role == "presenter":
            backend = item.get("backend")
            if backend not in {"null", "kms"}:
                raise ValueError("presenter closure requires backend null or kms")
            output[role]["backend"] = backend
        argv_by_role[role] = list(argv)
        if role == "helper":
            declared_env = item.get("environment", {})
            if not isinstance(declared_env, dict):
                raise ValueError("helper environment must be an object")
            allowed_env = {"MIMO_CODEC_CONVERTER": {"sws", "neon601"},
                           "MIMO_CODEC_CRC": {"slice8"}, "MIMO_CODEC_PROFILE": {"0", "1"}}
            for name, value in declared_env.items():
                if name not in allowed_env or not isinstance(value, str) or value not in allowed_env[name]:
                    raise ValueError(f"unapproved helper environment selector: {name}")
                helper_env[name] = value
    output["helper_environment"] = dict(sorted(helper_env.items()))
    return argv_by_role["helper"], argv_by_role["presenter"], helper_env, output


def _stderr_text(raw: bytes, limit: int = 64 * 1024) -> str:
    """Bounded diagnostic text only; runtime environment is never emitted."""
    return raw[-limit:].decode("utf-8", errors="replace")


def _extract_helper_profile(stderr_tail: str) -> dict[str, object] | None:
    matches = re.findall(r"^CODEC_PROFILE_V1 (\{.*\})$", stderr_tail, flags=re.MULTILINE)
    if not matches:
        return None
    parsed = json.loads(matches[-1])
    if (not isinstance(parsed, dict) or not isinstance(parsed.get("converter_impl"), str) or
            not isinstance(parsed.get("crc_impl"), str) or not isinstance(parsed.get("stages"), dict)):
        raise ValueError("helper profile has invalid schema")
    return parsed


def _load_fixture(manifest_path: Path, session_id: int, max_frames: int) -> tuple[list[EncodedAccessUnit], dict[str, object]]:
    manifest_bytes = manifest_path.read_bytes()
    raw = json.loads(manifest_bytes.decode("utf-8"))
    if not isinstance(raw, dict) or raw.get("schema") != "rf-video-real-h264-fixture-v1":
        raise ValueError("unsupported fixture schema")
    width, height, fps, frames = raw.get("width"), raw.get("height"), raw.get("fps"), raw.get("frames")
    if (width, height) != (1024, 600) or fps not in {24, 30, 60} or raw.get("color_contract") != "bt601-limited" or not isinstance(frames, list) or not frames:
        raise ValueError("fixture must be non-empty 1024x600 at 24, 30, or 60 fps")
    root = manifest_path.parent.resolve()
    if len(frames) > _MAX_RECORDS:
        raise ValueError("fixture has more than 4096 AUs")
    source_path = (root / raw.get("source_path", "source.h264")).resolve()
    if root not in source_path.parents or not source_path.is_file():
        raise ValueError("fixture source stream is absent or escapes manifest directory")
    source_stream = _file_digest(source_path, require_elf=False)
    if raw.get("source_bytes") != source_stream["bytes"] or raw.get("source_sha256") != source_stream["sha256"]:
        raise ValueError("fixture source stream integrity mismatch")
    access_units: list[EncodedAccessUnit] = []
    input_records: list[dict[str, object]] = []
    prior = -1
    for item in frames[:max_frames]:
        if not isinstance(item, dict):
            raise ValueError("fixture frame must be an object")
        sequence, relative, expected_size, expected_hash, idr = (
            item.get("sequence"), item.get("path"), item.get("bytes"), item.get("sha256"), item.get("idr"),
        )
        if not isinstance(sequence, int) or not 0 <= sequence <= 0xFFFFFFFF or sequence <= prior:
            raise ValueError("fixture sequence must be strictly increasing uint32 source IDs")
        if not isinstance(relative, str) or Path(relative).is_absolute():
            raise ValueError("fixture path must be relative")
        path = (root / relative).resolve()
        if root not in path.parents or not path.is_file():
            raise ValueError("fixture frame escapes manifest directory or is absent")
        payload = path.read_bytes()
        if (not isinstance(expected_size, int) or len(payload) != expected_size or
                not isinstance(expected_hash, str) or hashlib.sha256(payload).hexdigest() != expected_hash):
            raise ValueError(f"fixture frame {sequence} integrity mismatch")
        flags = (ZVID2_FLAGS_IDR | ZVID2_FLAGS_SPS | ZVID2_FLAGS_PPS) if idr else 0
        # The production helper ABI is milliseconds.  This fixture has no
        # source timestamp field; derive a deterministic ms PTS from its
        # preserved source ID only here (never use benchmark microseconds).
        access_units.append(EncodedAccessUnit(
            session_id, sequence, width, height, sequence * 1000 // int(fps), payload, flags,
        ))
        input_records.append({"sequence": sequence, "source_pts_ms": sequence * 1000 // int(fps),
                              "path": relative, "bytes": len(payload), "sha256": expected_hash, "idr": bool(idr)})
        prior = sequence
    return access_units, {
        "manifest_path": os.fspath(manifest_path.resolve()), "manifest_sha256": _sha256(manifest_bytes),
        "source_stream": source_stream, "fps": fps, "input_aus": input_records,
    }


def _load_reference(path: Path, expected_sha256: str, fixture: dict[str, object]) -> dict[str, object]:
    raw_bytes = path.read_bytes()
    if not isinstance(expected_sha256, str) or len(expected_sha256) != 64 or _sha256(raw_bytes) != expected_sha256:
        raise ValueError("reference manifest SHA-256 mismatch")
    raw = json.loads(raw_bytes.decode("utf-8"))
    required_contract = {"pixel_format": "yuv420p", "range": "limited", "matrix": "smpte170m",
                         "chroma_location": "left", "progressive": True}
    if (not isinstance(raw, dict) or raw.get("schema") != "native-codec-independent-reference-v1" or
            raw.get("fixture_manifest_sha256") != fixture["manifest_sha256"] or
            raw.get("annexb_stream_sha256") != fixture["source_stream"]["sha256"] or
            raw.get("width") != 1024 or raw.get("height") != 600 or raw.get("fps") != fixture["fps"] or
            raw.get("decoded_contract") != {**raw.get("decoded_contract", {}), **required_contract}):
        raise ValueError("reference manifest is not bound to this fixture")
    frames = raw.get("frames")
    fixture_aus = fixture["input_aus"]
    if not isinstance(frames, list) or len(frames) < len(fixture_aus):
        raise ValueError("reference manifest lacks selected AU records")
    seen_sequences = set()
    for index, au in enumerate(fixture_aus):
        record = frames[index]
        if (not isinstance(record, dict) or record.get("sequence") != au["sequence"] or
                record.get("sequence") in seen_sequences or record.get("annexb_sha256") != au["sha256"]):
            raise ValueError("reference Annex-B identity/hash mismatch")
        seen_sequences.add(record["sequence"])
    return {"path": os.fspath(path.resolve()), "sha256": _sha256(raw_bytes)}


def _write_fresh_receipt(fd: int, receipt: dict[str, object]) -> None:
    encoded = (json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    view = memoryview(encoded)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short receipt write")
        view = view[written:]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--runtime-closure", type=Path, required=True)
    parser.add_argument("--reference-manifest", type=Path, required=True)
    parser.add_argument("--expected-reference-sha256", required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--proof-output", type=Path, required=True)
    parser.add_argument("--session-id", type=int)
    parser.add_argument("--timeout-s", type=float, default=8.0)
    parser.add_argument("--max-frames", type=int, default=720)
    parser.add_argument("--pace-source", action="store_true",
                        help="pace fixture AU submission from existing source_pts_ms; required for KMS fixture audit")
    args = parser.parse_args()
    if args.session_id is None:
        args.session_id = secrets.randbits(32)
    if not 0 <= args.session_id <= 0xFFFFFFFF:
        parser.error("session-id must fit uint32")
    if not 1 <= args.max_frames <= _MAX_RECORDS:
        parser.error("max-frames must be 1..4096")
    receipt_path = args.receipt.resolve()
    proof_path = args.proof_output.resolve()
    if proof_path == receipt_path:
        parser.error("proof-output and receipt must be distinct")
    if proof_path.exists():
        parser.error("proof-output must be a fresh path (presenter creates it with O_EXCL)")
    try:
        receipt_fd = os.open(receipt_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except OSError as exc:
        parser.error(f"receipt must be a fresh writable path: {exc}")
    receipt: dict[str, object] = {"schema": "native-video-pipeline-r4-receipt-v1", "valid": False,
                                  "acceptance_semantics": "presenter_crc_valid_and_queued_not_physical_display_ack",
                                  "requested_max_frames": args.max_frames}
    pipeline = None
    started = None
    backend: str | None = None
    pacer: _SourcePacer | None = None
    try:
        helper_argv, presenter_argv, helper_env, runtime = _load_runtime_closure(args.runtime_closure)
        if "--proof-json" in presenter_argv:
            raise ValueError("runtime closure presenter argv may not supply proof-json")
        backend = runtime["presenter"]["backend"]
        _require_fixture_pacing(backend, args.pace_source)
        presenter_extra_argv = ["--backend", backend, "--proof-json", os.fspath(proof_path)]
        access_units, fixture = _load_fixture(args.manifest, args.session_id, args.max_frames)
        reference = _load_reference(args.reference_manifest, args.expected_reference_sha256, fixture)
        receipt.update({"run_nonce": secrets.token_hex(16), "source_session_id": args.session_id,
                        "runtime": runtime, "fixture": fixture, "reference": reference,
                        "selected_frames": len(access_units)})
        pacer = _SourcePacer(args.pace_source, access_units)
        receipt["loader_injection_scrubbed"] = [name for name in ("LD_PRELOAD", "LD_AUDIT", "LD_LIBRARY_PATH") if name in os.environ]
        accepted: list[dict[str, object]] = []
        expected = {(au.session_id, au.sequence, au.source_timestamp_ms): {"token": None, "accepted_token": None}
                    for au in access_units}
        def on_accepted(header):
            key = (header.identity.session_id, header.identity.sequence, header.identity.source_pts)
            expected_item = expected.get(key)
            if expected_item is None or expected_item["accepted_token"] is not None:
                raise RuntimeError("accepted feedback identity was not locally submitted")
            expected_item["accepted_token"] = header.identity.token
            accepted.append({"identity": header.identity.__dict__, "token": header.identity.token,
                             "accepted_payload_crc32": f"{header.payload_crc:08x}"})
        pipeline = NativeVideoPipeline(helper_argv, on_accepted, presenter_argv=presenter_argv, presenter_extra_argv=presenter_extra_argv, helper_env=helper_env,
                                       response_timeout_s=args.timeout_s)
        started = time.monotonic_ns()
        pipeline.start()
        for access_unit in access_units:
            pacer.wait_for(access_unit)
            token = pipeline.submit_wait(access_unit, args.timeout_s)
            expected_item = expected[(access_unit.session_id, access_unit.sequence, access_unit.source_timestamp_ms)]
            expected_item["token"] = token
            if expected_item["accepted_token"] is not None and expected_item["accepted_token"] != token:
                raise RuntimeError("accepted feedback token mismatched submitted token")
        pipeline.wait_idle(args.timeout_s)
        pipeline.stop(args.timeout_s)
    except BaseException as exc:
        receipt["failure"] = f"{type(exc).__name__}: {exc}"[:1024]
    finally:
        ended = time.monotonic_ns()
        if pipeline is not None:
            try:
                pipeline.close()
            except BaseException as close_exc:
                receipt["close_failure"] = f"{type(close_exc).__name__}: {close_exc}"[:1024]
            snapshot = pipeline.snapshot()
            helper_stderr, presenter_stderr = pipeline.diagnostics()
            helper_tail = _stderr_text(helper_stderr)
            presenter_tail = _stderr_text(presenter_stderr)
            try:
                helper_profile = _extract_helper_profile(helper_tail)
            except BaseException as profile_exc:
                helper_profile = None
                receipt["profile_failure"] = f"{type(profile_exc).__name__}: {profile_exc}"[:1024]
            receipt["terminal"] = {"state": snapshot.state, "pending": snapshot.pending,
                                   "workers_alive": list(snapshot.workers_alive), "stop_ack": snapshot.stop_ack,
                                   "helper_returncode": snapshot.helper_returncode,
                                   "presenter_returncode": snapshot.presenter_returncode,
                                   "failure": snapshot.failure}
            receipt["diagnostics"] = {"helper_stderr_tail": helper_tail,
                                      "presenter_stderr_tail": presenter_tail,
                                      "helper_stderr_retained_bytes": snapshot.helper_stderr_bytes_retained,
                                      "helper_stderr_dropped_bytes": snapshot.helper_stderr_bytes_dropped,
                                      "presenter_stderr_retained_bytes": snapshot.presenter_stderr_bytes_retained,
                                      "presenter_stderr_dropped_bytes": snapshot.presenter_stderr_bytes_dropped}
            receipt["helper_profile"] = helper_profile
        receipt["monotonic_start_ns"] = started
        receipt["monotonic_end_ns"] = ended
        receipt["duration_ns"] = None if started is None else ended - started
        receipt["source_pacing"] = (pacer.receipt() if pacer is not None else {
            "enabled": bool(args.pace_source), "policy": "not_started"})
        receipt["accepted"] = locals().get("accepted", [])
        if proof_path.is_file() and backend is not None:
            proof_bytes = proof_path.read_bytes()
            try:
                proof = json.loads(proof_bytes.decode("utf-8"))
                records = proof.get("records", []) if isinstance(proof, dict) else []
                expected_sequences = {item["sequence"] for item in receipt.get("fixture", {}).get("input_aus", [])}
                ordered_physical = (isinstance(records, list) and len(records) == receipt.get("selected_frames") and
                                    [item.get("source_frame_id") for item in records if isinstance(item, dict)] ==
                                    [item["sequence"] for item in receipt.get("fixture", {}).get("input_aus", [])] and
                                    all(isinstance(item, dict) and item.get("source_session_id") == args.session_id and
                                        item.get("source_frame_id") in expected_sequences for item in records))
                null_functional = (backend == "null" and isinstance(records, list) and not records and
                                   type(proof.get("presented_at_hdmi_boundary")) is int and
                                   proof.get("presented_at_hdmi_boundary") == 0 and proof.get("physical") is False and
                                   proof.get("capture_status") == "complete" and proof.get("proof_failures") == 0 and
                                   proof.get("backend_failures") == 0 and proof.get("capture_invalidated") is False and
                                   proof.get("failed_closed") is False)
                proof_identity_ok = null_functional or (backend == "kms" and ordered_physical)
            except BaseException:
                proof_identity_ok = False
            receipt["proof"] = {"path": os.fspath(proof_path), "bytes": len(proof_bytes),
                                "sha256": _sha256(proof_bytes), "backend": backend,
                                "functional_valid": proof_identity_ok,
                                "physical_acceptance": backend == "kms" and proof_identity_ok}
        try:
            valid = (pipeline is not None and receipt.get("terminal", {}).get("state") == "stopped" and
                     len(receipt["accepted"]) == receipt.get("selected_frames") and not receipt.get("close_failure") and
                     not receipt.get("failure") and not receipt.get("profile_failure") and
                     receipt.get("proof", {}).get("functional_valid") is True)
            receipt["valid"] = bool(valid)
            _write_fresh_receipt(receipt_fd, receipt)
        finally:
            os.close(receipt_fd)
    if not receipt["valid"]:
        print(f"native delivery failed; invalid receipt: {receipt_path}", file=sys.stderr)
        return 1
    print(json.dumps({"schema": "native-video-pipeline-r4-run-v1", "receipt": os.fspath(receipt_path),
                      "frames": receipt["selected_frames"], "duration_ns": receipt["duration_ns"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
