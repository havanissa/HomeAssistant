"""Occupancy signals.

Phase 0.1 scope: describe which signals a room has and whether they are
healthy. It does **not** derive an occupied/vacant state - the existing
occupancy helpers and automations remain the source of truth, and no
duplicate occupancy entity is created.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.const import STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .models import RoomConfig, SensorAssignment

AVAILABLE = "available"
UNAVAILABLE = "unavailable"
MISSING = "missing"
NOT_LOADED = "not_loaded"


def entity_health(hass: HomeAssistant, entity_id: str) -> str:
    """available / unavailable / not_loaded / missing.

    ``not_loaded`` means the entity is registered but has no state yet (e.g.
    its integration is still starting). It is not reported as missing.
    """
    state = hass.states.get(entity_id)
    if state is not None:
        if state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return UNAVAILABLE
        return AVAILABLE
    if er.async_get(hass).async_get(entity_id) is not None:
        return NOT_LOADED
    return MISSING


@dataclass(slots=True, frozen=True)
class SignalHealth:
    """Health of one assigned signal."""

    entity_id: str
    kind: str
    role: str
    health: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "kind": self.kind,
            "role": self.role,
            "health": self.health,
        }


def signal_health(hass: HomeAssistant, room: RoomConfig) -> list[SignalHealth]:
    return [
        SignalHealth(s.entity_id, s.kind.value, s.role.value, entity_health(hass, s.entity_id))
        for s in room.sensors
    ]


def live_signal_summary(hass: HomeAssistant, sensors: tuple[SensorAssignment, ...]) -> dict[str, list[str]]:
    """Which assigned signals are currently active, grouped by role.

    Only used for the on-demand diagnostics download, never as entity state
    or attributes, so it causes no Recorder writes.
    """
    summary: dict[str, list[str]] = {}
    for s in sensors:
        state = hass.states.get(s.entity_id)
        if state is not None and state.state == STATE_ON:
            summary.setdefault(s.role.value, []).append(s.entity_id)
    return summary
