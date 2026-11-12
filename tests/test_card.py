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
        # 0.3.5 Switches tab (configuration only; switch commands are executed by the integration)
        "switch/candidates", "switch/add", "switch/set", "switch/mapping", "switch/mode", "switch/learn",
        "switch/remove", "switch/settings",
        # 0.3.8 Presence tab (settings only: include/exclude a signal, presence timing)
        "presence/sensor", "presence/timing",
        # 0.3.9 Possible occupancy (settings only)
        "preliminary/set",
        # 0.4.0 Occupied and Vacant profiles: settings, copy, reset, and the explicit, confirmed Test (the integration
        # sends those commands; the card still never calls a service)
        "vacant/set", "profile/copy", "profile/reset", "profile/test",
    }, commands
    assert source.count(f"type: `${{WS}}/profile/test`") == 1 and "confirm: true" in source
    test = source.split("_testProfile(profile, mood) {", 1)[1].split("\n  }\n", 1)[0]
    assert "if (!confirm(" in test and "sends real commands" in test  # never without asking


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
@pytest.mark.parametrize("path", [CARD_FILE, LIGHTING_FILE])
def test_card_javascript_parses(path: Path) -> None:
    subprocess.run(["node", "--check", str(path)], check=True, cwd=Path(__file__).parent)


def test_room_card_layout() -> None:
    """Tabs are the single navigation; daylight settings live in the Automatic section only; one outdoor editor.

    0.3.5 adds the Switches tab, 0.3.7 the Log tab after Overview, 0.3.8 the Presence tab. 0.3.9 moves the
    configuration sections (Automatic, Presence, Switches, Advanced) into the Settings submenu.
    """
    source = LIGHTING_FILE.read_text(encoding="utf-8")
    main = re.search(r"const MAIN_TABS = (\[.*?\]);", source, re.S).group(1)
    assert [k for k, _ in re.findall(r'\["([a-z]+)", "([A-Za-z]+)", "mdi:[a-z-]+"\]', main)] == [
        "overview", "log", "lights", "overrides", "preview", "settings"]
    settings = re.search(r"const SETTINGS_TABS = (\[.*?\]);", source, re.S).group(1)
    assert [k for k, _ in re.findall(r'\["([a-z]+)", "([A-Za-z]+)", "mdi:[a-z-]+"', settings)] == [
        "automatic", "presence", "switches", "advanced"]
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
    # Switches tab: assigned switches with Configure / Learn buttons / Remove; details collapsed under Advanced.
    switches = source[source.index("  _switches(r) {"):source.index("  _advanced(r) {")]
    for text in ("Assigned switches", "Learn buttons", "Configure", "Remove", "Advanced switch settings",
                 "Observing", "Active", "Listener diagnostics"):
        assert text in switches, text
    assert "callService" not in switches and "dl-" not in switches
    # The tab bar, the conflicts "Review" link (0.3.4), the Last decision's "Log" link (0.3.7), and (0.3.9) the
    # Settings submenu (Back and its sections) and the Overview's "Switch settings" link; (0.4.0) the Vacant profile's
    # "vacancy settings" link in the Overrides tab.
    assert source.count('data-action="tab"') == 7


def test_operating_modes_on_the_main_screens() -> None:
    """0.3.6: the modes are shown and changed where they are used, never only in the browser.

    * Automatic tab -> "Automatic lighting" box: the room's mode (double-click status + selector);
    * Switches tab -> every switch card (not only its Configure page): Observing / Active.
    """
    source = LIGHTING_FILE.read_text(encoding="utf-8")
    auto = source[source.index("  _autoHtml(r) {"):source.index("  // Advanced → Daylight")]
    assert '<div class="section auto">${this._lightingModeHtml(r)}' in auto  # first thing in the box
    automatic = source[source.index("  _automatic(r) {"):source.index("  _autoHtml(r) {")]
    assert "this._lightingModeHtml(r)" in automatic  # also when no daylight information exists
    mode = source[source.index("  _lightingModeHtml(r, compact = false) {"):source.index("  _setRoomMode(mode) {")]
    assert "Double-click to switch between Observing and Automatic." in mode
    assert '<div class="stitle">Automatic lighting</div>' in mode  # the box's title, with the status beside it
    assert 'data-dbl="room-mode"' in mode and 'data-action="room-mode"' in mode
    assert 'data-mode="${mode}"' in mode and '"Observing"' in mode and '"Automatic"' in mode
    assert "aria-pressed" in mode and "aria-label" in mode
    # The status shows the integration's state (settings + diagnostics), and changes go to the backend.
    state = source[source.index("  _roomModeState(r) {"):source.index("  // Operating mode: the status")]
    assert "r.settings.control_enabled" in state and "d.control_mode" in state
    for m in ("paused", "blocked"):
        assert f'"{m}"' in state
    setter = source[source.index("  _setRoomMode(mode) {"):source.index("  _onDouble(kind, el) {")]
    assert "room/activate" in setter and "room/deactivate" in setter and "confirm: true" in setter
    assert "this._save(" in setter and "control_enabled =" not in setter  # no UI-only toggle
    # Switch cards: the mode control is on the card itself, with the effective state from the integration.
    card = source[source.index("  _switchCard(r, sw, x) {"):source.index("  _learningHtml(x) {")]
    assert "this._swModeHtml(x, " in card and "Last event" in card
    sw = source[source.index("  _swModeHtml(x, head) {"):source.index("  _setSwitchMode(id, mode) {")]
    assert "Double-click to switch between Observing and Active." in sw
    assert 'data-dbl="sw-mode"' in sw and 'data-action="sw-mode-set"' in sw and "x.operating" in sw
    setter = source[source.index("  _setSwitchMode(id, mode) {"):source.index("  _switchCard(r, sw, x) {")]
    assert "switch/mode" in setter and "confirm: true" in setter and "can_control" in setter
    # 0.3.9: both stay on the main screen (Overview) although their sections moved into Settings.
    overview = source[source.index("  _overview(r) {"):source.index("  // Overview: the latest decision")]
    assert "this._lightingModeHtml(r, true)" in overview and "this._switchQuickHtml(r)" in overview
    quick = source[source.index("  _switchQuickHtml(r) {"):source.index("  _prelimNow(r) {")]
    assert "this._swModeHtml(x, " in quick
    # Double-click is detected from two clicks on the same status (a re-render between them is harmless).
    tap = source[source.index("  _tap(el) {"):source.index("  _switchRgb(key, on) {")]
    assert "this._onDouble" in tap and "500" in tap
    assert 'closest("[data-dbl]")' in source and "touch-action: manipulation" in source


