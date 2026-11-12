"""Possible occupancy lighting (0.3.9): a dim, temporary light before occupancy is confirmed.

Occupancy comes from the real engine with the default timing (correlation
window 10 s: a PIR alone is possible occupancy until it has been active for
10 s or a second signal agrees). Lights are simulated by services that report
the requested state back (FakeLights), or that fail. The daylight decision
comes from a simulated indoor light sensor at noon (``start``), or from the
sun. Nothing here talks to real devices.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import Context, HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from custom_components.smart_occupancy.const import UncertainPolicy
from custom_components.smart_occupancy.daylight import IndoorSensor, RoomDaylight
from custom_components.smart_occupancy.lighting_config import LightingConfig, migrate_store
from custom_components.smart_occupancy.preliminary import (
    ConfirmAction,
    PreliminaryDefaults,
    PreliminaryTarget,
    RoomPreliminary,
)

from .conftest import PILOT_HELPER, Pilot, pilot, pilot_options, setup_entry  # noqa: F401
from .test_daylight_runtime import LUX, advance, lights, set_lux, start  # noqa: F401
from .test_adaptive_runtime import FakeLights
from .test_lighting_runtime import CT, PROFILES, Lit, lit, options  # noqa: F401
from .test_switches import WS

LAMP_10 = {"brightness_pct": 10, "color_temp_kelvin": 2200, "transition": 0.5}


def calls_for(lights: FakeLights, entity_id: str) -> list[tuple[str, dict[str, Any]]]:
    out = []
    for call in lights.calls:
        ids = call.data["entity_id"]
        if entity_id in ([ids] if isinstance(ids, str) else ids):
            out.append((call.service, {k: v for k, v in call.data.items() if k != "entity_id"}))
    return out


def log(ctrl, area: str) -> list[dict[str, Any]]:
    return ctrl.decisions.entries(area)


def state(ctrl, area: str) -> str:
    return ctrl.coordinator.occupancy.result(area).state.value


def prelim(*targets: PreliminaryTarget, **kw: Any) -> RoomPreliminary:
    return RoomPreliminary(enabled=True, targets=targets, **kw)


async def setup(hass: HomeAssistant, lit: Lit, freezer, lights: FakeLights, *, lux: Any = 10,
                targets: tuple[PreliminaryTarget, ...] | None = None, active: bool = True,
                daylight: RoomDaylight | None = None, **kw: Any):
    """Noon, every light off, an indoor light sensor; preliminary light: the lamp at 10 %, 2200 K, 0.5 s."""
    _entry, ctrl = await start(hass, lit, freezer, lux=lux, active=active, daylight=daylight)
    targets = targets if targets is not None else (PreliminaryTarget(lit.lamp, 10, 2200, 0.5),)
    ctrl.store.async_set(ctrl.with_room(lit.area, preliminary=prelim(*targets, **kw)))
    await hass.async_block_till_done()
    await advance(hass, freezer)
    lights.clear()
    return _entry, ctrl


async def motion(hass: HomeAssistant, lit: Lit, freezer, wait: float = 2) -> None:
    hass.states.async_set(lit.pir, "on")
    await hass.async_block_till_done()
    await advance(hass, freezer, wait)


async def presence(hass: HomeAssistant, lit: Lit, freezer, wait: float = 2) -> None:
    hass.states.async_set(lit.radar, "on")
    await hass.async_block_till_done()
    await advance(hass, freezer, wait)


# ------------------------------------------------------------- detection and activation
async def test_possible_occupancy_switches_on_a_dim_light_then_hands_over(hass: HomeAssistant, lit: Lit, freezer,
                                                                           lights: FakeLights) -> None:
    """PIR alone: possible occupancy, the lamp at 10 % 2200 K; radar confirms: Dagur takes the lamp over."""
    _entry, ctrl = await setup(hass, lit, freezer, lights)
    await motion(hass, lit, freezer)
    assert state(ctrl, lit.area) == "possible_occupancy"
    assert calls_for(lights, lit.lamp) == [("turn_on", LAMP_10)]
    # The normal lighting waits for confirmation: nothing else is switched on.
    assert [c for c in lights.calls if c.data["entity_id"] != [lit.lamp]] == []
    d = log(ctrl, lit.area)[0]
    assert (d["action"], d["status"], d["mode"], d["kind"]) == (
        "Preliminary light on", "executed", "automatic", "preliminary"), d
    assert d["explanation"].startswith("Possible occupancy (binary_sensor.kno_pir detected motion), dark (10 lx, "
                                       "on below 50 lx): ")
    assert "kno_lamp at 10 %, 2200 K switched on as a preliminary light." in d["explanation"]
    assert "otherwise it is switched off after 30 s" in d["explanation"]
    assert ctrl.room_diagnostics(lit.area)["preliminary"]["stage"]["owned"] == {lit.lamp: LAMP_10}
    # A mood change meanwhile does not bring up the full scene, and does not touch the preliminary light.
    hass.states.async_set("input_select.main_mood", "Kvøld", hass.states.get("input_select.main_mood").attributes)
    await hass.async_block_till_done()
    await advance(hass, freezer)
    assert calls_for(lights, lit.group) == [] and calls_for(lights, lit.lamp) == [("turn_on", LAMP_10)]
    hass.states.async_set("input_select.main_mood", "Dagur", hass.states.get("input_select.main_mood").attributes)
    await hass.async_block_till_done()
    await advance(hass, freezer)
    lights.clear()

    await presence(hass, lit, freezer)
    assert state(ctrl, lit.area) == "confirmed_occupancy"
    # Dagur: one command for the lamp (the scene sets its final level), no switch-off.
    assert calls_for(lights, lit.lamp) == [("turn_on", {"brightness_pct": 75, "color_temp_kelvin": 3500})]
    assert calls_for(lights, lit.group)[0][0] == "turn_on"
    assert all(c.service == "turn_on" for c in lights.calls)
    entries = log(ctrl, lit.area)
    assert entries[0]["action"] == "Preliminary light handed over", entries[0]
    assert entries[0]["explanation"].startswith("Occupancy confirmed (")
    assert "kno_lamp now follows Dagur" in entries[0]["explanation"]
    assert entries[1]["action"] == "Lights on" and entries[1]["trigger"] == "occupancy_confirmed"
    assert ctrl.room_diagnostics(lit.area)["preliminary"]["stage"] is None
    # Nothing more: the timeout of the ended stage does not fire.
    lights.clear()
    await advance(hass, freezer, 60)
    assert lights.calls == []


async def test_multiple_lights_turn_off_on_confirmation_and_the_scene_wins(hass: HomeAssistant, lit: Lit, freezer,
                                                                            lights: FakeLights) -> None:
    """Two preliminary lights, Turn off on confirmation: the desk lamp (not in the scene) goes off after the
    normal lighting is on; the lamp, which Dagur switches on, stays with Dagur's level (precedence)."""
    targets = (PreliminaryTarget(lit.lamp, 10, 2200, 0.5, ConfirmAction.TURN_OFF),
               PreliminaryTarget(lit.desk, 25, None, None, ConfirmAction.TURN_OFF))
    _entry, ctrl = await setup(hass, lit, freezer, lights, targets=targets)
    ctrl.store.async_set(ctrl.with_room(lit.area, light_overrides={"Dagur": {lit.desk: _ignore()}}))
    await hass.async_block_till_done()
    await motion(hass, lit, freezer)
    assert calls_for(lights, lit.lamp) == [("turn_on", LAMP_10)]
    # Independent settings: 25 %, no colour temperature, the global default transition (desk: no transition support).
    assert calls_for(lights, lit.desk) == [("turn_on", {"brightness_pct": 25})]
    lights.clear()
    await presence(hass, lit, freezer)
    services = [(c.service, c.data["entity_id"]) for c in lights.calls]
    assert services[-1] == ("turn_off", [lit.desk]), services  # after the normal lighting: no dark moment
    assert all(s == "turn_on" for s, _ in services[:-1])
    assert calls_for(lights, lit.lamp) == [("turn_on", {"brightness_pct": 75, "color_temp_kelvin": 3500})]
    assert hass.states.is_state(lit.desk, "off") and hass.states.is_state(lit.lamp, "on")
    d = log(ctrl, lit.area)[0]
    assert (d["action"], d["status"]) == ("Preliminary light off", "executed"), d
    assert "kno_lamp now follows Dagur" in d["explanation"]
    assert "kno_desk switched off (preliminary light)" in d["explanation"]


