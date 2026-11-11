"""Phase 0.3.1: overlapping labels (type / function / RGB tag), Uppljós, RGB variants,
simplified editor model, group choices and concise warnings."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import logging
from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed, async_mock_service

from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar, entity_registry as er, label_registry as lr

from custom_components.smart_occupancy.const import CategoryKind, LightAction
from custom_components.smart_occupancy.lighting_config import LightingConfig
from custom_components.smart_occupancy.models import IntegrationConfig, LightCategory, ProfileSetting, guess_kind
from custom_components.smart_occupancy.profiles import resolve

from .conftest import setup_entry

WS_LOGGER = "homeassistant.components.websocket_api.http.connection"
MOODS = ["Dagur", "Hygge", "Arbeiði", "Nátt", "Veistla"]
CT = {"supported_color_modes": ["color_temp"], "min_color_temp_kelvin": 2200, "max_color_temp_kelvin": 6500}
COLOUR = {"supported_color_modes": ["color_temp", "hs"], "min_color_temp_kelvin": 2200, "max_color_temp_kelvin": 6500}


def test_kind_is_derived_for_categories_created_before_031() -> None:
    assert guess_kind("rgb", "Rgb", "rgb") is CategoryKind.RGB
    assert guess_kind("uppljos", "Uppljós", None) is CategoryKind.FUNCTION
    assert guess_kind("spottar", "Spottar", "spottar") is CategoryKind.SOURCE
    assert LightCategory.from_dict({"key": "decorative", "name": "Decorative lighting"}).kind is CategoryKind.FUNCTION
    assert LightCategory.from_dict({"key": "x", "name": "X", "kind": "function"}).kind is CategoryKind.FUNCTION
    assert LightCategory.from_dict({"key": "x", "name": "X", "kind": "junk"}).kind is CategoryKind.SOURCE


def test_overlapping_targets_merge_from_least_to_most_specific() -> None:
    """Kitchen light = Spottar + Loft ljós + Rgb; Veistla: Spottar off, RGB lights purple 70 %."""
    cfg = LightingConfig(global_profiles={
        "Veistla": {
            "spottar": ProfileSetting(action=LightAction.OFF),
            "loft": ProfileSetting(action=LightAction.ON, brightness_pct=40, color_temp_kelvin=2700),
            "rgb": ProfileSetting(action=LightAction.ON, brightness_pct=70, rgb_color=(150, 50, 255)),
            "uplight+rgb": ProfileSetting(rgb_color=(255, 150, 40)),
        },
    })
    kitchen = resolve(cfg, "Veistla", None, "light.kitchen", ["spottar", "loft", "rgb", "spottar+rgb", "loft+rgb"])
    assert kitchen.action.value == "on" and kitchen.rgb_color == (150, 50, 255) and kitchen.brightness_pct == 70
    assert kitchen.color_temp_kelvin is None
    assert kitchen.sources["rgb_color"]["target"] == "rgb"
    # Pendant with upward RGB: Loft ljós + Uppljós + Rgb -> amber from "Uppljós + RGB", 70 % from RGB.
    pendant = resolve(cfg, "Veistla", None, "light.pendant", ["loft", "uplight", "rgb", "loft+rgb", "uplight+rgb"])
    assert pendant.rgb_color == (255, 150, 40) and pendant.brightness_pct == 70
    # A non-RGB light of the same types keeps the white profile.
    plain = resolve(cfg, "Veistla", None, "light.plain", ["spottar", "loft"])
    assert plain.action.value == "on" and plain.color_temp_kelvin == 2700 and plain.rgb_color is None


@dataclass
class Real:
    area: str
    kitchen: str
    pendant: str
    strip: str
    spot: str
    group: str
    labels: dict[str, str]


@pytest.fixture
async def real(hass: HomeAssistant) -> Real:
    """Kamar Norður with the user's label approach."""
    area = ar.async_get(hass).async_create("Kamar Norður")
    label_reg, ent_reg = lr.async_get(hass), er.async_get(hass)
    labels = {n: label_reg.async_create(n).label_id for n in ("Spottar", "Loft ljós", "LED strips", "Uppljós", "Rgb")}

    def light(object_id: str, names: list[str], attrs: dict[str, Any]) -> str:
        entry = ent_reg.async_get_or_create("light", "test", f"uid_{object_id}", suggested_object_id=object_id)
        ent_reg.async_update_entity(entry.entity_id, area_id=area.id, labels={labels[n] for n in names})
        hass.states.async_set(entry.entity_id, "off", attrs)
        return entry.entity_id

    kitchen = light("kitchen", ["Spottar", "Loft ljós", "Rgb"], COLOUR)
    pendant = light("pendant", ["Loft ljós", "Uppljós", "Rgb"], COLOUR)
    strip = light("strip", ["LED strips"], {"supported_color_modes": ["brightness"]})
    spot = light("spot_1", ["Spottar"], CT)
    group = light("spots_group", ["Spottar"], {**CT, "entity_id": [spot]})
    hass.states.async_set(group, "on", {**CT, "entity_id": [spot]})
    for e in ("binary_sensor.kno_pir", "binary_sensor.kno_radar"):
        hass.states.async_set(e, "off")
    hass.states.async_set("input_select.auto_mood", "Dagur", {"options": MOODS})
    hass.states.async_set("input_select.mood", "Auto", {"options": ["Auto", *MOODS]})
    hass.states.async_set("input_select.main_mood", "Dagur", {"options": MOODS})
    await hass.async_block_till_done()
    return Real(area.id, kitchen, pendant, strip, spot, group, labels)


