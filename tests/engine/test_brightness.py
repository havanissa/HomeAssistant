"""Phase 4.7 (0.6.7): ambient light levels and multi-zone brightness rules.

A synthetic Kamar Norður: PIR + radar for the room, a desk and a seating
presence sensor for two zones, a tunable-white floor lamp, an RGB strip, a
brightness-only dimmer, an on/off-only lamp and a relay output, an indoor
lux sensor that the floor lamp itself lights (natural light + lamp), and an
outdoor sensor. Every device is simulated like Zigbee2MQTT / Wi-Fi lights: a
command is accepted, then the device reports with a context of its own,
optionally late. Nothing here talks to real hardware.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest

from homeassistant.core import Context, Event, HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import area_registry as ar, entity_registry as er, label_registry as lr
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
from homeassistant.util import dt as dt_util

from custom_components.smart_occupancy import ambient as amb, brightness as br
from custom_components.smart_occupancy.ambient import AmbientSettings, AmbientTracker, RoomAmbient
from custom_components.smart_occupancy.brightness import ColumnClaim, LightMatrix
from custom_components.smart_occupancy.const import AdjustMode, LightAction
from custom_components.smart_occupancy.daylight import DaylightConfig, Estimate, IndoorSensor, RoomDaylight
from custom_components.smart_occupancy.lighting import Capabilities
from custom_components.smart_occupancy.lighting_config import LightingConfig, migrate_store, rename_entity
from custom_components.smart_occupancy.planner import build_command
from custom_components.smart_occupancy.profiles import ResolvedSetting
from custom_components.smart_occupancy.targets import RELAY_CAPABILITIES
from custom_components.smart_occupancy.zones import ZoneConfig, ZoneTarget

from .conftest import setup_entry
from .test_daylight_runtime import DAY, advance

MAIN = "input_select.main_mood"
MOODS = ["Hygge", "Arbeiði", "Dagur"]
CT = {"supported_color_modes": ["color_temp"], "min_color_temp_kelvin": 2200, "max_color_temp_kelvin": 6500,
      "supported_features": 32}
RGB = {"supported_color_modes": ["rgb"], "supported_features": 32}
DIM = {"supported_color_modes": ["brightness"], "supported_features": 32}
ONOFF = {"supported_color_modes": ["onoff"]}
FLOOR_LUX = 127  # lux the floor lamp adds to the indoor sensor at 100 % (15 % -> +19 lx)
LUX = "sensor.kno_lux"
OUTDOOR = "sensor.outdoor_lux"


# =================================================================== simulated devices
class Devices:
    """light.* / switch.* services that report back like Zigbee2MQTT devices (their own context)."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.calls: list[ServiceCall] = []
        self.delay: dict[str, float] = {}
        self.fail: set[str] = set()
        for domain in ("light", "switch"):
            for service in ("turn_on", "turn_off"):
                hass.services.async_register(domain, service, self._handle)

    @staticmethod
    def ids(call: ServiceCall) -> list[str]:
        ids = call.data["entity_id"]
        return [ids] if isinstance(ids, str) else list(ids)

    async def _handle(self, call: ServiceCall) -> None:
        self.calls.append(call)
        ids = self.ids(call)
        if failing := [e for e in ids if e in self.fail]:
            raise HomeAssistantError(f"No response from {failing[0]}")
        data = {k: v for k, v in call.data.items() if k != "entity_id"}
        for e in ids:
            if (seconds := self.delay.get(e)) is not None:
                async_call_later(self.hass, seconds, callback(
                    lambda _now, e=e, s=call.service, d=data: self.report(e, s == "turn_on", d)))
            else:
                self.report(e, call.service == "turn_on", data)

    def report(self, entity_id: str, on: bool, data: dict[str, Any] | None = None,
               context: Context | None = None) -> None:
        data = data or {}
        old = self.hass.states.get(entity_id)
        attrs = dict(old.attributes) if old else {}
        modes = set(attrs.get("supported_color_modes") or [])
        if entity_id.startswith("light."):
            if on:
                if modes - {"onoff"}:
                    if "brightness_pct" in data:
                        attrs["brightness"] = round(data["brightness_pct"] * 255 / 100)
                    attrs.setdefault("brightness", 255)
                if "color_temp_kelvin" in data and "color_temp" in modes:
                    attrs |= {"color_mode": "color_temp", "color_temp_kelvin": data["color_temp_kelvin"]}
                    attrs.pop("rgb_color", None)
                elif "rgb_color" in data and "rgb" in modes:
                    attrs |= {"color_mode": "rgb", "rgb_color": list(data["rgb_color"])}
                    attrs.pop("color_temp_kelvin", None)
                elif "color_mode" not in attrs:
                    attrs["color_mode"] = next(iter(sorted(modes)), "onoff")
            else:
                for k in ("brightness", "color_mode", "color_temp_kelvin", "rgb_color"):
                    attrs.pop(k, None)
        self.hass.states.async_set(entity_id, "on" if on else "off", attrs, context=context or Context())

    def of(self, entity_id: str) -> list[tuple[str, dict[str, Any]]]:
        return [(f"{c.domain}.{c.service}", {k: v for k, v in c.data.items() if k != "entity_id"})
                for c in self.calls if entity_id in self.ids(c)]

    def last(self, entity_id: str) -> tuple[str, dict[str, Any]] | None:
        calls = self.of(entity_id)
        return calls[-1] if calls else None

    def clear(self) -> None:
        self.calls.clear()


class Home:
    area: str
    stova: str
    pir: str
    radar: str
    desk: str
    seat: str
    floor: str
    strip: str
    dimmer: str
    onoff: str
    relay: str
    stova_pir: str
    shared: str
    labels: dict[str, str]


class LuxSim:
    """The indoor sensor sees natural light plus the floor lamp's own light (the feedback loop)."""

    def __init__(self, hass: HomeAssistant, h: Home) -> None:
        self.hass = hass
        self.h = h
        self.natural = 0.0
        self.lamp = True       # the floor lamp reaches the sensor
        self._jitter = 0.0
        self.unsub = async_track_state_change_event(hass, [h.floor], self._on_lamp)

    @callback
    def _on_lamp(self, _event: Event) -> None:
        self.report()

    def value(self) -> float:
        state = self.hass.states.get(self.h.floor)
        added = 0.0
        if self.lamp and state is not None and state.state == "on":
            added = FLOOR_LUX * (state.attributes.get("brightness") or 255) / 255
        return self.natural + added

    def report(self) -> None:
        # A real sensor's reading jitters a little: every report is a new state (a new measurement).
        self._jitter = 0.1 if self._jitter == 0 else 0.0
        self.hass.states.async_set(LUX, f"{self.value() + self._jitter:.1f}",
                                   {"unit_of_measurement": "lx", "device_class": "illuminance",
                                    "friendly_name": "Kamar lux"})


@pytest.fixture
def devices(hass: HomeAssistant) -> Devices:
    return Devices(hass)