def _ignore():
    from custom_components.smart_occupancy.models import ProfileSetting

    return ProfileSetting.from_dict({"action": "ignore"})


async def test_turn_off_keeps_the_light_when_nothing_else_is_on(hass: HomeAssistant, lit: Lit, freezer,
                                                                lights: FakeLights) -> None:
    """Turn off, but the normal lighting switched nothing on (a person keeps the spots off): the room is not left dark."""
    targets = (PreliminaryTarget(lit.desk, 25, None, None, ConfirmAction.TURN_OFF),)
    _entry, ctrl = await setup(hass, lit, freezer, lights, targets=targets)
    ctrl.store.async_set(ctrl.with_room(lit.area, light_overrides={"Dagur": {
        e: _ignore() for e in (lit.desk, lit.group, lit.lamp, lit.strip, lit.plug)}}))
    await hass.async_block_till_done()
    await motion(hass, lit, freezer)
    lights.clear()
    await presence(hass, lit, freezer)
    assert lights.calls == []
    d = log(ctrl, lit.area)[0]
    assert "kno_desk kept on: no other light of the room is on" in d["explanation"], d


# -------------------------------------------------------------------------- false detection
async def test_unconfirmed_motion_removes_only_the_preliminary_light(hass: HomeAssistant, lit: Lit, freezer,
                                                                    lights: FakeLights) -> None:
    """A short PIR blip: the lamp goes off after the 30 s timeout (later blips do not extend it); a light a
    person switched on meanwhile stays on; the vacancy action later sends nothing twice."""
    _entry, ctrl = await setup(hass, lit, freezer, lights)
    await motion(hass, lit, freezer, wait=3)
    hass.states.async_set(lit.pir, "off")
    await hass.async_block_till_done()
    await advance(hass, freezer, 2)
    assert state(ctrl, lit.area) == "possible_vacancy" and hass.states.is_state(lit.lamp, "on")
    # A person switches the LED strip on from the app: unrelated, it must stay on.
    hass.states.async_set(lit.strip, "on", {**hass.states.get(lit.strip).attributes, "brightness": 200},
                          context=Context(user_id="person"))
    await advance(hass, freezer, 10)
    # More motion inside the timeout: the same visit, no new stage, and the timeout is not extended.
    await motion(hass, lit, freezer, wait=1)
    hass.states.async_set(lit.pir, "off")
    await hass.async_block_till_done()
    await advance(hass, freezer, 5)
    assert hass.states.is_state(lit.lamp, "on")
    await advance(hass, freezer, 10)  # 31 s after the stage started
    assert calls_for(lights, lit.lamp) == [("turn_on", LAMP_10), ("turn_off", {"transition": 0.5})]
    assert hass.states.is_state(lit.strip, "on") and calls_for(lights, lit.strip) == []
    d = log(ctrl, lit.area)[0]
    assert (d["action"], d["status"], d["trigger"]) == ("Preliminary light off", "executed", "preliminary_end"), d
    assert d["explanation"] == ("Possible occupancy was not confirmed within 30 s: light.kno_lamp switched off "
                                "(preliminary light removed).")
    # The room becomes vacant later (exit confirmation: the visit was never confirmed): the lamp gets nothing
    # more. The strip is the vacancy action's (Turn off; the person moved in the room after switching it on).
    await advance(hass, freezer, 400)
    assert state(ctrl, lit.area) == "vacant"
    assert calls_for(lights, lit.lamp) == [("turn_on", LAMP_10), ("turn_off", {"transition": 0.5})]
    assert calls_for(lights, lit.strip) == [("turn_off", {})]


