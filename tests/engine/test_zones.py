"""Phase 4.5 (0.6.0): multi-zone lighting, relay outputs, rule arbitration, shared spaces and the Room Supervisor.

A synthetic WC Neðra (sink PIR, shower radar, ceiling downlights, mirror light,
a relay-switched fan light) and an open-plan Køkur / Stova. Every device is
simulated: a command is accepted, then the device reports its new state with a
context of its own (like Zigbee2MQTT), unless it is told to fail, to ignore the
command, or to report late. Nothing here talks to real hardware.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from homeassistant.core import Context, HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import area_registry as ar, entity_registry as er, label_registry as lr
from homeassistant.helpers.event import async_call_later

from custom_components.smart_occupancy import arbitration as arb
from custom_components.smart_occupancy.daylight import RoomDaylight
from custom_components.smart_occupancy.lighting_config import LightingConfig, migrate_store
from custom_components.smart_occupancy.planner import Target
from custom_components.smart_occupancy.lighting import Capabilities
from custom_components.smart_occupancy.supervisor import log_subject
from custom_components.smart_occupancy.zones import (
    SharedSpace,
    SpaceVacancy,
    SupervisorSettings,
    ZoneConfig,
    ZoneManual,
    ZoneTarget,
    ZoneUnavailable,
)

from .conftest import setup_entry
from .test_daylight_runtime import advance

MAIN = "input_select.main_mood"
MOODS = ["Dagur", "Hygge"]
CT = {"supported_color_modes": ["color_temp"], "min_color_temp_kelvin": 2200, "max_color_temp_kelvin": 6500,
      "supported_features": 32}


class Devices:
    """light.* and switch.* services like Zigbee2MQTT devices (see the module docstring)."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.calls: list[ServiceCall] = []
        self.fail: set[str] = set()
        self.ignore: dict[str, int] = {}   # entity -> how many commands it ignores (accepted, no state change)
        self.delay: dict[str, float] = {}  # entity -> seconds before it reports
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
        for e in ids:
            if self.ignore.get(e, 0) > 0:
                self.ignore[e] -= 1
                continue
            on, pct = call.service == "turn_on", call.data.get("brightness_pct")
            kelvin = call.data.get("color_temp_kelvin")
            if (seconds := self.delay.get(e)) is not None:
                async_call_later(self.hass, seconds, callback(
                    lambda _now, e=e, on=on, pct=pct, k=kelvin: self.report(e, on, pct, k)))
            else:
                self.report(e, on, pct, kelvin)

    def report(self, entity_id: str, on: bool, pct: float | None = None, kelvin: int | None = None,
               context: Context | None = None) -> None:
        old = self.hass.states.get(entity_id)
        attrs = dict(old.attributes) if old else {}
        if entity_id.startswith("light."):
            if on:
                if pct is not None:
                    attrs["brightness"] = round(pct * 255 / 100)
                attrs.setdefault("brightness", 255)
                attrs["color_mode"] = "color_temp"
                if kelvin:
                    attrs["color_temp_kelvin"] = kelvin
            else:
                attrs.pop("brightness", None)
        self.hass.states.async_set(entity_id, "on" if on else "off", attrs, context=context or Context())

    def of(self, entity_id: str) -> list[tuple[str, dict[str, Any]]]:
        return [(f"{c.domain}.{c.service}", {k: v for k, v in c.data.items() if k != "entity_id"})
                for c in self.calls if entity_id in self.ids(c)]

    def clear(self) -> None:
        self.calls.clear()


class Home:
    wc: str
    kitchen: str
    stova: str
    sink: str
    shower: str
    down: str
    mirror: str
    relay: str
    k_pir: str
    s_pir: str
    k_light: str
    s_light: str
    shared: str


@pytest.fixture
def devices(hass: HomeAssistant) -> Devices:
    return Devices(hass)


@pytest.fixture
async def home(hass: HomeAssistant) -> Home:
    area_reg, ent_reg, label_reg = ar.async_get(hass), er.async_get(hass), lr.async_get(hass)
    h = Home()
    h.wc = area_reg.async_create("WC Neðra").id
    h.kitchen = area_reg.async_create("Køkur").id
    h.stova = area_reg.async_create("Stova").id
    down_label = label_reg.async_create("Downlights").label_id
    h.label = down_label

    def make(domain: str, object_id: str, area_id: str, label: str | None = None, dc: str | None = None) -> str:
        entry = ent_reg.async_get_or_create(domain, "test", f"uid_{object_id}", suggested_object_id=object_id,
                                            original_device_class=dc)
        ent_reg.async_update_entity(entry.entity_id, area_id=area_id, labels={label} if label else set())
        return entry.entity_id

    h.sink = make("binary_sensor", "wc_sink_pir", h.wc, dc="motion")
    h.shower = make("binary_sensor", "wc_shower_radar", h.wc, dc="occupancy")
    h.down = make("light", "wc_downlights", h.wc, down_label)
    h.mirror = make("light", "wc_mirror", h.wc)
    h.relay = make("switch", "wc_fan_light", h.wc)
    h.k_pir = make("binary_sensor", "kokur_pir", h.kitchen, dc="motion")
    h.s_pir = make("binary_sensor", "stova_pir", h.stova, dc="motion")
    h.k_light = make("light", "kokur_spots", h.kitchen, down_label)
    h.s_light = make("light", "stova_spots", h.stova, down_label)
    h.shared = make("light", "island_pendant", h.kitchen)
    for e in (h.sink, h.shower, h.k_pir, h.s_pir):
        hass.states.async_set(e, "off")
    for e in (h.down, h.mirror, h.k_light, h.s_light, h.shared):
        hass.states.async_set(e, "off", CT)
    hass.states.async_set(h.relay, "off")
    hass.states.async_set(MAIN, "Dagur", {"options": MOODS})
    hass.states.async_set("input_select.auto_mood", "Dagur", {"options": MOODS})
    hass.states.async_set("input_select.mood", "Auto", {"options": ["Auto", *MOODS]})
    await hass.async_block_till_done()
    return h