@pytest.fixture
async def home(hass: HomeAssistant) -> Home:
    area_reg, ent_reg, label_reg = ar.async_get(hass), er.async_get(hass), lr.async_get(hass)
    h = Home()
    h.area = area_reg.async_create("Kamar Norður").id
    h.stova = area_reg.async_create("Stova").id
    h.labels = {k: label_reg.async_create(n).label_id for k, n in (("floor", "Uppljós"), ("strip", "LED strips"),
                                                                    ("lamps", "Lamps"))}

    def make(domain: str, object_id: str, area_id: str | None, label: str | None = None, dc: str | None = None) -> str:
        entry = ent_reg.async_get_or_create(domain, "test", f"uid_{object_id}", suggested_object_id=object_id,
                                            original_device_class=dc)
        ent_reg.async_update_entity(entry.entity_id, area_id=area_id, labels={h.labels[label]} if label else set())
        return entry.entity_id

    h.pir = make("binary_sensor", "kno_pir", h.area, dc="motion")
    h.radar = make("binary_sensor", "kno_radar", h.area, dc="occupancy")
    h.desk = make("binary_sensor", "kno_desk", h.area, dc="occupancy")
    h.seat = make("binary_sensor", "kno_seat", h.area, dc="occupancy")
    h.floor = make("light", "kno_floor", h.area, "floor")
    h.strip = make("light", "kno_strip", h.area, "strip")
    h.dimmer = make("light", "kno_dimmer", h.area, "lamps")
    h.onoff = make("light", "kno_onoff", h.area, "lamps")
    h.relay = make("switch", "kno_relay", h.area)
    h.stova_pir = make("binary_sensor", "stova_pir", h.stova, dc="motion")
    h.shared = make("light", "stova_pendant", h.stova, "lamps")
    make("sensor", "kno_lux", h.area, dc="illuminance")
    make("sensor", "outdoor_lux", None, dc="illuminance")
    for e in (h.pir, h.radar, h.desk, h.seat, h.stova_pir):
        hass.states.async_set(e, "off")
    hass.states.async_set(h.floor, "off", CT)
    hass.states.async_set(h.strip, "off", RGB)
    hass.states.async_set(h.dimmer, "off", DIM)
    hass.states.async_set(h.onoff, "off", ONOFF)
    hass.states.async_set(h.shared, "off", CT)
    hass.states.async_set(h.relay, "off")
    hass.states.async_set(MAIN, "Hygge", {"options": MOODS})
    hass.states.async_set("input_select.auto_mood", "Hygge", {"options": MOODS})
    hass.states.async_set("input_select.mood", "Auto", {"options": ["Auto", *MOODS]})
    await hass.async_block_till_done()
    return h


TIMING = {"correlation_window": 10, "exit_confirmation": 30, "inactivity_timeout": 60, "startup_hold": 0}
PROFILES = {
    "Hygge": {"floor": {"action": "on", "brightness_pct": 60, "color_temp_kelvin": 2700},
              "strip": {"action": "on", "brightness_pct": 50, "rgb_color": [255, 120, 0]},
              "lamps": {"action": "on", "brightness_pct": 40}},
    "Arbeiði": {"floor": {"action": "on", "brightness_pct": 90, "color_temp_kelvin": 4000},
                "strip": {"action": "on", "brightness_pct": 80, "rgb_color": [0, 0, 255]},
                "lamps": {"action": "on", "brightness_pct": 80}},
    "Dagur": {"floor": {"action": "on", "brightness_pct": 70, "color_temp_kelvin": 3500}},
}


def options(h: Home) -> dict[str, Any]:
    return {
        "config_version": 2, "operating_mode": "observe",
        "mood": {"auto_mood_entity": "input_select.auto_mood", "manual_mood_entity": "input_select.mood",
                 "main_mood_entity": MAIN, "auto_option": "Auto", "mismatch_grace_seconds": 30},
        "categories": [{"key": "floor", "name": "Uppljós", "label_id": h.labels["floor"]},
                       {"key": "strip", "name": "LED strips", "label_id": h.labels["strip"]},
                       {"key": "lamps", "name": "Lamps", "label_id": h.labels["lamps"]}],
        "rooms": {
            h.area: {"enabled": True, "timing": TIMING, "lights": [],
                     "sensors": [{"entity_id": h.pir, "kind": "pir", "role": "trigger"},
                                 {"entity_id": h.radar, "kind": "radar", "role": "maintain"},
                                 {"entity_id": h.desk, "kind": "radar", "role": "maintain"},
                                 {"entity_id": h.seat, "kind": "radar", "role": "maintain"}]},
            h.stova: {"enabled": True, "timing": TIMING, "lights": [h.shared],
                      "sensors": [{"entity_id": h.stova_pir, "kind": "pir", "role": "trigger"}]},
        },
        "global_profiles": PROFILES,
        "occupancy": TIMING,
    }


ZONE_TIMING = {"inactivity_timeout": 20, "exit_confirmation": 10}
DESK = ZoneConfig(id="desk", name="Desk", priority=80, timing=ZONE_TIMING, holds_room=False,
                  targets=(ZoneTarget("light.kno_floor", brightness_pct=100),))
SEAT = ZoneConfig(id="seating", name="Seating", priority=40, timing=ZONE_TIMING, holds_room=False,
                  targets=(ZoneTarget("light.kno_floor", brightness_pct=30),))
FLOOR = LightMatrix("light.kno_floor", room={"none": 15, "low": 15, "bright": 0},
                    zones={"desk": {"none": 65, "low": 45, "bright": 80}})
FACTOR = 0.01  # natural light = outdoor x 0.01: 1200 lx outside -> 12 lx (Low light)


def with_room(ctrl, area: str, **changes: Any) -> None:
    ctrl.store.async_set(ctrl.with_room(area, **changes))


async def wait(hass: HomeAssistant, freezer, seconds: float, sim: LuxSim | None = None) -> None:
    """Time passes in steps of at most 2 s; the indoor sensor reports every 20 s."""
    step = 0.0
    while seconds > 0:
        dt = min(seconds, 2)
        await advance(hass, freezer, dt)
        seconds -= dt
        step += dt
        if sim is not None and step >= 20:
            step = 0
            sim.report()
            await hass.async_block_till_done()


async def outdoor(hass: HomeAssistant, freezer, lux: float, settle: float = 0) -> None:
    hass.states.async_set(OUTDOOR, str(lux), {"unit_of_measurement": "lx", "device_class": "illuminance"})
    await hass.async_block_till_done()
    await wait(hass, freezer, settle)


async def start(hass: HomeAssistant, h: Home, freezer, devices: Devices, *, outdoor_lux: float | None = 1200,
                indoor: bool = False, zones=(DESK,), matrices: dict[str, LightMatrix] | None = None,
                ambient: RoomAmbient | None = None, active: bool = True, natural: float = 0.0, **room: Any):
    freezer.move_to(DAY)
    sim = LuxSim(hass, h)
    sim.natural = natural
    if indoor:
        sim.report()
    if outdoor_lux is not None:
        hass.states.async_set(OUTDOOR, str(outdoor_lux), {"unit_of_measurement": "lx", "device_class": "illuminance"})
    await advance(hass, freezer, 120)
    entry = await setup_entry(hass, options(h))
    ctrl = entry.runtime_data.lighting
    ctrl.store.async_set(replace(ctrl.lighting, daylight=DaylightConfig(
        outdoor_entity=OUTDOOR if outdoor_lux is not None else None, outdoor_window=0)))
    daylight = RoomDaylight(factor=FACTOR if outdoor_lux is not None else None,
                            sensors=(IndoorSensor(LUX),) if indoor else ())
    with_room(ctrl, h.area, control_enabled=active, daylight=daylight, zones=tuple(replace(z, sensors=(
        h.desk,) if z.id == "desk" else (h.seat,)) for z in zones),
        ambient=ambient or RoomAmbient(enabled=True, settings=AmbientSettings(stable_seconds=60)),
        brightness=matrices if matrices is not None else {h.floor: FLOOR}, **room)
    with_room(ctrl, h.stova, control_enabled=active)
    await hass.async_block_till_done()
    await wait(hass, freezer, 4, sim)
    devices.clear()
    return entry, ctrl, sim