def real_options(r: Real, categories: list[dict] | None = None) -> dict[str, Any]:
    return {
        "config_version": 2,
        "mood": {"auto_mood_entity": "input_select.auto_mood", "manual_mood_entity": "input_select.mood",
                 "main_mood_entity": "input_select.main_mood", "auto_option": "Auto"},
        # As a 0.3.0 user created them: no kind stored.
        "categories": categories or [
            {"key": "spottar", "name": "Spottar", "label_id": r.labels["Spottar"]},
            {"key": "loft", "name": "Loft ljós", "label_id": r.labels["Loft ljós"]},
            {"key": "led_strips", "name": "LED strips", "label_id": r.labels["LED strips"]},
            {"key": "uppljos", "name": "Uppljós", "label_id": r.labels["Uppljós"]},
            {"key": "rgb", "name": "Rgb", "label_id": r.labels["Rgb"]},
        ],
        "rooms": {r.area: {"enabled": True, "sensors": [
            {"entity_id": "binary_sensor.kno_pir", "kind": "pir", "role": "trigger"},
            {"entity_id": "binary_sensor.kno_radar", "kind": "radar", "role": "maintain"}], "lights": []}},
    }


async def test_categories_from_030_get_kinds_and_targets(hass: HomeAssistant, real: Real) -> None:
    entry = await setup_entry(hass, real_options(real))
    ctrl = entry.runtime_data.lighting
    kinds = {c.key: c.kind for c in IntegrationConfig.from_dict(dict(entry.options)).categories}
    assert kinds == {"spottar": "source", "loft": "source", "led_strips": "source", "uppljos": "function", "rgb": "rgb"}
    index = ctrl.index
    assert index.profile_targets(real.kitchen) == ["loft", "spottar", "rgb", "loft+rgb", "spottar+rgb"]
    assert index.profile_targets(real.pendant) == ["loft", "uppljos", "rgb", "loft+rgb", "uppljos+rgb"]
    assert index.profile_targets(real.strip) == ["led_strips"]  # brightness-only strip, not RGB-tagged
    assert index.category_of(real.kitchen) == "spottar" and index.category_of(real.pendant) == "loft"
    assert index.target_lights("uppljos+rgb") == [real.pendant]
    # Overlapping labels are normal now: no "light in several categories" warning.
    codes = {p.code for p in entry.runtime_data.data.problems}
    assert "light_in_multiple_categories" not in codes