async def test_a_light_changed_by_a_person_is_left_alone(hass: HomeAssistant, lit: Lit, freezer,
                                                         lights: FakeLights) -> None:
    _entry, ctrl = await setup(hass, lit, freezer, lights)
    await motion(hass, lit, freezer)
    hass.states.async_set(lit.pir, "off")
    hass.states.async_set(lit.lamp, "on", {**hass.states.get(lit.lamp).attributes, "brightness": 255},
                          context=Context(user_id="person"))
    await advance(hass, freezer, 40)
    assert calls_for(lights, lit.lamp) == [("turn_on", LAMP_10)]
    assert hass.states.is_state(lit.lamp, "on")
    d = log(ctrl, lit.area)[0]
    assert d["action"] == "No change" and "Left as it is: light.kno_lamp (changed since)" in d["explanation"], d


async def test_door_only_evidence_ends_when_the_room_is_vacant(hass: HomeAssistant, pilot: Pilot, freezer,
                                                               lights: FakeLights) -> None:
    """Kamar Norður: the door is selected as preliminary evidence. A door alone never occupies the room;
    when the engine returns to vacant the preliminary light is removed at once (before its timeout)."""
    ctrl = await pilot_room(hass, pilot, freezer, signals=(pilot.door, pilot.pir))
    hass.states.async_set(pilot.door, "on")
    await hass.async_block_till_done()
    await advance(hass, freezer)
    assert state(ctrl, pilot.area) == "possible_occupancy"
    assert calls_for(lights, pilot.light) == [("turn_on", {"brightness_pct": 10})]
    d = log(ctrl, pilot.area)[0]
    assert d["explanation"].startswith("Possible occupancy (Door Kamar Norður moved)"), d
    await advance(hass, freezer, 10)
    assert state(ctrl, pilot.area) == "vacant"
    assert calls_for(lights, pilot.light) == [("turn_on", {"brightness_pct": 10}), ("turn_off", {})]
    d = log(ctrl, pilot.area)[0]
    assert d["explanation"].startswith("Possible occupancy ended without confirmation (the room is vacant): "
                                       "Spots Kamar Norður switched off"), d


