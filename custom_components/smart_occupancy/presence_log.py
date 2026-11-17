"""Bounded presence event log (0.6.10, App Phase A3.1).

What the occupancy engine decided and when, and which presence settings
changed, for the App's live debugging timeline. Memory only and bounded:

* at most :data:`LOG_SIZE` entries for the whole installation (oldest dropped);
* kept in ``hass.data`` so it survives a reload of the integration (a reload
  is itself logged), not a restart of Home Assistant. Older history comes from
  the Recorder (the room state and reason entities, and every sensor).

Nothing here influences a decision: the engine reports its transitions, the
configuration paths report their changes. Entries are plain JSON.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime
from typing import TYPE_CHECKING, Any

import logging

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

if TYPE_CHECKING:
    from .engine import RoomResult
    from .models import IntegrationConfig

_LOGGER = logging.getLogger(__name__)

LOG_SIZE = 400
_KEY = "smart_occupancy_presence_log"
_SIGNAL_LIMIT = 12


class PresenceLog:
    """A ring buffer of presence events. ``seq`` increases by one per entry (also across reloads)."""

    def __init__(self) -> None:
        self.entries: deque[dict[str, Any]] = deque(maxlen=LOG_SIZE)
        self.seq = 0
        self._source: str | None = None

    def add(self, kind: str, area_id: str | None, at: datetime | None = None, **data: Any) -> dict[str, Any]:
        self.seq += 1
        entry = {"seq": self.seq, "time": (at or dt_util.utcnow()).isoformat(timespec="milliseconds"),
                 "kind": kind, "area_id": area_id, **data}
        self.entries.append(entry)
        return entry

    def expect_source(self, source: str) -> None:
        """The next configuration change comes from ``source`` (for example "app")."""
        self._source = source

    def take_source(self) -> str:
        source, self._source = self._source, None
        return source or "integration"

    def view(self, area_id: str | None = None, limit: int = LOG_SIZE) -> list[dict[str, Any]]:
        """Newest last. Global entries (area None) are included for every room."""
        items = [e for e in self.entries if area_id is None or e["area_id"] in (area_id, None)]
        return items[-limit:]


def presence_log(hass: HomeAssistant) -> PresenceLog:
    log = hass.data.get(_KEY)
    if log is None:
        log = hass.data[_KEY] = PresenceLog()
    return log


def _iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value else None


def record_transition(hass: HomeAssistant, area_id: str, previous: RoomResult | None, result: RoomResult,
                      at: datetime) -> None:
    """One occupancy transition, with the evidence the engine used for it."""
    from .engine import SignalStatus

    def names(*statuses: SignalStatus) -> list[dict[str, str]]:
        return [{"entity_id": v.track.entity_id, "name": v.track.label(), "status": v.status.value}
                for v in result.signals if v.status in statuses][:_SIGNAL_LIMIT]

    presence_log(hass).add(
        "transition", area_id, at,
        state=result.state.value,
        occupied=result.occupied,
        previous_state=previous.state.value if previous else None,
        previous_occupied=previous.occupied if previous else None,
        reason=result.reason,
        reason_code=result.reason_code,
        confidence=result.confidence.score,
        evidence=names(SignalStatus.ACTIVE, SignalStatus.GRACE, SignalStatus.RECENT),
        stuck=names(SignalStatus.STUCK),
        unknown=names(SignalStatus.UNKNOWN),
        vacancy_at=_iso(result.vacancy_at),
        required_quiet=result.required_quiet,
        vacancy_evidence=list(result.vacancy_evidence),
        starting_up=result.starting_up,
    )


def config_changes(old: IntegrationConfig, new: IntegrationConfig) -> list[dict[str, Any]]:
    """Presence-relevant differences: rooms, their sensors, and effective timing (per room and global)."""
    from .rooms_api import diff_room

    out: list[dict[str, Any]] = []
    old_global, new_global = old.occupancy.as_dict(), new.occupancy.as_dict()
    for key, value in new_global.items():
        if old_global.get(key) != value:
            out.append({"area_id": None, "type": "timing_global", "key": key, "old": old_global.get(key),
                        "new": value})
    for area_id in sorted(set(old.rooms) | set(new.rooms)):
        before = old.rooms.get(area_id)
        after = new.rooms.get(area_id)
        sensor_changes = [c for c in diff_room(area_id, before.as_dict() if before else None,
                                               after.as_dict() if after else None)
                          if c["type"].startswith(("sensor_", "room_"))]
        for c in sensor_changes:
            out.append({"area_id": area_id, **c})
        if before is not None and after is not None:
            for key, value in after.timing.items():
                if before.timing.get(key) != value:
                    out.append({"area_id": area_id, "type": "timing_override", "key": key,
                                "old": before.timing.get(key), "new": value})
            for key in before.timing:
                if key not in after.timing:
                    out.append({"area_id": area_id, "type": "timing_inherit", "key": key,
                                "old": before.timing[key], "new": None})
    return out


def record_config_change(hass: HomeAssistant, old: IntegrationConfig, new: IntegrationConfig, *,
                         applied: str) -> None:
    """Log presence configuration changes, one entry per room (and one for global defaults). Never raises."""
    log = presence_log(hass)
    source = log.take_source()
    try:
        changes = config_changes(old, new)
    except Exception:  # noqa: BLE001 - the log must never block applying a configuration
        _LOGGER.exception("Presence log: could not compare the configurations")
        return
    if not changes:
        return
    by_area: dict[str | None, list[dict[str, Any]]] = {}
    for c in changes:
        by_area.setdefault(c["area_id"], []).append({k: v for k, v in c.items() if k != "area_id"})
    for area_id, items in by_area.items():
        log.add("config", area_id, changes=items[:40], applied=applied, source=source)


def record_reload(hass: HomeAssistant) -> None:
    presence_log(hass).add("start", None, detail="Home Assistant started or the integration reloaded. Rooms continue "
                                                 "from their saved occupancy, held at least for the start-up hold.")