async def test_veistla_runs_with_types_functions_and_rgb(hass: HomeAssistant, real: Real, freezer, hass_ws_client) -> None:
    on = async_mock_service(hass, "light", "turn_on")
    off = async_mock_service(hass, "light", "turn_off")
    entry = await setup_entry(hass, real_options(real))
    ctrl = entry.runtime_data.lighting
    client = await hass_ws_client(hass)

    async def ws(**msg):
        await client.send_json_auto_id(msg)
        reply = await client.receive_json()
        assert reply["success"], reply
        return reply["result"]

    for target, setting in (
        ("spottar", {"action": "off"}),
        ("loft", {"action": "on", "brightness_pct": 40, "color_temp_kelvin": 2700}),
        ("led_strips", {"action": "on", "brightness_pct": 25}),
        ("uppljos+rgb", {"action": "on", "brightness_pct": 30, "rgb_color": [255, 150, 40]}),
        ("loft+rgb", {"action": "on", "rgb_color": [150, 50, 255], "brightness_pct": 70}),
    ):
        await ws(type="smart_occupancy/lighting/profile/set", scope="global", mood="Veistla", target=target, setting=setting)
    # Simplified editor model: one entry per light type, capability-aware.
    model = await ws(type="smart_occupancy/lighting/editor", mood="Veistla")
    types = {t["target"]: t for t in model["types"]}
    assert types["led_strips"]["caps"]["color"] is False  # brightness-only: no colour controls
    assert types["led_strips"]["caps"]["brightness"] is True and types["led_strips"]["caps"]["color_temp"] is False
    assert types["uppljos+rgb"]["caps"]["color"] is True and types["uppljos+rgb"]["name"] == "Uppljós + RGB"
    assert types["uppljos+rgb"]["configured"] and types["uppljos+rgb"]["effective"]["rgb_color"] == [255, 150, 40]
    assert "led_strips+rgb" not in types  # no RGB-tagged strips, nothing configured
    assert next(c for c in model["categories"] if c["key"] == "loft")["rgb_lights"] == 2

    hass.states.async_set("binary_sensor.kno_pir", "on")
    hass.states.async_set("binary_sensor.kno_radar", "on")
    ctrl.store.async_set(ctrl.with_room(real.area, control_enabled=True, mood_override="Veistla"))
    await hass.async_block_till_done()
    freezer.tick(timedelta(seconds=2))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    sent = {}
    for call in [*on, *off]:
        for e in call.data["entity_id"]:
            sent[e] = (call.service, {k: v for k, v in call.data.items() if k != "entity_id"})
    # Kitchen (Spottar + Loft ljós + Rgb): the Loft ljós RGB variant beats Spottar Off.
    assert sent[real.kitchen] == ("turn_on", {"brightness_pct": 70, "rgb_color": [150, 50, 255]})
    # Pendant with upward RGB light: "Uppljós + RGB" amber 30 %.
    assert sent[real.pendant] == ("turn_on", {"brightness_pct": 30, "rgb_color": [255, 150, 40]})
    assert sent[real.strip] == ("turn_on", {"brightness_pct": 25})  # no colour on a plain strip
    assert sent[real.group] == ("turn_off", {}) and real.spot not in sent


async def test_group_choices_and_dismissed_warnings(hass: HomeAssistant, real: Real, hass_ws_client) -> None:
    entry = await setup_entry(hass, real_options(real))
    ctrl = entry.runtime_data.lighting
    client = await hass_ws_client(hass)

    async def ws(**msg):
        await client.send_json_auto_id(msg)
        return await client.receive_json()

    model = (await ws(type="smart_occupancy/lighting/editor", area_id=real.area))["result"]
    issue = model["room"]["group_issues"][0]
    assert issue == {"group": real.group, "members": [real.spot], "members_known": True, "dismissed": False}
    # Manage individually: the group is excluded, its member controlled.
    assert (await ws(type="smart_occupancy/lighting/room/group_choice", area_id=real.area, group=real.group,
                     choice="individual"))["success"]
    managed = ctrl.managed_room(real.area)
    assert managed.excluded == {real.group: "excluded in this room"} and real.spot in managed.controlled
    assert (await ws(type="smart_occupancy/lighting/room/group_choice", area_id=real.area, group=real.group,
                     choice="restore"))["success"]
    # Manage as group: the member is excluded and the question is answered.
    assert (await ws(type="smart_occupancy/lighting/room/group_choice", area_id=real.area, group=real.group,
                     choice="group"))["success"]
    managed = ctrl.managed_room(real.area)
    assert managed.controlled.count(real.group) == 1 and real.spot in managed.excluded
    model = (await ws(type="smart_occupancy/lighting/editor", area_id=real.area))["result"]
    assert model["room"]["group_issues"] == []


