"""Global light adjustment and Manual mode (0.3.2) in a running Home Assistant.

The lights are simulated: ``light.turn_on/turn_off`` are replaced by a fake that
records every call and writes the resulting state back with the call's context
(like a real light integration), so our own echoes are recognised.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import pytest
from homeassistant.core import Context, HomeAssistant, ServiceCall
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import label_registry as lr
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.smart_occupancy.const import ALL_MOODS, DOMAIN

from .conftest import setup_entry

AUTO, MANUAL, MAIN = "input_select.auto_mood", "input_select.mood", "input_select.main_mood"
MOODS = ["Dagur", "Kvøld", "Hygge", "Arbeiði", "Veistla", "Nátt", "Onki"]
MAIN_OPTS = [*MOODS, "Manual"]
BRIGHT, CT = "sensor.global_brightness", "sensor.global_color_temp"
CT_ATTRS = {"supported_color_modes": ["color_temp"], "min_color_temp_kelvin": 2200, "max_color_temp_kelvin": 6500}


def set_moods(hass: HomeAssistant, main: str, manual: str | None = None, auto: str = "Dagur") -> None:
    hass.states.async_set(AUTO, auto, {"options": MOODS})
    hass.states.async_set(MANUAL, manual or main, {"options": ["Auto", *MAIN_OPTS]})
    hass.states.async_set(MAIN, main, {"options": MAIN_OPTS})


def set_sources(hass: HomeAssistant, brightness: Any = 62, kelvin: Any = 3200) -> None:
    if brightness is not None:
        hass.states.async_set(BRIGHT, str(brightness), {"unit_of_measurement": "%", "friendly_name": "Global brightness"})
    if kelvin is not None:
        hass.states.async_set(CT, str(kelvin), {"unit_of_measurement": "K", "friendly_name": "Global colour temperature"})


@dataclass
class Home:
    area: str
    pir: str
    radar: str
    spots: str       # HA light group (Spottar), members spot_1/spot_2
    spot_1: str
    spot_2: str
    lamp: str        # Loft ljós, CT, transition
    desk: str        # Loft ljós, CT, no transition
    uplight: str     # Uppljós + Rgb, colour and CT
    strip: str       # LED strip, brightness only
    rgb_only: str    # Uppljós + Rgb, hs only (no white mode)
    labels: dict[str, str]


class FakeLights:
    """light.turn_on/off that record calls and update the states with the caller's context."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.calls: list[ServiceCall] = []
        hass.services.async_register("light", "turn_on", self._on)
        hass.services.async_register("light", "turn_off", self._off)

    def _ids(self, call: ServiceCall) -> list[str]:
        ids = call.data["entity_id"]
        return [ids] if isinstance(ids, str) else list(ids)

    async def _on(self, call: ServiceCall) -> None:
        self.calls.append(call)
        for e in self._ids(call):
            old = self.hass.states.get(e)
            attrs = dict(old.attributes) if old else {}
            if "brightness_pct" in call.data:
                attrs["brightness"] = round(call.data["brightness_pct"] * 255 / 100)
            attrs.setdefault("brightness", 255)
            if "color_temp_kelvin" in call.data:
                attrs |= {"color_mode": "color_temp", "color_temp_kelvin": call.data["color_temp_kelvin"]}
            if "rgb_color" in call.data:
                attrs |= {"color_mode": "hs", "rgb_color": list(call.data["rgb_color"])}
                attrs.pop("color_temp_kelvin", None)
            if "effect" in call.data:
                attrs["effect"] = call.data["effect"]
            self.hass.states.async_set(e, "on", attrs, context=call.context)

    async def _off(self, call: ServiceCall) -> None:
        self.calls.append(call)
        for e in self._ids(call):
            old = self.hass.states.get(e)
            caps = {k: v for k, v in (old.attributes if old else {}).items()
                    if k in ("supported_color_modes", "min_color_temp_kelvin", "max_color_temp_kelvin",
                             "supported_features", "entity_id", "effect_list", "friendly_name")}
            self.hass.states.async_set(e, "off", caps, context=call.context)

    def clear(self) -> None:
        self.calls.clear()

    def by_entity(self) -> dict[str, tuple[str, dict[str, Any]]]:
        out: dict[str, tuple[str, dict[str, Any]]] = {}
        for call in self.calls:
            data = {k: v for k, v in call.data.items() if k != "entity_id"}
            for e in self._ids(call):
                out[e] = (call.service, data)
        return out

    def count(self, entity_id: str) -> int:
        return sum(entity_id in self._ids(c) for c in self.calls)


@pytest.fixture
def lights(hass: HomeAssistant) -> FakeLights:
    return FakeLights(hass)