TIMING = {"correlation_window": 10, "exit_confirmation": 30, "inactivity_timeout": 60, "startup_hold": 0}


def options(h: Home) -> dict[str, Any]:
    room = lambda sensors, lights: {"enabled": True, "sensors": sensors, "lights": lights, "timing": TIMING}  # noqa: E731
    return {
        "config_version": 2, "operating_mode": "observe",
        "mood": {"auto_mood_entity": "input_select.auto_mood", "manual_mood_entity": "input_select.mood",
                 "main_mood_entity": MAIN, "auto_option": "Auto", "mismatch_grace_seconds": 30},
        "categories": [{"key": "downlights", "name": "Downlights", "label_id": h.label}],
        "rooms": {
            h.wc: room([{"entity_id": h.sink, "kind": "pir", "role": "trigger"},
                        {"entity_id": h.shower, "kind": "radar", "role": "maintain"}], [h.down, h.mirror]),
            h.kitchen: room([{"entity_id": h.k_pir, "kind": "pir", "role": "trigger"}], [h.k_light, h.shared]),
            h.stova: room([{"entity_id": h.s_pir, "kind": "pir", "role": "trigger"}], [h.s_light, h.shared]),
        },
        "global_profiles": {"Dagur": {"downlights": {"action": "on", "brightness_pct": 60}},
                            "Hygge": {"downlights": {"action": "on", "brightness_pct": 40}}},
        "occupancy": TIMING,
    }


SINK = ZoneConfig(id="sink", name="Sink", priority=40, targets=(
    ZoneTarget("light.wc_downlights", brightness_pct=30), ZoneTarget("light.wc_mirror", brightness_pct=80)))
SHOWER = ZoneConfig(id="shower", name="Shower", priority=80, timing={"inactivity_timeout": 180, "exit_confirmation": 90},
                    targets=(ZoneTarget("light.wc_downlights", brightness_pct=100),))


def with_room(ctrl, area: str, **changes: Any) -> None:
    ctrl.store.async_set(ctrl.with_room(area, **changes))


async def start(hass: HomeAssistant, h: Home, freezer, devices: Devices, *, zones=(SINK, SHOWER),
                supervisor: SupervisorSettings | None = None, active: bool = True, relays: tuple[str, ...] = (),
                rooms: tuple[str, ...] | None = None):
    freezer.move_to("2026-06-15 12:00:00+00:00")
    entry = await setup_entry(hass, options(h))
    ctrl = entry.runtime_data.lighting
    zones = tuple(replace_sensors(z, h) for z in zones)
    for area in rooms or (h.wc, h.kitchen, h.stova):
        with_room(ctrl, area, control_enabled=active, daylight=RoomDaylight.from_dict({"auto_on": "always"}),
                  **({"zones": zones, "relays": relays} if area == h.wc else {}),
                  **({"supervisor": supervisor} if supervisor else {}))
    await hass.async_block_till_done()
    await advance(hass, freezer, 5)
    devices.clear()
    return entry, ctrl


def replace_sensors(zone: ZoneConfig, h: Home) -> ZoneConfig:
    from dataclasses import replace

    sensors = {"sink": (h.sink,), "shower": (h.shower,)}.get(zone.id, zone.sensors)
    return replace(zone, sensors=zone.sensors or sensors)


def pct(hass: HomeAssistant, entity_id: str) -> int | None:
    state = hass.states.get(entity_id)
    if state is None or state.state != "on":
        return None
    return round(state.attributes["brightness"] / 255 * 100)


async def sensor(hass: HomeAssistant, freezer, entity_id: str, value: str, wait: float = 2) -> None:
    """A sensor reports; time then passes in steps of at most 2 s (timers fire about when they are due)."""
    hass.states.async_set(entity_id, value)
    await hass.async_block_till_done()
    while wait > 0:
        await advance(hass, freezer, min(wait, 2))
        wait -= 2


def log(ctrl, area: str) -> list[dict[str, Any]]:
    return ctrl.decisions.entries(area)


def sup_log(ctrl, area: str) -> list[dict[str, Any]]:
    return ctrl.decisions.entries(log_subject(area))


def occupancy(ctrl, area: str) -> str:
    return ctrl.coordinator.occupancy.result(area).state.value


# =================================================================== pure arbitration
def _target(entity_id: str, state: str = "off", **kw: Any) -> Target:
    return Target(entity_id=entity_id, name=entity_id, category=None, category_name=None, state=state,
                  attributes={"brightness": 76} if state == "on" else {},
                  capabilities=Capabilities(known=True, brightness=True, color_temp=True), **kw)


def test_claims_are_deterministic_and_priority_wins() -> None:
    low = ZoneConfig(id="a", name="A", priority=40, targets=(ZoneTarget("light.x", brightness_pct=30),))
    high = ZoneConfig(id="b", name="B", priority=80, targets=(ZoneTarget("light.x", brightness_pct=100),))
    same = ZoneConfig(id="c", name="C", priority=80, targets=(ZoneTarget("light.x", brightness_pct=50),))
    claims = arb.claims_by_target([(low, 0, None, None), (same, 2, None, None), (high, 1, None, None)])
    assert [c.zone.id for c in claims["light.x"]] == ["b", "c", "a"]  # priority, then configuration order
    item = arb.zone_item(_target("light.x"), claims["light.x"], manual_mode=False, control_enabled=True)
    assert (item.decision, item.service, item.data) == ("change", "turn_on", {"brightness_pct": 100})
    assert item.rule["name"] == "B zone" and item.rule["superseded"] == ["C zone", "A zone"]


