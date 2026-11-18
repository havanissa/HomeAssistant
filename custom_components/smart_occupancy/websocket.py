"""Registration of every websocket command of the integration.

The lighting commands live in ``lighting_websocket.py`` and the modules next to it
(Phase A4 moved them there, unchanged, so the Smart Lighting App can run the same
handlers); the other modules register their own handler tuples here.
"""

from __future__ import annotations

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .lighting_websocket import _THROTTLE_SECONDS, LIGHTING_HANDLERS  # noqa: F401 - re-exported for the tests

_REGISTERED = f"{DOMAIN}_websocket_registered"


def async_register_websocket_commands(hass: HomeAssistant) -> None:
    if hass.data.get(_REGISTERED):
        return
    hass.data[_REGISTERED] = True
    from .brightness_websocket import BRIGHTNESS_HANDLERS
    from .curtain_bridge_api import CURTAIN_BRIDGE_HANDLERS, curtain_managed_guard
    from .curtains.websocket import CURTAIN_HANDLERS
    from .engine_api import ENGINE_HANDLERS
    from .forecast_bridge_api import FORECAST_BRIDGE_HANDLERS, forecast_managed_guard
    from .forecast_websocket import FORECAST_HANDLERS
    from .lighting_bridge_api import LIGHTING_BRIDGE_HANDLERS, managed_guard
    from .preliminary_websocket import PRELIMINARY_HANDLERS
    from .presence import PRESENCE_HANDLERS
    from .presence_api import PRESENCE_API_HANDLERS
    from .priority_websocket import PRIORITY_HANDLERS
    from .profile_websocket import PROFILE_HANDLERS
    from .rooms_api import ROOMS_HANDLERS
    from .switch_websocket import SWITCH_HANDLERS
    from .zone_websocket import ZONE_HANDLERS

    for handler in (*PRESENCE_HANDLERS, *ROOMS_HANDLERS, *PRESENCE_API_HANDLERS, *ENGINE_HANDLERS,
                    *LIGHTING_BRIDGE_HANDLERS, *CURTAIN_BRIDGE_HANDLERS, *FORECAST_BRIDGE_HANDLERS):
        websocket_api.async_register_command(hass, handler)
    # Curtain and forecast editors (Phase A5): read-only while the Smart Lighting App owns them.
    for handler in CURTAIN_HANDLERS:
        websocket_api.async_register_command(hass, curtain_managed_guard(handler))
    for handler in FORECAST_HANDLERS:
        websocket_api.async_register_command(hass, forecast_managed_guard(handler))
    # Lighting editors (Phase A4): read-only while the Smart Lighting App controls lighting.
    for handler in (*LIGHTING_HANDLERS, *SWITCH_HANDLERS, *PRELIMINARY_HANDLERS, *PROFILE_HANDLERS, *ZONE_HANDLERS,
                    *BRIGHTNESS_HANDLERS, *PRIORITY_HANDLERS):
        websocket_api.async_register_command(hass, managed_guard(handler))