# ---------------------------------------------------------------------------------- daylight
async def test_bright_room_skips_the_preliminary_light(hass: HomeAssistant, lit: Lit, freezer,
                                                       lights: FakeLights) -> None:
    _entry, ctrl = await setup(hass, lit, freezer, lights, lux=400)
    await motion(hass, lit, freezer)
    assert lights.calls == []
    d = log(ctrl, lit.area)[0]
    assert (d["action"], d["status"]) == ("Keep preliminary light off", "no_change"), d
    assert d["explanation"] == ("Possible occupancy (binary_sensor.kno_pir detected motion): preliminary lighting "
                                "skipped: daylight is enough (400 lx, on below 50 lx).")
    await presence(hass, lit, freezer)  # the normal lighting is not needed either
    assert lights.calls == []
    await advance(hass, freezer, 60)
    assert lights.calls == []


async def test_preliminary_threshold_inherits_or_overrides(hass: HomeAssistant, lit: Lit, freezer,
                                                            lights: FakeLights) -> None:
    """100 lx: bright for the room (on below 50 lx). A preliminary threshold of 200 lx switches the lamp on;
    on confirmation the normal lighting is not needed, so the lamp is switched off again (Hand over too)."""
    _entry, ctrl = await setup(hass, lit, freezer, lights, lux=100, on_below=200)
    await motion(hass, lit, freezer)
    assert calls_for(lights, lit.lamp) == [("turn_on", LAMP_10)]
    assert "dark (100 lx, on below 200 lx)" in log(ctrl, lit.area)[0]["explanation"]
    lights.clear()
    await presence(hass, lit, freezer)
    assert [(c.service, c.data["entity_id"]) for c in lights.calls] == [("turn_off", [lit.lamp])]
    d = log(ctrl, lit.area)[0]
    assert "kno_lamp switched off: the normal lighting is not needed (daylight is enough" in d["explanation"], d


async def test_unknown_lux_is_not_darkness(hass: HomeAssistant, lit: Lit, freezer, lights: FakeLights) -> None:
    """The indoor sensor is unavailable at noon: daylight is uncertain, and the room's fallback decides."""
    daylight = RoomDaylight(sensors=(IndoorSensor(LUX),), uncertain=UncertainPolicy.STAY_OFF)
    _entry, ctrl = await setup(hass, lit, freezer, lights, lux="unavailable", daylight=daylight)
    await motion(hass, lit, freezer)
    assert lights.calls == []
    d = log(ctrl, lit.area)[0]
    assert (d["status"], d["action"]) == ("blocked", "Keep preliminary light off"), d
    assert "daylight is uncertain" in d["explanation"] and "leaves lights off" in d["explanation"]


async def test_unknown_lux_with_the_turn_on_fallback(hass: HomeAssistant, lit: Lit, freezer,
                                                     lights: FakeLights) -> None:
    daylight = RoomDaylight(sensors=(IndoorSensor(LUX),), uncertain=UncertainPolicy.TURN_ON)
    _entry, ctrl = await setup(hass, lit, freezer, lights, lux="unknown", daylight=daylight)
    await motion(hass, lit, freezer)
    assert calls_for(lights, lit.lamp) == [("turn_on", LAMP_10)]
    assert "daylight is uncertain" in log(ctrl, lit.area)[0]["explanation"]