def test_zone_item_protections_and_manual_policy() -> None:
    from homeassistant.util import dt as dt_util

    now = dt_util.utcnow()
    earlier = (now - timedelta(minutes=5)).isoformat()
    later = (now + timedelta(minutes=1)).isoformat()
    respect = ZoneConfig(id="s", name="Sink", targets=(ZoneTarget("light.x", brightness_pct=30),))
    override = ZoneConfig(id="w", name="Shower", manual=ZoneManual.OVERRIDE, targets=(ZoneTarget("light.x", brightness_pct=100),))
    unchanged = ZoneConfig(id="u", name="U", targets=(ZoneTarget("light.x", action="unchanged"),))
    claim = lambda z: arb.claims_by_target([(z, 0, now, None)])["light.x"]  # noqa: E731
    o_before = {"source": "ui", "since": earlier}
    assert arb.zone_item(_target("light.x", override=o_before), claim(respect), manual_mode=False,
                         control_enabled=True).reason_code == "manual_override"
    assert arb.zone_item(_target("light.x", override=o_before), claim(override), manual_mode=False,
                         control_enabled=True).decision == "change"  # made before the zone: the zone wins
    assert arb.zone_item(_target("light.x", override={"source": "ui", "since": later}), claim(override),
                         manual_mode=False, control_enabled=True).reason_code == "manual_override"
    assert arb.zone_item(_target("light.x"), claim(respect), manual_mode=True,
                         control_enabled=True).reason_code == "manual_mode"
    assert arb.zone_item(_target("light.x", state="unavailable"), claim(respect), manual_mode=False,
                         control_enabled=True).reason_code == "offline"
    item = arb.zone_item(_target("light.x", state="on"), claim(unchanged), manual_mode=False, control_enabled=True)
    assert (item.decision, item.reason_code, item.service) == ("unchanged", "zone_unchanged", None)


def test_relay_targets_get_on_off_only() -> None:
    from custom_components.smart_occupancy.targets import RELAY_CAPABILITIES

    zone = ZoneConfig(id="z", name="Z", targets=(ZoneTarget("switch.relay", brightness_pct=40, transition=2),))
    t = Target(entity_id="switch.relay", name="Relay", category=None, category_name=None, state="off",
               attributes={}, capabilities=RELAY_CAPABILITIES)
    item = arb.zone_item(t, arb.claims_by_target([(zone, 0, None, None)])["switch.relay"], manual_mode=False,
                         control_enabled=True)
    assert (item.service, item.data, item.decision) == ("turn_on", {}, "change")
    assert {n["field"] for n in item.unsupported} == {"brightness_pct", "transition"}


def test_store_migration_adds_nothing_that_changes_a_room() -> None:
    data = {"rooms": {"wc": {"control_enabled": True}}}
    migrated = migrate_store(1, 6, data)
    room = migrated["rooms"]["wc"]
    assert room["zones"] == [] and room["relays"] == [] and migrated["shared_spaces"] == []
    config = LightingConfig.from_dict(migrated)
    assert config.supervisor.mode.value == "observing" and config.supervisor_for("wc").mode.value == "observing"
    assert config.room("wc").zones == () and config.space_of("wc") is None
    # Round trip.
    assert LightingConfig.from_dict(config.as_dict()) == config


# =================================================================== WC Neðra: sink / shower
async def test_sink_dims_downlights_and_switches_on_the_mirror(hass, home, freezer, devices) -> None:
    _e, ctrl = await start(hass, home, freezer, devices)
    await sensor(hass, freezer, home.sink, "on")
    assert pct(hass, home.down) == 30 and pct(hass, home.mirror) == 80
    d = next(x for x in log(ctrl, home.wc) if x["kind"] == "zone")
    assert d["action"] == "Sink zone activated" and d["status"] in ("executed", "dispatched"), d
    assert "light.wc_downlights set to 30 %" in d["explanation"], d["explanation"]
    rules = {r["entity_id"]: r for r in ctrl.room_diagnostics(home.wc)["zones"]["status"][0]["sensors"]}
    assert rules[home.sink]["available"] is True
    # The mood's own application afterwards (first motion) sends nothing contradictory.
    downs = [c for c in devices.of(home.down)]
    assert downs == [("light.turn_on", {"brightness_pct": 30})], downs


async def test_shower_overrides_the_sink_and_restores_it(hass, home, freezer, devices) -> None:
    _e, ctrl = await start(hass, home, freezer, devices)
    await sensor(hass, freezer, home.sink, "on")
    await sensor(hass, freezer, home.shower, "on")
    assert pct(hass, home.down) == 100 and pct(hass, home.mirror) == 80
    d = next(x for x in log(ctrl, home.wc) if x["kind"] == "zone")
    assert d["action"] == "Shower zone activated"
    assert "light.wc_downlights set to 100 %, overriding Sink zone" in d["explanation"], d["explanation"]
    plan = ctrl.effective_plan(home.wc)
    item = next(i for i in plan.items if i.entity_id == home.down)
    assert item.rule["name"] == "Shower zone" and "Sink zone" in item.rule["superseded"]
    # A mood change while the shower is occupied does not dim the shower.
    devices.clear()
    hass.states.async_set(MAIN, "Hygge", {"options": MOODS})
    await hass.async_block_till_done()
    await advance(hass, freezer, 3)
    assert devices.of(home.down) == [] and pct(hass, home.down) == 100
    # Leaving the shower while still at the sink: the sink's 30 % again (after the shower's hold).
    await sensor(hass, freezer, home.shower, "off", wait=1)
    await advance(hass, freezer, 60)
    assert pct(hass, home.down) == 100  # brief interruptions do not dim the shower (hold 180 s)
    await advance(hass, freezer, 160)
    assert pct(hass, home.down) == 30
    d = next(x for x in log(ctrl, home.wc) if x["kind"] == "zone")
    assert d["action"] == "Shower zone ended", d