async def test_dismissed_opaque_group_warning_becomes_a_note(hass: HomeAssistant, real: Real, hass_ws_client) -> None:
    from homeassistant.helpers import device_registry as dr
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    source = MockConfigEntry(domain="test")
    source.add_to_hass(hass)
    dev = dr.async_get(hass).async_get_or_create(config_entry_id=source.entry_id, identifiers={("z2m", "g")},
                                                manufacturer="Zigbee2MQTT", model="Group", name="Spottar group")
    dr.async_get(hass).async_update_device(dev.id, area_id=real.area)
    z2m = er.async_get(hass).async_get_or_create("light", "mqtt", "z2m_g", suggested_object_id="z2m_spots", device_id=dev.id)
    er.async_get(hass).async_update_entity(z2m.entity_id, labels={real.labels["Spottar"]})
    hass.states.async_set(z2m.entity_id, "off", CT)
    entry = await setup_entry(hass, real_options(real))
    data = entry.runtime_data.data
    warning = next(p for p in data.problems if p.code == "group_overlap_unknown")
    assert warning.severity.value == "warning"
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": "smart_occupancy/lighting/room/group_choice", "area_id": real.area,
                                    "group": z2m.entity_id, "choice": "dismiss"})
    assert (await client.receive_json())["success"]
    await hass.async_block_till_done()
    await entry.runtime_data.async_refresh()
    note = next(p for p in entry.runtime_data.data.problems if p.code == "group_overlap_unknown")
    assert note.severity.value == "info"  # still visible under Advanced, no longer a warning


async def test_category_commands_and_label_suggestions(hass: HomeAssistant, real: Real, hass_ws_client, caplog) -> None:
    caplog.set_level(logging.CRITICAL, logger=WS_LOGGER)
    opts = real_options(real, categories=[{"key": "spottar", "name": "Spottar", "label_id": real.labels["Spottar"]}])
    entry = await setup_entry(hass, opts)
    client = await hass_ws_client(hass)

    async def ws(**msg):
        await client.send_json_auto_id(msg)
        return await client.receive_json()

    model = (await ws(type="smart_occupancy/lighting/editor"))["result"]
    suggested = {s["name"]: s["kind"] for s in model["label_suggestions"]}
    assert suggested == {"Uppljós": "function", "Rgb": "rgb"}
    reply = await ws(type="smart_occupancy/lighting/category/set", name="Uppljós", label_id=real.labels["Uppljós"],
                     kind="function")
    assert reply["success"]
    await hass.async_block_till_done()
    cats = {c["name"]: c for c in entry.options["categories"]}
    assert cats["Uppljós"]["kind"] == "function" and cats["Uppljós"]["label_id"] == real.labels["Uppljós"]
    key = cats["Uppljós"]["key"]
    assert entry.runtime_data.lighting.index.category_of(real.pendant) == key
    assert not (await ws(type="smart_occupancy/lighting/category/set", name="spottar", kind="source"))["success"]
    reply = await ws(type="smart_occupancy/lighting/category/remove", key=key)
    assert reply["success"]
    await hass.async_block_till_done()
    assert key not in {c["key"] for c in entry.options["categories"]}


async def test_short_warning_lines(hass: HomeAssistant, real: Real) -> None:
    hass.states.async_set("input_boolean.trigger", "off")
    from homeassistant.setup import async_setup_component

    assert await async_setup_component(hass, "automation", {"automation": [
        {"id": "a", "alias": "Old kitchen", "triggers": [{"trigger": "state", "entity_id": "input_boolean.trigger"}],
         "actions": [{"action": "light.turn_on", "target": {"entity_id": real.kitchen}}]},
    ]})
    entry = await setup_entry(hass, real_options(real))
    ctrl = entry.runtime_data.lighting
    ctrl.store.async_set(ctrl.with_room(real.area, control_enabled=True))
    await hass.async_block_till_done()
    await entry.runtime_data.async_refresh()
    state = hass.states.get(er.async_get(hass).async_get_entity_id(
        "sensor", "smart_occupancy", f"smart_occupancy_room_{real.area}_state"))
    lighting = state.attributes["health"]["lighting"]
    assert "1 lighting conflict (Old kitchen)" in lighting["reasons"]
    assert all(len(r) < 90 for r in lighting["reasons"])
    assert any("appears to control" in d for d in lighting["details"])  # full text kept for Advanced