# ------------------------------------------------------------------------ modes and overrides
async def test_observing_sends_nothing(hass: HomeAssistant, lit: Lit, freezer, lights: FakeLights) -> None:
    _entry, ctrl = await setup(hass, lit, freezer, lights, active=False)
    await motion(hass, lit, freezer, wait=3)
    hass.states.async_set(lit.pir, "off")
    await hass.async_block_till_done()
    await advance(hass, freezer, 40)
    assert lights.calls == []
    entries = log(ctrl, lit.area)
    assert (entries[1]["action"], entries[1]["status"], entries[1]["mode"]) == (
        "Preliminary light on", "observed", "observing"), entries[1]
    assert "would switch on light.kno_lamp at 10 %, 2200 K as a preliminary light. Observing: nothing was sent." \
        in entries[1]["explanation"]
    assert (entries[0]["status"], entries[0]["explanation"]) == ("observed", (
        "Possible occupancy was not confirmed within 30 s: would switch off light.kno_lamp (preliminary light). "
        "Observing: nothing was sent."))


async def test_a_person_switched_the_room_off(hass: HomeAssistant, lit: Lit, freezer, lights: FakeLights) -> None:
    """The spots were switched off by a person (manual override): no preliminary light comes on."""
    _entry, ctrl = await setup(hass, lit, freezer, lights)
    attrs = hass.states.get(lit.group).attributes
    hass.states.async_set(lit.group, "on", attrs, context=Context(user_id="person"))
    await advance(hass, freezer)
    hass.states.async_set(lit.group, "off", attrs, context=Context(user_id="person"))
    await advance(hass, freezer)
    await motion(hass, lit, freezer)
    assert lights.calls == []
    d = log(ctrl, lit.area)[0]
    assert (d["action"], d["status"]) == ("Keep preliminary light off", "blocked"), d
    assert "lights of this room were switched off by a person" in d["explanation"]


async def test_manual_mood_pauses_preliminary_lighting(hass: HomeAssistant, lit: Lit, freezer,
                                                       lights: FakeLights) -> None:
    _entry, ctrl = await setup(hass, lit, freezer, lights)
    main = "input_select.main_mood"
    hass.states.async_set(main, "Manual", {"options": [*hass.states.get(main).attributes["options"], "Manual"]})
    await hass.async_block_till_done()
    await motion(hass, lit, freezer)
    assert lights.calls == []
    d = log(ctrl, lit.area)[0]
    assert (d["status"], d["mode"]) == ("blocked", "paused") and "Main Mood is Manual" in d["explanation"], d


async def test_unavailable_and_failing_lights(hass: HomeAssistant, lit: Lit, freezer, lights: FakeLights) -> None:
    targets = (PreliminaryTarget(lit.lamp, 10), PreliminaryTarget(lit.desk, 10))
    _entry, ctrl = await setup(hass, lit, freezer, lights, targets=targets)
    hass.states.async_set(lit.desk, "unavailable", {})

    async def fail(call: ServiceCall) -> None:
        raise HomeAssistantError("Zigbee timeout")

    hass.services.async_register("light", "turn_on", fail)
    await advance(hass, freezer)
    await motion(hass, lit, freezer)
    d = log(ctrl, lit.area)[0]
    assert (d["action"], d["status"]) == ("Preliminary light on", "failed"), d
    assert "no preliminary light was switched on. Failed: light.kno_lamp: Zigbee timeout." in d["explanation"]
    assert "light.kno_desk: blocked — Light is unavailable" in " ".join(d["details"]["targets"])
    # Nothing was switched on, so nothing is switched off.
    await advance(hass, freezer, 40)
    assert [c for c in lights.calls if c.service == "turn_off"] == []


# -------------------------------------------------------------------------- confirmation paths
async def test_immediate_confirmation_skips_the_preliminary_stage(hass: HomeAssistant, lit: Lit, freezer,
                                                                   lights: FakeLights) -> None:
    _entry, ctrl = await setup(hass, lit, freezer, lights)
    hass.states.async_set(lit.pir, "on")
    hass.states.async_set(lit.radar, "on")
    await hass.async_block_till_done()
    await advance(hass, freezer)
    assert state(ctrl, lit.area) == "confirmed_occupancy"
    assert calls_for(lights, lit.lamp) == [("turn_on", {"brightness_pct": 75, "color_temp_kelvin": 3500})]
    assert not [d for d in log(ctrl, lit.area) if d["kind"] == "preliminary"]