async def test_leaving_the_room_runs_the_vacancy_action(hass, home, freezer, devices) -> None:
    _e, ctrl = await start(hass, home, freezer, devices)
    await sensor(hass, freezer, home.sink, "on")
    await sensor(hass, freezer, home.sink, "off")
    assert pct(hass, home.mirror) == 80
    for _ in range(10):
        await advance(hass, freezer, 20)
    assert occupancy(ctrl, home.wc) == "vacant"
    assert hass.states.is_state(home.down, "off") and hass.states.is_state(home.mirror, "off")
    assert ctrl.runtime[home.wc].zone_owned == {}


async def test_sink_ends_while_room_occupied_mirror_switched_off_downlights_back_to_mood(hass, home, freezer,
                                                                                           devices) -> None:
    """The mirror is set by no other rule: the zone switches it off. The downlights follow Dagur (60 %)."""
    short = ZoneConfig(id="sink", name="Sink", priority=40, timing={"inactivity_timeout": 30, "exit_confirmation": 0},
                       targets=SINK.targets)
    _e, ctrl = await start(hass, home, freezer, devices, zones=(short,))
    await sensor(hass, freezer, home.shower, "on")  # someone in the room (not in the sink zone)
    await sensor(hass, freezer, home.sink, "on")
    assert pct(hass, home.down) == 30 and pct(hass, home.mirror) == 80
    await sensor(hass, freezer, home.sink, "off")
    await advance(hass, freezer, 40)
    assert occupancy(ctrl, home.wc) == "confirmed_occupancy"
    assert pct(hass, home.down) == 60 and hass.states.is_state(home.mirror, "off")
    d = next(x for x in log(ctrl, home.wc) if x["kind"] == "zone")
    assert "switched off (no remaining rule keeps it on)" in d["explanation"], d["explanation"]


async def test_shower_holds_the_room_occupied(hass, home, freezer, devices) -> None:
    """The room's own vacancy (60 s) would end before the shower's hold (180 s): the zone keeps it occupied."""
    _e, ctrl = await start(hass, home, freezer, devices)
    await sensor(hass, freezer, home.shower, "on", wait=12)
    await sensor(hass, freezer, home.shower, "off")
    await advance(hass, freezer, 120)
    assert occupancy(ctrl, home.wc) == "confirmed_occupancy"
    assert ctrl.coordinator.occupancy.engines[home.wc].external_hold == frozenset({"Shower"})
    await advance(hass, freezer, 200)
    assert occupancy(ctrl, home.wc) in ("possible_vacancy", "vacant")
    await advance(hass, freezer, 70)
    assert occupancy(ctrl, home.wc) == "vacant" and hass.states.is_state(home.down, "off")


@pytest.mark.parametrize(("policy", "expected"), [(ZoneUnavailable.HOLD, 100), (ZoneUnavailable.OCCUPIED, 100),
                                                  (ZoneUnavailable.VACANT, 60)])
async def test_shower_sensor_unavailable_fail_safe(hass, home, freezer, devices, policy, expected) -> None:
    from dataclasses import replace

    _e, ctrl = await start(hass, home, freezer, devices, zones=(replace(SHOWER, unavailable=policy),))
    await sensor(hass, freezer, home.sink, "on")  # the room stays occupied by the sink PIR
    await sensor(hass, freezer, home.shower, "on")
    assert pct(hass, home.down) == 100
    await sensor(hass, freezer, home.shower, "unavailable")
    status = ctrl.zones.status(home.wc)[0]
    assert status["failsafe"] == policy.value and status["available"] is False
    assert pct(hass, home.down) == expected
    # An unavailable zone never holds the room occupied.
    assert ctrl.coordinator.occupancy.engines[home.wc].external_hold == frozenset()


async def test_zones_in_observing_rooms_send_nothing(hass, home, freezer, devices) -> None:
    _e, ctrl = await start(hass, home, freezer, devices, active=False)
    await sensor(hass, freezer, home.sink, "on")
    await sensor(hass, freezer, home.shower, "on")
    assert devices.calls == []
    d = next(x for x in log(ctrl, home.wc) if x["kind"] == "zone")
    assert d["status"] == "observed" and "would be set to 100 %" in d["explanation"], d
    assert d["explanation"].endswith("Observing: nothing was sent.")


async def test_manual_override_is_respected_by_the_sink(hass, home, freezer, devices) -> None:
    _e, ctrl = await start(hass, home, freezer, devices)
    await sensor(hass, freezer, home.sink, "on")
    devices.report(home.down, True, 70, context=Context(user_id="person"))  # a person dims to 70 % in the app
    await hass.async_block_till_done()
    assert home.down in ctrl.runtime[home.wc].overrides
    await sensor(hass, freezer, home.sink, "off")
    await sensor(hass, freezer, home.sink, "on")
    assert pct(hass, home.down) == 70


async def test_deleting_an_active_zone_restores_the_room(hass, home, freezer, devices) -> None:
    _e, ctrl = await start(hass, home, freezer, devices)
    await sensor(hass, freezer, home.shower, "on")
    assert pct(hass, home.down) == 100
    with_room(ctrl, home.wc, zones=(replace_sensors(SINK, home),))
    await hass.async_block_till_done()
    await advance(hass, freezer, 2)
    assert pct(hass, home.down) == 60  # Dagur


async def test_zone_state_survives_a_restart(hass, home, freezer, devices) -> None:
    entry, ctrl = await start(hass, home, freezer, devices)
    await sensor(hass, freezer, home.shower, "on")
    assert ctrl.zones.status(home.wc)[1]["active"] is True
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    devices.clear()
    ctrl = entry.runtime_data.lighting
    await advance(hass, freezer, 5)
    assert ctrl.zones.zone(home.wc, "shower").active is True
    assert ctrl.runtime[home.wc].zone_winners == {home.down: "shower"}
    assert devices.calls == []  # nothing is applied on start


