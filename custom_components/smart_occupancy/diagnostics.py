"""Downloadable diagnostics (Settings → Devices & services → ⋮ → Download diagnostics).

Computed on demand, so the detailed data here never touches the Recorder.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import label_registry as lr

from . import discovery
from .const import PHASE
from .coordinator import SmartOccupancyConfigEntry
from .lighting import expand_group
from .occupancy import live_signal_summary


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: SmartOccupancyConfigEntry
) -> dict[str, Any]:
    coordinator = entry.runtime_data
    config = coordinator.config
    snapshot = coordinator.data

    lighting = coordinator.lighting
    rooms: dict[str, Any] = {}
    for area_id, room in config.enabled_rooms.items():
        managed = lighting.managed_room(area_id)
        rooms[area_id] = {
            "active_signals_now": live_signal_summary(hass, room.sensors),
            "group_members": {
                light: sorted(members)
                for light in managed.targets
                if (members := expand_group(hass, light))
            },
            # Preview of what *would* apply in the room's current mood. Nothing is applied.
            "resolved_profiles_preview": lighting.plan(area_id, "preview").as_dict(),
        }

    return {
        "phase": PHASE,
        "config": config.as_dict(),
        "snapshot": snapshot.as_dict(),
        "rooms": rooms,
        "labels": [
            {"label_id": label.label_id, "name": label.name}
            for label in lr.async_get(hass).async_list_labels()
        ],
        "areas": [
            discovery.discover_area(hass, area.id).as_dict()
            for area in discovery.list_areas(hass)
        ],
        "tracked_entities": coordinator.tracked_entities,
        "occupancy": coordinator.occupancy.diagnostics(),
        "lighting": lighting.diagnostics(),
        # Occupancy forecast (0.5.0): settings, learning status, current forecasts (aggregates only).
        "forecast": coordinator.forecast.diagnostics(),
    }
