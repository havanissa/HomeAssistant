"""Trigger subscriptions (``homeassistant.helpers.trigger`` subset): validated and run by Home Assistant itself.

The App cannot run Home Assistant's trigger platforms; it asks Home Assistant to
validate them (``validate_config``) and to run them (``subscribe_trigger``), and
receives the trigger variables. Used for MQTT device triggers of wall switches.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..core import CALLBACK_TYPE, HomeAssistant
from ..exceptions import HomeAssistantError


async def async_validate_trigger_config(hass: HomeAssistant, trigger_config: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if hass.backend is None:
        raise HomeAssistantError("No connection to Home Assistant")
    return await hass.backend.async_validate_triggers(list(trigger_config))


async def async_initialize_triggers(hass: HomeAssistant, trigger_config: list[dict[str, Any]],
                                    action: Callable[..., Any], domain: str, name: str,
                                    log_cb: Callable[..., Any], home_assistant_start: bool = False,
                                    variables: Any = None, trigger_context: Any = None) -> CALLBACK_TYPE | None:
    if hass.backend is None:
        raise HomeAssistantError("No connection to Home Assistant")
    return await hass.backend.async_subscribe_triggers(list(trigger_config), action)