# =================================================================== relay outputs
async def test_relay_output_switched_with_switch_services(hass, home, freezer, devices) -> None:
    relay_zone = ZoneConfig(id="sink", name="Sink", targets=(ZoneTarget("switch.wc_fan_light", brightness_pct=50),))
    _e, ctrl = await start(hass, home, freezer, devices, zones=(relay_zone,), relays=(home.relay,))
    assert home.relay in ctrl.managed_room(home.wc).controlled
    await sensor(hass, freezer, home.sink, "on")
    assert devices.of(home.relay) == [("switch.turn_on", {})]  # no light attribute is sent to a switch
    assert hass.states.is_state(home.relay, "on")
    model = ctrl.supervisor.model(home.wc, details=True)
    assert next(t for t in model["targets"] if t["entity_id"] == home.relay)["type"] == "relay"
    await sensor(hass, freezer, home.sink, "off")
    await advance(hass, freezer, 200)
    assert devices.of(home.relay)[-1] == ("switch.turn_off", {})
    assert hass.states.is_state(home.relay, "off")


async def test_switches_are_not_lights_unless_designated(hass, home, freezer, devices) -> None:
    _e, ctrl = await start(hass, home, freezer, devices)
    assert home.relay not in ctrl.managed_room(home.wc).targets


# =================================================================== Room Supervisor
AUTO = SupervisorSettings.from_dict({"mode": "automatic", "max_retries": 3, "retry_delay": 5})


async def test_supervisor_corrects_a_device_that_ignored_a_command(hass, home, freezer, devices) -> None:
    _e, ctrl = await start(hass, home, freezer, devices, supervisor=AUTO)
    devices.ignore[home.down] = 1
    await sensor(hass, freezer, home.shower, "on")
    assert hass.states.is_state(home.down, "off")
    await advance(hass, freezer, 5)  # verification delay
    await advance(hass, freezer, 2)
    assert pct(hass, home.down) == 100
    texts = [d["explanation"] for d in sup_log(ctrl, home.wc)]
    assert any("expected 100 %, actual off" in t and "Correction requested." in t for t in texts), texts
    assert any("Corrective light.turn_on issued (attempt 1 of 3)" in t for t in texts), texts
    await advance(hass, freezer, 30)
    texts = [d["explanation"] for d in sup_log(ctrl, home.wc)]
    assert any("verified at 100 %" in t for t in texts), texts
    assert ctrl.supervisor.room(home.wc).status == "healthy", ctrl.supervisor.model(home.wc)


async def test_supervisor_retries_are_limited(hass, home, freezer, devices) -> None:
    _e, ctrl = await start(hass, home, freezer, devices, supervisor=AUTO, relays=(home.relay,),
                           zones=(ZoneConfig(id="sink", name="Sink", targets=(ZoneTarget("switch.wc_fan_light"),)),))
    devices.ignore[home.relay] = 100
    await sensor(hass, freezer, home.sink, "on")
    for _ in range(40):
        await advance(hass, freezer, 5)
    sends = devices.of(home.relay)
    assert sends == [("switch.turn_on", {})] * 4, sends  # the command + 3 corrections, never more
    model = ctrl.supervisor.model(home.wc, details=True)
    assert model["status"] == "device_failure", model
    t = next(t for t in model["targets"] if t["entity_id"] == home.relay)
    assert t["result"] == "device_failure" and t["attempts"] == 3
    assert any("device failure suspected" in d["explanation"] for d in sup_log(ctrl, home.wc))
    # The device comes back online: it is verified again (and corrected within the limits).
    devices.ignore[home.relay] = 0
    hass.states.async_set(home.relay, "unavailable")
    await hass.async_block_till_done()
    hass.states.async_set(home.relay, "off")
    await hass.async_block_till_done()
    for _ in range(6):
        await advance(hass, freezer, 5)
    assert hass.states.is_state(home.relay, "on")


async def test_supervisor_failed_command_and_rate_limit(hass, home, freezer, devices) -> None:
    sup = SupervisorSettings.from_dict({"mode": "automatic", "max_retries": 10, "retry_delay": 1, "max_per_hour": 2})
    _e, ctrl = await start(hass, home, freezer, devices, supervisor=sup)
    devices.fail.add(home.down)
    await sensor(hass, freezer, home.shower, "on")
    for _ in range(30):
        await advance(hass, freezer, 5)
    t = next(t for t in ctrl.supervisor.model(home.wc, details=True)["targets"] if t["entity_id"] == home.down)
    assert t["attempts"] == 2 and t["last_correction"]["attempt"] == 2, t  # at most 2 corrections per hour
    assert "rate limit" in t["reason"], t
    corrections = [d for d in sup_log(ctrl, home.wc) if d["action"] == "Correction requested"]
    assert sum(d["count"] for d in corrections) == 2 and corrections[0]["status"] == "failed", corrections


async def test_delayed_reports_are_not_mismatches(hass, home, freezer, devices) -> None:
    _e, ctrl = await start(hass, home, freezer, devices, supervisor=AUTO)
    devices.delay[home.down] = 3
    await sensor(hass, freezer, home.shower, "on", wait=1)
    for _ in range(4):
        await advance(hass, freezer, 2)
    assert pct(hass, home.down) == 100
    for _ in range(10):
        await advance(hass, freezer, 30)
    assert len(devices.of(home.down)) == 1  # repeated reconciliation sends nothing
    assert not [d for d in sup_log(ctrl, home.wc) if d["action"] == "Mismatch detected"]