async def test_sustained_motion_confirms_without_a_second_sensor(hass: HomeAssistant, lit: Lit, freezer,
                                                                 lights: FakeLights) -> None:
    """A PIR that stays active for the correlation window confirms occupancy on its own (engine rule)."""
    _entry, ctrl = await setup(hass, lit, freezer, lights)
    await motion(hass, lit, freezer, wait=5)
    assert state(ctrl, lit.area) == "possible_occupancy" and hass.states.is_state(lit.group, "off")
    await advance(hass, freezer, 7)
    assert state(ctrl, lit.area) == "confirmed_occupancy"
    await advance(hass, freezer)  # (the normal lighting is applied after the short coalescing delay)
    assert hass.states.is_state(lit.group, "on")


async def test_disabled_or_without_lights_keeps_the_previous_behaviour(hass: HomeAssistant, lit: Lit, freezer,
                                                                       lights: FakeLights) -> None:
    """Global default enabled, but the room has no preliminary light: first motion switches the scene on, as before."""
    _entry, ctrl = await setup(hass, lit, freezer, lights, targets=())
    ctrl.store.async_set(LightingConfig.from_dict({**ctrl.lighting.as_dict(),
                                                   "preliminary": {"enabled": True}}))
    await hass.async_block_till_done()
    assert not ctrl.preliminary_settings(lit.area).active
    await motion(hass, lit, freezer)
    assert state(ctrl, lit.area) == "possible_occupancy"
    assert calls_for(lights, lit.lamp) == [("turn_on", {"brightness_pct": 75, "color_temp_kelvin": 3500})]
    assert calls_for(lights, lit.group)[0][0] == "turn_on"


# ------------------------------------------------------------------------ Kamar Norður regression
async def pilot_room(hass: HomeAssistant, p: Pilot, freezer, **kw: Any):
    """The live pilot's sensors (PIR, radar, Nobara PC, door) and its spots; Dagur 100 %; Automatic."""
    hass.states.async_set(p.light, "off", {"friendly_name": "Spots Kamar Norður", "supported_color_modes": ["color_temp"],
                                           "min_color_temp_kelvin": 2200, "max_color_temp_kelvin": 6500})
    entry = await setup_entry(hass, pilot_options(p, hall=False))
    ctrl = entry.runtime_data.lighting
    lighting = LightingConfig.from_dict({**ctrl.lighting.as_dict(), "global_profiles": {
        "Dagur": {"downlights": {"action": "on", "brightness_pct": 100, "color_temp_kelvin": 3500}}}})
    room = lighting.room(p.area)
    lighting = lighting.with_room(p.area, room.__class__(**{
        **{f: getattr(room, f) for f in room.__dataclass_fields__}, "control_enabled": True,
        "daylight": RoomDaylight.from_dict({"auto_on": "always"}),
        "preliminary": RoomPreliminary(enabled=True, targets=(PreliminaryTarget(p.light, 10),), **kw)}))
    ctrl.store.async_set(lighting)
    await hass.async_block_till_done()
    await advance(hass, freezer)
    return ctrl


async def test_pilot_pir_dims_radar_confirms(hass: HomeAssistant, pilot: Pilot, freezer, lights: FakeLights) -> None:
    """PIR: the spots at 10 % (not the full scene); radar confirms: Dagur 100 %."""
    ctrl = await pilot_room(hass, pilot, freezer)
    lights.clear()
    hass.states.async_set(pilot.pir, "on")
    await hass.async_block_till_done()
    await advance(hass, freezer)
    assert state(ctrl, pilot.area) == "possible_occupancy"
    assert calls_for(lights, pilot.light) == [("turn_on", {"brightness_pct": 10})]
    hass.states.async_set(pilot.radar, "on")
    await hass.async_block_till_done()
    await advance(hass, freezer)
    assert state(ctrl, pilot.area) == "confirmed_occupancy"
    assert calls_for(lights, pilot.light)[-1] == ("turn_on", {"brightness_pct": 100, "color_temp_kelvin": 3500})
    assert len(calls_for(lights, pilot.light)) == 2


