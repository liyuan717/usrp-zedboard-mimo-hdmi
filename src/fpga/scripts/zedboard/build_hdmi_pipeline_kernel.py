#!/usr/bin/env python3
"""Build an isolated ZedBoard display/RF diagnostic kernel output tree.

This producer copies a known-good complete ARM kernel output tree before
changing its configuration.  It never writes the supplied baseline output or
the supplied source tree.  The result contains a zImage, modules, config,
Module.symvers, a machine-readable receipt and a module/alias audit.

If a host interruption terminates a fresh producer after configuration but
before ``make zImage modules`` reaches a terminal status, ``--resume`` is a
strictly narrower recovery path.  It reuses only that producer's existing
private O directory, verifies its resolved configuration and copy isolation,
then runs the same full target.  It never recopies the baseline, reapplies the
fragment, or overwrites a previous exit receipt.

Run this *on the Linux build VM*, not on the target board.  The parent output
directory must be new; refusing reuse prevents an old partial build from
being mistaken for a fresh artifact.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterable


REQUIRED_EXACT = {
    "DEBUG_FS": "y",
    "CF_AXI_DDS": "y",
    "I2C_XILINX": "m",
    "COMMON_CLK_AXI_CLKGEN": "m",
    "DRM": "y",
    "DRM_KMS_HELPER": "y",
    "DRM_BRIDGE": "y",
    "DRM_GEM_DMA_HELPER": "y",
    "DRM_I2C_ADV7511": "m",
    "DRM_ADI_AXI_HDMI": "n",
    "DRM_FBDEV_EMULATION": "n",
}
REQUIRED_NON_N = ("DRM_MIPI_DSI", "REGMAP_I2C")
REQUIRED_MODULE_STEMS = ("i2c-xiic.ko", "clk-axi-clkgen.ko", "adv7511.ko")
SOURCE_ANCHORS = (
    "Makefile",
    "Kconfig",
    "drivers/gpu/drm/bridge/adv7511/Kconfig",
    "drivers/gpu/drm/bridge/adv7511/Makefile",
    "drivers/i2c/busses/i2c-xiic.c",
    "drivers/clk/clk-axi-clkgen.c",
    "drivers/iio/frequency/cf_axi_dds.c",
)
BASELINE_GUARDS = (
    ".config",
    "vmlinux",
    "Module.symvers",
    "arch/arm/boot/zImage",
    "include/config/auto.conf",
)


class BuildError(RuntimeError):
    pass


def run(command: list[str], *, cwd: Path | None = None, log=None) -> None:
    printable = " ".join(command)
    print(f"+ {printable}", flush=True)
    if log is not None:
        log.write(f"+ {printable}\n")
        log.flush()
    proc = subprocess.Popen(
        command,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        sys.stdout.write(line)
        if log is not None:
            log.write(line)
    if proc.wait() != 0:
        raise BuildError(f"command failed ({proc.returncode}): {printable}")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def hash_existing(root: Path, relative_paths: Iterable[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for relative in relative_paths:
        candidate = root / relative
        if not candidate.is_file():
            raise BuildError(f"required guard file missing: {candidate}")
        result[relative] = sha256(candidate)
    return result


def same_inode(left: Path, right: Path) -> bool:
    left_stat = left.stat()
    right_stat = right.stat()
    return (left_stat.st_dev, left_stat.st_ino) == (right_stat.st_dev, right_stat.st_ino)


def ensure_no_baseline_writer(baseline: Path) -> None:
    """Reject only processes explicitly operating on the immutable baseline O."""
    output = subprocess.check_output(["ps", "-eo", "pid=,args="], text=True)
    # A producer and its ssh/launcher shell necessarily mention --baseline-o;
    # that is a read-only input, not a write.  Kbuild writes an output tree
    # through O=<tree> (or, less commonly, with that tree as -C).  Restrict the
    # guard to those concrete writer forms so a recovery launcher cannot
    # mistake its own command line for a baseline writer.
    matches = []
    for line in output.splitlines():
        fields = line.strip().split(None, 1)
        if len(fields) != 2:
            continue
        arguments = fields[1]
        writer_forms = (
            f"O={baseline}",
            f"-C {baseline}",
            f"--out={baseline}",
            f"--out {baseline}",
        )
        if not any(form in arguments for form in writer_forms):
            continue
        matches.append(line.strip())
    if matches:
        raise BuildError("baseline output appears active; refusing copy:\n" + "\n".join(matches))


def parse_fragment(fragment: Path) -> list[tuple[str, str]]:
    operations: list[tuple[str, str]] = []
    enabled = re.compile(r"^CONFIG_([A-Z0-9_]+)=(y|m)$")
    disabled = re.compile(r"^# CONFIG_([A-Z0-9_]+) is not set$")
    for raw in fragment.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        match = enabled.match(line)
        if match:
            operations.append((match.group(1), match.group(2)))
            continue
        match = disabled.match(line)
        if match:
            operations.append((match.group(1), "n"))
    if not operations:
        raise BuildError(f"no Kconfig settings parsed from fragment: {fragment}")
    return operations


def apply_fragment(source: Path, output: Path, fragment: Path, cross: str, jobs: int, log) -> None:
    config_tool = source / "scripts" / "config"
    if not config_tool.is_file() or not os.access(config_tool, os.X_OK):
        raise BuildError(f"missing executable scripts/config: {config_tool}")
    config_path = output / ".config"
    for symbol, state in parse_fragment(fragment):
        option = {"y": "--enable", "m": "--module", "n": "--disable"}[state]
        run([str(config_tool), "--file", str(config_path), option, symbol], log=log)
    # Kconfig, rather than a hand-written dependency guess, resolves the
    # ADV7511 selected MIPI DSI/REGMAP dependencies and hidden symbols.
    run(
        [
            "make",
            "-C",
            str(source),
            f"O={output}",
            "ARCH=arm",
            f"CROSS_COMPILE={cross}",
            f"-j{jobs}",
            "olddefconfig",
        ],
        log=log,
    )


def config_state(config: Path, symbol: str) -> str:
    enabled = re.compile(rf"^CONFIG_{re.escape(symbol)}=(y|m)$")
    disabled = re.compile(rf"^# CONFIG_{re.escape(symbol)} is not set$")
    for raw in config.read_text(encoding="utf-8").splitlines():
        match = enabled.match(raw)
        if match:
            return match.group(1)
        if disabled.match(raw):
            return "n"
    return "absent"


def audit_config(config: Path) -> dict[str, str]:
    resolved = {symbol: config_state(config, symbol) for symbol in REQUIRED_EXACT}
    resolved.update({symbol: config_state(config, symbol) for symbol in REQUIRED_NON_N})
    failures = [
        f"CONFIG_{symbol}={resolved[symbol]!r}, expected {expected!r}"
        for symbol, expected in REQUIRED_EXACT.items()
        if resolved[symbol] != expected
    ]
    failures.extend(
        f"CONFIG_{symbol} must be selected/non-n, got {resolved[symbol]!r}"
        for symbol in REQUIRED_NON_N
        if resolved[symbol] not in {"y", "m"}
    )
    if failures:
        raise BuildError("final .config closure failed:\n" + "\n".join(failures))
    return resolved


def find_module(output: Path, stem: str) -> Path:
    hits = list(output.rglob(stem))
    if len(hits) != 1:
        raise BuildError(f"expected exactly one {stem} under {output}, got {hits}")
    return hits[0]


def collect_module_metadata(modules: Iterable[Path]) -> dict[str, dict[str, object]]:
    metadata: dict[str, dict[str, object]] = {}
    for module in modules:
        # The minimal build VM deliberately does not ship userspace kmod
        # (and therefore no `modinfo`).  ELF .modinfo is the authoritative
        # data that modinfo would read, and host readelf can inspect ARM .ko
        # files without executing them.
        lines = subprocess.check_output(
            ["readelf", "-p", ".modinfo", str(module)], text=True, stderr=subprocess.STDOUT
        )
        fields: dict[str, list[str]] = {}
        for line in lines.splitlines():
            if "]" not in line:
                continue
            payload = line.split("]", 1)[1].strip()
            if "=" not in payload:
                continue
            key, value = payload.split("=", 1)
            fields.setdefault(key.strip(), []).append(value.strip())
        metadata[str(module)] = {
            "sha256": sha256(module),
            "name": fields.get("name", []),
            "depends": fields.get("depends", []),
            "alias": fields.get("alias", []),
            "vermagic": fields.get("vermagic", []),
        }
    return metadata


def require_regular_copy(baseline: Path, copied: Path) -> None:
    for relative in BASELINE_GUARDS:
        source = baseline / relative
        target = copied / relative
        if same_inode(source, target):
            raise BuildError(f"copy shares inode with baseline (hardlink forbidden): {relative}")


def finalise_receipt(
    *,
    receipt: dict[str, object],
    source: Path,
    baseline: Path,
    copied: Path,
    output_root: Path,
) -> None:
    """Perform post-build guards and populate terminal artifact evidence."""
    receipt["source_anchor_sha256_after"] = hash_existing(source, SOURCE_ANCHORS)
    receipt["baseline_guard_sha256_after"] = hash_existing(baseline, BASELINE_GUARDS)
    if receipt["source_anchor_sha256_after"] != receipt["source_anchor_sha256_before"]:
        raise BuildError("source guard changed during build")
    if receipt["baseline_guard_sha256_after"] != receipt["baseline_guard_sha256_before"]:
        raise BuildError("baseline O guard changed during build")

    receipt["config"] = audit_config(copied / ".config")
    artifacts = {
        "zImage": copied / "arch/arm/boot/zImage",
        "vmlinux": copied / "vmlinux",
        "Module.symvers": copied / "Module.symvers",
        "config": copied / ".config",
    }
    for label, artifact in artifacts.items():
        if not artifact.is_file() or artifact.stat().st_size == 0:
            raise BuildError(f"missing/empty final artifact {label}: {artifact}")
    receipt["artifacts"] = {
        label: {"path": str(artifact), "bytes": artifact.stat().st_size, "sha256": sha256(artifact)}
        for label, artifact in artifacts.items()
    }
    module_paths = [find_module(copied, stem) for stem in REQUIRED_MODULE_STEMS]
    receipt["module_audit"] = collect_module_metadata(module_paths)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--baseline-o", type=Path, required=True)
    parser.add_argument("--fragment", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--cross-compile", required=True)
    parser.add_argument("--jobs", type=int, choices=(1, 2), default=1)
    parser.add_argument(
        "--resume",
        action="store_true",
        help=(
            "resume an interrupted producer in this exact private --out directory; "
            "does not copy the baseline or modify the saved .config"
        ),
    )
    args = parser.parse_args()

    source = args.source.resolve(strict=True)
    baseline = args.baseline_o.resolve(strict=True)
    fragment = args.fragment.resolve(strict=True)
    output_root = args.out.resolve()
    copied = output_root / "o"
    if args.resume:
        if not output_root.is_dir() or not copied.is_dir() or not (copied / ".config").is_file():
            raise BuildError(f"resume requires an existing configured private O directory: {copied}")
        for terminal_name in ("receipt.json", "receipt.failed.json", "resume.receipt.json", "resume.receipt.failed.json"):
            if (output_root / terminal_name).exists():
                raise BuildError(f"resume refuses an output directory with an existing exit receipt: {output_root / terminal_name}")
    elif output_root.exists():
        raise BuildError(f"fresh output required; refusing to overwrite: {output_root}")
    if not (source / "Makefile").is_file() or not (baseline / ".config").is_file():
        raise BuildError("source or baseline O is not a usable kernel tree")
    ensure_no_baseline_writer(baseline)
    if not args.resume:
        output_root.parent.mkdir(parents=True, exist_ok=True)

    receipt: dict[str, object] = {
        "schema": 2,
        "producer": Path(__file__).name,
        "mode": "resume" if args.resume else "fresh",
        "started_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "host": platform.node(),
        "source": str(source),
        "baseline_output": str(baseline),
        "fragment": str(fragment),
        "cross_compile": args.cross_compile,
        "jobs": args.jobs,
        "design_choice": {
            "DRM_ADI_AXI_HDMI": "n",
            "reason": "external presentation module, built against this exact Module.symvers",
            "framebuffer_emulation": "n; native KMS modeset",
        },
        "source_anchor_sha256_before": hash_existing(source, SOURCE_ANCHORS),
        "baseline_guard_sha256_before": hash_existing(baseline, BASELINE_GUARDS),
    }

    if args.resume:
        # Both checks are deliberately before any make invocation.  A resume
        # is safe only when it retains the already-resolved closure and the
        # private O tree is still a regular (non-hardlinked) baseline copy.
        require_regular_copy(baseline, copied)
        receipt["config"] = audit_config(copied / ".config")
        initial_log = output_root / "kernel-build.log"
        if not initial_log.is_file():
            raise BuildError(f"resume provenance missing initial producer log: {initial_log}")
        receipt["interrupted_producer_log"] = {
            "path": str(initial_log),
            "sha256": sha256(initial_log),
        }
        log_path = output_root / "kernel-resume.log"
        receipt_path = output_root / "resume.receipt.json"
        failed_receipt_path = output_root / "resume.receipt.failed.json"
    else:
        output_root.mkdir()
        log_path = output_root / "kernel-build.log"
        receipt_path = output_root / "receipt.json"
        failed_receipt_path = output_root / "receipt.failed.json"
    try:
        with log_path.open("w", encoding="utf-8") as log:
            if args.resume:
                log.write("# resuming interrupted producer; O/config are immutable recovery inputs\n")
                log.flush()
            else:
                run(["cp", "-a", str(baseline / "."), str(copied)], log=log)
                require_regular_copy(baseline, copied)
                apply_fragment(source, copied, fragment, args.cross_compile, args.jobs, log)
                receipt["config"] = audit_config(copied / ".config")
            run(
                [
                    "make",
                    "-C",
                    str(source),
                    f"O={copied}",
                    "ARCH=arm",
                    f"CROSS_COMPILE={args.cross_compile}",
                    f"-j{args.jobs}",
                    "zImage",
                    "modules",
                ],
                log=log,
            )

        finalise_receipt(
            receipt=receipt,
            source=source,
            baseline=baseline,
            copied=copied,
            output_root=output_root,
        )
        receipt["finished_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
        receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps(receipt, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        receipt["failed_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
        receipt["error"] = str(exc)
        failed_receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        raise


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BuildError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(2)