async def test_observing_supervisor_reports_but_never_sends(hass, home, freezer, devices) -> None:
    _e, ctrl = await start(hass, home, freezer, devices)  # default: Observing
    devices.ignore[home.down] = 100
    await sensor(hass, freezer, home.shower, "on")
    for _ in range(10):
        await advance(hass, freezer, 10)
    assert devices.of(home.down) == [("light.turn_on", {"brightness_pct": 100})]
    model = ctrl.supervisor.model(home.wc)
    assert model["status"] == "mismatch" and model["discrepancies"] == 1, model
    d = sup_log(ctrl, home.wc)[0]
    assert d["status"] == "observed" and "Not corrected: Observing: not corrected." in d["explanation"], d


async def test_supervisor_never_corrects_a_manual_change(hass, home, freezer, devices) -> None:
    _e, ctrl = await start(hass, home, freezer, devices, supervisor=AUTO)
    await sensor(hass, freezer, home.shower, "on")
    devices.report(home.down, True, 20, context=Context(user_id="person"))
    await hass.async_block_till_done()
    for _ in range(6):
        await advance(hass, freezer, 10)
    assert pct(hass, home.down) == 20
    t = next(t for t in ctrl.supervisor.model(home.wc, details=True)["targets"] if t["entity_id"] == home.down)
    assert t["result"] == "override"


async def test_external_automation_is_reported_not_fought(hass, home, freezer, devices) -> None:
    _e, ctrl = await start(hass, home, freezer, devices, supervisor=AUTO)
    # Confirmed occupancy applied, and longer ago than a device's own report may take (20 s): nothing pending.
    await sensor(hass, freezer, home.shower, "on", wait=25)
    parent = Context()
    devices.report(home.down, True, 10, context=Context(parent_id=parent.id))  # an automation dims it
    await hass.async_block_till_done()
    for _ in range(6):
        await advance(hass, freezer, 10)
    assert pct(hass, home.down) == 10 and len(devices.of(home.down)) == 1
    t = next(t for t in ctrl.supervisor.model(home.wc, details=True)["targets"] if t["entity_id"] == home.down)
    assert t["result"] == "external" and "not corrected" in t["reason"], t
    # Allowed to correct once: corrected; changed again by the automation: conflict, not corrected again.
    with_room(ctrl, home.wc, supervisor=SupervisorSettings.from_dict({"mode": "automatic", "correct_external": True}))
    await hass.async_block_till_done()
    for _ in range(3):
        await advance(hass, freezer, 5)
    assert pct(hass, home.down) == 100
    # (A change in the same direction within 20 s of a command cannot be told apart from the device's own report.)
    await advance(hass, freezer, 21)
    devices.report(home.down, True, 10, context=Context(parent_id=Context().id))
    await hass.async_block_till_done()
    for _ in range(8):
        await advance(hass, freezer, 10)
    assert pct(hass, home.down) == 10 and len(devices.of(home.down)) == 2
    assert ctrl.supervisor.model(home.wc)["status"] == "conflict"


async def test_supervisor_never_restarts_or_reactivates_possible_occupancy(hass, home, freezer, devices) -> None:
    """Possible occupancy (0.3.10 regression): a Zigbee and a Wi-Fi light go off at the timeout; the supervisor
    verifies the switch-off, never switches them on again and never extends the stage."""
    from custom_components.smart_occupancy.preliminary import PreliminaryTarget, RoomPreliminary

    _e, ctrl = await start(hass, home, freezer, devices, zones=(), supervisor=AUTO)
    with_room(ctrl, home.wc, preliminary=RoomPreliminary(enabled=True, signals=(home.sink,), targets=(
        PreliminaryTarget(home.down, 10), PreliminaryTarget(home.mirror, 10))))
    await hass.async_block_till_done()
    await advance(hass, freezer, 2)
    devices.clear()
    devices.delay[home.mirror] = 2  # the "Wi-Fi" light reports late
    await sensor(hass, freezer, home.sink, "on", wait=3)
    await sensor(hass, freezer, home.sink, "off", wait=1)
    assert pct(hass, home.down) == 10
    deadline = ctrl.runtime[home.wc].preliminary["deadline"]
    for _ in range(4):
        await advance(hass, freezer, 10)
    assert hass.states.is_state(home.down, "off") and hass.states.is_state(home.mirror, "off")
    assert ctrl.runtime[home.wc].preliminary is None
    for _ in range(12):
        await advance(hass, freezer, 30)
    ons = [c for c in devices.calls if c.service == "turn_on"]
    assert len(ons) == 2, devices.calls  # only the stage's own switch-on, never again
    assert deadline  # the stage ran to its own deadline (not restarted)


async def test_preliminary_light_that_stays_on_is_corrected(hass, home, freezer, devices) -> None:
    from custom_components.smart_occupancy.preliminary import PreliminaryTarget, RoomPreliminary

    _e, ctrl = await start(hass, home, freezer, devices, zones=(), supervisor=AUTO)
    with_room(ctrl, home.wc, preliminary=RoomPreliminary(enabled=True, signals=(home.sink,), targets=(
        PreliminaryTarget(home.down, 10),)))
    await hass.async_block_till_done()
    await advance(hass, freezer, 2)
    devices.ignore[home.down] = 0
    await sensor(hass, freezer, home.sink, "on", wait=3)  # an Aqara-like PIR: holds "on" for about 45 s
    assert pct(hass, home.down) == 10
    devices.ignore[home.down] = 1  # the switch-off at the timeout is accepted, but the light stays on
    for _ in range(7):
        await advance(hass, freezer, 5)  # 38 s: the stage timed out at 30 s, still possible occupancy
    assert occupancy(ctrl, home.wc) == "possible_occupancy"
    assert hass.states.is_state(home.down, "off"), devices.calls
    assert any("Corrective light.turn_off issued" in d["explanation"] for d in sup_log(ctrl, home.wc))