@pytest.fixture
async def home(hass: HomeAssistant) -> Home:
    area_reg, ent_reg, label_reg = ar.async_get(hass), er.async_get(hass), lr.async_get(hass)
    room = area_reg.async_create("Kamar Norður")
    labels = {k: label_reg.async_create(n).label_id for k, n in (
        ("spottar", "Spottar"), ("loft", "Loft ljós"), ("strips", "LED strips"), ("uppljos", "Uppljós"), ("rgb", "Rgb"))}

    def make(domain: str, object_id: str, *label_keys: str, device_class: str | None = None) -> str:
        entry = ent_reg.async_get_or_create(domain, "test", f"uid_{object_id}", suggested_object_id=object_id,
                                            original_device_class=device_class)
        ent_reg.async_update_entity(entry.entity_id, area_id=room.id, labels={labels[k] for k in label_keys})
        return entry.entity_id

    pir = make("binary_sensor", "kno_pir", device_class="motion")
    radar = make("binary_sensor", "kno_radar", device_class="occupancy")
    spots = make("light", "kno_spottar", "spottar")
    spot_1 = make("light", "kno_spot_1", "spottar")
    spot_2 = make("light", "kno_spot_2", "spottar")
    lamp = make("light", "kno_lamp", "loft")
    desk = make("light", "kno_desk", "loft")
    uplight = make("light", "kno_uplight", "uppljos", "rgb")
    strip = make("light", "kno_strip", "strips")
    rgb_only = make("light", "kno_rgb_only", "uppljos", "rgb")
    for e in (pir, radar):
        hass.states.async_set(e, "off")
    hass.states.async_set(spots, "off", {**CT_ATTRS, "entity_id": [spot_1, spot_2], "supported_features": 32})
    for e in (spot_1, spot_2):
        hass.states.async_set(e, "off", CT_ATTRS)
    hass.states.async_set(lamp, "off", {**CT_ATTRS, "supported_features": 32})
    hass.states.async_set(desk, "off", CT_ATTRS)
    hass.states.async_set(uplight, "off", {"supported_color_modes": ["color_temp", "hs"], "min_color_temp_kelvin": 2200,
                                           "max_color_temp_kelvin": 6500, "effect_list": ["rainbow"],
                                           "supported_features": 4})
    hass.states.async_set(strip, "off", {"supported_color_modes": ["brightness"]})
    hass.states.async_set(rgb_only, "off", {"supported_color_modes": ["hs"]})
    set_moods(hass, "Dagur", "Auto")
    set_sources(hass)
    await hass.async_block_till_done()
    return Home(room.id, pir, radar, spots, spot_1, spot_2, lamp, desk, uplight, strip, rgb_only, labels)


ON = {"action": "on"}
FOLLOW = {"action": "on", "brightness_mode": "global", "color_temp_mode": "global"}
PROFILES: dict[str, Any] = {
    "Dagur": {"spottar": FOLLOW, "loft": FOLLOW, "strips": {**FOLLOW}, "uppljos": ON,
              "uppljos+rgb": {"action": "on", "brightness_mode": "offset", "brightness_offset": -20,
                              "rgb_color": [255, 150, 40]}},
    "Kvøld": {"loft": {"action": "on", "brightness_mode": "offset", "brightness_offset": -20,
                       "color_temp_mode": "offset", "color_temp_offset": -300}},
    "Hygge": {"loft": {"action": "on", "brightness_pct": 30, "color_temp_kelvin": 2200}, "spottar": {"action": "off"}},
    "Arbeiði": {"loft": {"action": "on", "brightness_pct": 100, "color_temp_mode": "global"}},
    "Veistla": {"uppljos": {"action": "on", "effect": "rainbow", "brightness_mode": "global"}},
}


def options(h: Home, profiles: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "config_version": 2,
        "operating_mode": "observe",
        "mood": {"auto_mood_entity": AUTO, "manual_mood_entity": MANUAL, "main_mood_entity": MAIN,
                 "auto_option": "Auto", "mismatch_grace_seconds": 30},
        "categories": [
            {"key": "spottar", "name": "Spottar", "label_id": h.labels["spottar"], "kind": "source"},
            {"key": "loft", "name": "Loft ljós", "label_id": h.labels["loft"], "kind": "source"},
            {"key": "strips", "name": "LED strips", "label_id": h.labels["strips"], "kind": "source"},
            {"key": "uppljos", "name": "Uppljós", "label_id": h.labels["uppljos"], "kind": "function"},
            {"key": "rgb", "name": "Rgb", "label_id": h.labels["rgb"], "kind": "rgb"},
        ],
        "rooms": {h.area: {
            "enabled": True,
            "sensors": [{"entity_id": h.pir, "kind": "pir", "role": "trigger"},
                        {"entity_id": h.radar, "kind": "radar", "role": "maintain"}],
            "lights": [],
        }},
        "global_profiles": PROFILES if profiles is None else profiles,
    }


async def advance(hass: HomeAssistant, freezer, seconds: float = 2.5) -> None:
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def occupy(hass: HomeAssistant, h: Home, freezer) -> None:
    hass.states.async_set(h.pir, "on")
    hass.states.async_set(h.radar, "on")
    await hass.async_block_till_done()
    await advance(hass, freezer)


def ctrl_of(entry):
    return entry.runtime_data.lighting


