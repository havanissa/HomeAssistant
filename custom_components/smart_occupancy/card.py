"""Serves the Smart Occupancy Room Status dashboard card.

The card is a plain JavaScript module shipped inside the integration
(``frontend/smart-occupancy-room-card.js``). It is served from a static path
and added to the frontend with ``frontend.add_extra_js_url``, the supported
way for an integration to provide frontend code. Users do not need to add a
dashboard resource, and no dashboard is changed.
"""

from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.core import HomeAssistant

from .const import CARD_FILENAME, CARD_URL_BASE, DOMAIN, VERSION

_LOGGER = logging.getLogger(__name__)

CARD_DIR = Path(__file__).parent / "frontend"
CARD_URL = f"{CARD_URL_BASE}/{CARD_FILENAME}"
_REGISTERED = f"{DOMAIN}_card_registered"


async def async_register_card(hass: HomeAssistant) -> bool:
    """Register the static path and load the card in the frontend.

    Returns False (and does nothing) when the HTTP server or the frontend is
    not loaded, e.g. in a minimal test setup.
    """
    if hass.data.get(_REGISTERED):
        return True
    if getattr(hass, "http", None) is None or "frontend" not in hass.config.components:
        _LOGGER.debug("card_not_registered reason=frontend_not_loaded")
        return False
    from homeassistant.components.frontend import add_extra_js_url
    from homeassistant.components.http import StaticPathConfig

    await hass.http.async_register_static_paths(
        [StaticPathConfig(CARD_URL_BASE, str(CARD_DIR), cache_headers=False)]
    )
    # The version query string makes browsers fetch the new card after an update.
    add_extra_js_url(hass, f"{CARD_URL}?v={VERSION}")
    hass.data[_REGISTERED] = True
    _LOGGER.debug("card_registered url=%s", CARD_URL)
    return True
