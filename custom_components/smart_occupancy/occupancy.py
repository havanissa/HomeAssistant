"""Occupancy signals and entity health.

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
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .const import SensorRole
from .models import RoomConfig, SensorAssignment

# Entity health values.
AVAILABLE = "available"
UNAVAILABLE = "unavailable"      # integration reports the device as unavailable
UNKNOWN = "unknown"              # entity exists but has not reported a value
NOT_PROVIDED = "not_provided"    # registered, but its integration does not provide it
PENDING = "pending"              # still loading during start-up; not a problem yet
DISABLED = "disabled"            # disabled in the entity registry
MISSING = "missing"              # does not exist at all (deleted)

# Health values that mean "exists but cannot be used right now".
OPERATIONAL_UNAVAILABLE = frozenset({UNAVAILABLE, UNKNOWN, NOT_PROVIDED})
# Health values that mean "the configuration points at something unusable".
CONFIGURATION_INVALID = frozenset({MISSING, DISABLED})

# Roles whose signals a room needs in order to be observable.
ESSENTIAL_ROLES = frozenset({SensorRole.TRIGGER, SensorRole.MAINTAIN})


def entity_health(hass: HomeAssistant, entity_id: str, *, settled: bool = True) -> str:
    """Classify an entity's health.

    ``settled`` is False while Home Assistant is starting (plus a short grace
    period). Entities that are registered but not yet provided are then
    ``pending`` rather than ``not_provided``.
    """
    entry = er.async_get(hass).async_get(entity_id)
    if entry is not None and entry.disabled_by is not None:
        return DISABLED
    state = hass.states.get(entity_id)
    if state is None:
        if entry is None:
            return MISSING
        return NOT_PROVIDED if settled else PENDING
    if state.state == STATE_UNAVAILABLE:
        # Home Assistant writes a "restored" placeholder state for registered
        # entities that no integration has provided.
        if state.attributes.get("restored"):
            return NOT_PROVIDED if settled else PENDING
        return UNAVAILABLE
    if state.state == STATE_UNKNOWN:
        return UNKNOWN
    return AVAILABLE


def friendly_name(hass: HomeAssistant, entity_id: str) -> str | None:
    """Best available human name for an entity, without inventing one."""
    state = hass.states.get(entity_id)
    if state is not None and (name := state.attributes.get("friendly_name")):
        return str(name)
    entry = er.async_get(hass).async_get(entity_id)
    if entry is None:
        return None
    name = entry.name or entry.original_name
    if entry.device_id and (device := dr.async_get(hass).async_get(entry.device_id)):
        device_name = device.name_by_user or device.name
        if device_name and (entry.has_entity_name or not name):
            return f"{device_name} {name}".strip() if name else device_name
    return name


@dataclass(slots=True, frozen=True)
class SignalHealth:
    """Health of one assigned signal."""

    entity_id: str
    kind: str
    role: str
    health: str

    @property
    def essential(self) -> bool:
        return self.role in ESSENTIAL_ROLES

    def as_dict(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "kind": self.kind,
            "role": self.role,
            "health": self.health,
        }


def signal_health(hass: HomeAssistant, room: RoomConfig, *, settled: bool = True) -> list[SignalHealth]:
    return [
        SignalHealth(s.entity_id, s.kind.value, s.role.value, entity_health(hass, s.entity_id, settled=settled))
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