def test_decision_log_ui() -> None:
    """0.3.7: Last decision on the Overview, a Log tab, and a log per switch, all from the integration's records."""
    source = LIGHTING_FILE.read_text(encoding="utf-8")
    overview = source[source.index("  _overview(r) {"):source.index("  // Overview: the latest decision")]
    assert "this._lastDecisionHtml(r.decisions" in overview
    last = source[source.index("  _lastDecisionHtml(dec"):source.index("  // Log tab:")]
    assert "No decisions recorded yet." in last and "d.explanation" in last and "d.action" in last
    assert "fmtTime(d.at)" in last and "decisionPill(d)" in last
    # Every status the integration records has a word and an icon (never colour alone).
    table = source[source.index("const DECISION = {"):source.index("const decisionStatus")]
    for status in ("executed", "dispatched", "partial", "failed", "observed", "blocked", "no_change", "no_action",
                   "suppressed", "noted"):
        assert re.search(rf'{status}: \["[A-Z][^"]+", "[a-z]+", "mdi:[a-z-]+"\]', table), status
    assert "<ha-icon icon=" in source[source.index("const decisionPill"):source.index("const DECISION_MODES")]
    assert "not confirmed" in source  # sent but not reported back is not shown as executed
    entries = source[source.index("  _decisionList(entries, scope) {"):source.index("  // Only what the integration")]
    assert "aria-expanded" in entries and 'data-action="dec-toggle"' in entries and "<button" in entries
    details = source[source.index("  _decisionDetails(d) {"):source.index("  // One Overview line")]
    assert "d.details" in details and "callWS" not in details
    card = source[source.index("  _switchCard(r, sw, x) {"):source.index("  _learningHtml(x) {")]
    assert "Last decision" in card and "x.decisions" in card and 'data-action="sw-log"' in card
    # The log link is outside the administrator-only buttons: read-only users see the log too.
    assert card.index('data-action="sw-log"') < card.index('${ro ? "" : `<div class="ibtns">')


