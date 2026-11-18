"""Curtain bridge protocol (Phase A5): shared by the integration and the Smart Lighting App.

One record decides who commands the covers (``curtain_bridge.py``). This module
holds what both sides must agree on: the protocol constants, the configuration
revision, the cover command allowlist and the validation of every message that
can reach a device or change the mirror. It imports nothing from Home Assistant.

Curtains are a separate physical-device control domain from lighting: their
ownership record, lease and sequence numbers are their own, so the two
subsystems can change owner independently. The rules are the same as the
lighting bridge's (``lighting_sync.py``), whose constants and helpers are reused
here so one definition exists for each.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .lighting_sync import (  # noqa: F401 - re-exported: one definition of the shared rules
    DRAIN_SECONDS,
    HEARTBEAT_SECONDS,
    HISTORY_SIZE,
    LEASE_SECONDS,
    LOST_CODES,
    MAX_CONFIG_BYTES,
    MAX_STATE_BYTES,
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
    REJECT_FAILED,
    REJECT_INVALID,
    REJECT_LEASE,
    REJECT_NOT_ALLOWED,
    REJECT_NOT_OWNER,
    REJECT_NOT_READY,
    REJECT_SEQUENCE,
    REJECT_STALE_EPOCH,
    REJECT_UNKNOWN_SESSION,
    REJECT_VERSION,
    RESUME_GRACE_SECONDS,
    ProtocolError,
    _ENTITY,
    canonical,
    config_revision,
    token_hash,
    valid_context_id,
    valid_id,
)

PROTOCOL_VERSION = 1
#: A handover is refused while a cover of a configured window is being moved by an operation (the operation is
#: never contradicted or replayed); the claim waits at most this long for it to finish.
MOVING_WAIT_SECONDS = DRAIN_SECONDS
#: A cover command may take this long (the manager's own timeout for Home Assistant to accept it).
COMMAND_SECONDS = 15.0

#: The only cover services the curtain executor sends (``curtains/executor.py``).
COVER_SERVICES = frozenset({"set_cover_position", "open_cover", "close_cover"})
#: Origins of a command (``supervision.CurtainOrigin``).
PURPOSES = frozenset({"manual_confirmed", "automatic"})
#: Sections of the published curtain runtime state (``CurtainManager.export_state``).
STATE_SECTIONS = frozenset({"last", "history", "manual", "samples", "auto", "sensors", "house"})


def covers_of(doc: dict[str, Any]) -> set[str]:
    """Every cover entity the configuration's windows name (their layers)."""
    out: set[str] = set()
    for window in doc.get("windows") or ():
        if not isinstance(window, dict):
            continue
        for layer in window.get("layers") or ():
            entity = layer.get("entity_id") if isinstance(layer, dict) else None
            if isinstance(entity, str) and entity.startswith("cover."):
                out.add(entity)
    return out


def check_cover_command(domain: Any, service: Any, data: Any, *, covers: Iterable[str]) -> list[str]:
    """Validate one cover command; returns its entity ids. Raises ProtocolError.

    Only ``cover.set_cover_position`` (with a whole-number ``position`` 0-100), ``cover.open_cover`` and
    ``cover.close_cover`` (no other data), for exactly one configured cover (a layer of a configured window;
    the executor sends one call per cover).
    """
    if not isinstance(domain, str) or not isinstance(service, str) or not isinstance(data, dict):
        raise ProtocolError(REJECT_INVALID, "invalid command")
    if domain != "cover" or service not in COVER_SERVICES:
        raise ProtocolError(REJECT_NOT_ALLOWED, f"{domain}.{service} is not allowed for curtains")
    raw = data.get("entity_id")
    ids = [raw] if isinstance(raw, str) else raw
    if not isinstance(ids, list) or len(ids) != 1 or not all(isinstance(e, str) and _ENTITY.match(e) for e in ids):
        raise ProtocolError(REJECT_INVALID, "a cover command targets exactly one cover entity")
    if not ids[0].startswith("cover."):
        raise ProtocolError(REJECT_NOT_ALLOWED, "every target must be a cover")
    extra = set(data) - {"entity_id"}
    if service == "set_cover_position":
        position = data.get("position")
        if extra != {"position"} or isinstance(position, bool) or not isinstance(position, (int, float)) \
                or position != int(position) or not 0 <= position <= 100:
            raise ProtocolError(REJECT_INVALID, "set_cover_position needs a whole-number position 0-100 and nothing else")
    elif extra:
        raise ProtocolError(REJECT_NOT_ALLOWED, f"cover.{service} takes no data")
    if ids[0] not in set(covers):
        raise ProtocolError(REJECT_NOT_ALLOWED, f"not a configured cover: {ids[0]}")
    return ids


def check_state(state: Any) -> dict[str, Any]:
    """A published curtain runtime state: an object of the known sections, bounded."""
    if not isinstance(state, dict):
        raise ProtocolError(REJECT_INVALID, "state must be an object")
    if set(state) - STATE_SECTIONS:
        raise ProtocolError(REJECT_INVALID, f"unknown state sections: {sorted(set(state) - STATE_SECTIONS)}")
    for key in ("last", "history", "manual", "auto", "sensors", "house"):
        if key in state and state[key] is not None and not isinstance(state[key], dict):
            raise ProtocolError(REJECT_INVALID, f"state section {key} must be an object")
    if "samples" in state and not isinstance(state["samples"], list):
        raise ProtocolError(REJECT_INVALID, "state section samples must be a list")
    if len(canonical(state)) > MAX_STATE_BYTES:
        raise ProtocolError(REJECT_INVALID, "state too large")
    return state


def running_operations(state: dict[str, Any] | None) -> list[str]:
    """Windows whose last operation is still running (covers may be travelling)."""
    out = []
    for window_id, op in ((state or {}).get("last") or {}).items():
        if isinstance(op, dict) and op.get("status") == "running":
            out.append(str(window_id))
    return out