async def test_pilot_nobara_activity_sustains_occupancy(hass: HomeAssistant, pilot: Pilot, freezer,
                                                        lights: FakeLights) -> None:
    """Radar is left out of preliminary evidence by default. PIR + Nobara confirm; Nobara alone keeps the
    room occupied (Maintain) long after the PIR cleared; when it stops, the vacancy action switches off."""
    ctrl = await pilot_room(hass, pilot, freezer)
    lights.clear()
    hass.states.async_set(pilot.pir, "on")
    hass.states.async_set(pilot.pc, "on")
    await hass.async_block_till_done()
    await advance(hass, freezer)
    assert state(ctrl, pilot.area) == "confirmed_occupancy"
    hass.states.async_set(pilot.pir, "off")
    await hass.async_block_till_done()
    await advance(hass, freezer, 1800)
    assert state(ctrl, pilot.area) == "confirmed_occupancy" and hass.states.is_state(pilot.light, "on")
    hass.states.async_set(pilot.pc, "off")
    await hass.async_block_till_done()
    await advance(hass, freezer, 700)
    assert state(ctrl, pilot.area) == "vacant" and hass.states.is_state(pilot.light, "off")
    assert [s for s, _ in calls_for(lights, pilot.light)] == ["turn_on", "turn_off"]


async def test_pilot_radar_selected_as_evidence(hass: HomeAssistant, pilot: Pilot, freezer, lights: FakeLights) -> None:
    """Radar selected: radar alone is possible occupancy (dim); after the correlation window it confirms."""
    ctrl = await pilot_room(hass, pilot, freezer, signals=(pilot.radar,))
    lights.clear()
    hass.states.async_set(pilot.radar, "on")
    await hass.async_block_till_done()
    await advance(hass, freezer)
    assert calls_for(lights, pilot.light) == [("turn_on", {"brightness_pct": 10})]
    await advance(hass, freezer, 10)
    assert state(ctrl, pilot.area) == "confirmed_occupancy"
    await advance(hass, freezer)
    assert calls_for(lights, pilot.light)[-1] == ("turn_on", {"brightness_pct": 100, "color_temp_kelvin": 3500})


async def test_pilot_wall_switch_still_overrides(hass: HomeAssistant, pilot: Pilot, freezer, lights: FakeLights) -> None:
    """A wall switch (listen-only entity) switching the spots off during the stage is a person's change:
    the stage leaves them alone at its end."""
    ctrl = await pilot_room(hass, pilot, freezer)
    switch = "sensor.kno_wall_switch_action"
    hass.states.async_set(switch, "")
    ctrl.store.async_set(ctrl.with_room(pilot.area, manual_switches=(switch,)))
    await hass.async_block_till_done()
    lights.clear()
    hass.states.async_set(pilot.pir, "on")
    await hass.async_block_till_done()
    await advance(hass, freezer)
    assert hass.states.is_state(pilot.light, "on")
    ctx = Context()
    hass.states.async_set(switch, "off", context=ctx)
    hass.states.async_set(pilot.light, "off", hass.states.get(pilot.light).attributes, context=ctx)
    await hass.async_block_till_done()
    overrides = ctrl.runtime[pilot.area].overrides
    assert overrides[pilot.light]["source"] == "wall_switch"
    await advance(hass, freezer, 40)
    assert [s for s, _ in calls_for(lights, pilot.light)] == ["turn_on"]


# --------------------------------------------------------------- persistence, migration, websocket
async def test_stage_survives_a_restart(hass: HomeAssistant, lit: Lit, freezer, lights: FakeLights,
                                        hass_storage) -> None:
    entry, ctrl = await setup(hass, lit, freezer, lights)
    await motion(hass, lit, freezer, wait=3)
    hass.states.async_set(lit.pir, "off")
    await hass.async_block_till_done()
    await advance(hass, freezer, 2)
    assert hass.states.is_state(lit.lamp, "on")
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    ctrl = entry.runtime_data.lighting
    room = ctrl.lighting.room(lit.area).preliminary
    assert room.enabled is True and room.targets[0] == PreliminaryTarget(lit.lamp, 10, 2200, 0.5)
    assert hass_storage["smart_occupancy.lighting"]["minor_version"] == 5
    assert ctrl.runtime[lit.area].preliminary["owned"] == {lit.lamp: LAMP_10}
    await advance(hass, freezer, 30)
    assert calls_for(lights, lit.lamp)[-1] == ("turn_off", {"transition": 0.5})
    assert ctrl.runtime[lit.area].preliminary is None