async def configure_sources(hass: HomeAssistant, entry, **adaptive: Any) -> None:
    ctrl = ctrl_of(entry)
    from dataclasses import replace

    from custom_components.smart_occupancy.lighting_config import AdaptiveConfig
    data = {"brightness_entity": BRIGHT, "color_temp_entity": CT, **adaptive}
    ctrl.store.async_set(replace(ctrl.lighting, adaptive=AdaptiveConfig.from_dict(data)))
    await hass.async_block_till_done()


async def activate(hass: HomeAssistant, entry, h: Home, freezer, **room: Any) -> None:
    ctrl = ctrl_of(entry)
    ctrl.store.async_set(ctrl.with_room(h.area, control_enabled=True, **room))
    await hass.async_block_till_done()
    await advance(hass, freezer)


async def ready(hass: HomeAssistant, h: Home, lights: FakeLights, freezer, profiles=None, **adaptive: Any):
    """Entry with both sources, room occupied and activated (Dagur applied)."""
    entry = await setup_entry(hass, options(h, profiles))
    await configure_sources(hass, entry, **adaptive)
    await occupy(hass, h, freezer)
    await activate(hass, entry, h, freezer)
    return entry


def lighting_state(hass: HomeAssistant, h: Home):
    entity_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{DOMAIN}_room_{h.area}_mood_lighting")
    return hass.states.get(entity_id)


# --------------------------------------------------------------- upgrade
async def test_upgrade_from_031_store_keeps_settings(hass: HomeAssistant, home: Home, hass_storage, lights,
                                                     freezer) -> None:
    """A 0.3.1 lighting store loads unchanged: fixed stays fixed, nothing follows a global source."""
    stored = {
        "global_profiles": {"Dagur": {"loft": {"action": "on", "brightness_pct": 75, "color_temp_kelvin": 3500}},
                            "Hygge": {"loft": {"action": "on", "brightness_pct": 30}}},
        "parents": {"Hygge": "Dagur"}, "fallback_mood": None, "unrepresentable": {}, "exclude_label": None,
        "manual_override_minutes": 45, "unattributed": "ignore",
        "rooms": {home.area: {"control_enabled": True, "activated_at": "2026-11-10T20:00:00+00:00",
                              "activated_by": "u1", "acknowledged": [], "mood_override": None, "auto_discover": True,
                              "excluded": [home.strip], "mood_overrides": {"Hygge": {"loft": {"action": "inherit",
                                                                                          "brightness_pct": 25}}},
                              "light_overrides": {}, "manual_override_minutes": None, "manual_switches": [],
                              "dismissed": []}},
    }
    hass_storage["smart_occupancy.lighting"] = {"version": 1, "minor_version": 1, "key": "smart_occupancy.lighting",
                                                "data": stored}
    entry = await setup_entry(hass, options(home, {}))
    ctrl = ctrl_of(entry)
    cfg = ctrl.lighting
    assert cfg.manual_override_minutes == 45 and cfg.parents == {"Hygge": "Dagur"}
    assert cfg.room(home.area).control_enabled and cfg.room(home.area).excluded == (home.strip,)
    assert cfg.adaptive.entities == () and cfg.manual_option == "Manual"
    assert lights.calls == []  # nothing applied on start-up
    # Fixed stays fixed, even once a global source is configured.
    await configure_sources(hass, entry)
    await occupy(hass, home, freezer)
    plan = ctrl.plan(home.area)
    lamp = next(i for i in plan.items if i.entity_id == home.lamp)
    assert lamp.data == {"brightness_pct": 75, "color_temp_kelvin": 3500}
    assert lamp.resolved.adaptive["brightness"]["status"] == "fixed" and lamp.adaptive_update is None
    # Saving keeps every old key and adds the new ones.
    ctrl.store.async_set(ctrl.with_room(home.area, manual_override_minutes=10))
    await ctrl.async_flush()
    data = hass_storage["smart_occupancy.lighting"]["data"]
    assert data["global_profiles"] == stored["global_profiles"]
    assert data["rooms"][home.area]["mood_overrides"] == stored["rooms"][home.area]["mood_overrides"]
    assert data["adaptive"]["brightness_entity"] == BRIGHT and data["manual_option"] == "Manual"


