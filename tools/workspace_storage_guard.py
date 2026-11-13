"""Read-only disk telemetry for archive build tools."""

import shutil
import time
from pathlib import Path

GIB = 1024**3
ROOT = Path(__file__).resolve().parents[1]


def disk():
    usage = shutil.disk_usage(ROOT)
    priority = "normal"
    if usage.free < 5 * GIB:
        priority = "handle_first"
    elif usage.free < 10 * GIB:
        priority = "inspect_usage"
    return {
        "total_bytes": usage.total,
        "used_bytes": usage.used,
        "available_bytes": usage.free,
        "available_GiB": round(usage.free / GIB, 3),
        "priority": priority,
    }


def check(phase):
    return {"phase": phase, "time_unix": time.time(), "disk": disk()}
