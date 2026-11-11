"""Room Status card: served and registered through the real HTTP and frontend components."""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

import pytest

from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from custom_components.smart_occupancy.card import CARD_DIR, CARD_URL
from custom_components.smart_occupancy.const import CARD_FILENAME, VERSION

from .conftest import Pilot, pilot_options, setup_entry

CARD_FILE = CARD_DIR / CARD_FILENAME

try:
    import hass_frontend  # noqa: F401  (home-assistant-frontend package)
except ImportError:  # pragma: no cover
    hass_frontend = None


@pytest.mark.skipif(hass_frontend is None, reason="home-assistant-frontend not installed")
async def test_card_served_and_registered(hass: HomeAssistant, hass_client, pilot: Pilot) -> None:
    assert await async_setup_component(hass, "http", {})
    assert await async_setup_component(hass, "frontend", {})
    await setup_entry(hass, pilot_options(pilot))
    assert await async_setup_component(hass, "smart_occupancy", {})

    from homeassistant.components.frontend import DATA_EXTRA_MODULE_URL

    assert f"{CARD_URL}?v={VERSION}" in hass.data[DATA_EXTRA_MODULE_URL].urls
    client = await hass_client()
    response = await client.get(CARD_URL)
    assert response.status == 200
    body = await response.text()
    assert 'customElements.define(CARD_TYPE, SmartOccupancyRoomCard)' in body
    assert f'CARD_VERSION = "{VERSION}"' in body


async def test_card_not_registered_without_frontend(hass: HomeAssistant, pilot: Pilot) -> None:
    """Minimal installations (no frontend) still load the integration."""
    entry = await setup_entry(hass, pilot_options(pilot))
    assert entry.state.value == "loaded"
    assert not hass.data.get("smart_occupancy_card_registered")


def test_card_is_self_contained() -> None:
    source = CARD_FILE.read_text(encoding="utf-8")
    assert "import " not in source.split("*/", 1)[1]  # no external modules / CDN
    assert "callService" not in source and "callWS" not in source  # read only
    assert "http://" not in source and "https://cdn" not in source


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_card_javascript_parses() -> None:
    subprocess.run(["node", "--check", str(CARD_FILE)], check=True, cwd=Path(__file__).parent)
