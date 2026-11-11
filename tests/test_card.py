"""Room Status card: served and registered through the real HTTP and frontend components."""

from __future__ import annotations

from pathlib import Path
import re
import shutil
import subprocess

import pytest

from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from custom_components.smart_occupancy.card import CARD_DIR, CARD_URL, LIGHTING_CARD_URL
from custom_components.smart_occupancy.const import CARD_FILENAME, LIGHTING_CARD_FILENAME, VERSION

from .conftest import Pilot, pilot_options, setup_entry

CARD_FILE = CARD_DIR / CARD_FILENAME
LIGHTING_FILE = CARD_DIR / LIGHTING_CARD_FILENAME

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

    assert f"{LIGHTING_CARD_URL}?v={VERSION}" in hass.data[DATA_EXTRA_MODULE_URL].urls
    response = await client.get(LIGHTING_CARD_URL)
    assert response.status == 200
    body = await response.text()
    assert "customElements.define(MOOD_CARD, SmartOccupancyMoodEditorCard)" in body
    assert "customElements.define(ROOM_CARD, SmartOccupancyRoomLightingCard)" in body
    assert f'const VERSION = "{VERSION}"' in body


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


def test_lighting_cards_never_control_lights() -> None:
    """The editors talk to the integration's websocket API only; they never call a service."""
    source = LIGHTING_FILE.read_text(encoding="utf-8")
    assert "import " not in source.split("*/", 1)[1]
    assert "callService" not in source and "call_service" not in source
    assert "http://" not in source and "https://cdn" not in source
    commands = set(re.findall(r"type: `\$\{WS\}/([a-z_/]+)`", source))
    assert len(commands) >= 10, commands
    assert commands <= {
        "subscribe", "editor", "preview", "profile/set", "mood/set", "settings/set", "room/set", "room/activation",
        "room/activate", "room/deactivate", "room/acknowledge", "override/clear", "room/group_choice",
        "category/set", "category/remove", "adaptive/set", "room/daylight", "daylight/set",
    }, commands


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
@pytest.mark.parametrize("path", [CARD_FILE, LIGHTING_FILE])
def test_card_javascript_parses(path: Path) -> None:
    subprocess.run(["node", "--check", str(path)], check=True, cwd=Path(__file__).parent)


def test_room_card_layout() -> None:
    """Tabs are the single navigation; daylight settings live in the Automatic tab only; one outdoor editor."""
    source = LIGHTING_FILE.read_text(encoding="utf-8")
    tabs = re.search(r"const tabs = (\[.*?\]);", source, re.S).group(1)
    order = re.findall(r'\["([a-z]+)", "([A-Za-z]+)"\]', tabs)
    assert [k for k, _ in order] == ["overview", "lights", "automatic", "overrides", "preview", "advanced"]
    # No duplicate navigation buttons at the bottom of Overview.
    for label in ("Review lights", "Configure overrides", "Preview room"):
        assert label not in source
    overview = source[source.index("  _overview(r) {"):source.index("  _daylightSummary(r) {")]
    assert "_autoHtml" not in overview and "<select" not in overview and "_daylightSummary" in overview
    advanced = source[source.index("  _advanced(r) {"):source.index("  async _loadReview() {")]
    assert "_daylightAdvanced" not in advanced and "dl-" not in advanced
    automatic = source[source.index("  _automatic(r) {"):source.index("  _autoHtml(r) {")]
    assert "_autoHtml(r)" in automatic and "Advanced daylight settings" in automatic and "_daylightAdvanced(r)" in automatic
    # One shared renderer for the global outdoor sensor, used by the global card and the room card.
    assert source.count('data-control="dl-outdoor"') == 1
    assert source.count("this._outdoorGlobalHtml(") == 2