def pct(hass: HomeAssistant, entity_id: str) -> int | None:
    state = hass.states.get(entity_id)
    if state is None or state.state != "on":
        return None
    return round((state.attributes.get("brightness") or 255) / 255 * 100)


def kelvin(hass: HomeAssistant, entity_id: str) -> int | None:
    state = hass.states.get(entity_id)
    return state.attributes.get("color_temp_kelvin") if state else None


async def occupy(hass: HomeAssistant, h: Home, freezer, sim: LuxSim | None = None) -> None:
    hass.states.async_set(h.pir, "on")
    hass.states.async_set(h.radar, "on")
    await hass.async_block_till_done()
    await wait(hass, freezer, 14, sim)
    hass.states.async_set(h.pir, "off")
    await wait(hass, freezer, 2, sim)


async def sensor(hass: HomeAssistant, freezer, entity_id: str, value: str, settle: float = 4,
                 sim: LuxSim | None = None) -> None:
    hass.states.async_set(entity_id, value)
    await hass.async_block_till_done()
    await wait(hass, freezer, settle, sim)


def level(ctrl, area: str) -> str:
    return ctrl.ambient.status(area).key


def entries(ctrl, area: str) -> list[dict[str, Any]]:
    return ctrl.decisions.entries(area)


# =================================================================== 1-8: ambient levels (pure)
def _est(lux: float | None, *, measured: bool = False, bound: bool = False, confidence: str | None = "high"):
    return Estimate(lux, confidence if lux is not None else None, "x", f"Natural light: {lux} lx",
                    measured=measured or bound, upper_bound=bound)


def test_default_levels_room_overrides_and_exact_boundaries() -> None:
    glob = AmbientSettings.from_dict({}, defaults=True)
    assert glob.thresholds == (5, 20) and glob.fallback == amb.AmbientFallback.HOLD
    config = LightingConfig.from_dict({"rooms": {"kno": {"ambient": {"enabled": True, "thresholds": [8, 30]}},
                                                 "wc": {}}})
    assert config.ambient_for("kno").thresholds == (8, 30) and config.ambient_for("wc").thresholds == (5, 20)
    assert config.ambient.sources(config.room("kno").ambient.settings)["thresholds"] == "room"
    # A reading equal to a threshold belongs to the higher category.
    t = (5, 20)
    assert [amb.raw_level(v, t) for v in (0, 4.99, 5, 5.01, 19.99, 20, 20.01, 5000)] == [0, 0, 1, 1, 1, 2, 2, 2]
    assert amb.LEVEL_KEYS[amb.raw_level(5, t)] == "low" and amb.LEVEL_KEYS[amb.raw_level(20, t)] == "bright"
    # Invalid thresholds are refused (not increasing, wrong count, zero).
    for bad in ([20, 5], [5, 5], [5], [0, 20], [5, 20, 40]):
        assert amb.valid_thresholds(bad) is None
    assert AmbientSettings.from_dict({"thresholds": [20, 5]}).thresholds is None  # (then: the global default)


def test_hysteresis_and_minimum_dwell() -> None:
    t, h = (5, 20), 2
    assert amb.hysteretic_level(19, t, h, 2) == 2       # just below Bright: stays Bright
    assert amb.hysteretic_level(18, t, h, 2) == 2       # at threshold - hysteresis: still Bright
    assert amb.hysteretic_level(17.9, t, h, 2) == 1     # below it: Low light
    assert amb.hysteretic_level(20, t, h, 1) == 2       # up at the threshold itself
    assert amb.hysteretic_level(3.5, t, h, 1) == 1 and amb.hysteretic_level(2.9, t, h, 1) == 0
    tracker = AmbientTracker()
    settings = AmbientSettings.from_dict({"stable_seconds": 120}, defaults=True)
    now = dt_util.utcnow()
    amb.classify(tracker, _est(12, measured=True), settings, now)
    assert tracker.committed == 1  # the first evaluation commits at once
    _ev, old = amb.classify(tracker, _est(25, measured=True), settings, now + timedelta(seconds=30))
    assert old is None and tracker.candidate == 2
    _ev, old = amb.classify(tracker, _est(12, measured=True), settings, now + timedelta(seconds=60))
    assert tracker.candidate is None and tracker.committed == 1  # a passing cloud: back before the dwell ended
    amb.classify(tracker, _est(25, measured=True), settings, now + timedelta(seconds=90))
    _ev, old = amb.classify(tracker, _est(26, measured=True), settings, now + timedelta(seconds=209))
    assert old is None
    _ev, old = amb.classify(tracker, _est(26, measured=True), settings, now + timedelta(seconds=210))
    assert old == 1 and tracker.committed == 2


def test_lamp_lit_readings_never_raise_the_category() -> None:
    """An "at most" reading (lamps on) proves a lower category, never a higher one."""
    t, settings = (5, 20), AmbientSettings.from_dict({"stable_seconds": 0}, defaults=True)
    ev = amb.evidence(_est(23, bound=True), t, 2, 0)
    assert ev.level == 0 and ev.held  # 23 lx with the lamp on: still No light (held)
    ev = amb.evidence(_est(23, bound=True), t, 2, None)
    assert ev.level is None and ev.held  # nothing known before: not proven, the fallback decides
    ev = amb.evidence(_est(2, bound=True), t, 2, 2)
    assert ev.level == 0 and not ev.held  # at most 2 lx (below 5 - 2 lx hysteresis): proven darker
    assert amb.evidence(_est(3, bound=True), t, 2, 2).level == 1  # (3 lx is still within the hysteresis)
    tracker = AmbientTracker(committed=0)
    now = dt_util.utcnow()
    for i in range(10):
        amb.classify(tracker, _est(23 + i, bound=True), settings, now + timedelta(minutes=i))
    assert tracker.committed == 0
    st = amb.status(tracker, amb.evidence(_est(23, bound=True), t, 2, 0), settings, enabled=True)
    assert st.basis == "held" and st.note.startswith("Ambient sensor affected by room lighting. Holding No light")