# ------------------------------------------------------- sources + websocket
async def test_source_configuration_live_values_and_websocket(hass: HomeAssistant, home: Home, hass_ws_client,
                                                              lights) -> None:
    entry = await setup_entry(hass, options(home))
    ctrl = ctrl_of(entry)
    client = await hass_ws_client(hass)

    async def ws(msg: dict[str, Any]) -> dict[str, Any]:
        await client.send_json_auto_id(msg)
        return await client.receive_json()

    model = (await ws({"type": "smart_occupancy/lighting/editor"}))["result"]
    assert model["adaptive"]["status"] == "not_configured"
    cands = model["adaptive"]["candidates"]
    assert BRIGHT in [c["entity_id"] for c in cands["brightness"]]
    assert CT in [c["entity_id"] for c in cands["color_temp"]]
    # Brightness only.
    res = await ws({"type": "smart_occupancy/lighting/adaptive/set", "brightness_entity": BRIGHT})
    assert res["success"], res
    values = ctrl.global_values()
    assert values.brightness.value == 62 and values.color_temp.status == "not_configured"
    assert values.status == "connected"
    # Temperature only.
    assert (await ws({"type": "smart_occupancy/lighting/adaptive/set", "brightness_entity": None,
                      "color_temp_entity": CT}))["success"]
    values = ctrl.global_values()
    assert values.brightness.status == "not_configured" and values.color_temp.value == 3200
    # Both, plus advanced timing.
    assert (await ws({"type": "smart_occupancy/lighting/adaptive/set", "brightness_entity": BRIGHT,
                      "min_interval": 30, "transition": 5}))["success"]
    model = (await ws({"type": "smart_occupancy/lighting/editor"}))["result"]
    assert model["adaptive"]["values"]["brightness"]["value"] == 62
    assert model["adaptive"]["values"]["color_temp"]["value"] == 3200
    assert model["adaptive"]["config"]["min_interval"] == 30 and model["adaptive"]["status"] == "connected"
    # Live updates through the state subscription (no polling).
    set_sources(hass, 70, 2900)
    await hass.async_block_till_done()
    assert (ctrl.global_values().brightness.value, ctrl.global_values().color_temp.value) == (70, 2900)
    # Invalid input is refused.
    res = await ws({"type": "smart_occupancy/lighting/adaptive/set", "brightness_entity": "sensor.does_not_exist"})
    assert not res["success"] and res["error"]["code"] == "not_found"
    res = await ws({"type": "smart_occupancy/lighting/adaptive/set", "brightness_entity": "not an id"})
    assert not res["success"]
    # Every-mood entries via the API, validated.
    res = await ws({"type": "smart_occupancy/lighting/profile/set", "scope": "global", "mood": ALL_MOODS,
                    "target": "loft", "setting": {"action": "on", "brightness_mode": "global"}})
    assert not res["success"] and res["error"]["code"] == "invalid_profile"
    res = await ws({"type": "smart_occupancy/lighting/profile/set", "scope": "light", "mood": ALL_MOODS,
                    "area_id": home.area, "target": home.strip,
                    "setting": {"action": "inherit", "brightness_mode": "global", "color_temp_mode": "ignore"}})
    assert res["success"], res
    model = (await ws({"type": "smart_occupancy/lighting/editor", "area_id": home.area, "mood": ALL_MOODS}))["result"]
    assert ALL_MOODS not in [m["name"] for m in model["moods"]] and "Manual" not in [m["name"] for m in model["moods"]]
    strip = next(li for li in model["room"]["lights"] if li["entity_id"] == home.strip)
    assert strip["own"] == {"action": "inherit", "brightness_mode": "global", "color_temp_mode": "ignore"}
    assert strip["now"]["brightness_pct"] == 70  # Dagur: follow global (+ strip ignores CT via its "*" entry)
    assert "color_temp_kelvin" not in strip["now"]
    assert lights.calls == []


async def test_unavailable_source_is_never_zero_and_last_value_survives_restart(
        hass: HomeAssistant, home: Home, lights, freezer, hass_storage) -> None:
    entry = await ready(hass, home, lights, freezer)
    ctrl = ctrl_of(entry)
    assert lights.by_entity()[home.lamp][1]["brightness_pct"] == 62
    lights.clear()
    hass.states.async_set(BRIGHT, "unavailable")
    hass.states.async_set(CT, "unknown")
    await hass.async_block_till_done()
    await advance(hass, freezer, 120)
    assert lights.calls == []
    values = ctrl.global_values()
    assert values.brightness.status == "stale" and values.brightness.value == 62
    assert values.status == "unavailable"
    await entry.runtime_data.async_refresh()
    problem = next(p for p in entry.runtime_data.data.problems if p.code == "global_source_unavailable")
    assert "last good value" in problem.explanation
    await ctrl.async_flush()
    assert hass_storage["smart_occupancy.lighting_state"]["data"]["adaptive"]["brightness"][1] == 62
    # Reload: the last good value comes back (the source is still unavailable).
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert ctrl_of(entry).global_values().brightness.value == 62
    assert ctrl_of(entry).global_values().brightness.status == "stale"
    assert lights.calls == []  # nothing on reload


async def test_never_seen_source_skips_property(hass: HomeAssistant, home: Home, lights, freezer) -> None:
    hass.states.async_set(BRIGHT, "unavailable")
    entry = await setup_entry(hass, options(home))
    await configure_sources(hass, entry)
    await occupy(hass, home, freezer)
    await activate(hass, entry, home, freezer)
    sent = lights.by_entity()
    assert sent[home.lamp] == ("turn_on", {"color_temp_kelvin": 3200})  # brightness left as it is, never 0
    await entry.runtime_data.async_refresh()
    problem = next(p for p in entry.runtime_data.data.problems if p.code == "global_source_unavailable")
    assert "never taken as 0" in problem.explanation