async def test_supervisor_status_is_never_healthy_without_information(hass, home, freezer, devices) -> None:
    _e, ctrl = await start(hass, home, freezer, devices, supervisor=AUTO)
    await advance(hass, freezer, 40)
    assert ctrl.supervisor.model(home.wc)["status"] == "healthy"
    hass.states.async_set(home.shower, "unavailable")
    hass.states.async_set(home.sink, "unavailable")
    await hass.async_block_till_done()
    await advance(hass, freezer, 70)
    assert ctrl.supervisor.model(home.wc)["status"] in ("degraded", "unknown")
    assert any(d["action"] == "Sensor availability" for d in sup_log(ctrl, home.wc))


# =================================================================== shared space Køkur / Stova
async def _space(ctrl, h: Home, vacancy=SpaceVacancy.INDEPENDENT, mood=None) -> None:
    from dataclasses import replace

    ctrl.store.async_set(replace(ctrl.lighting, shared_spaces=(SharedSpace(
        id="living", name="Køkur/Stova", rooms=(h.kitchen, h.stova), vacancy=vacancy, mood_override=mood),)))


async def test_shared_space_rooms_are_independent(hass, home, freezer, devices) -> None:
    _e, ctrl = await start(hass, home, freezer, devices)
    await _space(ctrl, home)
    await hass.async_block_till_done()
    await sensor(hass, freezer, home.k_pir, "on")
    assert pct(hass, home.k_light) == 60 and hass.states.is_state(home.s_light, "off")
    await sensor(hass, freezer, home.s_pir, "on")
    assert pct(hass, home.s_light) == 60
    # The shared pendant is not set by the profile (no label): nobody fights over it; Køkur's spots stay.
    await sensor(hass, freezer, home.k_pir, "off")
    await advance(hass, freezer, 120)
    assert occupancy(ctrl, home.kitchen) == "vacant"
    assert hass.states.is_state(home.k_light, "off") and pct(hass, home.s_light) == 60


async def test_shared_target_is_not_switched_off_while_the_other_room_is_occupied(hass, home, freezer,
                                                                                  devices) -> None:
    _e, ctrl = await start(hass, home, freezer, devices)
    from custom_components.smart_occupancy.models import ProfileSetting

    for area in (home.kitchen, home.stova):
        with_room(ctrl, area, light_overrides={"Dagur": {home.shared: ProfileSetting.from_dict(
            {"action": "on", "brightness_pct": 50 if area == home.kitchen else 90})}})
    await _space(ctrl, home)
    await hass.async_block_till_done()
    await sensor(hass, freezer, home.k_pir, "on")
    await sensor(hass, freezer, home.s_pir, "on")
    assert pct(hass, home.shared) == 50  # one room at a time: Køkur is first in the space
    assert len({c.data.get("brightness_pct") for c in devices.calls if home.shared in devices.ids(c)}) == 1
    await sensor(hass, freezer, home.k_pir, "off")
    await advance(hass, freezer, 120)
    assert occupancy(ctrl, home.kitchen) == "vacant"
    assert hass.states.is_state(home.shared, "on")  # Stova is occupied: kept on
    item = next(i for i in ctrl.effective_plan(home.kitchen).items if i.entity_id == home.shared)
    assert item.reason_code in ("shared_in_use", "already"), item


async def test_space_that_vacates_together(hass, home, freezer, devices) -> None:
    _e, ctrl = await start(hass, home, freezer, devices)
    await _space(ctrl, home, vacancy=SpaceVacancy.TOGETHER, mood="Hygge")
    await hass.async_block_till_done()
    await sensor(hass, freezer, home.k_pir, "on")
    assert pct(hass, home.k_light) == 40  # the space's mood
    await sensor(hass, freezer, home.s_pir, "on")
    await sensor(hass, freezer, home.k_pir, "off")
    await advance(hass, freezer, 120)
    assert occupancy(ctrl, home.kitchen) == "vacant" and pct(hass, home.k_light) == 40
    assert ctrl.runtime[home.kitchen].space_deferred is True
    assert any(d["action"] == "Vacancy waits" for d in log(ctrl, home.kitchen))
    await sensor(hass, freezer, home.s_pir, "off")
    await advance(hass, freezer, 120)
    assert hass.states.is_state(home.k_light, "off") and hass.states.is_state(home.s_light, "off")


# =================================================================== compatibility
async def test_rooms_without_zones_behave_as_before(hass, home, freezer, devices) -> None:
    _e, ctrl = await start(hass, home, freezer, devices, zones=())
    await sensor(hass, freezer, home.sink, "on")
    assert devices.of(home.down) == [("light.turn_on", {"brightness_pct": 60})]
    item = next(i for i in ctrl.plan(home.wc).items if i.entity_id == home.down)
    assert item.rule == {"kind": "occupied", "name": "Occupied profile (Dagur)"}
    assert not [d for d in log(ctrl, home.wc) if d["kind"] == "zone"]


async def test_forecast_never_activates_a_zone(hass, home, freezer, devices) -> None:
    _e, ctrl = await start(hass, home, freezer, devices)
    for entity in [s.entity_id for s in hass.states.async_all("binary_sensor") if s.entity_id.endswith("_predicted")]:
        hass.states.async_set(entity, "on")
    await hass.async_block_till_done()
    await advance(hass, freezer, 5)
    assert not any(z["active"] for z in ctrl.zones.status(home.wc))
    assert devices.calls == []