def test_stale_unavailable_and_invalid_evidence_hold_or_fall_back() -> None:
    now = dt_util.utcnow()
    hold = AmbientSettings.from_dict({"stable_seconds": 0}, defaults=True)
    tracker = AmbientTracker()
    ev, _ = amb.classify(tracker, _est(None), hold, now)
    st = amb.status(tracker, ev, hold, enabled=True)
    assert tracker.committed is None and st.key == "low" and st.basis == "fallback"  # never known: fallback level
    amb.classify(tracker, _est(30, measured=True), hold, now)
    ev, _ = amb.classify(tracker, _est(None), hold, now + timedelta(minutes=5))
    st = amb.status(tracker, ev, hold, enabled=True)
    assert st.key == "bright" and st.basis == "held" and "Holding Bright" in st.note
    ev, _ = amb.classify(tracker, _est(12, confidence="low"), hold, now + timedelta(minutes=6))
    assert tracker.committed == 2  # a low-confidence estimate is not evidence
    fallback = AmbientSettings.from_dict({"stable_seconds": 0, "fallback": "level", "fallback_level": "none"},
                                         defaults=True)
    ev, old = amb.classify(tracker, _est(None), fallback, now + timedelta(minutes=7))
    assert tracker.committed == 0 and old == 2
    assert amb.status(tracker, ev, fallback, enabled=True).basis == "fallback"


# =================================================================== 9-18: matrix decisions (pure)
def test_matrix_columns_priority_tie_break_and_inheritance() -> None:
    m = LightMatrix("light.f", room={"none": 15, "low": 15, "bright": 0},
                    zones={"desk": {"none": 65, "low": 45, "bright": 80}, "seat": {"none": 30, "low": 20,
                                                                                  "bright": None}})
    desk, seat = ColumnClaim("desk", "Desk", 80, 0), ColumnClaim("seat", "Seating", 40, 1)
    assert br.decide(m, "low", []).value == 15 and br.decide(m, "low", []).column == "room"
    d = br.decide(m, "low", [desk, seat])
    assert (d.column, d.value) == ("desk", 45)
    assert d.explanation == "Desk zone wins over Seating zone due to higher priority"
    assert d.superseded == ["Seating zone", "Room occupied"]
    tie = br.decide(m, "low", [ColumnClaim("seat", "Seating", 80, 0), ColumnClaim("desk", "Desk", 80, 1)])
    assert tie.column == "seat" and "same priority; listed first" in tie.explanation
    # Seating has no value for Bright: it does not claim the brightness; the Room occupied value applies.
    d = br.decide(m, "bright", [seat])
    assert (d.column, d.value, d.off) == ("room", 0, True)
    assert br.decide(m, "bright", [seat, desk]).value == 80  # (the list order comes from the arbitration)
    # Not configured anywhere: the light keeps its previous rules.
    empty = LightMatrix("light.f", room={"none": None, "low": None, "bright": None})
    assert br.decide(empty, "low", []) is None
    assert br.decide(replace(m, enabled=False), "low", []) is None


def test_off_cells_and_mood_appearance_are_merged() -> None:
    m = LightMatrix("light.f", room={"none": 15, "low": 15, "bright": 0})
    hygge = ResolvedSetting(action=LightAction.ON, brightness_pct=60, color_temp_kelvin=2700,
                            color_temp_mode=AdjustMode.FIXED, transition=1)
    on = br.merged_setting(br.decide(m, "low", []), hygge, transition=2)
    assert (on.action, on.brightness_pct, on.color_temp_kelvin, on.transition) == (LightAction.ON, 15, 2700, 2)
    off = br.merged_setting(br.decide(m, "bright", []), hygge, transition=2)
    assert off.action == LightAction.OFF and off.brightness_pct is None  # 0 % is Off, not turn_on at 0 %
    service, data, _req, _notes = build_command(off, Capabilities(known=True, brightness=True, transition=True))
    assert (service, data) == ("turn_off", {"transition": 2})
    # The global brightness adjustment is not used for an enrolled light; its global colour temperature is.
    follow = ResolvedSetting(action=LightAction.ON, brightness_pct=70, brightness_mode=AdjustMode.GLOBAL,
                             color_temp_kelvin=3100, color_temp_mode=AdjustMode.GLOBAL,
                             adaptive={"brightness": {"mode": "global", "status": "applied", "value": 70},
                                       "color_temp": {"mode": "global", "status": "applied", "value": 3100}})
    merged = br.merged_setting(br.decide(m, "none", []), follow, transition=None)
    assert merged.brightness_pct == 15 and merged.color_temp_kelvin == 3100
    assert merged.adaptive["brightness"]["mode"] == "fixed" and merged.adaptive["color_temp"]["mode"] == "global"
    assert br.describe_appearance(hygge, "Hygge") == "colour from Hygge (2700 K)"


@pytest.mark.parametrize(("caps", "expected", "note"), [
    (Capabilities(known=True, brightness=True, color_temp=True, min_kelvin=2200, max_kelvin=6500, transition=True),
     {"brightness_pct": 45, "color_temp_kelvin": 2700, "transition": 2}, None),
    (Capabilities(known=True, brightness=True, color=True, transition=True),
     {"brightness_pct": 45, "rgb_color": [255, 120, 0], "transition": 2}, None),
    (Capabilities(known=True, brightness=True, transition=True), {"brightness_pct": 45, "transition": 2},
     "color_temp_kelvin"),  # a brightness-only strip: no colour is invented
    (Capabilities(known=True), {}, "brightness_pct"),
    (RELAY_CAPABILITIES, {}, "brightness_pct"),
])
def test_commands_respect_light_capabilities(caps, expected, note) -> None:
    rgb = ResolvedSetting(action=LightAction.ON, brightness_pct=60, rgb_color=(255, 120, 0))
    ct = ResolvedSetting(action=LightAction.ON, brightness_pct=60, color_temp_kelvin=2700,
                         color_temp_mode=AdjustMode.FIXED)
    mood = rgb if caps.color else ct
    m = LightMatrix("light.f", room={"none": 45, "low": 45, "bright": 45})
    service, data, _req, notes = build_command(br.merged_setting(br.decide(m, "low", []), mood, transition=2), caps)
    assert service == "turn_on"
    assert {k: v for k, v in data.items()} == expected
    if note:
        assert any(n["field"] == note for n in notes)


def test_store_migration_rename_and_removed_zones() -> None:
    old = {"rooms": {"kno": {"control_enabled": True, "zones": [{"id": "desk", "name": "Desk"}],
                             "light_overrides": {"Hygge": {"light.kno_floor": {"action": "on"}}}}},
           "global_profiles": {"Hygge": {"floor": {"action": "on", "brightness_pct": 60}}}}
    migrated = migrate_store(1, 7, old)
    assert migrated["rooms"]["kno"]["ambient"]["enabled"] is False and migrated["rooms"]["kno"]["brightness"] == {}
    assert migrated["ambient"]["thresholds"] == [5, 20] and migrated["brightness_templates"] == []
    config = LightingConfig.from_dict(migrated)
    room = config.room("kno")
    assert not room.ambient.enabled and room.brightness == {}  # nothing is enrolled by the migration
    assert room.light_overrides and config.global_profiles  # every earlier setting is intact
    assert LightingConfig.from_dict(config.as_dict()) == config
    # Entity renames follow the matrix; columns of removed zones are dropped.
    config = config.with_room("kno", replace(room, brightness={"light.kno_floor": replace(
        FLOOR, zones={"desk": {"none": 65}, "gone": {"none": 1}})}))
    renamed = rename_entity(config, "light.kno_floor", "light.kno_uplight")
    assert set(renamed.room("kno").brightness) == {"light.kno_uplight"}
    assert renamed.room("kno").brightness["light.kno_uplight"].entity_id == "light.kno_uplight"
    data = renamed.as_dict()["rooms"]["kno"]["brightness"]["light.kno_uplight"]
    assert set(data["zones"]) == {"desk"}
    assert set(LightingConfig.from_dict(renamed.as_dict()).room("kno").brightness["light.kno_uplight"].zones) == {
        "desk"}