# ------------------------------------------------------- following + updates
async def test_follow_global_smooth_throttled_updates(hass: HomeAssistant, home: Home, lights, freezer) -> None:
    entry = await ready(hass, home, lights, freezer)
    sent = lights.by_entity()
    # Dagur: everything follows the current global values; the group is commanded, not its members.
    assert sent[home.spots] == ("turn_on", {"brightness_pct": 62, "color_temp_kelvin": 3200})
    assert home.spot_1 not in sent and home.spot_2 not in sent
    assert sent[home.lamp] == ("turn_on", {"brightness_pct": 62, "color_temp_kelvin": 3200})
    assert sent[home.strip] == ("turn_on", {"brightness_pct": 62})  # brightness-only strip: no temperature
    assert sent[home.uplight] == ("turn_on", {"brightness_pct": 42, "rgb_color": [255, 150, 40]})  # RGB kept
    assert sent[home.rgb_only] == ("turn_on", {"brightness_pct": 42, "rgb_color": [255, 150, 40]})
    lights.clear()

    # Small changes (below 3 % / 100 K) send nothing.
    set_sources(hass, 64, 3250)
    await hass.async_block_till_done()
    await advance(hass, freezer, 70)
    assert lights.calls == []

    # A meaningful change: one coalesced update, with the configured transition where supported.
    set_sources(hass, 70, 3200)
    await hass.async_block_till_done()
    await advance(hass, freezer)
    sent = lights.by_entity()
    assert sent[home.lamp] == ("turn_on", {"brightness_pct": 70, "transition": 10.0})
    assert sent[home.spots] == ("turn_on", {"brightness_pct": 70, "transition": 10.0})
    assert sent[home.desk] == ("turn_on", {"brightness_pct": 70})  # no transition support
    assert sent[home.uplight] == ("turn_on", {"brightness_pct": 50})  # offset -20, colour untouched
    assert home.spot_1 not in sent
    assert len(lights.calls) == len({json_key(c) for c in lights.calls})  # identical data batched into one call
    lights.clear()

    # Rapid updates within the interval: deferred and coalesced, the newest value wins.
    for value in (72, 75, 80):
        set_sources(hass, value, 3200)
        await hass.async_block_till_done()
        await advance(hass, freezer, 1)
    await advance(hass, freezer, 5)
    assert lights.calls == []
    plan = ctrl_of(entry).plan(home.area)
    assert next(i for i in plan.items if i.entity_id == home.lamp).adaptive_update["decision"] == "deferred"
    await advance(hass, freezer, 60)
    assert lights.count(home.lamp) == 1 and lights.by_entity()[home.lamp][1]["brightness_pct"] == 80
    diag = lighting_state(hass, home).attributes
    assert diag["global_adjustment"] == "connected" and "followed the global adjustment" in diag["last_adaptive"]
    lights.clear()

    # A light switched off by something else is never switched on by a global change ...
    hass.states.async_set(home.desk, "off", CT_ATTRS, context=Context())
    await hass.async_block_till_done()
    await advance(hass, freezer, 61)
    set_sources(hass, 90, 3200)
    await hass.async_block_till_done()
    await advance(hass, freezer)
    assert home.desk not in lights.by_entity() and lights.by_entity()[home.lamp][1]["brightness_pct"] == 90
    item = next(i for i in ctrl_of(entry).plan(home.area).items if i.entity_id == home.desk)
    assert item.adaptive_update["code"] == "light_off"
    lights.clear()
    # ... and when it is switched on later (not by a person), it gets the current values right away.
    hass.states.async_set(home.desk, "on", {**CT_ATTRS, "brightness": 255, "color_mode": "color_temp",
                                            "color_temp_kelvin": 4000}, context=Context())
    await hass.async_block_till_done()
    await advance(hass, freezer)
    assert lights.by_entity()[home.desk] == ("turn_on", {"brightness_pct": 90, "color_temp_kelvin": 3200})


def json_key(call: ServiceCall) -> str:
    import json
    return json.dumps({k: v for k, v in call.data.items() if k != "entity_id"}, sort_keys=True) + call.service


async def test_global_changes_never_switch_lights_on(hass: HomeAssistant, home: Home, lights, freezer) -> None:
    entry = await ready(hass, home, lights, freezer)
    lights.clear()
    for e in (home.spots, home.lamp, home.desk, home.uplight, home.strip, home.rgb_only):
        hass.states.async_set(e, "off", dict(hass.states.get(e).attributes) | {"brightness": None}, context=Context())
    await hass.async_block_till_done()
    for b, k in ((20, 2200), (90, 6000), (45, 2700)):
        set_sources(hass, b, k)
        await hass.async_block_till_done()
        await advance(hass, freezer, 65)
    assert lights.calls == []
    # Nor on reload / start-up.
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    set_sources(hass, 70, 3000)
    await hass.async_block_till_done()
    await advance(hass, freezer, 65)
    assert lights.calls == []