async def test_zone_websocket_api(hass, home, freezer, devices, hass_ws_client, hass_admin_user) -> None:
    _e, ctrl = await start(hass, home, freezer, devices, zones=())
    refresh = await hass.auth.async_create_refresh_token(hass_admin_user, "https://example.com/app")
    ws = await hass_ws_client(hass, access_token=hass.auth.async_create_access_token(refresh))
    await ws.send_json({"id": 1, "type": "smart_occupancy/lighting/zone/set", "area_id": home.wc,
                        "zone": {"name": "Shower", "sensors": [home.shower], "priority": 80,
                                 "targets": [{"entity_id": home.down, "brightness_pct": 100}]}})
    res = await ws.receive_json()
    assert res["success"], res
    assert res["result"]["zone"]["id"] == "shower"
    await ws.send_json({"id": 2, "type": "smart_occupancy/lighting/zone/set", "area_id": home.wc,
                        "zone": {"id": "shower", "sensors": [home.k_pir]}})
    res = await ws.receive_json()
    assert not res["success"] and "not a presence signal" in res["error"]["message"]
    await ws.send_json({"id": 3, "type": "smart_occupancy/lighting/zone/set", "area_id": home.wc,
                        "zone": {"id": "shower", "targets": [{"entity_id": home.k_light}]}})
    res = await ws.receive_json()
    assert not res["success"] and "this room controls" in res["error"]["message"]
    await ws.send_json({"id": 4, "type": "smart_occupancy/lighting/relay/set", "area_id": home.wc,
                        "relays": [home.relay]})
    assert (await ws.receive_json())["success"]
    await ws.send_json({"id": 5, "type": "smart_occupancy/lighting/relay/set", "area_id": home.wc,
                        "relays": [home.down]})
    assert not (await ws.receive_json())["success"]
    await ws.send_json({"id": 6, "type": "smart_occupancy/lighting/space/set",
                        "space": {"name": "Køkur/Stova", "rooms": [home.kitchen, home.stova]}})
    assert (await ws.receive_json())["success"]
    await ws.send_json({"id": 7, "type": "smart_occupancy/lighting/supervisor/set", "area_id": home.wc,
                        "mode": "automatic", "max_retries": 2})
    assert (await ws.receive_json())["success"]
    await ws.send_json({"id": 8, "type": "smart_occupancy/lighting/editor", "area_id": home.wc})
    res = await ws.receive_json()
    room = res["result"]["room"]
    assert [z["name"] for z in room["zones"]["zones"]] == ["Shower"]
    assert room["zones"]["relays"] == [home.relay] and room["supervisor"]["mode"] == "automatic"
    assert {t["entity_id"]: t["type"] for t in room["zones"]["targets"]}[home.relay] == "relay"
    await ws.send_json({"id": 9, "type": "smart_occupancy/lighting/supervisor/room", "area_id": home.wc,
                        "check": True})
    res = await ws.receive_json()
    assert res["success"] and res["result"]["settings"]["max_retries"] == 2
    await ws.send_json({"id": 10, "type": "smart_occupancy/lighting/zone/remove", "area_id": home.wc,
                        "zone_id": "shower"})
    assert (await ws.receive_json())["success"]
    assert ctrl.lighting.room(home.wc).zones == ()


async def test_person_commands_to_a_relay_use_switch_services(hass, home, freezer, devices) -> None:
    """A wall switch / profile test command (a person's request) to a relay: switch.turn_on without light data."""
    _e, ctrl = await start(hass, home, freezer, devices, zones=(), relays=(home.relay,))
    errors = await ctrl._send_for_person(home.wc, "turn_on", {home.relay: {"brightness_pct": 40, "transition": 2},
                                                              home.mirror: {"brightness_pct": 40}}, Context())
    assert errors == {}
    assert devices.of(home.relay) == [("switch.turn_on", {})]
    assert devices.of(home.mirror) == [("light.turn_on", {"brightness_pct": 40})]


async def test_zone_to_zone_handover(hass, home, freezer, devices) -> None:
    """Shower, then the person moves to the sink: the downlights go 100 % -> 30 % when the shower's hold ends,
    and the mirror (sink only) comes on at once."""
    _e, ctrl = await start(hass, home, freezer, devices)
    await sensor(hass, freezer, home.shower, "on", wait=12)
    assert pct(hass, home.down) == 100 and hass.states.is_state(home.mirror, "off")
    await sensor(hass, freezer, home.shower, "off")
    await sensor(hass, freezer, home.sink, "on")
    assert pct(hass, home.down) == 100 and pct(hass, home.mirror) == 80
    for _ in range(20):
        await advance(hass, freezer, 10)
    assert ctrl.zones.zone(home.wc, "shower").active is False
    assert pct(hass, home.down) == 30 and pct(hass, home.mirror) == 80


async def test_zone_cannot_target_a_member_of_a_managed_group(hass, home, freezer, devices, hass_ws_client,
                                                             hass_admin_user) -> None:
    entry = er.async_get(hass).async_get_or_create("light", "test", "uid_wc_group", suggested_object_id="wc_spots_group")
    hass.states.async_set(entry.entity_id, "off", {**CT, "entity_id": [home.down]})
    _e, ctrl = await start(hass, home, freezer, devices, zones=())
    with_room(ctrl, home.wc, excluded=())
    opts = dict(_e.options)
    rooms = dict(opts["rooms"])
    rooms[home.wc] = {**rooms[home.wc], "lights": [entry.entity_id, home.down, home.mirror]}
    hass.config_entries.async_update_entry(_e, options={**opts, "rooms": rooms})
    await hass.async_block_till_done()
    ctrl = _e.runtime_data.lighting
    assert ctrl.managed_room(home.wc).covered_by.get(home.down) == entry.entity_id
    refresh = await hass.auth.async_create_refresh_token(hass_admin_user, "https://example.com/app")
    ws = await hass_ws_client(hass, access_token=hass.auth.async_create_access_token(refresh))
    await ws.send_json({"id": 1, "type": "smart_occupancy/lighting/zone/set", "area_id": home.wc,
                        "zone": {"name": "Sink", "targets": [{"entity_id": home.down}]}})
    res = await ws.receive_json()
    assert not res["success"] and "this room controls" in res["error"]["message"]
