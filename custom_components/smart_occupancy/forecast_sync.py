"""Forecast bridge protocol (Phase A5): shared by the integration and the Smart Lighting App.

Forecasts are observation only: no bridge message can reach a device. One record
decides which side *produces* the forecasts (``forecast_bridge.py``): the
integration's manager learns and publishes (default), or the App's does and the
integration only shows what the App published on its entities and in its
read-only views. This module holds the constants, the validation of the
published view and of the learned-data export, and the revisions both sides
compare. It imports nothing from Home Assistant.
"""

from __future__ import annotations

import hashlib
from typing import Any

from .lighting_sync import (  # noqa: F401 - re-exported: one definition of the shared rules
    HEARTBEAT_SECONDS,
    HISTORY_SIZE,
    LEASE_SECONDS,
    LOST_CODES,
    OWNER_APP,
    OWNER_INTEGRATION,
    REASON_APP_RESTARTED,
    REASON_HANDOVER,
    REASON_LEASE_EXPIRED,
    REASON_NOT_RESUMED,
    REASON_RELEASE,
    REASON_RESUMED,
    REASON_REVOKED,
    REASON_SERVICE,
    REJECT_BUSY,
    REJECT_CONFIG,
    REJECT_INVALID,
    REJECT_LEASE,
    REJECT_NOT_OWNER,
    REJECT_NOT_READY,
    REJECT_SEQUENCE,
    REJECT_STALE_EPOCH,
    REJECT_UNKNOWN_SESSION,
    REJECT_VERSION,
    RESUME_GRACE_SECONDS,
    ProtocolError,
    canonical,
    token_hash,
    valid_id,
)

PROTOCOL_VERSION = 1
#: The learned-data export (settings and daily summaries) may be this large.
MAX_EXPORT_BYTES = 8_000_000
MAX_VIEW_BYTES = 2_000_000
EXPORT_SCHEMA = 1

#: Sections of the published forecast view (what the integration's entities and read-only views show).
VIEW_SECTIONS = frozenset({"rooms", "next_room", "status", "days_learned", "settings"})
#: Sections of the learned-data export (``ForecastManager.export_learning``).
EXPORT_SECTIONS = frozenset({"schema", "settings", "days", "no_data", "recorder_oldest", "last_update",
                             "connections", "exported_at"})


def revision(doc: Any) -> str:
    """Revision of a settings document or a learned-data export: equal documents, equal revision."""
    return hashlib.sha256(canonical(doc).encode()).hexdigest()[:16]


def check_view(view: Any) -> dict[str, Any]:
    """A published forecast view: an object of the known sections, bounded."""
    if not isinstance(view, dict):
        raise ProtocolError(REJECT_INVALID, "view must be an object")
    if set(view) - VIEW_SECTIONS:
        raise ProtocolError(REJECT_INVALID, f"unknown view sections: {sorted(set(view) - VIEW_SECTIONS)}")
    rooms = view.get("rooms")
    if rooms is not None and not (isinstance(rooms, dict) and all(isinstance(k, str) and isinstance(v, dict)
                                                                   for k, v in rooms.items())):
        raise ProtocolError(REJECT_INVALID, "rooms must map area ids to forecast objects")
    if view.get("next_room") is not None and not isinstance(view["next_room"], dict):
        raise ProtocolError(REJECT_INVALID, "next_room must be an object or null")
    if view.get("status") is not None and not isinstance(view["status"], dict):
        raise ProtocolError(REJECT_INVALID, "status must be an object")
    if view.get("settings") is not None and not isinstance(view["settings"], dict):
        raise ProtocolError(REJECT_INVALID, "settings must be an object")
    if len(canonical(view)) > MAX_VIEW_BYTES:
        raise ProtocolError(REJECT_INVALID, "view too large")
    return view


def check_export(data: Any) -> dict[str, Any]:
    """A learned-data export (shape only; the forecast model validates every day when it loads it)."""
    if not isinstance(data, dict):
        raise ProtocolError(REJECT_INVALID, "export must be an object")
    if data.get("schema") != EXPORT_SCHEMA:
        raise ProtocolError(REJECT_VERSION, f"unsupported export schema {data.get('schema')!r}")
    if set(data) - EXPORT_SECTIONS:
        raise ProtocolError(REJECT_INVALID, f"unknown export sections: {sorted(set(data) - EXPORT_SECTIONS)}")
    days = data.get("days")
    if not isinstance(days, dict) or not all(isinstance(k, str) and isinstance(v, dict) for k, v in days.items()):
        raise ProtocolError(REJECT_INVALID, "days must map dates to daily summaries")
    if not isinstance(data.get("settings"), dict):
        raise ProtocolError(REJECT_INVALID, "settings must be an object")
    no_data = data.get("no_data")
    if no_data is not None and not (isinstance(no_data, list) and all(isinstance(d, str) for d in no_data)):
        raise ProtocolError(REJECT_INVALID, "no_data must be a list of dates")
    oldest = data.get("recorder_oldest")
    if oldest is not None and (isinstance(oldest, bool) or not isinstance(oldest, (int, float))):
        raise ProtocolError(REJECT_INVALID, "recorder_oldest must be a timestamp or null")
    if len(canonical(data)) > MAX_EXPORT_BYTES:
        raise ProtocolError(REJECT_INVALID, "export too large")
    return data


def learned_revision(data: dict[str, Any]) -> str:
    """Revision of what was learned (the days and the dates without data), independent of when it was exported."""
    return revision({"days": data.get("days") or {}, "no_data": sorted(data.get("no_data") or [])})