async def test_per_mood_modes_offsets_rgb_effects(hass: HomeAssistant, home: Home, lights, freezer) -> None:
    entry = await ready(hass, home, lights, freezer)
    ctrl = ctrl_of(entry)

    async def mood(name: str) -> dict[str, tuple[str, dict[str, Any]]]:
        lights.clear()
        set_moods(hass, name)
        await hass.async_block_till_done()
        await advance(hass, freezer)
        return lights.by_entity()

    # Kvøld: global -20 points and -300 K (continuous, no separate evening mood).
    assert (await mood("Kvøld"))[home.lamp] == ("turn_on", {"brightness_pct": 42, "color_temp_kelvin": 2900})
    # Arbeiði: fixed bright, global temperature; a global change adjusts only the temperature.
    assert (await mood("Arbeiði"))[home.lamp] == ("turn_on", {"brightness_pct": 100, "color_temp_kelvin": 3200})
    lights.clear()
    await advance(hass, freezer, 61)
    set_sources(hass, 30, 4000)
    await hass.async_block_till_done()
    await advance(hass, freezer)
    assert lights.by_entity()[home.lamp] == ("turn_on", {"color_temp_kelvin": 4000, "transition": 10.0})
    # Hygge: fixed warm values override the global adjustment; global changes do nothing there.
    sent = await mood("Hygge")
    assert sent[home.lamp] == ("turn_on", {"brightness_pct": 30, "color_temp_kelvin": 2200})
    assert sent[home.spots][0] == "turn_off"
    lights.clear()
    await advance(hass, freezer, 61)
    set_sources(hass, 80, 5000)
    await hass.async_block_till_done()
    await advance(hass, freezer)
    assert lights.calls == []
    # Veistla: effect + global brightness; the global temperature never overwrites the effect.
    sent = await mood("Veistla")
    assert sent[home.uplight] == ("turn_on", {"brightness_pct": 80, "effect": "rainbow"})
    lights.clear()
    await advance(hass, freezer, 61)
    set_sources(hass, 50, 2500)
    await hass.async_block_till_done()
    await advance(hass, freezer)
    assert lights.by_entity()[home.uplight] == ("turn_on", {"brightness_pct": 50})
    # The preview explains it (and changes nothing).
    item = next(i for i in ctrl.plan(home.area).items if i.entity_id == home.uplight)
    assert item.resolved.adaptive["brightness"]["status"] == "applied"


async def test_every_mood_attachment_room_and_light(hass: HomeAssistant, home: Home, lights, freezer) -> None:
    profiles = {
        ALL_MOODS: {"loft": {"action": "inherit", "brightness_mode": "global"}},
        "Dagur": {"loft": {"action": "on"}, "strips": {"action": "on"}},
        "Hygge": {"loft": {"action": "on", "brightness_pct": 30}},
    }
    entry = await setup_entry(hass, options(home, profiles))
    await configure_sources(hass, entry)
    ctrl = ctrl_of(entry)
    room = ctrl.lighting.room(home.area)
    from dataclasses import replace

    from custom_components.smart_occupancy.models import ProfileSetting
    ctrl.store.async_set(ctrl.lighting.with_room(home.area, replace(
        room,
        mood_overrides={ALL_MOODS: {"loft": ProfileSetting.from_dict({"color_temp_mode": "global"})}},
        light_overrides={ALL_MOODS: {
            home.desk: ProfileSetting.from_dict({"brightness_mode": "offset", "brightness_offset": -20}),
            home.strip: ProfileSetting.from_dict({"brightness_mode": "global", "color_temp_mode": "ignore"}),
        }},
    )))
    await hass.async_block_till_done()
    await occupy(hass, home, freezer)
    await activate(hass, entry, home, freezer)
    sent = lights.by_entity()
    assert sent[home.lamp] == ("turn_on", {"brightness_pct": 62, "color_temp_kelvin": 3200})
    assert sent[home.desk] == ("turn_on", {"brightness_pct": 42, "color_temp_kelvin": 3200})
    assert sent[home.strip] == ("turn_on", {"brightness_pct": 62})
    assert home.spots not in sent  # "*" entries never switch anything on: Spottar has no Dagur action
    lights.clear()
    set_moods(hass, "Hygge")
    await hass.async_block_till_done()
    await advance(hass, freezer)
    sent = lights.by_entity()
    assert sent[home.lamp] == ("turn_on", {"brightness_pct": 30, "color_temp_kelvin": 3200})  # mood value wins
    # The desk's own "every mood" offset beats Hygge's fixed 30 %: it already is at 42 % / 3200 K.
    assert home.desk not in sent
    desk = next(i for i in ctrl.plan(home.area).items if i.entity_id == home.desk)
    assert desk.data == {"brightness_pct": 42, "color_temp_kelvin": 3200} and desk.reason_code == "already"