# =================================================================== acceptance: Kamar Norður
async def test_acceptance_kamar_nordur(hass, home, freezer, devices) -> None:
    """The Phase 4.7 acceptance test: occupancy chooses the scope, ambient light the brightness, mood the look."""
    h = home
    _e, ctrl, sim = await start(hass, h, freezer, devices, outdoor_lux=1200)
    assert level(ctrl, h.area) == "low"
    # 1. Room occupied, Low light -> 15 %, colour from Hygge.
    await occupy(hass, h, freezer, sim)
    assert pct(hass, h.floor) == 15 and kelvin(hass, h.floor) == 2700
    assert devices.of(h.floor) == [("light.turn_on", {"brightness_pct": 15, "color_temp_kelvin": 2700,
                                                      "transition": 2})]
    d = next(x for x in entries(ctrl, h.area) if x["kind"] == "activation")
    assert "Room occupied, Low light (≈ 12 lx), light.kno_floor → 15 %, colour from Hygge (2700 K)" in \
        d["explanation"], d["explanation"]
    # 2. Desk zone active -> 45 % (promptly: no lux stability period).
    devices.clear()
    await sensor(hass, freezer, h.desk, "on", settle=2, sim=sim)
    assert pct(hass, h.floor) == 45
    assert devices.of(h.floor)[-1][1]["brightness_pct"] == 45
    z = next(x for x in entries(ctrl, h.area) if x["kind"] == "zone")
    assert "Desk zone activated" in z["explanation"] and "→ 45 %" in z["explanation"], z["explanation"]
    assert "colour unchanged" in z["explanation"], z["explanation"]
    # 3. Bright with the Desk zone active -> 80 %.
    devices.clear()
    await outdoor(hass, freezer, 2400, settle=70)
    assert level(ctrl, h.area) == "bright"
    assert pct(hass, h.floor) == 80
    a = entries(ctrl, h.area)[0]
    assert a["trigger"] == "ambient" and "Ambient light changed to Bright" in a["explanation"], a
    # 4. Desk zone inactive, room still occupied, Bright -> Off.
    devices.clear()
    await sensor(hass, freezer, h.desk, "off", settle=40, sim=sim)
    assert hass.states.get(h.floor).state == "off"
    assert devices.of(h.floor)[-1][0] == "light.turn_off"
    assert ctrl.coordinator.occupancy.result(h.area).occupied is True
    # 5. No light while the room stays occupied -> 15 %.
    devices.clear()
    await outdoor(hass, freezer, 150, settle=70)  # 1.5 lx: below 5 - 2 lx (hysteresis)
    assert level(ctrl, h.area) == "none"
    assert pct(hass, h.floor) == 15
    # 6. A mood change updates the colour, never the matrix brightness.
    devices.clear()
    hass.states.async_set(MAIN, "Arbeiði", {"options": MOODS})
    await wait(hass, freezer, 4, sim)
    assert pct(hass, h.floor) == 15 and kelvin(hass, h.floor) == 4000
    assert devices.of(h.floor) == [("light.turn_on", {"brightness_pct": 15, "color_temp_kelvin": 4000,
                                                      "transition": 2})]
    # 7. A manual change is respected (no correction, not even at the next category change).
    devices.clear()
    hass.states.async_set(h.floor, "on", {**hass.states.get(h.floor).attributes, "brightness": 200},
                          context=Context(user_id="person"))
    await wait(hass, freezer, 2, sim)
    assert h.floor in ctrl.runtime[h.area].overrides
    await outdoor(hass, freezer, 1200, settle=70)
    assert level(ctrl, h.area) == "low" and devices.of(h.floor) == []
    # Resume Automatic Lighting: the override ends and the matrix takes over at once.
    assert ctrl.async_clear_override(h.area, h.floor) == 1
    await wait(hass, freezer, 4, sim)
    assert pct(hass, h.floor) == 15
    # A new manual change starts a new override.
    hass.states.async_set(h.floor, "off", CT, context=Context(user_id="person"))
    await wait(hass, freezer, 2, sim)
    assert h.floor in ctrl.runtime[h.area].overrides


async def test_feedback_loop_lamp_lights_its_own_sensor(hass, home, freezer, devices) -> None:
    """4 lx natural; the floor lamp at 15 % makes the sensor read 23 lx: never Bright, never off-on-off."""
    h = home
    _e, ctrl, sim = await start(hass, h, freezer, devices, outdoor_lux=None, indoor=True, natural=4,
                                ambient=RoomAmbient(enabled=True, settings=AmbientSettings(stable_seconds=60)))
    assert level(ctrl, h.area) == "none"
    assert ctrl.ambient.status(h.area).basis == "measured"
    await occupy(hass, h, freezer, sim)
    assert pct(hass, h.floor) == 15
    assert 22 <= float(hass.states.get(LUX).state) <= 24  # the lamp lights its own sensor
    await wait(hass, freezer, 45 * 60, sim)
    st = ctrl.ambient.status(h.area)
    assert st.key == "none", st.as_dict()
    assert [c for c in devices.of(h.floor)] == [("light.turn_on", {"brightness_pct": 15, "color_temp_kelvin": 2700,
                                                                   "transition": 2})]
    assert pct(hass, h.floor) == 15
    held = [x for x in entries(ctrl, h.area) if x["kind"] == "ambient"]
    assert held and held[0]["explanation"].startswith(
        "Ambient sensor affected by room lighting. Holding No light classification"), held
    assert len(held) == 1  # once per episode, not on every reading
    # While any lamp of the room is on, 30 lx at the sensor proves nothing either.
    sim.natural = 30
    hass.states.async_set(h.floor, "off", CT, context=Context(user_id="person"))
    await wait(hass, freezer, 4 * 60, sim)
    assert level(ctrl, h.area) == "none" and pct(hass, h.dimmer)  # (the mood's other lamps are still on)
    # Daylight that is real (every lamp off and settled) is recognised.
    for e, attrs in ((h.dimmer, DIM), (h.onoff, ONOFF), (h.strip, RGB)):
        hass.states.async_set(e, "off", attrs, context=Context(user_id="person"))
    await wait(hass, freezer, 4 * 60, sim)
    st = ctrl.ambient.status(h.area)
    assert st.key == "bright" and st.basis == "measured", st.as_dict()


# =================================================================== zones, devices, protections
TWO = {"light.kno_floor": LightMatrix("light.kno_floor", room={"none": 15, "low": 15, "bright": 0}, zones={
    "desk": {"none": 65, "low": 45, "bright": 80}, "seating": {"none": 30, "low": 20, "bright": None}})}


