"""Shared fixtures: a small synthetic home built in real HA registries."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import Event, HomeAssistant
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
    label_registry as lr,
)
from homeassistant.const import EVENT_CALL_SERVICE

from custom_components.smart_occupancy.const import DOMAIN

MOODS = ["Morgun", "Dagur", "Kvøld", "Nátt"]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture(autouse=True)
def no_errors_logged(caplog):
    """Fail any test during which an ERROR (or worse) was logged."""
    yield
    errors = [r for r in caplog.get_records("call") + caplog.get_records("teardown")
              if r.levelno >= logging.ERROR]
    assert not errors, [r.getMessage() for r in errors]


@pytest.fixture(autouse=True)
def no_deprecated_usage_reported(caplog):
    """Fail if Home Assistant reports deprecated or wrong API usage by this integration."""
    yield
    reports = [
        r.getMessage() for r in caplog.get_records("call") + caplog.get_records("teardown")
        if "smart_occupancy" in r.getMessage()
        and ("Detected that custom integration" in r.getMessage() or "deprecated" in r.getMessage().lower())
    ]
    assert not reports, reports


@dataclass
class Home:
    pilot_area: str
    other_area: str
    radar: str
    pir: str
    door: str
    window: str
    tamper: str
    group: str
    member_a: str
    member_b: str
    lamp: str
    downlight_label: str
    lamp_label: str


def set_moods(hass: HomeAssistant, auto: str, manual: str, main: str) -> None:
    hass.states.async_set("input_select.auto_mood", auto, {"options": MOODS})
    hass.states.async_set("input_select.mood", manual, {"options": ["Auto", *MOODS]})
    hass.states.async_set("input_select.main_mood", main, {"options": MOODS})


def _entity(hass, ent_reg, domain, uid, *, device_id=None, device_class=None,
            entity_category=None, object_id=None, area_id=None) -> str:
    entry = ent_reg.async_get_or_create(
        domain, "test", uid, device_id=device_id, original_device_class=device_class,
        entity_category=entity_category, suggested_object_id=object_id or uid,
    )
    if area_id:
        ent_reg.async_update_entity(entry.entity_id, area_id=area_id)
    return entry.entity_id


@pytest.fixture
async def home(hass: HomeAssistant) -> Home:
    area_reg = ar.async_get(hass)
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    label_reg = lr.async_get(hass)

    pilot = area_reg.async_create("Kamar Norður")
    other = area_reg.async_create("Stova")
    downlights = label_reg.async_create("Downlights")
    lamps = label_reg.async_create("Lamps")

    source = MockConfigEntry(domain="test")
    source.add_to_hass(hass)

    def device(name: str, area_id: str, labels: set[str] | None = None) -> str:
        d = dev_reg.async_get_or_create(
            config_entry_id=source.entry_id, identifiers={("test", name)}, name=name
        )
        dev_reg.async_update_device(d.id, area_id=area_id, labels=labels or set())
        return d.id

    radar_dev = device("radar", pilot.id)
    pir_dev = device("pir", pilot.id)
    door_dev = device("door", pilot.id)
    lamp_dev = device("lamp", other.id, {lamps.label_id})

    radar = _entity(hass, ent_reg, "binary_sensor", "radar_occ", device_id=radar_dev, device_class="occupancy")
    pir = _entity(hass, ent_reg, "binary_sensor", "pir_motion", device_id=pir_dev, device_class="motion")
    tamper = _entity(hass, ent_reg, "binary_sensor", "pir_tamper", device_id=pir_dev,
                     device_class="tamper", entity_category=er.EntityCategory.DIAGNOSTIC)
    door = _entity(hass, ent_reg, "binary_sensor", "door_contact", device_id=door_dev, device_class="door")
    window = _entity(hass, ent_reg, "binary_sensor", "window_contact", device_id=door_dev, device_class="window")
    group = _entity(hass, ent_reg, "light", "spots_group", area_id=pilot.id)
    member_a = _entity(hass, ent_reg, "light", "spot_a", area_id=pilot.id)
    member_b = _entity(hass, ent_reg, "light", "spot_b", area_id=pilot.id)
    lamp = _entity(hass, ent_reg, "light", "floor_lamp", device_id=lamp_dev)
    ent_reg.async_update_entity(group, labels={downlights.label_id})

    for e in (radar, pir, door, window, tamper):
        hass.states.async_set(e, "off")
    color = {"supported_color_modes": ["color_temp", "xy"], "effect_list": ["candle"]}
    hass.states.async_set(group, "off", {"entity_id": [member_a, member_b], **color})
    hass.states.async_set(member_a, "off", color)
    hass.states.async_set(member_b, "off", color)
    hass.states.async_set(lamp, "off", {"supported_color_modes": ["color_temp"]})
    set_moods(hass, "Dagur", "Auto", "Dagur")
    await hass.async_block_till_done()

    return Home(
        pilot_area=pilot.id, other_area=other.id, radar=radar, pir=pir, door=door,
        window=window, tamper=tamper, group=group, member_a=member_a, member_b=member_b,
        lamp=lamp, downlight_label=downlights.label_id, lamp_label=lamps.label_id,
    )


def base_options(home: Home, **room_overrides: Any) -> dict[str, Any]:
    room = {
        "enabled": True,
        "sensors": [
            {"entity_id": home.radar, "kind": "radar", "role": "maintain"},
            {"entity_id": home.pir, "kind": "pir", "role": "trigger"},
            {"entity_id": home.door, "kind": "door", "role": "trigger"},
        ],
        "lights": [home.group],
        **room_overrides,
    }
    return {
        "config_version": 1,
        "operating_mode": "observe",
        "mood": {
            "auto_mood_entity": "input_select.auto_mood",
            "manual_mood_entity": "input_select.mood",
            "main_mood_entity": "input_select.main_mood",
            "auto_option": "Auto",
            "mismatch_grace_seconds": 30,
        },
        "categories": [
            {"key": "downlights", "name": "Downlights", "label_id": home.downlight_label},
            {"key": "lamps", "name": "Lamps", "label_id": home.lamp_label},
            {"key": "led_strips", "name": "LED strips", "label_id": None},
        ],
        "rooms": {home.pilot_area: room},
        "global_profiles": {},
    }


@pytest.fixture
def service_calls(hass: HomeAssistant) -> list[Event]:
    """Every service call made while the test runs (should stay empty)."""
    calls: list[Event] = []
    hass.bus.async_listen(EVENT_CALL_SERVICE, calls.append)
    return calls


async def setup_entry(hass: HomeAssistant, options: dict[str, Any]) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, title="Smart Occupancy & Lighting", data={}, options=options, version=1)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


# --------------------------------------------------------------------- pilot
@dataclass
class Pilot:
    """Kamar Norður with the entity ids of the live installation, plus a hallway."""

    area: str
    hall: str
    pir: str
    radar: str
    door: str
    pc: str
    hall_pir: str
    light: str
    helper: str


PILOT_HELPER = "input_boolean.tilstadar_kamar_nord"


@pytest.fixture
async def pilot(hass: HomeAssistant) -> Pilot:
    area_reg = ar.async_get(hass)
    ent_reg = er.async_get(hass)
    label_reg = lr.async_get(hass)
    room = area_reg.async_create("Kamar Norður")
    hall = area_reg.async_create("Gongd")
    spots = label_reg.async_create("Downlights")

    def make(domain: str, object_id: str, area_id: str, device_class: str | None = None, name: str | None = None):
        entry = ent_reg.async_get_or_create(
            domain, "test", f"uid_{object_id}", suggested_object_id=object_id,
            original_device_class=device_class, original_name=name,
        )
        ent_reg.async_update_entity(entry.entity_id, area_id=area_id)
        return entry.entity_id

    # The Aqara PIR reports device class "occupancy" (like the radar).
    pir = make("binary_sensor", "pir_kan_occupancy", room.id, "occupancy", "PIR Kamar Norður")
    radar = make("binary_sensor", "rdr_kno_occupancy", room.id, "occupancy", "Radar Kamar Norður")
    door = make("binary_sensor", "swh_kno_contact", room.id, "door", "Door Kamar Norður")
    pc = make("binary_sensor", "nobara_pc_active", room.id, None, "Nobara PC active")
    hall_pir = make("binary_sensor", "pir_gongd_occupancy", hall.id, "motion", "PIR Gongd")
    light = make("light", "spt_kno_group", room.id, None, "Spots Kamar Norður")
    ent_reg.async_update_entity(light, labels={spots.label_id})

    names = {pir: "PIR Kamar Norður", radar: "Radar Kamar Norður", door: "Door Kamar Norður",
             pc: "Nobara PC active", hall_pir: "PIR Gongd"}
    for entity_id, name in names.items():
        hass.states.async_set(entity_id, "off", {"friendly_name": name})
    hass.states.async_set(light, "off", {"friendly_name": "Spots Kamar Norður", "supported_color_modes": ["color_temp"]})
    hass.states.async_set(PILOT_HELPER, "off", {"friendly_name": "Tilstaðar Kamar Norð"})
    set_moods(hass, "Dagur", "Auto", "Dagur")
    await hass.async_block_till_done()
    return Pilot(room.id, hall.id, pir, radar, door, pc, hall_pir, light, PILOT_HELPER)


def pilot_options(p: Pilot, *, hall: bool = True, timing: dict[str, int] | None = None) -> dict[str, Any]:
    """Phase 0.1.1-shaped options (config_version 1) for the pilot, as the live entry stores them."""
    rooms: dict[str, Any] = {
        p.area: {
            "enabled": True,
            "sensors": [
                {"entity_id": p.pir, "kind": "pir", "role": "trigger"},
                {"entity_id": p.radar, "kind": "radar", "role": "maintain"},
                {"entity_id": p.pc, "kind": "pc_activity", "role": "maintain"},
                {"entity_id": p.door, "kind": "door", "role": "trigger"},
            ],
            "lights": [p.light],
            "reference_occupancy_entity": p.helper,
            "mood_overrides": {},
            "light_overrides": {},
        }
    }
    if hall:
        rooms[p.hall] = {
            "enabled": True,
            "sensors": [{"entity_id": p.hall_pir, "kind": "pir", "role": "trigger"}],
            "lights": [],
            "reference_occupancy_entity": None,
        }
    options: dict[str, Any] = {
        "config_version": 1,
        "operating_mode": "observe",
        "mood": {
            "auto_mood_entity": "input_select.auto_mood",
            "manual_mood_entity": "input_select.mood",
            "main_mood_entity": "input_select.main_mood",
            "auto_option": "Auto",
            "mismatch_grace_seconds": 30,
        },
        "categories": [{"key": "downlights", "name": "Downlights", "label_id": "downlights"}],
        "rooms": rooms,
        "global_profiles": {},
    }
    if timing:
        options["occupancy"] = timing
    return options