# ----------------------------------------------------- competing controllers
async def test_existing_automation_controller_is_not_competed_with(hass: HomeAssistant, home: Home, lights,
                                                                    freezer) -> None:
    assert await async_setup_component(hass, "automation", {"automation": [{
        "id": "adaptive_lamp", "alias": "Adaptive lamp",
        "triggers": [{"trigger": "state", "entity_id": BRIGHT}],
        "actions": [{"action": "light.turn_on", "target": {"entity_id": home.lamp},
                     "data": {"brightness_pct": "{{ states('sensor.global_brightness') | int }}"}}],
    }]})
    await hass.async_block_till_done()
    entry = await setup_entry(hass, options(home))
    await configure_sources(hass, entry)
    ctrl = ctrl_of(entry)
    assert [c.entity_id for c in ctrl.source_controllers()] == ["automation.adaptive_lamp"]
    await occupy(hass, home, freezer)
    await activate(hass, entry, home, freezer, acknowledged=("automation.adaptive_lamp",))
    assert home.lamp in lights.by_entity()  # acknowledged co-controller: the mood itself is applied
    lights.clear()
    await advance(hass, freezer, 61)
    set_sources(hass, 80, 2900)
    await hass.async_block_till_done()
    await advance(hass, freezer)
    lamp_calls = [c for c in lights.calls if home.lamp in lights._ids(c)]
    assert lamp_calls and all(c.context.parent_id for c in lamp_calls)  # only the automation adjusted it
    assert lights.by_entity()[home.desk][1]["brightness_pct"] == 80  # other lights still follow
    item = next(i for i in ctrl.plan(home.area).items if i.entity_id == home.lamp)
    assert item.adaptive_update["code"] == "competing_controller"
    await entry.runtime_data.async_refresh()
    assert "adaptive_controller_conflict" in {p.code for p in entry.runtime_data.data.problems}
    assert hass.states.get("automation.adaptive_lamp").state == "on"  # never disabled by us


async def test_adaptive_lighting_switch_source(hass: HomeAssistant, home: Home, lights, freezer) -> None:
    al = "switch.adaptive_lighting_kno"
    attrs = {"brightness_pct": 55, "color_temp_kelvin": 3000, "manual_control": [],
             "configuration": {"lights": [home.desk]}}
    hass.states.async_set(al, "on", attrs)
    entry = await setup_entry(hass, options(home))
    await configure_sources(hass, entry, brightness_entity=al, color_temp_entity=al)
    ctrl = ctrl_of(entry)
    assert (ctrl.global_values().brightness.value, ctrl.global_values().color_temp.value) == (55, 3000)
    assert ctrl.source_controllers()[0].kind == "adaptive_lighting"
    await occupy(hass, home, freezer)
    await activate(hass, entry, home, freezer)
    lights.clear()
    await advance(hass, freezer, 61)
    hass.states.async_set(al, "on", {**attrs, "brightness_pct": 70})
    await hass.async_block_till_done()
    await advance(hass, freezer)
    assert home.desk not in lights.by_entity() and home.lamp in lights.by_entity()
    # Adaptive Lighting switched off: it no longer adjusts the desk, so Smart Occupancy may.
    lights.clear()
    hass.states.async_set(al, "off", {**attrs, "brightness_pct": 70})
    await hass.async_block_till_done()
    await advance(hass, freezer)
    assert lights.by_entity()[home.desk][1]["brightness_pct"] == 70


# ------------------------------------------------------------ protections
async def test_manual_override_and_observe_mode(hass: HomeAssistant, home: Home, lights, freezer) -> None:
    entry = await ready(hass, home, lights, freezer)
    lights.clear()
    hass.states.async_set(home.lamp, "on", {**CT_ATTRS, "supported_features": 32, "brightness": 30,
                                            "color_mode": "color_temp", "color_temp_kelvin": 2700},
                          context=Context(user_id="person"))
    await hass.async_block_till_done()
    await advance(hass, freezer, 61)
    set_sources(hass, 85, 4000)
    await hass.async_block_till_done()
    await advance(hass, freezer)
    sent = lights.by_entity()
    assert home.lamp not in sent and sent[home.desk][1]["brightness_pct"] == 85
    item = next(i for i in ctrl_of(entry).plan(home.area).items if i.entity_id == home.lamp)
    assert item.adaptive_update["code"] == "manual_override"
    # Observe: previews say "would", nothing is ever sent.
    ctrl = ctrl_of(entry)
    ctrl.store.async_set(ctrl.with_room(home.area, control_enabled=False))
    await hass.async_block_till_done()
    lights.clear()
    await advance(hass, freezer, 61)
    set_sources(hass, 20, 2400)
    await hass.async_block_till_done()
    await advance(hass, freezer, 120)
    assert lights.calls == []
    plan = ctrl.plan(home.area).as_dict()
    desk = next(i for i in plan["items"] if i["entity_id"] == home.desk)
    assert desk["adaptive_update"]["would_send"] is True and plan["control_enabled"] is False
    assert lights.calls == []


