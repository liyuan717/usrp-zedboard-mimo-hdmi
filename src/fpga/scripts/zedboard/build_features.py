"""Canonical build-time feature selection for competition artifacts."""
from __future__ import annotations

from collections.abc import Mapping

DISPLAY_MODES = ("off", "static", "dynamic")
VARIANTS = ("nohdmi", "hdmi-static", "hdmi-dynamic")


def _binary(name: str, value: str) -> int:
    if value not in ("0", "1"):
        raise ValueError(f"{name} must be 0 or 1")
    return int(value)


def resolve(env: Mapping[str, str]) -> dict[str, str | bool]:
    """Resolve canonical ENABLE_HDMI/DISPLAY_MODE values.

    MIMO_HDMI_DEMO is accepted only as a compatibility alias. Supplying both
    names is safe only when they agree.
    """
    canonical = env.get("ENABLE_HDMI")
    legacy = env.get("MIMO_HDMI_DEMO")
    if canonical is not None and legacy is not None:
        if _binary("ENABLE_HDMI", canonical) != _binary("MIMO_HDMI_DEMO", legacy):
            raise ValueError("ENABLE_HDMI conflicts with MIMO_HDMI_DEMO")
    raw_enabled = canonical if canonical is not None else legacy
    enable_hdmi = _binary("ENABLE_HDMI", raw_enabled) if raw_enabled is not None else 0

    display_mode = env.get("DISPLAY_MODE", "off").strip().lower()
    if display_mode not in DISPLAY_MODES:
        raise ValueError("DISPLAY_MODE must be off, static or dynamic")
    if not enable_hdmi and display_mode != "off":
        raise ValueError("ENABLE_HDMI=0 requires DISPLAY_MODE=off")
    return {
        "enable_hdmi": bool(enable_hdmi),
        "display_mode": display_mode,
    }


def variant_features(variant: str) -> dict[str, str | bool]:
    if variant == "nohdmi":
        return {"enable_hdmi": False, "display_mode": "off"}
    if variant == "hdmi-static":
        return {"enable_hdmi": True, "display_mode": "static"}
    if variant == "hdmi-dynamic":
        return {"enable_hdmi": True, "display_mode": "dynamic"}
    raise ValueError(f"unknown competition variant: {variant}")


if __name__ == "__main__":
    import json
    import os

    print(json.dumps(resolve(os.environ), sort_keys=True))
