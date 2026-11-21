"""Compatibility between this App and the companion integration it publishes to (versions and protocol)."""

from __future__ import annotations

import re
from typing import Any

from . import protocol as P

#: The oldest companion that speaks this App's protocol, and the one released with this App.
MIN_COMPANION = "0.1.0"
RECOMMENDED_COMPANION = "0.2.0"
HACS_REPOSITORY = "https://github.com/havanissa/smart-occupancy-detection"


def version_tuple(value: Any) -> tuple[int, ...] | None:
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)", str(value or ""))
    return tuple(int(x) for x in match.groups()) if match else None


def check(companion_version: Any, companion_protocol: Any) -> dict[str, Any]:
    """``level`` ok / warning / incompatible, with an actionable ``message``."""
    if isinstance(companion_protocol, int) and companion_protocol != P.PROTOCOL_VERSION:
        if companion_protocol < P.PROTOCOL_VERSION:
            message = (f"The Smart Occupancy Detection integration ({companion_version}) is too old for this App: "
                       "update it in HACS (Smart Occupancy Detection -> Update), then restart Home Assistant.")
        else:
            message = (f"The Smart Occupancy Detection integration ({companion_version}) is newer than this App "
                       "supports: update the App (Settings -> Apps -> Smart Occupancy Detection -> Update).")
        return {"level": "incompatible", "message": message, "protocol": companion_protocol}
    version = version_tuple(companion_version)
    if version is None:
        return {"level": "warning", "message": "The integration's version is unknown."}
    if version < version_tuple(MIN_COMPANION):
        return {"level": "incompatible", "message": (
            f"The integration {companion_version} is too old: update it in HACS to {RECOMMENDED_COMPANION} or newer.")}
    if version < version_tuple(RECOMMENDED_COMPANION):
        return {"level": "warning", "message": (
            f"The integration {companion_version} works, but {RECOMMENDED_COMPANION} is recommended with this App "
            "(better diagnostics): update it in HACS.")}
    return {"level": "ok", "message": None}