async def test_several_zones_priority_tie_break_and_return_to_room(hass, home, freezer, devices) -> None:
    h = home
    _e, ctrl, sim = await start(hass, h, freezer, devices, zones=(DESK, SEAT), matrices=TWO)
    await occupy(hass, h, freezer, sim)
    assert pct(hass, h.floor) == 15
    await sensor(hass, freezer, h.seat, "on", sim=sim)
    assert pct(hass, h.floor) == 20
    devices.clear()
    await sensor(hass, freezer, h.desk, "on", sim=sim)
    assert pct(hass, h.floor) == 45 and len(devices.of(h.floor)) == 1  # one command, never an average
    item = next(i for i in ctrl.plan(h.area).items if i.entity_id == h.floor)
    b = item.rule["brightness"]
    assert (b["column"], b["value"]) == ("desk", 45) and b["superseded"] == ["Seating zone", "Room occupied"]
    assert b["explanation"] == "Desk zone wins over Seating zone due to higher priority"
    z = next(x for x in entries(ctrl, h.area) if x["kind"] == "zone")
    assert "Desk zone wins over Seating zone due to higher priority" in z["explanation"], z["explanation"]
    await sensor(hass, freezer, h.desk, "off", settle=40, sim=sim)
    assert pct(hass, h.floor) == 20  # back to the zone that is still active
    await sensor(hass, freezer, h.seat, "off", settle=40, sim=sim)
    assert pct(hass, h.floor) == 15  # back to Room occupied
    # Same priority: the zone listed first wins (deterministic).
    with_room(ctrl, h.area, zones=tuple(replace(z, priority=50) for z in ctrl.lighting.room(h.area).zones))
    await sensor(hass, freezer, h.seat, "on", sim=sim)
    await sensor(hass, freezer, h.desk, "on", sim=sim)
    assert pct(hass, h.floor) == 45
    assert "same priority; listed first" in next(
        i for i in ctrl.plan(h.area).items if i.entity_id == h.floor).rule["brightness"]["explanation"]
    # Bright: Seating has no value, so only Desk claims; without Desk the Room occupied Off applies.
    await outdoor(hass, freezer, 2400, settle=70)
    assert pct(hass, h.floor) == 80
    await sensor(hass, freezer, h.desk, "off", settle=40, sim=sim)
    assert hass.states.get(h.floor).state == "off"


async def test_lights_of_every_kind(hass, home, freezer, devices) -> None:
    """RGB, tunable white, brightness-only, on/off-only and a relay: only what each supports is sent."""
    h = home
    cells = {"none": 30, "low": 30, "bright": 0}
    matrices = {e: LightMatrix(e, room=dict(cells)) for e in (h.floor, h.strip, h.dimmer, h.onoff, h.relay)}
    _e, ctrl, sim = await start(hass, h, freezer, devices, matrices=matrices, relays=(h.relay,))
    await occupy(hass, h, freezer, sim)
    assert devices.last(h.floor) == ("light.turn_on", {"brightness_pct": 30, "color_temp_kelvin": 2700,
                                                       "transition": 2})
    assert devices.last(h.strip) == ("light.turn_on", {"brightness_pct": 30, "rgb_color": [255, 120, 0],
                                                       "transition": 2})
    assert devices.last(h.dimmer) == ("light.turn_on", {"brightness_pct": 30, "transition": 2})
    assert devices.last(h.onoff) == ("light.turn_on", {})
    assert devices.last(h.relay) == ("switch.turn_on", {})
    item = next(i for i in ctrl.plan(h.area).items if i.entity_id == h.onoff)
    assert any(n["level"] == "info" and "means On" in n["message"] for n in item.unsupported), item.unsupported
    devices.clear()
    await outdoor(hass, freezer, 2400, settle=70)
    assert {e: devices.last(e)[0] for e in matrices} == {
        h.floor: "light.turn_off", h.strip: "light.turn_off", h.dimmer: "light.turn_off", h.onoff: "light.turn_off",
        h.relay: "switch.turn_off"}
    preview = ctrl.brightness_preview(h.area, h.onoff)
    assert any("on and off only" in c for c in preview["conflicts"])


async def test_possible_occupancy_and_off_cells_wait_for_confirmation(hass, home, freezer, devices) -> None:
    h = home
    _e, ctrl, sim = await start(hass, h, freezer, devices, outdoor_lux=2400)
    hass.states.async_set(h.pir, "on")  # a PIR alone: possible occupancy (first motion switches on)
    await wait(hass, freezer, 3, sim)
    assert ctrl.coordinator.occupancy.result(h.area).state.value == "possible_occupancy"
    assert hass.states.get(h.floor).state == "off"  # Bright: the Room occupied cell is Off
    await outdoor(hass, freezer, 1200, settle=70)
    assert pct(hass, h.floor) == 15
    hass.states.async_set(h.pir, "off")
    await wait(hass, freezer, 4, sim)
    # Bright again while occupancy is only possible: lights are not switched off on uncertain evidence.
    assert ctrl.coordinator.occupancy.result(h.area).state.value in ("possible_occupancy", "possible_vacancy")
    await outdoor(hass, freezer, 2400, settle=70)
    item = next(i for i in ctrl.plan(h.area).items if i.entity_id == h.floor)
    if ctrl.coordinator.occupancy.result(h.area).occupied:
        assert item.reason_code == "off_needs_confirmation", item.reason
    # The room becomes vacant: the existing vacancy behaviour (Turn off) applies, zones end.
    await wait(hass, freezer, 120, sim)
    assert ctrl.coordinator.occupancy.result(h.area).occupied is False
    assert hass.states.get(h.floor).state == "off"


async def test_preliminary_lighting_is_kept(hass, home, freezer, devices) -> None:
    from custom_components.smart_occupancy.preliminary import PreliminaryTarget, RoomPreliminary

    h = home
    _e, ctrl, sim = await start(hass, h, freezer, devices, preliminary=RoomPreliminary(
        enabled=True, targets=(PreliminaryTarget(h.floor, brightness_pct=5),)))
    hass.states.async_set(h.pir, "on")
    await wait(hass, freezer, 3, sim)
    assert pct(hass, h.floor) == 5  # the dim preliminary light, not the matrix
    hass.states.async_set(h.radar, "on")
    await wait(hass, freezer, 14, sim)
    assert ctrl.coordinator.occupancy.result(h.area).state.value == "confirmed_occupancy"
    await wait(hass, freezer, 14, sim)
    assert pct(hass, h.floor) == 15  # on confirmation the matrix takes over


async def test_unavailable_and_late_reporting_devices(hass, home, freezer, devices) -> None:
    h = home
    _e, ctrl, sim = await start(hass, h, freezer, devices)
    hass.states.async_set(h.floor, "unavailable", CT)
    await occupy(hass, h, freezer, sim)
    item = next(i for i in ctrl.plan(h.area).items if i.entity_id == h.floor)
    assert item.reason_code == "offline" and devices.of(h.floor) == []
    hass.states.async_set(h.floor, "off", CT)
    await wait(hass, freezer, 4, sim)
    assert pct(hass, h.floor) == 15  # applied when it is back
    # A Wi-Fi light that reports 3 s late: one command, no correction, no mismatch.
    devices.delay[h.floor] = 3
    devices.clear()
    await sensor(hass, freezer, h.desk, "on", settle=1, sim=sim)
    assert pct(hass, h.floor) == 15  # not reported yet
    await wait(hass, freezer, 6, sim)
    assert pct(hass, h.floor) == 45 and len(devices.of(h.floor)) == 1
    model = ctrl.supervisor.reconcile(h.area)
    assert model["status"] != "mismatch", model


