"""Mood lighting in a running Home Assistant: discovery, execution, protections, websocket API."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import timedelta
import logging
from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    async_mock_service,
)

from homeassistant.core import Context, HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
    label_registry as lr,
)
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.setup import async_setup_component

from custom_components.smart_occupancy.const import DOMAIN, SIGNAL_LIGHTING_UPDATED
from custom_components.smart_occupancy.models import ProfileSetting

from .conftest import setup_entry

WS_LOGGER = "homeassistant.components.websocket_api.http.connection"
AUTO = "input_select.auto_mood"
MANUAL = "input_select.mood"
MAIN = "input_select.main_mood"
AUTO_OPTS = ["Morgun", "Dagur", "Kvøld", "Nátt", "Onki"]
MAIN_OPTS = ["Morgun", "Dagur", "Kvøld", "Nátt", "Hygge"]

CT = {"supported_color_modes": ["color_temp"], "min_color_temp_kelvin": 2200, "max_color_temp_kelvin": 6500}


@dataclass
class Lit:
    area: str
    other: str
    pir: str
    radar: str
    group: str
    spot_1: str
    spot_2: str
    lamp: str
    desk: str
    strip: str
    plug: str
    other_lamp: str
    labels: dict[str, str]


def set_moods(hass: HomeAssistant, auto: str, manual: str, main: str, *, main_opts=MAIN_OPTS,
              manual_opts=None) -> None:
    hass.states.async_set(AUTO, auto, {"options": AUTO_OPTS})
    hass.states.async_set(MANUAL, manual, {"options": ["Auto", *(manual_opts or MAIN_OPTS)]})
    hass.states.async_set(MAIN, main, {"options": main_opts})


@pytest.fixture
async def lit(hass: HomeAssistant) -> Lit:
    area_reg, ent_reg, label_reg = ar.async_get(hass), er.async_get(hass), lr.async_get(hass)
    room = area_reg.async_create("Kamar Norður")
    other = area_reg.async_create("Stova")
    labels = {k: label_reg.async_create(n).label_id for k, n in (
        ("downlights", "Downlights"), ("lamps", "Lamps"), ("led_strips", "LED strips"), ("decor", "Decor"),
        ("exclude", "No mood lighting"))}

    def make(domain, object_id, area_id, label=None, device_class=None) -> str:
        entry = ent_reg.async_get_or_create(domain, "test", f"uid_{object_id}", suggested_object_id=object_id,
                                            original_device_class=device_class)
        ent_reg.async_update_entity(entry.entity_id, area_id=area_id, labels={labels[label]} if label else set())
        return entry.entity_id

    pir = make("binary_sensor", "kno_pir", room.id, device_class="motion")
    radar = make("binary_sensor", "kno_radar", room.id, device_class="occupancy")
    group = make("light", "kno_spots", room.id, "downlights")
    spot_1 = make("light", "kno_spot_1", room.id, "downlights")
    spot_2 = make("light", "kno_spot_2", room.id, "downlights")
    lamp = make("light", "kno_lamp", room.id, "lamps")
    desk = make("light", "kno_desk", room.id, "lamps")
    strip = make("light", "kno_strip", room.id, "led_strips")
    plug = make("light", "kno_plug", room.id, "lamps")
    other_lamp = make("light", "stova_lamp", other.id, "lamps")
    for e in (pir, radar):
        hass.states.async_set(e, "off")
    hass.states.async_set(group, "on", {**CT, "entity_id": [spot_1, spot_2], "supported_features": 32,
                                        "brightness": 255, "color_mode": "color_temp", "color_temp_kelvin": 4000})
    for e in (spot_1, spot_2):
        hass.states.async_set(e, "on", {**CT, "brightness": 255, "color_mode": "color_temp", "color_temp_kelvin": 4000})
    hass.states.async_set(lamp, "off", {**CT, "supported_features": 32})
    hass.states.async_set(desk, "off", CT)
    hass.states.async_set(strip, "off", {"supported_color_modes": ["color_temp", "hs"], **{k: v for k, v in CT.items()
                                         if k != "supported_color_modes"}, "effect_list": ["candle", "rainbow"],
                                         "supported_features": 36})
    hass.states.async_set(plug, "off", {"supported_color_modes": ["onoff"]})
    hass.states.async_set(other_lamp, "off", CT)
    set_moods(hass, "Dagur", "Auto", "Dagur")
    await hass.async_block_till_done()
    return Lit(room.id, other.id, pir, radar, group, spot_1, spot_2, lamp, desk, strip, plug, other_lamp, labels)


PROFILES = {
    "Hygge": {
        "downlights": {"action": "off", "transition": 2},
        "lamps": {"action": "on", "brightness_pct": 70, "color_temp_kelvin": 2700, "transition": 2},
        "led_strips": {"action": "on", "brightness_pct": 25, "color_temp_kelvin": 2200},
    },
    "Dagur": {
        "downlights": {"action": "on", "brightness_pct": 100, "color_temp_kelvin": 3500},
        "lamps": {"action": "on", "brightness_pct": 75, "color_temp_kelvin": 3500},
        "led_strips": {"action": "on", "brightness_pct": 50, "color_temp_kelvin": 3500},
    },
    "Kvøld": {
        "downlights": {"action": "on", "brightness_pct": 50, "color_temp_kelvin": 3000},
        "lamps": {"action": "on", "brightness_pct": 75, "color_temp_kelvin": 2700},
        "led_strips": {"action": "ignore"},
    },
    "Nátt": {"downlights": {"action": "off"}, "lamps": {"action": "on", "brightness_pct": 10, "color_temp_kelvin": 2200}},
}


def options(lit: Lit, **room: Any) -> dict[str, Any]:
    return {
        "config_version": 2,
        "operating_mode": "observe",
        "mood": {"auto_mood_entity": AUTO, "manual_mood_entity": MANUAL, "main_mood_entity": MAIN,
                 "auto_option": "Auto", "mismatch_grace_seconds": 30},
        "categories": [
            {"key": "downlights", "name": "Downlights", "label_id": lit.labels["downlights"]},
            {"key": "lamps", "name": "Lamps", "label_id": lit.labels["lamps"]},
            {"key": "led_strips", "name": "LED strips", "label_id": lit.labels["led_strips"]},
            {"key": "decor", "name": "Decor", "label_id": lit.labels["decor"]},
        ],
        "rooms": {lit.area: {
            "enabled": True,
            "sensors": [{"entity_id": lit.pir, "kind": "pir", "role": "trigger"},
                        {"entity_id": lit.radar, "kind": "radar", "role": "maintain"}],
            "lights": [],
            **room,
        }},
        "global_profiles": PROFILES,
    }


class Calls:
    def __init__(self, hass: HomeAssistant) -> None:
        self.on = async_mock_service(hass, "light", "turn_on")
        self.off = async_mock_service(hass, "light", "turn_off")

    @property
    def all(self) -> list[ServiceCall]:
        return [*self.on, *self.off]

    def clear(self) -> None:
        self.on.clear()
        self.off.clear()

    def by_entity(self) -> dict[str, tuple[str, dict[str, Any]]]:
        out = {}
        for call in self.all:
            data = {k: v for k, v in call.data.items() if k != "entity_id"}
            ids = call.data["entity_id"]
            for e in [ids] if isinstance(ids, str) else ids:
                out[e] = (call.service, data)
        return out


@pytest.fixture
def calls(hass: HomeAssistant) -> Calls:
    return Calls(hass)


async def advance(hass: HomeAssistant, freezer, seconds: float = 2) -> None:
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def occupy(hass: HomeAssistant, lit: Lit, freezer) -> None:
    hass.states.async_set(lit.pir, "on")
    hass.states.async_set(lit.radar, "on")
    await hass.async_block_till_done()
    await advance(hass, freezer)


async def activate(hass: HomeAssistant, entry, lit: Lit, freezer, **room) -> None:
    ctrl = entry.runtime_data.lighting
    ctrl.store.async_set(ctrl.with_room(lit.area, control_enabled=True, **room))
    await hass.async_block_till_done()
    await advance(hass, freezer)


def ctrl_of(entry):
    return entry.runtime_data.lighting


def lighting_state(hass: HomeAssistant, lit: Lit):
    entity_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{DOMAIN}_room_{lit.area}_mood_lighting")
    return hass.states.get(entity_id)


# ------------------------------------------------------------------ upgrade
async def test_upgrade_from_phase_02_keeps_configuration(hass: HomeAssistant, lit: Lit, hass_storage) -> None:
    opts = options(lit, mood_overrides={"Hygge": {"lamps": {"brightness_pct": 50}}},
                   light_overrides={"Hygge": {lit.desk: {"brightness_pct": 80}}})
    entry = MockConfigEntry(domain=DOMAIN, title="Smart Occupancy & Lighting", options=opts, version=1, minor_version=2)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.minor_version == 2 and dict(entry.options) == opts  # options are not rewritten
    stored = hass_storage["smart_occupancy.lighting"]["data"]
    assert stored["global_profiles"]["Hygge"]["lamps"]["brightness_pct"] == 70
    assert stored["rooms"][lit.area]["mood_overrides"]["Hygge"]["lamps"] == {"action": "inherit", "brightness_pct": 50}
    assert stored["rooms"][lit.area]["control_enabled"] is False  # every room starts in Observe
    assert lighting_state(hass, lit).state == "observe"

    # Later changes live in the store and survive a reload; the options are not re-imported.
    ctrl = ctrl_of(entry)
    ctrl.store.async_set(ctrl.with_room(lit.area, mood_override="Nátt"))
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert ctrl_of(entry).lighting.room(lit.area).mood_override == "Nátt"
    assert ctrl_of(entry).lighting.room(lit.area).mood_overrides["Hygge"]["lamps"].brightness_pct == 50


# ------------------------------------------------------------------ observe
async def test_observe_mode_never_controls_lights(hass: HomeAssistant, lit: Lit, calls: Calls, freezer,
                                                  hass_ws_client) -> None:
    entry = await setup_entry(hass, options(lit))
    await occupy(hass, lit, freezer)
    set_moods(hass, "Dagur", "Hygge", "Hygge")
    await hass.async_block_till_done()
    await advance(hass, freezer, 5)
    assert calls.all == []
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": "smart_occupancy/lighting/preview", "area_id": lit.area})
    plan = (await client.receive_json())["result"]["plans"][0]
    assert plan["control_enabled"] is False and plan["will_execute"] is False
    lamp = next(i for i in plan["items"] if i["entity_id"] == lit.lamp)
    assert lamp["decision"] == "change" and lamp["reason"].startswith("Would")
    assert calls.all == []
    assert ctrl_of(entry).room_diagnostics(lit.area)["control_mode"] == "observe"


# --------------------------------------------------------------- execution
async def test_pilot_executes_any_mood_after_activation(hass: HomeAssistant, lit: Lit, calls: Calls, freezer) -> None:
    """Manual Mood -> Hygge, Main Mood -> Hygge: Hygge is applied like any other mood."""
    entry = await setup_entry(hass, options(lit))
    await occupy(hass, lit, freezer)
    await activate(hass, entry, lit, freezer)  # activation applies the current mood (Dagur)
    assert calls.by_entity()[lit.lamp] == ("turn_on", {"brightness_pct": 75, "color_temp_kelvin": 3500})
    calls.clear()

    set_moods(hass, "Dagur", "Hygge", "Dagur")  # Manual changed; the user's automation copies it ...
    await hass.async_block_till_done()
    set_moods(hass, "Dagur", "Hygge", "Hygge")  # ... into Main Mood
    await hass.async_block_till_done()
    await advance(hass, freezer)
    sent = calls.by_entity()
    # Downlights off through the group only (members are not commanded twice).
    assert sent[lit.group] == ("turn_off", {"transition": 2})
    assert lit.spot_1 not in sent and lit.spot_2 not in sent
    # Lamps warm white; transition only where supported; the on/off plug without brightness or colour.
    assert sent[lit.lamp] == ("turn_on", {"brightness_pct": 70, "color_temp_kelvin": 2700, "transition": 2})
    assert sent[lit.desk] == ("turn_on", {"brightness_pct": 70, "color_temp_kelvin": 2700})
    # The on/off plug already got the same "turn_on" (no brightness/colour) for Dagur: not resent.
    assert lit.plug not in sent
    assert next(i for i in ctrl_of(entry).plan(lit.area).items if i.entity_id == lit.plug).reason_code == "already_sent"
    assert sent[lit.strip] == ("turn_on", {"brightness_pct": 25, "color_temp_kelvin": 2200})
    assert lit.other_lamp not in sent  # other room
    # Identical service data is batched into one call.
    assert len([c for c in calls.on if c.data.get("brightness_pct") == 70 and "transition" not in c.data]) == 1
    diag = lighting_state(hass, lit).attributes
    assert lighting_state(hass, lit).state == "active"
    assert diag["last_result"] == "applied" and diag["last_applied_profile"] == "Hygge"
    assert diag["last_trigger"] == "mood_change" and "Hygge" in diag["last_reason"]

    # No repeated commands when nothing relevant changed.
    calls.clear()
    hass.states.async_set(MAIN, "Hygge", {"options": MAIN_OPTS, "icon": "mdi:sofa"})
    await hass.async_block_till_done()
    await advance(hass, freezer)
    assert calls.all == []


async def test_three_selector_resolution(hass: HomeAssistant, lit: Lit, calls: Calls, freezer) -> None:
    entry = await setup_entry(hass, options(lit))
    ctrl = ctrl_of(entry)
    await occupy(hass, lit, freezer)
    await activate(hass, entry, lit, freezer)
    calls.clear()
    # Manual Mood = Auto: Main follows Auto Mood.
    set_moods(hass, "Kvøld", "Auto", "Kvøld")
    await hass.async_block_till_done()
    await advance(hass, freezer)
    assert ctrl.mood_status().follows == "auto" and ctrl.mood_status().mood == "Kvøld"
    assert calls.by_entity()[lit.lamp][1]["color_temp_kelvin"] == 2700
    assert lit.strip not in calls.by_entity()  # Kvøld ignores LED strips
    # Manual mood selected; Auto Mood keeps changing underneath without effect.
    set_moods(hass, "Kvøld", "Nátt", "Nátt")
    await hass.async_block_till_done()
    await advance(hass, freezer)
    calls.clear()
    set_moods(hass, "Morgun", "Nátt", "Nátt")
    await hass.async_block_till_done()
    await advance(hass, freezer)
    assert calls.all == []
    status = ctrl.mood_status()
    assert (status.mood, status.follows, status.expected) == ("Nátt", "manual", "Nátt")


async def test_room_mood_override_and_vacancy(hass: HomeAssistant, lit: Lit, calls: Calls, freezer) -> None:
    entry = await setup_entry(hass, options(lit))
    ctrl = ctrl_of(entry)
    await activate(hass, entry, lit, freezer)  # vacant: nothing happens
    assert calls.all == []
    plan = ctrl.plan(lit.area)
    assert {i.reason_code for i in plan.items if i.service and not i.reason_code == "covered_by_group"} == {"vacant"}
    # Ambient strip follows the mood while vacant.
    ctrl.store.async_set(ctrl.with_room(
        lit.area, mood_override="Hygge",
        mood_overrides={"Hygge": {"led_strips": ProfileSetting(persistent=True)}},
    ))
    await hass.async_block_till_done()
    await advance(hass, freezer)
    assert set(calls.by_entity()) == {lit.strip}
    calls.clear()
    # Newly confirmed occupancy applies the room mood (not the global Dagur).
    await occupy(hass, lit, freezer)
    sent = calls.by_entity()
    assert sent[lit.lamp][1]["brightness_pct"] == 70 and sent[lit.group][0] == "turn_off"
    calls.clear()
    # Global mood changes do not affect a room with its own mood.
    set_moods(hass, "Nátt", "Auto", "Nátt")
    await hass.async_block_till_done()
    await advance(hass, freezer)
    assert calls.all == []
    assert ctrl.room_diagnostics(lit.area)["follows_global"] is False


async def test_profile_edits_never_send_and_apply_at_the_next_trigger(hass: HomeAssistant, lit: Lit, calls: Calls,
                                                                       freezer, hass_ws_client) -> None:
    """0.4.0: editing a profile never changes a light; the next trigger (here a mood change) applies it."""
    entry = await setup_entry(hass, options(lit))
    await occupy(hass, lit, freezer)
    await activate(hass, entry, lit, freezer)
    calls.clear()
    client = await hass_ws_client(hass)

    async def ws(**msg):
        await client.send_json_auto_id(msg)
        return await client.receive_json()

    reply = await ws(type="smart_occupancy/lighting/profile/set", scope="room", area_id=lit.area, mood="Dagur",
                     target="lamps", setting={"action": "inherit", "brightness_pct": 50})
    assert reply["success"], reply
    await hass.async_block_till_done()
    await advance(hass, freezer, 30)
    assert calls.all == []  # configuration editing does not change a physical light
    reply = await ws(type="smart_occupancy/lighting/profile/set", scope="light", area_id=lit.area, mood="Dagur",
                     target=lit.desk, setting={"brightness_pct": 80})
    assert reply["success"]
    await hass.async_block_till_done()
    await advance(hass, freezer, 30)
    assert calls.all == []
    # The next trigger uses the edited profile: Kvøld and back to Dagur.
    set_moods(hass, "Kvøld", "Auto", "Kvøld")
    await hass.async_block_till_done()
    await advance(hass, freezer)
    calls.clear()
    set_moods(hass, "Dagur", "Auto", "Dagur")
    await hass.async_block_till_done()
    await advance(hass, freezer)
    assert calls.by_entity()[lit.lamp] == ("turn_on", {"brightness_pct": 50, "color_temp_kelvin": 3500})
    assert calls.by_entity()[lit.desk] == ("turn_on", {"brightness_pct": 80, "color_temp_kelvin": 3500})
    reply = await ws(type="smart_occupancy/lighting/editor", area_id=lit.area, mood="Dagur")
    desk = next(li for li in reply["result"]["room"]["lights"] if li["entity_id"] == lit.desk)
    assert desk["effective"]["brightness_pct"] == 80 and desk["effective"]["sources"]["brightness_pct"]["level"] == "light"
    assert desk["effective"]["sources"]["color_temp_kelvin"]["level"] == "global"
    assert desk["inherited"]["brightness_pct"] == 50


# -------------------------------------------------------------- the moods
async def test_onki_is_not_auto_and_its_handling_is_configurable(hass: HomeAssistant, lit: Lit, calls: Calls,
                                                                  freezer, hass_ws_client) -> None:
    entry = await setup_entry(hass, options(lit))
    ctrl = ctrl_of(entry)
    await occupy(hass, lit, freezer)
    await activate(hass, entry, lit, freezer)
    calls.clear()
    set_moods(hass, "Onki", "Auto", "Dagur")  # Main Mood cannot follow: no Onki option
    await hass.async_block_till_done()
    await advance(hass, freezer)
    status = ctrl.mood_status()
    assert (status.mood, status.source, status.unrepresentable) == ("Dagur", "follow_main", "Onki")
    assert calls.all == []  # existing behaviour preserved: Main Mood still Dagur
    await entry.runtime_data.async_refresh()
    problems = {p.code: p for p in entry.runtime_data.data.problems}
    assert "Onki" in problems["mood_unrepresentable"].explanation
    assert MAIN in problems["mood_unrepresentable"].action
    assert entry.runtime_data.data.mood.main_mood == "Dagur"  # helpers untouched
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": "smart_occupancy/lighting/settings/set",
                                    "unrepresentable": {"Onki": {"mode": "use_mood", "mood": "Nátt"}}})
    assert (await client.receive_json())["success"]
    await hass.async_block_till_done()
    await advance(hass, freezer)
    assert ctrl.mood_status().mood == "Nátt"
    assert calls.by_entity()[lit.lamp][1]["brightness_pct"] == 10
    calls.clear()
    await client.send_json_auto_id({"type": "smart_occupancy/lighting/settings/set",
                                    "unrepresentable": {"Onki": {"mode": "no_mood"}}})
    assert (await client.receive_json())["success"]
    await hass.async_block_till_done()
    await advance(hass, freezer)
    assert ctrl.mood_status().mood is None and calls.all == []
    assert hass.states.get(AUTO).state == "Onki" and hass.states.get(MAIN).state == "Dagur"


async def test_new_moods_are_discovered(hass: HomeAssistant, lit: Lit, hass_ws_client) -> None:
    entry = await setup_entry(hass, options(lit))
    ctrl = ctrl_of(entry)
    assert "Work" not in {m["name"] for m in ctrl.discovered_moods()}
    set_moods(hass, "Dagur", "Auto", "Dagur", manual_opts=[*MAIN_OPTS, "Work"])  # added to Manual Mood only
    await hass.async_block_till_done()
    work = next(m for m in ctrl.discovered_moods() if m["name"] == "Work")
    assert work["in_manual"] and not work["in_main"]
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": "smart_occupancy/lighting/profile/set", "scope": "global", "mood": "Work",
                                    "target": "lamps", "setting": {"action": "on", "brightness_pct": 100,
                                                                   "color_temp_kelvin": 5000}})
    assert (await client.receive_json())["success"]
    await hass.async_block_till_done()
    await entry.runtime_data.async_refresh()
    codes = {p.code for p in entry.runtime_data.data.problems}
    assert "mood_not_in_main" in codes
    set_moods(hass, "Dagur", "Auto", "Dagur", main_opts=[*MAIN_OPTS, "Work"], manual_opts=[*MAIN_OPTS, "Work"])
    await hass.async_block_till_done()
    await entry.runtime_data.async_refresh()
    assert "mood_not_in_main" not in {p.code for p in entry.runtime_data.data.problems}
    await client.send_json_auto_id({"type": "smart_occupancy/lighting/editor", "mood": "Work"})
    model = (await client.receive_json())["result"]
    assert model["global"]["lamps"]["effective"]["color_temp_kelvin"] == 5000


async def test_labels_classify_lights_and_custom_categories(hass: HomeAssistant, lit: Lit, freezer) -> None:
    entry = await setup_entry(hass, options(lit))
    ctrl = ctrl_of(entry)
    managed = ctrl.managed_room(lit.area)
    assert set(managed.targets) == {lit.group, lit.spot_1, lit.spot_2, lit.lamp, lit.desk, lit.strip, lit.plug}
    assert managed.covered_by == {lit.spot_1: lit.group, lit.spot_2: lit.group}
    # A new light with the custom "Decor" label becomes available automatically.
    ent_reg = er.async_get(hass)
    garland = ent_reg.async_get_or_create("light", "test", "uid_garland", suggested_object_id="kno_garland")
    hass.states.async_set(garland.entity_id, "off", {"supported_color_modes": ["onoff"]})
    ent_reg.async_update_entity(garland.entity_id, area_id=lit.area, labels={lit.labels["decor"]})
    await hass.async_block_till_done()
    await advance(hass, freezer, 3)
    assert garland.entity_id in ctrl.managed_room(lit.area).targets
    assert ctrl.index.category_of(garland.entity_id) == "decor"
    # Moving it to another area removes it from the room; the exclusion label excludes a light.
    ent_reg.async_update_entity(garland.entity_id, area_id=lit.other)
    ctrl.store.async_set(replace(ctrl.lighting, exclude_label=lit.labels["exclude"]))
    ent_reg.async_update_entity(lit.plug, labels={lit.labels["lamps"], lit.labels["exclude"]})
    await hass.async_block_till_done()
    await advance(hass, freezer, 3)
    managed = ctrl.managed_room(lit.area)
    assert garland.entity_id not in managed.targets
    assert managed.excluded == {lit.plug: "exclusion label"}
    assert lit.plug not in managed.controlled


async def test_excluded_and_unsupported_lights(hass: HomeAssistant, lit: Lit, calls: Calls, freezer) -> None:
    opts = options(lit)
    opts["global_profiles"]["Dagur"]["lamps"]["effect"] = "candle"
    entry = await setup_entry(hass, opts)
    await occupy(hass, lit, freezer)
    await activate(hass, entry, lit, freezer, excluded=(lit.desk,))
    sent = calls.by_entity()
    assert lit.desk not in sent
    assert sent[lit.plug] == ("turn_on", {})  # on/off only: brightness, colour and effect omitted
    assert "effect" not in sent[lit.lamp][1]
    await entry.runtime_data.async_refresh()
    unsupported = [p for p in entry.runtime_data.data.problems if p.code == "lighting_unsupported"]
    assert {p.entity_id for p in unsupported} == {lit.plug, lit.lamp}
    assert all(p.aspect == "lighting" for p in unsupported)
    state = hass.states.get(er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{DOMAIN}_room_{lit.area}_state"))
    health = state.attributes["health"]
    assert health["lighting"]["status"] == "warning"
    assert health["occupancy"]["status"] == "healthy"  # lighting problems never degrade occupancy


async def test_opaque_group_warning(hass: HomeAssistant, lit: Lit) -> None:
    """A Zigbee2MQTT group (device model "Group") hides its members: possible duplicates are reported."""
    source = MockConfigEntry(domain="test")
    source.add_to_hass(hass)
    dev = dr.async_get(hass).async_get_or_create(config_entry_id=source.entry_id, identifiers={("z2m", "g1")},
                                                manufacturer="Zigbee2MQTT", model="Group", name="Spots group")
    dr.async_get(hass).async_update_device(dev.id, area_id=lit.area)
    ent = er.async_get(hass).async_get_or_create("light", "mqtt", "z2m_g1", suggested_object_id="kno_z2m_group",
                                                 device_id=dev.id)
    er.async_get(hass).async_update_entity(ent.entity_id, labels={lit.labels["downlights"]})
    hass.states.async_set(ent.entity_id, "off", CT)
    entry = await setup_entry(hass, options(lit))
    assert ent.entity_id in ctrl_of(entry).managed_room(lit.area).opaque_groups
    codes = [p for p in entry.runtime_data.data.problems if p.code == "group_overlap_unknown"]
    assert codes and codes[0].entity_id == ent.entity_id


# --------------------------------------------------------- protections
async def test_manual_overrides(hass: HomeAssistant, lit: Lit, calls: Calls, freezer, hass_admin_user,
                                hass_ws_client) -> None:
    entry = await setup_entry(hass, options(lit))
    ctrl = ctrl_of(entry)
    client = await hass_ws_client(hass)  # before the clock is moved (access tokens expire)
    await occupy(hass, lit, freezer)
    await activate(hass, entry, lit, freezer, manual_override_minutes=30, manual_switches=("event.kno_switch",))
    calls.clear()
    # The user dims the lamp in the app: recognised from the context's user.
    hass.states.async_set(lit.lamp, "on", {**CT, "brightness": 40}, context=Context(user_id=hass_admin_user.id))
    # A change without any context is not assumed to be manual.
    hass.states.async_set(lit.desk, "on", {**CT, "brightness": 90}, context=Context())
    await hass.async_block_till_done()
    rt = ctrl.runtime[lit.area]
    assert set(rt.overrides) == {lit.lamp} and rt.overrides[lit.lamp]["source"] == "ui"
    # A wall switch configured for the room attributes the next change to a person.
    hass.states.async_set("event.kno_switch", "2026-11-11T20:00:00+00:00", {"event_type": "single"})
    hass.states.async_set(lit.strip, "on", {"brightness": 200}, context=Context())
    await hass.async_block_till_done()
    assert rt.overrides[lit.strip]["source"] == "wall_switch"
    # Next mood change: manual changes are respected, the rest follows.
    set_moods(hass, "Kvøld", "Auto", "Kvøld")
    await hass.async_block_till_done()
    await advance(hass, freezer)
    sent = calls.by_entity()
    assert lit.lamp not in sent and lit.desk in sent
    plan = ctrl.plan(lit.area)
    assert next(i for i in plan.items if i.entity_id == lit.lamp).reason_code == "manual_override"
    # Overrides expire.
    await advance(hass, freezer, 31 * 60)
    assert rt.overrides == {}
    # ... can be cleared from the card; vacancy clears them all.
    hass.states.async_set(lit.lamp, "off", CT, context=Context(user_id=hass_admin_user.id))
    hass.states.async_set(lit.desk, "off", CT, context=Context(user_id=hass_admin_user.id))
    await hass.async_block_till_done()
    await client.send_json_auto_id({"type": "smart_occupancy/lighting/override/clear", "area_id": lit.area,
                                    "entity_id": lit.lamp})
    assert (await client.receive_json())["result"] == {"cleared": 1}
    assert set(rt.overrides) == {lit.desk}
    hass.states.async_set(lit.pir, "off")
    hass.states.async_set(lit.radar, "off")
    await hass.async_block_till_done()
    await advance(hass, freezer, 700)
    await advance(hass, freezer, 5)
    assert ctrl.coordinator.occupancy.result(lit.area).state.value == "vacant"
    assert rt.overrides == {}


async def test_manual_change_of_a_group_member_protects_the_group(hass: HomeAssistant, lit: Lit, calls: Calls,
                                                                  freezer, hass_admin_user) -> None:
    entry = await setup_entry(hass, options(lit))
    ctrl = ctrl_of(entry)
    await occupy(hass, lit, freezer)
    await activate(hass, entry, lit, freezer)
    hass.states.async_set(lit.spot_1, "off", CT, context=Context(user_id=hass_admin_user.id))
    await hass.async_block_till_done()
    assert lit.group in ctrl.runtime[lit.area].overrides
    calls.clear()
    set_moods(hass, "Kvøld", "Auto", "Kvøld")
    await hass.async_block_till_done()
    await advance(hass, freezer)
    assert lit.group not in calls.by_entity() and lit.lamp in calls.by_entity()


async def test_own_commands_are_not_manual(hass: HomeAssistant, lit: Lit, calls: Calls, freezer) -> None:
    entry = await setup_entry(hass, options(lit))
    ctrl = ctrl_of(entry)
    await occupy(hass, lit, freezer)
    await activate(hass, entry, lit, freezer)
    call = next(c for c in calls.on if lit.lamp in c.data["entity_id"])
    # The light reports back with our context (local) or a new one shortly after (MQTT echo).
    hass.states.async_set(lit.lamp, "on", {**CT, "brightness": 191}, context=call.context)
    hass.states.async_set(lit.desk, "on", {**CT, "brightness": 191}, context=Context())
    await hass.async_block_till_done()
    assert ctrl.runtime[lit.area].overrides == {}


async def test_automation_conflicts(hass: HomeAssistant, lit: Lit, calls: Calls, freezer, hass_ws_client) -> None:
    hass.states.async_set("input_boolean.trigger", "off")
    assert await async_setup_component(hass, "automation", {"automation": [
        {"id": "a1", "alias": "Kamar lamp on motion", "triggers": [{"trigger": "state", "entity_id": "input_boolean.trigger"}],
         "actions": [{"action": "light.turn_on", "target": {"entity_id": lit.lamp}}]},
        {"id": "a2", "alias": "Kamar all off", "triggers": [{"trigger": "state", "entity_id": "input_boolean.trigger"}],
         "actions": [{"action": "light.turn_off", "target": {"area_id": lit.area}}]},
        {"id": "a3", "alias": "Notify", "triggers": [{"trigger": "state", "entity_id": lit.lamp}],
         "actions": [{"action": "persistent_notification.create", "data": {"message": "x"}}]},
    ]})
    await hass.async_block_till_done()
    entry = await setup_entry(hass, options(lit))
    ctrl = ctrl_of(entry)
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": "smart_occupancy/lighting/room/activation", "area_id": lit.area})
    review = (await client.receive_json())["result"]
    autos = {a["automation"]: a for a in review["automations"]}
    assert set(autos) == {"automation.kamar_lamp_on_motion", "automation.kamar_all_off"}  # triggers don't count
    assert {li["via"] for li in autos["automation.kamar_lamp_on_motion"]["lights"]} == {"entity"}
    assert {li["via"] for li in autos["automation.kamar_all_off"]["lights"]} == {"area"}
    # Activate acknowledging only the area-wide one: the lamp is left alone.
    await occupy(hass, lit, freezer)
    await client.send_json_auto_id({"type": "smart_occupancy/lighting/room/activate", "area_id": lit.area,
                                    "confirm": True, "acknowledge": ["automation.kamar_all_off"]})
    assert (await client.receive_json())["success"]
    await hass.async_block_till_done()
    await advance(hass, freezer)
    sent = calls.by_entity()
    assert lit.lamp not in sent and lit.desk in sent
    assert ctrl.room_diagnostics(lit.area)["blocking_automations"] == ["automation.kamar_lamp_on_motion"]
    await entry.runtime_data.async_refresh()
    conflict = next(p for p in entry.runtime_data.data.problems if p.code == "automation_conflict"
                    and p.entity_id == "automation.kamar_lamp_on_motion")
    assert conflict.severity.value == "warning" and "never changed" in conflict.action
    assert hass.states.get("automation.kamar_lamp_on_motion").state == "on"  # never disabled by us
    # Disabling the automation (by the user) resolves the conflict.
    await hass.services.async_call("automation", "turn_off", {"entity_id": "automation.kamar_lamp_on_motion"},
                                   blocking=True)
    await advance(hass, freezer, 3)
    assert ctrl.room_diagnostics(lit.area)["blocking_automations"] == []


async def test_offline_light_and_reconnect(hass: HomeAssistant, lit: Lit, calls: Calls, freezer) -> None:
    entry = await setup_entry(hass, options(lit))
    await occupy(hass, lit, freezer)
    hass.states.async_set(lit.lamp, "unavailable", CT)
    await hass.async_block_till_done()
    await activate(hass, entry, lit, freezer)
    assert lit.lamp not in calls.by_entity()
    diag = ctrl_of(entry).room_diagnostics(lit.area)
    assert diag["offline_lights"] == [lit.lamp]
    await entry.runtime_data.async_refresh()
    state = hass.states.get(er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{DOMAIN}_room_{lit.area}_state"))
    assert state.attributes["health"]["occupancy"]["status"] == "healthy"
    assert state.attributes["health"]["lighting"]["status"] == "warning"
    calls.clear()
    hass.states.async_set(lit.lamp, "off", CT)
    await hass.async_block_till_done()
    await advance(hass, freezer)
    assert set(calls.by_entity()) == {lit.lamp}


async def test_failed_command_is_reported(hass: HomeAssistant, lit: Lit, freezer) -> None:
    async def fail(call: ServiceCall) -> None:
        raise HomeAssistantError("Zigbee timeout")

    hass.services.async_register("light", "turn_on", fail)
    async_mock_service(hass, "light", "turn_off")
    entry = await setup_entry(hass, options(lit))
    await occupy(hass, lit, freezer)
    await activate(hass, entry, lit, freezer)
    diag = ctrl_of(entry).room_diagnostics(lit.area)
    assert diag["last_result"] == "failed" and "Zigbee timeout" in diag["last_errors"][0]
    await entry.runtime_data.async_refresh()
    assert any(p.code == "lighting_execution_failed" for p in entry.runtime_data.data.problems)


# ----------------------------------------------------------- lifecycle
async def test_restart_and_reload_send_nothing(hass: HomeAssistant, lit: Lit, calls: Calls, freezer,
                                               hass_admin_user) -> None:
    entry = await setup_entry(hass, options(lit))
    await occupy(hass, lit, freezer)
    await activate(hass, entry, lit, freezer)
    hass.states.async_set(lit.lamp, "on", {**CT, "brightness": 40}, context=Context(user_id=hass_admin_user.id))
    await hass.async_block_till_done()
    calls.clear()
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    await advance(hass, freezer, 10)
    assert calls.all == []
    ctrl = ctrl_of(entry)
    assert ctrl.room_active(lit.area)
    assert ctrl.runtime[lit.area].last_applied_mood == "Dagur"
    assert lit.lamp in ctrl.runtime[lit.area].overrides  # persisted


async def test_unload_cleans_up_listeners(hass: HomeAssistant, lit: Lit, calls: Calls, freezer) -> None:
    def counts() -> dict[str, int]:
        return dict(hass.bus.async_listeners())

    # A first cycle loads Home Assistant's own platform components, which stay loaded.
    entry = await setup_entry(hass, options(lit))
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    before = counts()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await occupy(hass, lit, freezer)
    await activate(hass, entry, lit, freezer)
    assert counts() != before
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    after = counts()
    leaked = {k: v - before.get(k, 0) for k, v in after.items() if v > before.get(k, 0)}
    assert not leaked, leaked
    signal_listeners = []
    unsub = async_dispatcher_connect(hass, SIGNAL_LIGHTING_UPDATED, lambda: signal_listeners.append(1))
    unsub()
    assert not hass.data.get("dispatcher", {}).get(SIGNAL_LIGHTING_UPDATED)
    calls.clear()
    set_moods(hass, "Dagur", "Hygge", "Hygge")
    await hass.async_block_till_done()
    await advance(hass, freezer, 5)
    assert calls.all == []


async def test_entity_rename_followed_in_lighting_store(hass: HomeAssistant, lit: Lit, freezer) -> None:
    entry = await setup_entry(hass, options(lit))
    ctrl = ctrl_of(entry)
    ctrl.store.async_set(ctrl.with_room(lit.area, excluded=(lit.desk,)))
    er.async_get(hass).async_update_entity(lit.desk, new_entity_id="light.kno_desk_lamp")
    await hass.async_block_till_done()
    assert ctrl_of(entry).lighting.room(lit.area).excluded == ("light.kno_desk_lamp",)


# ------------------------------------------------------------ websocket
async def test_websocket_permissions(hass: HomeAssistant, lit: Lit, calls: Calls, hass_ws_client,
                                     hass_read_only_access_token, caplog) -> None:
    caplog.set_level(logging.CRITICAL, logger=WS_LOGGER)  # rejected messages are logged as errors by HA
    await setup_entry(hass, options(lit))
    client = await hass_ws_client(hass, hass_read_only_access_token)
    await client.send_json_auto_id({"type": "smart_occupancy/lighting/editor", "area_id": lit.area})
    reply = await client.receive_json()
    assert reply["success"] and reply["result"]["can_edit"] is False
    for msg in (
        {"type": "smart_occupancy/lighting/profile/set", "scope": "global", "mood": "Hygge", "target": "lamps",
         "setting": {"action": "on"}},
        {"type": "smart_occupancy/lighting/room/activate", "area_id": lit.area, "confirm": True},
        {"type": "smart_occupancy/lighting/settings/set", "fallback_mood": "Dagur"},
        {"type": "smart_occupancy/lighting/room/set", "area_id": lit.area, "mood_override": "Nátt"},
    ):
        await client.send_json_auto_id(msg)
        reply = await client.receive_json()
        assert not reply["success"] and reply["error"]["code"] == "unauthorized", msg
    await client.send_json_auto_id({"type": "smart_occupancy/lighting/preview", "area_id": lit.area})
    assert (await client.receive_json())["success"]
    assert calls.all == []


async def test_websocket_validation_and_subscription(hass: HomeAssistant, lit: Lit, hass_ws_client, freezer,
                                                    caplog) -> None:
    caplog.set_level(logging.CRITICAL, logger=WS_LOGGER)  # rejected messages are logged as errors by HA
    await setup_entry(hass, options(lit))
    client = await hass_ws_client(hass)

    async def ws(**msg):
        await client.send_json_auto_id(msg)
        return await client.receive_json()

    reply = await ws(type="smart_occupancy/lighting/profile/set", scope="global", mood="Hygge", target="lamps",
                     setting={"action": "on", "brightness_pct": 0})
    assert reply["error"]["code"] == "invalid_profile" and "1 to 100" in reply["error"]["message"]
    reply = await ws(type="smart_occupancy/lighting/profile/set", scope="global", mood="Hygge", target="nope",
                     setting={"action": "on"})
    assert not reply["success"]
    assert (await ws(type="smart_occupancy/lighting/mood/set", mood="Hygge", parent="Kvøld"))["success"]
    reply = await ws(type="smart_occupancy/lighting/mood/set", mood="Kvøld", parent="Hygge")
    assert reply["error"]["code"] == "inheritance_loop"
    reply = await ws(type="smart_occupancy/lighting/room/activate", area_id=lit.area)
    assert not reply["success"]  # explicit confirmation required
    sub = await ws(type="smart_occupancy/lighting/subscribe")
    assert sub["success"]
    assert (await ws(type="smart_occupancy/lighting/settings/set", fallback_mood="Dagur"))["success"]
    await advance(hass, freezer, 1)
    event = await client.receive_json()
    assert event["id"] == sub["id"] and event["event"] == {"changed": True}
    model = (await ws(type="smart_occupancy/lighting/editor", mood="Hygge"))["result"]
    assert model["settings"]["fallback_mood"] == "Dagur"
    hygge = next(m for m in model["moods"] if m["name"] == "Hygge")
    assert hygge["parent"] == "Kvøld" and hygge["in_main"]
    assert model["global"]["lamps"]["own"]["brightness_pct"] == 70