# ------------------------------------------------------------- Manual mode
async def test_manual_mode_pauses_all_automatic_lighting(hass: HomeAssistant, home: Home, lights, freezer,
                                                         hass_ws_client) -> None:
    entry = await ready(hass, home, lights, freezer)
    ctrl = ctrl_of(entry)
    lights.clear()
    set_moods(hass, "Manual")
    await hass.async_block_till_done()
    await advance(hass, freezer)
    assert lights.calls == []
    state = lighting_state(hass, home)
    assert state.state == "paused" and state.attributes["control_mode_reason"] == \
        "Manual control — automatic lighting paused"
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": "smart_occupancy/lighting/editor", "area_id": home.area})
    model = (await client.receive_json())["result"]
    assert model["manual_mode"] is True and model["mood"]["detail"] == "Manual control — automatic lighting paused"
    assert "Manual" not in [m["name"] for m in model["moods"]]  # not a mood; needs no profile
    await entry.runtime_data.async_refresh()
    problems = entry.runtime_data.data.problems
    assert "lighting_manual_mode" in {p.code for p in problems}
    assert not [p for p in problems if "Manual" in p.explanation and p.code != "lighting_manual_mode"]
    room_status = next(s for s in hass.states.async_all("sensor")
                       if s.attributes.get("area_id") == home.area and "lighting_paused" in s.attributes)
    assert room_status.attributes["lighting_paused"] is True

    # Occupancy: vacant, then a new confirmed visit -> nothing.
    hass.states.async_set(home.pir, "off")
    hass.states.async_set(home.radar, "off")
    await hass.async_block_till_done()
    await advance(hass, freezer, 900)
    await occupy(hass, home, freezer)
    # Global adjustment updates -> nothing.
    set_sources(hass, 90, 5000)
    await hass.async_block_till_done()
    await advance(hass, freezer, 120)
    # A person changes a light: not corrected, and not recorded as an override for later.
    hass.states.async_set(home.lamp, "on", {**CT_ATTRS, "brightness": 20, "color_mode": "color_temp",
                                            "color_temp_kelvin": 2700}, context=Context(user_id="person"))
    await hass.async_block_till_done()
    await advance(hass, freezer, 70)
    assert ctrl.runtime[home.area].overrides == {}
    # Room mood override and profile edits -> nothing (Manual beats room overrides).
    ctrl.store.async_set(ctrl.with_room(home.area, mood_override="Hygge"))
    await hass.async_block_till_done()
    await advance(hass, freezer)
    ctrl.store.async_set(ctrl.with_room(home.area, mood_override=None))
    await hass.async_block_till_done()
    await advance(hass, freezer)
    plan = ctrl.plan(home.area)
    assert plan.manual_mode and {i.reason_code for i in plan.items} == {"manual_mode"}
    # Reload while in Manual -> nothing.
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    set_sources(hass, 40, 2600)
    await hass.async_block_till_done()
    await advance(hass, freezer, 120)
    assert lights.calls == []

    # Leaving Manual: the newly selected mood is applied with the current values; nothing old is replayed.
    set_moods(hass, "Kvøld")
    await hass.async_block_till_done()
    await advance(hass, freezer)
    sent = lights.by_entity()
    assert sent == {home.lamp: ("turn_on", {"brightness_pct": 20, "color_temp_kelvin": 2300}),
                    home.desk: ("turn_on", {"brightness_pct": 20, "color_temp_kelvin": 2300})}
    assert lighting_state(hass, home).state == "active"


async def test_manual_mode_drops_queued_commands(hass: HomeAssistant, home: Home, lights, freezer) -> None:
    entry = await ready(hass, home, lights, freezer)
    lights.clear()
    await advance(hass, freezer, 61)
    # A mood change and a global update are queued (debounce) ... then Manual starts.
    set_moods(hass, "Hygge")
    set_sources(hass, 85, 4500)
    await hass.async_block_till_done()
    set_moods(hass, "Manual")
    await hass.async_block_till_done()
    await advance(hass, freezer, 120)
    assert lights.calls == []
    assert ctrl_of(entry)._pending == {} and ctrl_of(entry)._adaptive_timers == {}
    # Back to Dagur: Dagur with the current values; Hygge (queued earlier) is never applied.
    set_moods(hass, "Dagur")
    await hass.async_block_till_done()
    await advance(hass, freezer)
    sent = lights.by_entity()
    assert sent[home.lamp] == ("turn_on", {"brightness_pct": 85, "color_temp_kelvin": 4500})
    assert all(service == "turn_on" for service, _ in sent.values())  # Hygge would switch Spottar off


async def test_manual_mode_at_start_up(hass: HomeAssistant, home: Home, lights, freezer, hass_storage) -> None:
    """Restart with Main Mood = Manual and an activated, occupied room: nothing is sent at any point."""
    set_moods(hass, "Manual")
    hass.states.async_set(home.pir, "on")
    hass.states.async_set(home.radar, "on")
    hass_storage["smart_occupancy.lighting"] = {"version": 1, "minor_version": 1, "key": "smart_occupancy.lighting",
                                                "data": {"rooms": {home.area: {"control_enabled": True}},
                                                         "global_profiles": PROFILES,
                                                         "adaptive": {"brightness_entity": BRIGHT,
                                                                      "color_temp_entity": CT}}}
    entry = await setup_entry(hass, options(home))
    await advance(hass, freezer, 200)
    set_sources(hass, 75, 3800)
    await hass.async_block_till_done()
    await advance(hass, freezer, 200)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    await advance(hass, freezer, 200)
    assert lights.calls == []
    assert lighting_state(hass, home).state == "paused"