async def test_supervisor_checks_the_matrix_target(hass, home, freezer, devices) -> None:
    h = home
    _e, ctrl, sim = await start(hass, h, freezer, devices)
    await occupy(hass, h, freezer, sim)
    ctrl.supervisor.reconcile(h.area)
    tc = ctrl.supervisor.room(h.area).targets[h.floor]
    assert tc.desired and tc.desired.get("brightness_pct") == 15  # not the mood's 60 %
    assert tc.result == "correct", (tc.result, tc.reason)
    # An intentional Off cell is not a malfunction.
    await outdoor(hass, freezer, 2400, settle=70)
    ctrl.supervisor.reconcile(h.area)
    tc = ctrl.supervisor.room(h.area).targets[h.floor]
    assert hass.states.get(h.floor).state == "off" and tc.result == "correct", (tc.result, tc.reason)
    assert ctrl.supervisor.room(h.area).status not in ("mismatch", "device_failure")


async def test_observing_room_logs_what_would_happen(hass, home, freezer, devices) -> None:
    h = home
    _e, ctrl, sim = await start(hass, h, freezer, devices, active=False)
    await occupy(hass, h, freezer, sim)
    await outdoor(hass, freezer, 2400, settle=70)  # Bright: Off, and the lamp is off (nothing was sent)
    assert "light.kno_floor already Off" in entries(ctrl, h.area)[0]["explanation"], entries(ctrl, h.area)[0]
    await outdoor(hass, freezer, 150, settle=70)
    assert devices.calls == []
    texts = [x["explanation"] for x in entries(ctrl, h.area)]
    assert any("Would set" in t and "light.kno_floor → 15 %" in t for t in texts), texts
    assert any(x["trigger"] == "ambient" and "Observing: nothing was sent" in x["explanation"]
               for x in entries(ctrl, h.area)), texts


async def test_rooms_and_lights_without_the_feature_are_unchanged(hass, home, freezer, devices) -> None:
    h = home
    # Ambient control off: an enrolled light keeps its 0.6.6 behaviour (mood 60 %, zone target 100 %).
    _e, ctrl, sim = await start(hass, h, freezer, devices, ambient=RoomAmbient(enabled=False))
    legacy = {i.entity_id: (i.service, i.data, i.decision) for i in ctrl.plan(h.area, assume=None).items}
    await occupy(hass, h, freezer, sim)
    assert pct(hass, h.floor) == 60
    await sensor(hass, freezer, h.desk, "on", sim=sim)
    assert pct(hass, h.floor) == 100
    # Ambient control on with no enrolled light: every plan item is exactly as before.
    with_room(ctrl, h.area, brightness={})
    before = [(i.entity_id, i.service, i.data, i.decision, i.reason_code) for i in ctrl.plan(h.area).items]
    with_room(ctrl, h.area, ambient=RoomAmbient(enabled=True))
    after = [(i.entity_id, i.service, i.data, i.decision, i.reason_code) for i in ctrl.plan(h.area).items]
    assert before == after
    # Only the enrolled light changes owner; the others keep their mood settings.
    with_room(ctrl, h.area, brightness={h.floor: FLOOR})
    plan = {i.entity_id: i for i in ctrl.plan(h.area).items}
    assert plan[h.floor].data["brightness_pct"] == 45
    for e in (h.strip, h.dimmer, h.onoff):
        assert (plan[e].service, plan[e].data) == (legacy[e][0], legacy[e][1])


async def test_manual_mood_wall_switch_and_rate_limit(hass, home, freezer, devices) -> None:
    h = home
    _e, ctrl, sim = await start(hass, h, freezer, devices, ambient=RoomAmbient(enabled=True, settings=AmbientSettings(
        stable_seconds=60, min_interval=600)))
    await occupy(hass, h, freezer, sim)
    assert pct(hass, h.floor) == 15
    # The minimum interval between automatic brightness changes delays the second lux-driven change.
    await outdoor(hass, freezer, 2400, settle=70)
    assert hass.states.get(h.floor).state == "off"
    devices.clear()
    await outdoor(hass, freezer, 1200, settle=70)
    assert level(ctrl, h.area) == "low" and devices.of(h.floor) == []
    await wait(hass, freezer, 420, sim)
    assert devices.of(h.floor) == []  # (still within the 10 minutes)
    await wait(hass, freezer, 120, sim)
    assert pct(hass, h.floor) == 15
    # A zone is never delayed by it.
    await sensor(hass, freezer, h.desk, "on", settle=2, sim=sim)
    assert pct(hass, h.floor) == 45
    # Manual mood pauses everything, the brightness rules too.
    devices.clear()
    hass.states.async_set(MAIN, "Manual", {"options": [*MOODS, "Manual"]})
    await wait(hass, freezer, 2, sim)
    await outdoor(hass, freezer, 2400, settle=70)
    await sensor(hass, freezer, h.desk, "off", settle=40, sim=sim)
    assert devices.calls == []
    # A wall switch press makes the next change a person's: respected.
    hass.states.async_set(MAIN, "Hygge", {"options": [*MOODS, "Manual"]})
    await wait(hass, freezer, 4, sim)
    ctrl.note_switch_press(h.area)
    hass.states.async_set(h.floor, "on", {**CT, "brightness": 255, "color_mode": "color_temp"})
    await wait(hass, freezer, 2, sim)
    assert ctrl.runtime[h.area].overrides[h.floor]["source"] == "wall_switch"
    devices.clear()
    await sensor(hass, freezer, h.desk, "on", settle=2, sim=sim)
    assert devices.of(h.floor) == []


async def test_dead_band_and_mood_change_keep_brightness(hass, home, freezer, devices) -> None:
    h = home
    _e, ctrl, sim = await start(hass, h, freezer, devices, ambient=RoomAmbient(enabled=True, settings=AmbientSettings(
        stable_seconds=60, dead_band=5)))
    await occupy(hass, h, freezer, sim)
    await sensor(hass, freezer, h.desk, "on", settle=2, sim=sim)
    # Later the device drifts to 41 % (its own report, not a person): within the 5 % dead band.
    await wait(hass, freezer, 60, sim)
    devices.report(h.floor, True, {"brightness_pct": 41, "color_temp_kelvin": 2700})
    await wait(hass, freezer, 2, sim)
    item = next(i for i in ctrl.plan(h.area).items if i.entity_id == h.floor)
    assert (item.decision, item.reason_code) == ("unchanged", "dead_band"), item.reason
    devices.clear()
    hass.states.async_set(MAIN, "Arbeiði", {"options": MOODS})
    await wait(hass, freezer, 4, sim)
    assert devices.of(h.floor) == [("light.turn_on", {"brightness_pct": 45, "color_temp_kelvin": 4000,
                                                      "transition": 2})]
    assert pct(hass, h.floor) == 45 and kelvin(hass, h.floor) == 4000


async def test_level_and_matrices_survive_a_restart(hass, home, freezer, devices, hass_storage) -> None:
    h = home
    entry, ctrl, sim = await start(hass, h, freezer, devices, outdoor_lux=2400)
    assert level(ctrl, h.area) == "bright"
    await hass.async_block_till_done()
    await wait(hass, freezer, 10, sim)
    assert hass_storage["smart_occupancy.lighting"]["minor_version"] == 10  # App 0.9.0 (store 1.10: brightness cell delays)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    ctrl = entry.runtime_data.lighting
    assert ctrl.lighting.room(h.area).brightness[h.floor] == FLOOR
    assert ctrl.ambient.trackers[h.area].committed == 2