def test_migration_adds_disabled_settings() -> None:
    stored = {"rooms": {"kamar": {"control_enabled": True, "daylight": {"on_first_motion": True},
                                  "switches": [], "vacancy": {"action": "turn_off", "mood": None}}}}
    migrated = migrate_store(1, 4, stored)
    assert migrated["preliminary"] == PreliminaryDefaults().as_dict()
    assert migrated["preliminary"]["enabled"] is False
    assert migrated["rooms"]["kamar"]["preliminary"] == RoomPreliminary().as_dict()
    config = LightingConfig.from_dict(migrated)
    assert config.room("kamar").preliminary.targets == () and config.room("kamar").control_enabled
    assert LightingConfig.from_dict(config.as_dict()) == config


def test_settings_are_validated() -> None:
    t = PreliminaryTarget.from_dict({"entity_id": "light.a", "brightness_pct": 400, "color_temp_kelvin": 100,
                                     "transition": -3, "on_confirm": "nonsense"})
    assert (t.brightness_pct, t.color_temp_kelvin, t.transition, t.on_confirm) == (100, 1500, 0, None)
    room = RoomPreliminary.from_dict({"enabled": "yes", "timeout": 1, "targets": ["light.a", {"entity_id": "light.a"},
                                                                                  {"x": 1}], "signals": "nope"})
    assert room.enabled is None and room.timeout == 5 and [x.entity_id for x in room.targets] == ["light.a"]
    assert room.signals is None
    assert PreliminaryDefaults.from_dict({"timeout": "x", "on_below": 9999}).timeout == 30


async def test_websocket_room_and_global_settings(hass: HomeAssistant, lit: Lit, freezer, lights: FakeLights,
                                                  hass_ws_client, hass_admin_user) -> None:
    _entry, ctrl = await setup(hass, lit, freezer, lights, targets=())
    refresh = await hass.auth.async_create_refresh_token(hass_admin_user, "https://example.com/app")
    ws = WS(await hass_ws_client(hass, access_token=hass.auth.async_create_access_token(refresh)))
    t = "smart_occupancy/lighting/preliminary/set"
    reply = await ws(type=t, area_id=lit.area, targets=[{"entity_id": lit.lamp, "brightness_pct": 15}],
                     timeout=20, signals=[lit.pir, lit.radar])
    assert reply["success"], reply
    # Global defaults: a room's own value is not overwritten; inherited values follow.
    assert (await ws(type=t, timeout=45, brightness_pct=5, transition=1, on_below=80))["success"]
    eff = ctrl.preliminary_settings(lit.area)
    assert eff.timeout == 20 and eff.sources["timeout"] == "room" and eff.on_below == 80
    target = eff.target(lit.lamp)
    assert (target.brightness_pct, target.transition, target.sources["transition"]) == (15, 1.0, "global")
    assert eff.signals == (lit.pir, lit.radar) and eff.active
    # Back to the global default.
    assert (await ws(type=t, area_id=lit.area, timeout=None))["success"]
    assert ctrl.preliminary_settings(lit.area).timeout == 45
    model = (await ws(type="smart_occupancy/lighting/editor", area_id=lit.area))["result"]["room"]["preliminary"]
    assert model["effective"]["sources"]["timeout"] == "global" and model["normal_on_below"] == 50
    assert {c["entity_id"] for c in model["candidates"]} >= {lit.lamp, lit.desk, lit.group}
    assert model["targets"][0]["scene"]["switches_on"] is True
    assert [s["entity_id"] for s in model["sensors"] if s["default"]] == [lit.pir]
    # Errors: a light of another room, a signal of another room, a value for the wrong level.
    assert (await ws(type=t, area_id=lit.area, targets=[{"entity_id": lit.other_lamp}]))["error"]["code"] == \
        "invalid_format"
    assert not (await ws(type=t, area_id=lit.area, signals=["binary_sensor.elsewhere"]))["success"]
    assert not (await ws(type=t, area_id=lit.area, brightness_pct=20))["success"]
    assert not (await ws(type=t, enabled=None))["success"]
