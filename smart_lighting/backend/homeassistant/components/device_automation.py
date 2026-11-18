"""Device automations (``homeassistant.components.device_automation`` subset): trigger lists from Home Assistant."""

from __future__ import annotations

from collections.abc import Iterable
from enum import Enum
from typing import Any

from ..core import HomeAssistant


class DeviceAutomationType(Enum):
    TRIGGER = "trigger"
    CONDITION = "condition"
    ACTION = "action"


async def async_get_device_automations(hass: HomeAssistant, automation_type: DeviceAutomationType,
                                       device_ids: Iterable[str] | None = None) -> dict[str, list[dict[str, Any]]]:
    if automation_type is not DeviceAutomationType.TRIGGER or hass.backend is None:
        return {}
    return await hass.backend.async_device_triggers(list(device_ids or ()))