async def test_shared_light_is_not_switched_off_while_the_other_room_is_occupied(hass, home, freezer,
                                                                                 devices) -> None:
    from custom_components.smart_occupancy.zones import SharedSpace

    h = home
    _e, ctrl, sim = await start(hass, h, freezer, devices, matrices={})
    ctrl.store.async_set(replace(ctrl.lighting, shared_spaces=(SharedSpace("open", "Open", (h.area, h.stova)),)))
    entry_options = dict(_e.options)
    rooms = dict(entry_options["rooms"])
    rooms[h.area] = {**rooms[h.area], "lights": [h.shared]}
    hass.config_entries.async_update_entry(_e, options={**entry_options, "rooms": rooms})
    await hass.async_block_till_done()
    await wait(hass, freezer, 4, sim)
    ctrl = _e.runtime_data.lighting
    with_room(ctrl, h.area, ambient=RoomAmbient(enabled=True), brightness={
        h.shared: LightMatrix(h.shared, room={"none": 40, "low": 40, "bright": 0})})
    await sensor(hass, freezer, h.stova_pir, "on", sim=sim)
    await occupy(hass, h, freezer, sim)
    assert hass.states.get(h.shared).state == "on"
    await outdoor(hass, freezer, 2400, settle=130)
    assert level(ctrl, h.area) == "bright"
    item = next(i for i in ctrl.plan(h.area).items if i.entity_id == h.shared)
    assert item.service == "turn_off" and item.reason_code == "shared_in_use", item.reason
    assert hass.states.get(h.shared).state == "on"  # kept on: it also lights the occupied Stova


# =================================================================== websocket API
async def test_websocket_api(hass, home, freezer, devices, hass_ws_client, hass_admin_user) -> None:
    h = home
    _e, ctrl, sim = await start(hass, h, freezer, devices, matrices={})
    await occupy(hass, h, freezer, sim)
    devices.clear()
    client = await hass_ws_client(hass, access_token=hass.auth.async_create_access_token(
        await hass.auth.async_create_refresh_token(hass_admin_user, "https://example.com/")))
    n = 0

    async def ws(**msg: Any) -> dict[str, Any]:
        nonlocal n
        n += 1
        await client.send_json({"id": n, **msg})
        return await client.receive_json()

    editor = await ws(type="smart_occupancy/lighting/editor", area_id=h.area)
    model = editor["result"]["room"]["brightness"]
    assert model["ambient"]["enabled"] is True and model["ambient"]["status"]["level"] == "low"
    assert [x["key"] for x in model["levels"]] == ["none", "low", "bright"]
    floor = next(x for x in model["lights"] if x["entity_id"] == h.floor)
    assert floor["enrolled"] is False and [c["id"] for c in floor["columns"]] == ["room", "desk"]
    assert any(t["id"] == "floor" for t in model["templates"])
    # Preview: what enrolling would do; nothing is sent.
    res = await ws(type="smart_occupancy/lighting/brightness/preview", area_id=h.area, entity_id=h.floor,
                   template="floor")
    p = res["result"]
    assert p["sends_now"] is False and p["after"]["data"]["brightness_pct"] == 15
    assert p["before"]["data"]["brightness_pct"] == 60 and p["would_send"] is True
    assert any("Hygge sets 60 %" in c for c in p["conflicts"]), p["conflicts"]
    assert any("Desk zone sets it 100 %" in c for c in p["conflicts"]), p["conflicts"]
    # Enrolling needs the confirmation after the preview; it sends nothing.
    res = await ws(type="smart_occupancy/lighting/brightness/set", area_id=h.area, entity_id=h.floor,
                   matrix={"room": {"none": 15, "low": 15, "bright": 0}})
    assert res["success"] is False and res["error"]["code"] == "preview_required"
    res = await ws(type="smart_occupancy/lighting/brightness/set", area_id=h.area, entity_id=h.floor, confirm=True,
                   matrix={"room": {"none": 15, "low": 15, "bright": 0}, "zones": {"desk": {"low": 45}},
                           "template": "floor"})
    assert res["success"] and res["result"]["sent"] is False
    await wait(hass, freezer, 4, sim)
    assert devices.calls == []
    stored = ctrl.lighting.room(h.area).brightness[h.floor]
    assert stored.room == {"none": 15, "low": 15, "bright": 0} and stored.zones["desk"]["low"] == 45
    assert stored.zones["desk"]["none"] is None and stored.enrolled_by == hass_admin_user.id
    # Once enrolled, the preview still compares with its previous rules (the mood's 60 %), not with itself.
    p = (await ws(type="smart_occupancy/lighting/brightness/preview", area_id=h.area, entity_id=h.floor))["result"]
    assert p["before"]["data"]["brightness_pct"] == 60 and p["after"]["data"]["brightness_pct"] == 15
    # A column for a zone that does not list the light is refused.
    with_room(ctrl, h.area, zones=(*ctrl.lighting.room(h.area).zones, ZoneConfig(id="bed", name="Bed")))
    res = await ws(type="smart_occupancy/lighting/brightness/set", area_id=h.area, entity_id=h.floor,
                   matrix={"zones": {"bed": {"low": 10}}})
    assert res["success"] is False
    # Ambient settings: room override, invalid thresholds, global defaults.
    res = await ws(type="smart_occupancy/lighting/ambient/set", area_id=h.area, thresholds=[20, 5])
    assert res["success"] is False
    res = await ws(type="smart_occupancy/lighting/ambient/set", area_id=h.area, thresholds=[8, 30], hysteresis=1)
    assert res["success"] and ctrl.lighting.ambient_for(h.area).thresholds == (8, 30)
    res = await ws(type="smart_occupancy/lighting/ambient/set", area_id=h.area, thresholds=None)
    assert ctrl.lighting.ambient_for(h.area).thresholds == (5, 20)
    res = await ws(type="smart_occupancy/lighting/ambient/set", stable_seconds=300)
    assert res["success"] and ctrl.lighting.ambient.stable_seconds == 300
    res = await ws(type="smart_occupancy/lighting/ambient/set", stable_seconds=None)
    assert res["success"] is False
    await wait(hass, freezer, 4, sim)
    assert devices.calls == []  # editing the levels never sends a command
    # Templates: save, built-in refused, delete.
    res = await ws(type="smart_occupancy/lighting/brightness/template", op="save", name="My lamp",
                   room={"none": 20, "low": 10, "bright": 0}, zone={"none": 70})
    tid = res["result"]["template"]["id"]
    assert any(t.id == tid for t in ctrl.lighting.brightness_templates)
    res = await ws(type="smart_occupancy/lighting/brightness/template", op="delete", template_id="floor")
    assert res["success"] is False
    res = await ws(type="smart_occupancy/lighting/brightness/template", op="delete", template_id=tid)
    assert res["success"] and not ctrl.lighting.brightness_templates
    # Leave the brightness rules.
    res = await ws(type="smart_occupancy/lighting/brightness/remove", area_id=h.area, entity_id=h.floor)
    assert res["success"] and h.floor not in ctrl.lighting.room(h.area).brightness