def test_icon_tabs_are_accessible_and_never_hidden() -> None:
    """0.3.8: compact icon tabs with names (tooltip, accessible label, visible for the selected tab), keyboard
    navigation, and a bar that wraps instead of scrolling tabs out of sight."""
    source = LIGHTING_FILE.read_text(encoding="utf-8")
    main = re.search(r"const MAIN_TABS = (\[.*?\]);", source, re.S).group(1)
    sub = re.search(r"const SETTINGS_TABS = (\[.*?\]);", source, re.S).group(1)
    entries = re.findall(r'\["([a-z]+)", "([A-Za-z]+)", "(mdi:[a-z-]+)"', main + sub)
    assert len(entries) == 10 and len({icon for *_, icon in entries}) == 10  # one distinct icon per tab
    assert ("settings", "Settings", "mdi:cog-outline") in entries
    # Every settings section has a descriptive tooltip.
    assert len(re.findall(r'"mdi:[a-z-]+", "[A-Z][a-z]+: [^"]+"\]', sub)) == 4
    body = source[source.index("  _body() {", source.index("class SmartOccupancyRoomLightingCard")):
                  source.index("  // Tabs: arrow keys")]
    for text in ('role="tablist"', 'role="tab"', 'aria-selected="${on}"', 'aria-label="${esc(label)}"', "data-tip=",
                 'tabindex="${on ? 0 : -1}"', 'role="tabpanel"', 'aria-labelledby="tab-${tab}"', 'class="tabcap"',
                 '<span class="tlabel"',
                 # 0.3.9 Settings submenu: its own tab list, icon and name, a tooltip, and a Back button.
                 'aria-label="Settings sections"', 'class="subback"', 'aria-label="Back to the main sections',
                 'class="stab ${on ? "on" : ""}"', 'title="${esc(tip)}"', "<span>${l}</span>"):
        assert text in body, text
    keys = source[source.index("  _onKey(ev) {"):source.index("  _openTab(key) {")]
    for key in ("ArrowRight", "ArrowLeft", "Home", "End", "preventDefault", ".focus()", "Escape", ".subtabs"):
        assert key in keys, key
    opener = source[source.index("  _openTab(key) {"):source.index("  // \"Brightness: Global")]
    assert "ui.settingsTab" in opener  # the gear reopens the last settings section
    assert 'addEventListener("keydown"' in source
    css = source[source.index("const STYLE = `<style>"):]
    tab_css = css[css.index("  .tabs {"):css.index("  .tabcap {")]
    assert "flex-wrap: wrap" in tab_css and "overflow-x" not in tab_css and "scrollbar-width" not in tab_css
    assert ":hover::after" in tab_css and ":focus-visible::after" in tab_css and "attr(data-tip)" in tab_css
    assert "min-height: 40px" in tab_css and "touch-action: manipulation" in tab_css  # touch targets
    subnav = css[css.index("  .subnav {"):css.index("  .plist {")]
    assert subnav.count("flex-wrap: wrap") == 2 and "overflow-x" not in subnav and "touch-action: manipulation" in subnav
    narrow = css[css.index("@container (max-width: 520px)"):]
    assert ".tabcap { display: block; }" in narrow  # the selected name stays visible without hover
    assert "@container (max-width: 320px)" in narrow and "flex-wrap: wrap" in narrow.split("@container (max-width: 320px)")[1]


def test_presence_tab_and_vacancy_controls() -> None:
    source = LIGHTING_FILE.read_text(encoding="utf-8")
    presence = source[source.index("  _presence(r) {"):source.index("  _savePresenceTime(")]
    for text in ('data-control="pr-enabled"', 'role="switch"', "s.label", "s.entity_id", "s.kind_name", "s.role_name",
                 "s.last_changed", "PRESENCE[s.condition]", '"Room override"', '"Global default"',
                 'data-action="pr-inherit"', "Use global default", 'data-action="pr-reset-all"', 'data-control="pr-global"',
                 "Global defaults (all rooms)", "Room overrides are not changed"):
        assert text in presence, text
    assert "callService" not in presence
    table = source[source.index("const PRESENCE = {"):source.index("const KIND_ICONS")]
    for condition in ("active", "clear", "disabled", "unavailable", "invalid", "stuck"):
        assert re.search(rf'{condition}: \["[A-Z][^"]+", "[a-z]+", "mdi:[a-z-]+"\]', table), condition
    vacancy = source[source.index("  _vacancyHtml(r) {"):source.index("  // Presence tab (0.3.8)")]
    for text in ("When the room becomes vacant", 'data-action="vac-action"', 'data-control="vac-mood"', "aria-pressed",
                 "Ambient lights keep following the mood"):
        assert text in vacancy, text
    for action in ('"turn_off", "Turn off"', '"vacant_mood", "Vacant mood"', '"leave", "Leave unchanged"'):
        assert action in source
    automatic = source[source.index("  _automatic(r) {"):source.index("  _autoHtml(r) {")]
    assert automatic.count("this._vacancyHtml(r)") == 2  # with and without daylight information
    overview = source[source.index("  _overview(r) {"):source.index("  // Overview: the latest decision")]
    assert "When vacant" in overview


def test_possible_occupancy_controls() -> None:
    """0.3.9: Settings → Automatic → Possible occupancy, configuration only, inherited vs overridden values."""
    source = LIGHTING_FILE.read_text(encoding="utf-8")
    automatic = source[source.index("  _automatic(r) {"):source.index("  _autoHtml(r) {")]
    assert automatic.count("this._prelimHtml(r)") == 2  # with and without daylight information
    prelim = source[source.index("  _prelimHtml(r) {"):source.index("  _vacancyWord(r) {")]
    for text in ("Possible occupancy", 'data-control="pre-enabled"', 'data-control="pre-add"', 'data-action="pre-remove"',
                 'data-key="brightness_pct"', 'data-key="color_temp_kelvin"', 'data-key="transition"',
                 'data-action="pre-confirm"', "Hand over", "Turn off", 'data-control="pre-signal"',
                 'data-key="timeout"', 'data-key="on_below"', '"Room override"', '"Global default"',
                 "Use global default", "Global defaults (all rooms)", "precedence", "same as normal", "aria-pressed"):
        assert text in prelim, text
    assert "callService" not in prelim and "preliminary/set" in prelim
