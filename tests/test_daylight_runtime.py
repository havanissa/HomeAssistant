"""Daylight-aware occupancy activation in a running Home Assistant (Phase 0.3.3)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from custom_components.smart_occupancy.const import AutoOn, Darkness, ReportMode, UncertainPolicy
from custom_components.smart_occupancy.daylight import DaylightConfig, IndoorSensor, RoomDaylight

from .conftest import setup_entry
from .test_adaptive_runtime import FakeLights
from .test_lighting_runtime import CT, MAIN, PROFILES, Lit, lit, options, set_moods  # noqa: F401



def _next_utc_hour(hour: int):
    """The next time with this UTC hour, at least an hour ahead: the clock only moves forward."""
    now = dt_util.utcnow()
    when = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    while when <= now + timedelta(hours=1):
        when += timedelta(days=1)
    return when


DAY = _next_utc_hour(20)              # about noon in the test location (US/Pacific): sun high
NIGHT = DAY + timedelta(hours=12)     # about midnight
LUX = "sensor.kno_lux"
PIR_LUX = "sensor.kno_pir_lux"
OUTDOOR = "sensor.outdoor_lux"


@pytest.fixture
def lights(hass: HomeAssistant) -> FakeLights:
    return FakeLights(hass)


async def advance(hass: HomeAssistant, freezer, seconds: float = 2) -> None:
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


def set_lux(hass: HomeAssistant, value: Any, entity: str = LUX, **attrs: Any) -> None:
    hass.states.async_set(entity, str(value), {"unit_of_measurement": "lx", "device_class": "illuminance",
                                               "friendly_name": entity.split(".")[1], **attrs})


def all_off(hass: HomeAssistant, lit: Lit) -> None:
    hass.states.async_set(lit.group, "off", {**CT, "entity_id": [lit.spot_1, lit.spot_2], "supported_features": 32})
    for e in (lit.spot_1, lit.spot_2):
        hass.states.async_set(e, "off", CT)


def lux_entities(hass: HomeAssistant, lit: Lit) -> None:
    ent_reg = er.async_get(hass)
    for object_id in ("kno_lux", "kno_pir_lux", "outdoor_lux"):
        entry = ent_reg.async_get_or_create("sensor", "test", f"uid_{object_id}", suggested_object_id=object_id,
                                            original_device_class="illuminance")
        if object_id != "outdoor_lux":
            ent_reg.async_update_entity(entry.entity_id, area_id=lit.area)


async def start(hass: HomeAssistant, lit: Lit, freezer, *, when=DAY, lux: Any = None,
                daylight: RoomDaylight | None = None, glob: DaylightConfig | None = None, active: bool = True,
                **room: Any):
    freezer.move_to(when)
    lux_entities(hass, lit)
    all_off(hass, lit)
    await advance(hass, freezer, 120)  # the lights have been off for a while ...
    if lux is not None:
        set_lux(hass, lux)  # ... before this value was measured
    await advance(hass, freezer, 600)
    entry = await setup_entry(hass, options(lit))
    ctrl = entry.runtime_data.lighting
    config = ctrl.lighting
    if glob is not None:
        config = config.__class__(**{**{f: getattr(config, f) for f in config.__dataclass_fields__},
                                     "daylight": glob})
        ctrl.store.async_set(config)
    ctrl.store.async_set(ctrl.with_room(lit.area, control_enabled=active,
                                        daylight=daylight or RoomDaylight(sensors=(IndoorSensor(LUX),)), **room))
    await hass.async_block_till_done()
    await advance(hass, freezer)
    return entry, ctrl


async def occupy(hass: HomeAssistant, lit: Lit, freezer, *, radar: bool = True, wait: float = 2) -> None:
    hass.states.async_set(lit.pir, "on")
    if radar:
        hass.states.async_set(lit.radar, "on")
    await hass.async_block_till_done()
    await advance(hass, freezer, wait)


def daylight_state(hass: HomeAssistant):
    return hass.states.get("sensor.smart_occupancy_kamar_nordur_daylight")


def turned_on(lights: FakeLights) -> set[str]:
    return {e for e, (service, _) in lights.by_entity().items() if service == "turn_on"}


async def test_sun_positions_used_by_the_tests(hass: HomeAssistant, lit: Lit, freezer) -> None:
    _entry, ctrl = await start(hass, lit, freezer, when=DAY)
    assert ctrl.daylight.status(lit.area).sun.elevation > 20
    freezer.move_to(NIGHT)
    assert ctrl.daylight.evaluate(lit.area).sun.dark


# 1, 16 ------------------------------------------------------------------------
async def test_dark_room_turns_on_permitted_lights(hass: HomeAssistant, lit: Lit, freezer, lights) -> None:
    _entry, ctrl = await start(hass, lit, freezer, lux=12)
    assert daylight_state(hass).state == "dark"
    assert daylight_state(hass).attributes["natural_light"] == "Natural light: 10 lx — measured indoors, lights off"
    await occupy(hass, lit, freezer)
    on = turned_on(lights)
    # Dagur: downlights (as the group, not its members), lamps and the LED strip are On.
    assert on == {lit.group, lit.lamp, lit.desk, lit.plug, lit.strip}
    rt = ctrl.runtime[lit.area]
    assert rt.activation["result"] == "turned_on" and rt.activation["lights"] == 5
    assert rt.activation["text"].startswith("Turned on 5 lights (occupancy confirmed) · Dark: Natural light")
    assert rt.last_activation_at
    assert daylight_state(hass).attributes["activation"].startswith("Turned on 5 lights")


async def test_mood_ignore_and_off_with_daylight(hass: HomeAssistant, lit: Lit, freezer, lights) -> None:
    """Kvøld ignores the strip; Hygge switches downlights off even in daylight (Off is never gated)."""
    set_moods(hass, "Kvøld", "Kvøld", "Kvøld")
    set_lux(hass, 20, OUTDOOR)
    _entry, ctrl = await start(hass, lit, freezer, daylight=RoomDaylight(factor=0.05),
                               glob=DaylightConfig(outdoor_entity=OUTDOOR))
    await occupy(hass, lit, freezer)
    assert lit.strip not in turned_on(lights) and lit.lamp in turned_on(lights)
    # Bright now (estimated from outdoor light), the downlights are on: Hygge switches them off,
    # and no lamp that is off is switched on.
    lights.clear()
    for e in (lit.lamp, lit.desk, lit.plug):
        hass.states.async_set(e, "off", CT)
    set_lux(hass, 20000, OUTDOOR)
    await advance(hass, freezer, 400)
    assert ctrl.daylight.status(lit.area).decision == Darkness.BRIGHT
    assert lights.calls == []
    set_moods(hass, "Hygge", "Hygge", "Hygge")
    await hass.async_block_till_done()
    await advance(hass, freezer)
    by = lights.by_entity()
    assert by[lit.group][0] == "turn_off"
    assert not turned_on(lights)
    plan = ctrl.plan(lit.area)
    assert {i.entity_id: i.reason_code for i in plan.items}[lit.lamp] == "daylight"


# 2 ----------------------------------------------------------------------------
async def test_bright_room_does_not_turn_on(hass: HomeAssistant, lit: Lit, freezer, lights) -> None:
    _entry, ctrl = await start(hass, lit, freezer, lux=400)
    await occupy(hass, lit, freezer)
    assert lights.calls == []
    rt = ctrl.runtime[lit.area]
    assert rt.activation["result"] == "not_needed"
    assert "daylight is enough" in rt.activation["text"] and "400 lx" in rt.activation["text"]
    assert rt.last_activation_at is None
    item = next(i for i in ctrl.plan(lit.area).items if i.entity_id == lit.lamp)
    assert item.decision == "blocked" and item.reason_code == "daylight" and "on below 50 lx" in item.reason


# 3, 4 -------------------------------------------------------------------------
async def test_pir_switches_on_promptly_and_radar_holds(hass: HomeAssistant, lit: Lit, freezer, lights) -> None:
    _entry, ctrl = await start(hass, lit, freezer, lux=5)
    hass.states.async_set(lit.pir, "on")  # PIR only: possible occupancy, not yet confirmed
    await hass.async_block_till_done()
    await advance(hass, freezer, 1.5)
    assert ctrl.coordinator.occupancy.result(lit.area).state.value == "possible_occupancy"
    assert lit.lamp in turned_on(lights)
    assert ctrl.runtime[lit.area].activation["trigger"] == "motion"
    first = len(lights.calls)
    # Radar keeps the room occupied after the PIR clears; confirmation sends nothing new.
    hass.states.async_set(lit.radar, "on")
    await advance(hass, freezer, 15)
    hass.states.async_set(lit.pir, "off")
    await advance(hass, freezer, 900)
    assert ctrl.coordinator.occupancy.result(lit.area).occupied is True
    assert len(lights.calls) == first
    assert ctrl.runtime[lit.area].activation["result"] == "turned_on"  # not overwritten by the confirmation


async def test_conservative_activation_waits_for_confirmation(hass: HomeAssistant, lit: Lit, freezer,
                                                              lights) -> None:
    daylight = RoomDaylight(sensors=(IndoorSensor(LUX),), on_first_motion=False)
    _entry, ctrl = await start(hass, lit, freezer, lux=5, daylight=daylight)
    hass.states.async_set(lit.pir, "on")
    await advance(hass, freezer, 2)
    assert lights.calls == []
    await advance(hass, freezer, 10)  # the PIR stayed active for the correlation window: confirmed
    await advance(hass, freezer, 2)
    assert lit.lamp in turned_on(lights)
    assert ctrl.runtime[lit.area].activation["trigger"] == "occupancy_confirmed"


# 5, 6 -------------------------------------------------------------------------
async def test_light_on_readings_are_rejected_and_settling(hass: HomeAssistant, lit: Lit, freezer, lights) -> None:
    _entry, ctrl = await start(hass, lit, freezer, lux=20)
    await occupy(hass, lit, freezer)
    assert lit.lamp in turned_on(lights)
    # The lamps light the sensor: 350 lx. That is never taken as daylight.
    set_lux(hass, 350)
    await advance(hass, freezer, 300)
    status = ctrl.daylight.status(lit.area)
    assert status.contamination.state == "lit" and status.decision == Darkness.DARK
    assert status.trusted.lux == 20  # the last trustworthy unlit measurement is kept
    assert status.estimate.basis == "recent_indoor"
    # Lights off: the old (lit) value is still not a measurement, during and after the settling period.
    lights.clear()
    for e in (lit.group, lit.lamp, lit.desk, lit.plug, lit.strip):
        hass.states.async_set(e, "off", CT)
    await advance(hass, freezer, 5)
    assert ctrl.daylight.status(lit.area).contamination.state == "settling"
    await advance(hass, freezer, 120)
    status = ctrl.daylight.status(lit.area)
    assert status.contamination.state == "clear" and status.trusted.lux == 20
    # A new reading after the settling period is trusted again.
    set_lux(hass, 18)
    await advance(hass, freezer, 5)
    status = ctrl.daylight.status(lit.area)
    assert status.estimate.basis == "indoor" and status.trusted.lux == 18
    assert lights.calls == []  # nothing was switched off because of the bright reading


async def test_lamp_switched_on_by_another_automation_while_it_gets_dark(hass: HomeAssistant, lit: Lit, freezer,
                                                                       lights) -> None:
    """Real-instance finding: an existing automation's lamp lights the sensor as it gets dark."""
    _entry, ctrl = await start(hass, lit, freezer, lux=300,
                               daylight=RoomDaylight(sensors=(IndoorSensor(LUX),), stable_seconds=5))
    await occupy(hass, lit, freezer)
    assert lights.calls == []  # bright
    hass.states.async_set(lit.other_lamp, "on", CT)  # not ours, but listed as influencing
    ctrl.store.async_set(ctrl.with_room(lit.area, daylight=RoomDaylight(
        sensors=(IndoorSensor(LUX),), stable_seconds=5, influencing=(lit.other_lamp,))))
    await advance(hass, freezer, 5)
    set_lux(hass, 12)  # dark, although that lamp is on
    for _ in range(5):
        await advance(hass, freezer, 5)
    assert lit.lamp in turned_on(lights)
    assert ctrl.runtime[lit.area].activation["trigger"] == "became_dark"
    set_lux(hass, 450)  # our lamps light the sensor now
    await advance(hass, freezer, 30)
    status = ctrl.daylight.status(lit.area)
    assert status.decision == Darkness.DARK and status.estimate.basis == "recent_bound"


# 7, 8 -------------------------------------------------------------------------
async def test_outdoor_darkness_fallback(hass: HomeAssistant, lit: Lit, freezer, lights) -> None:
    set_lux(hass, 8, OUTDOOR)
    _entry, ctrl = await start(hass, lit, freezer, daylight=RoomDaylight(),
                               glob=DaylightConfig(outdoor_entity=OUTDOOR))
    status = ctrl.daylight.status(lit.area)
    assert status.estimate.text == "Natural light: Low — estimated from outdoor darkness"
    await occupy(hass, lit, freezer)
    assert lit.lamp in turned_on(lights)


async def test_bright_outside_but_naturally_dark_room(hass: HomeAssistant, lit: Lit, freezer, lights) -> None:
    set_lux(hass, 25000, OUTDOOR)
    _entry, ctrl = await start(hass, lit, freezer, lux=15, glob=DaylightConfig(outdoor_entity=OUTDOOR))
    assert ctrl.daylight.status(lit.area).decision == Darkness.DARK
    await occupy(hass, lit, freezer)
    assert lit.lamp in turned_on(lights)


async def test_bright_outside_without_room_factor_is_uncertain(hass: HomeAssistant, lit: Lit, freezer,
                                                              lights) -> None:
    set_lux(hass, 25000, OUTDOOR)
    daylight = RoomDaylight(uncertain=UncertainPolicy.STAY_OFF)
    _entry, ctrl = await start(hass, lit, freezer, daylight=daylight, glob=DaylightConfig(outdoor_entity=OUTDOOR))
    status = ctrl.daylight.status(lit.area)
    assert status.decision == Darkness.UNCERTAIN and "share is not known" in status.estimate.text
    await occupy(hass, lit, freezer)
    assert lights.calls == []
    assert ctrl.runtime[lit.area].activation["result"] == "uncertain"


# 9 ----------------------------------------------------------------------------
async def test_frequent_fp2_updates_cause_no_commands(hass: HomeAssistant, lit: Lit, freezer, lights) -> None:
    daylight = RoomDaylight(sensors=(IndoorSensor(LUX, report_mode=ReportMode.REPORTS),))
    _entry, ctrl = await start(hass, lit, freezer, lux=300, daylight=daylight)
    await occupy(hass, lit, freezer)
    assert lights.calls == []
    notified: list[int] = []
    from homeassistant.helpers.dispatcher import async_dispatcher_connect

    from custom_components.smart_occupancy.const import SIGNAL_LIGHTING_UPDATED

    unsub = async_dispatcher_connect(hass, SIGNAL_LIGHTING_UPDATED, lambda: notified.append(1))
    for i in range(40):
        set_lux(hass, 300 + (i % 3))
        await advance(hass, freezer, 1)
    unsub()
    assert lights.calls == [] and ctrl.daylight.status(lit.area).decision == Darkness.BRIGHT
    assert len(notified) <= 2  # rounded values: the dashboards are not refreshed for noise
    # A stable value keeps reporting: it is not stale (no limit configured).
    await advance(hass, freezer, 6 * 3600)
    assert ctrl.daylight.status(lit.area).readings[0].usable


# 10, 11, 12 -------------------------------------------------------------------
async def test_infrequent_pir_lux_stale_and_fallback(hass: HomeAssistant, lit: Lit, freezer, lights) -> None:
    set_lux(hass, 5, OUTDOOR)
    daylight = RoomDaylight(sensors=(IndoorSensor(PIR_LUX, max_age=60),))
    _entry, ctrl = await start(hass, lit, freezer, daylight=daylight, glob=DaylightConfig(outdoor_entity=OUTDOOR))
    set_lux(hass, 300, PIR_LUX)  # reported with a motion event
    await advance(hass, freezer, 5)
    assert ctrl.daylight.status(lit.area).estimate.basis == "indoor"
    await advance(hass, freezer, 3700)  # no report for over an hour
    status = ctrl.daylight.status(lit.area)
    assert status.readings[0].status == "stale" and status.estimate.basis == "outdoor_dark"
    await ctrl.coordinator.async_refresh()
    problems = {p.code: p for p in ctrl.coordinator.data.problems if p.area_id == lit.area}
    assert problems["daylight_sensor_unavailable"].aspect == "daylight"


async def test_unknown_and_unavailable_sensors(hass: HomeAssistant, lit: Lit, freezer, lights) -> None:
    daylight = RoomDaylight(sensors=(IndoorSensor(LUX), IndoorSensor(PIR_LUX)))
    _entry, ctrl = await start(hass, lit, freezer, lux="unavailable", daylight=daylight)
    set_lux(hass, 400, PIR_LUX)
    await advance(hass, freezer, 5)
    status = ctrl.daylight.status(lit.area)
    assert status.chosen.entity_id == PIR_LUX and status.decision == Darkness.BRIGHT  # fallback sensor
    set_lux(hass, "unknown", PIR_LUX)
    await advance(hass, freezer, 5)
    status = ctrl.daylight.status(lit.area)
    assert status.estimate.lux is None or status.estimate.basis == "recent_indoor"  # never 0 lx
    await advance(hass, freezer, 3600)
    status = ctrl.daylight.status(lit.area)
    assert status.decision == Darkness.UNCERTAIN and status.estimate.lux is None
    # Default fallback: switch on as before 0.3.3, and say why.
    await occupy(hass, lit, freezer)
    assert lit.lamp in turned_on(lights)
    assert "Uncertain" in ctrl.runtime[lit.area].activation["text"]
    # Sensor problems are daylight health, never occupancy health.
    await ctrl.coordinator.async_refresh()
    from custom_components.smart_occupancy.health import room_health

    health = room_health(ctrl.coordinator.data, lit.area, ctrl.coordinator.occupancy.result(lit.area))
    assert health["daylight"]["status"] == "warning" and health["occupancy"]["status"] == "healthy"
    assert health["configuration"]["status"] == "healthy"


# 13, 14 -----------------------------------------------------------------------
async def test_outdoor_spike_and_hysteresis_and_becoming_dark(hass: HomeAssistant, lit: Lit, freezer,
                                                             lights) -> None:
    set_lux(hass, 10000, OUTDOOR)
    daylight = RoomDaylight(factor=0.012, stable_seconds=90)  # about 120 lx now
    _entry, ctrl = await start(hass, lit, freezer, daylight=daylight, glob=DaylightConfig(outdoor_entity=OUTDOOR))
    await occupy(hass, lit, freezer)
    assert lights.calls == [] and ctrl.daylight.status(lit.area).decision == Darkness.BRIGHT
    # A short dip (a passing shadow on the sensor) changes nothing.
    set_lux(hass, 900, OUTDOOR)
    await advance(hass, freezer, 20)
    set_lux(hass, 10000, OUTDOOR)
    await advance(hass, freezer, 200)
    assert lights.calls == []
    # Between the thresholds (70 lx): still bright (hysteresis).
    set_lux(hass, 5800, OUTDOOR)
    await advance(hass, freezer, 600)
    assert lights.calls == [] and ctrl.daylight.status(lit.area).decision == Darkness.BRIGHT
    # Below 50 lx for longer than the smoothing window and the stability period: the occupied room becomes dark.
    set_lux(hass, 3000, OUTDOOR)
    await advance(hass, freezer, 100)
    assert lights.calls == []  # not yet: smoothing and stability
    for _ in range(6):
        await advance(hass, freezer, 60)
    assert lit.lamp in turned_on(lights)
    assert ctrl.runtime[lit.area].activation["trigger"] == "became_dark"
    count = len(lights.calls)
    # Noise around the threshold does not repeat commands.
    for value in (4400, 3500, 4300, 3600):
        set_lux(hass, value, OUTDOOR)
        await advance(hass, freezer, 120)
    assert len(lights.calls) == count


# 15 ---------------------------------------------------------------------------
async def test_occupancy_uncertainty_and_conflicting_evidence(hass: HomeAssistant, lit: Lit, freezer,
                                                             lights) -> None:
    _entry, ctrl = await start(hass, lit, freezer, lux=5)
    # Radar alone (a presence sensor, not a trigger) waits for confirmation.
    hass.states.async_set(lit.radar, "on")
    await advance(hass, freezer, 2)
    assert lights.calls == []
    hass.states.async_set(lit.radar, "off")
    await advance(hass, freezer, 1000)
    # Every occupancy sensor unavailable: uncertain, nothing is switched on.
    hass.states.async_set(lit.pir, "unavailable")
    hass.states.async_set(lit.radar, "unavailable")
    await advance(hass, freezer, 5)
    assert ctrl.coordinator.occupancy.result(lit.area).state.value == "uncertain"
    assert lights.calls == []


# 17 ---------------------------------------------------------------------------
async def test_global_brightness_is_used_when_switching_on(hass: HomeAssistant, lit: Lit, freezer,
                                                           lights) -> None:
    hass.states.async_set("input_number.global_light_settings", "62", {"unit_of_measurement": "%"})
    hass.states.async_set("input_number.global_light_temprature", "3100", {"unit_of_measurement": "K"})
    _entry, ctrl = await start(hass, lit, freezer, lux=5)
    from dataclasses import replace

    from custom_components.smart_occupancy.lighting_config import AdaptiveConfig, set_profile
    from custom_components.smart_occupancy.models import ProfileSetting

    config = replace(ctrl.lighting, adaptive=AdaptiveConfig(brightness_entity="input_number.global_light_settings",
                                                            color_temp_entity="input_number.global_light_temprature"))
    config = set_profile(config, "global", "Dagur", "lamps", ProfileSetting.from_dict(
        {"action": "on", "brightness_mode": "global", "color_temp_mode": "global"}))
    ctrl.store.async_set(config)
    await advance(hass, freezer)
    await occupy(hass, lit, freezer)
    data = lights.by_entity()[lit.lamp][1]
    assert data["brightness_pct"] == 62 and data["color_temp_kelvin"] == 3100


# 18, 19 -----------------------------------------------------------------------
async def test_manual_mode_and_observe(hass: HomeAssistant, lit: Lit, freezer, lights) -> None:
    set_moods(hass, "Manual", "Manual", "Manual", main_opts=["Dagur", "Manual"])
    _entry, ctrl = await start(hass, lit, freezer, lux=5)
    await occupy(hass, lit, freezer)
    assert lights.calls == []
    assert ctrl.runtime[lit.area].activation["result"] == "manual"


async def test_observe_room_reports_what_would_happen(hass: HomeAssistant, lit: Lit, freezer, lights) -> None:
    _entry, ctrl = await start(hass, lit, freezer, lux=5, active=False)
    await occupy(hass, lit, freezer)
    assert lights.calls == []
    activation = ctrl.runtime[lit.area].activation
    assert activation["result"] == "observe" and activation["text"].startswith("Observe: would turn on 5 lights")
    assert daylight_state(hass).state == "dark"


# 20, 21 -----------------------------------------------------------------------
async def test_manual_override_and_conflicts_are_respected(hass: HomeAssistant, lit: Lit, freezer, lights,
                                                           hass_admin_user) -> None:
    hass.states.async_set("input_boolean.trigger", "off")
    assert await async_setup_component(hass, "automation", {"automation": [
        {"id": "a1", "alias": "Desk on", "triggers": [{"trigger": "state", "entity_id": "input_boolean.trigger"}],
         "actions": [{"action": "light.turn_on", "target": {"entity_id": lit.desk}}]},
    ]})
    _entry, ctrl = await start(hass, lit, freezer, lux=300)
    await occupy(hass, lit, freezer)
    assert lights.calls == []  # bright
    # Someone in the room switched the lamp on and off again: that is respected.
    hass.states.async_set(lit.lamp, "on", {**CT, "brightness": 40}, context=Context(user_id=hass_admin_user.id))
    await hass.async_block_till_done()
    hass.states.async_set(lit.lamp, "off", CT, context=Context(user_id=hass_admin_user.id))
    await advance(hass, freezer, 120)
    assert lit.lamp in ctrl.runtime[lit.area].overrides
    set_lux(hass, 10)  # it gets dark
    for _ in range(8):
        await advance(hass, freezer, 30)
    assert ctrl.runtime[lit.area].activation["trigger"] == "became_dark"
    on = turned_on(lights)
    assert lit.lamp not in on and lit.desk not in on  # manual override; unacknowledged automation
    assert lit.plug in on and lit.group in on
    assert hass.states.get("automation.desk_on").state == "on"  # never changed


# 22 ---------------------------------------------------------------------------
async def test_ambient_lighting_is_never_gated(hass: HomeAssistant, lit: Lit, freezer, lights) -> None:
    from custom_components.smart_occupancy.lighting_config import set_profile
    from custom_components.smart_occupancy.models import ProfileSetting

    _entry, ctrl = await start(hass, lit, freezer, lux=900)
    ctrl.store.async_set(set_profile(ctrl.lighting, "global", "Kvøld", "lamps", ProfileSetting.from_dict(
        {"action": "on", "brightness_pct": 30, "persistent": True})))
    await advance(hass, freezer)
    set_moods(hass, "Kvøld", "Kvøld", "Kvøld")  # vacant room, bright daylight
    await hass.async_block_till_done()
    await advance(hass, freezer)
    assert turned_on(lights) == {lit.lamp, lit.desk, lit.plug}  # ambient lamps follow the mood
    assert all(c.domain == "light" for c in lights.calls)


# 23 ---------------------------------------------------------------------------
async def test_group_is_commanded_once_not_its_members(hass: HomeAssistant, lit: Lit, freezer, lights) -> None:
    _entry, _ctrl = await start(hass, lit, freezer, lux=5)
    await occupy(hass, lit, freezer)
    assert lights.count(lit.group) == 1
    assert lights.count(lit.spot_1) == lights.count(lit.spot_2) == 0


# 24 ---------------------------------------------------------------------------
async def test_dashboard_settings_persist_and_samples_survive_restart(hass: HomeAssistant, lit: Lit, freezer,
                                                                     lights, hass_ws_client, hass_admin_user,
                                                                     hass_storage) -> None:
    set_lux(hass, 9000, OUTDOOR)
    entry, ctrl = await start(hass, lit, freezer, lux=45, daylight=RoomDaylight())
    # A token issued now (the clock moved forward).
    refresh = await hass.auth.async_create_refresh_token(hass_admin_user, "https://example.com/app")
    client = await hass_ws_client(hass, access_token=hass.auth.async_create_access_token(refresh))
    await client.send_json_auto_id({"type": "smart_occupancy/lighting/daylight/set", "outdoor_entity": OUTDOOR})
    assert (await client.receive_json())["success"]
    await client.send_json_auto_id({"type": "smart_occupancy/lighting/room/daylight", "area_id": lit.area,
                                    "sensors": [{"entity_id": LUX}, {"entity_id": PIR_LUX, "max_age": 90}],
                                    "on_below": 60, "uncertain": "stay_off", "influencing": ["light.stova_lamp"]})
    assert (await client.receive_json())["success"]
    # Validation: thresholds, entity types, admin only.
    await client.send_json_auto_id({"type": "smart_occupancy/lighting/room/daylight", "area_id": lit.area,
                                    "on_below": 100, "off_above": 90})
    assert (await client.receive_json())["error"]["code"] == "invalid_format"
    await client.send_json_auto_id({"type": "smart_occupancy/lighting/room/daylight", "area_id": lit.area,
                                    "sensors": [{"entity_id": lit.lamp}]})
    assert not (await client.receive_json())["success"]
    await client.send_json_auto_id({"type": "smart_occupancy/lighting/editor", "area_id": lit.area})
    model = (await client.receive_json())["result"]
    daylight = model["room"]["daylight"]
    assert daylight["config"]["on_below"] == 60 and daylight["config"]["off_above"] == 100
    assert [s["entity_id"] for s in daylight["config"]["sensors"]] == [LUX, PIR_LUX]
    assert daylight["global"]["outdoor_entity"] == OUTDOOR
    assert daylight["status"]["decision"] == "dark" and daylight["status"]["sensors"][0]["status"] == "ok"
    assert LUX in {c["entity_id"] for c in daylight["candidates"] if c["in_room"]}
    assert "light.stova_lamp" in daylight["influencing"]
    assert ctrl.daylight.status(lit.area).trusted.lux == 45
    # Reload and restart: settings, the trusted sample and the outdoor reference are kept.
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    ctrl = entry.runtime_data.lighting
    assert ctrl.lighting.room(lit.area).daylight.uncertain == UncertainPolicy.STAY_OFF
    assert ctrl.lighting.daylight.outdoor_entity == OUTDOOR
    assert ctrl.daylight.rooms[lit.area].trusted[LUX].lux == 45
    assert hass_storage["smart_occupancy.lighting"]["minor_version"] == 6  # 0.4.0: occupied and vacant profiles


async def test_upgrade_from_032_store_migrates(hass: HomeAssistant, lit: Lit, freezer, lights,
                                               hass_storage) -> None:
    hass_storage["smart_occupancy.lighting"] = {
        "version": 1, "minor_version": 1, "key": "smart_occupancy.lighting",
        "data": {"rooms": {lit.area: {"control_enabled": True, "excluded": [lit.plug]}}, "global_profiles": PROFILES,
                 "manual_option": "Manual"}}
    freezer.move_to(DAY)
    lux_entities(hass, lit)
    all_off(hass, lit)
    await advance(hass, freezer, 120)
    set_lux(hass, 5)
    await advance(hass, freezer, 600)
    entry = await setup_entry(hass, options(lit))
    ctrl = entry.runtime_data.lighting
    room = ctrl.lighting.room(lit.area)
    assert room.control_enabled and room.excluded == (lit.plug,)
    assert room.daylight.on_first_motion is False and room.daylight.auto_on == AutoOn.DARK
    assert room.daylight.sensors == ()  # no light sensor yet: switch-on works as before
    hass.states.async_set(lit.pir, "on")
    await advance(hass, freezer, 2)
    assert lights.calls == []  # waits for confirmation, as in 0.3.2
    await advance(hass, freezer, 10)
    await advance(hass, freezer, 2)
    assert lit.lamp in turned_on(lights) and lit.plug not in turned_on(lights)
    await hass.async_block_till_done()
    assert entry.state.value == "loaded"


async def test_never_policy_and_always_policy(hass: HomeAssistant, lit: Lit, freezer, lights) -> None:
    _entry, ctrl = await start(hass, lit, freezer, lux=5,
                               daylight=RoomDaylight(sensors=(IndoorSensor(LUX),), auto_on=AutoOn.NEVER))
    await occupy(hass, lit, freezer)
    assert lights.calls == [] and ctrl.runtime[lit.area].activation["result"] == "disabled"
    hass.states.async_set(lit.pir, "off")
    hass.states.async_set(lit.radar, "off")
    await advance(hass, freezer, 1000)
    set_lux(hass, 900)
    ctrl.store.async_set(ctrl.with_room(lit.area, daylight=RoomDaylight(sensors=(IndoorSensor(LUX),),
                                                                        auto_on=AutoOn.ALWAYS)))
    await advance(hass, freezer, 300)
    await occupy(hass, lit, freezer)
    assert lit.lamp in turned_on(lights)  # regardless of daylight


async def test_activation_record_survives_restart_and_next_visit_is_new(hass: HomeAssistant, lit: Lit, freezer,
                                                                       lights) -> None:
    entry, ctrl = await start(hass, lit, freezer, lux=5)
    await occupy(hass, lit, freezer)
    assert ctrl.runtime[lit.area].activation["result"] == "turned_on"
    hass.states.async_set(lit.pir, "off")
    hass.states.async_set(lit.radar, "off")
    await advance(hass, freezer, 1000)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    ctrl = entry.runtime_data.lighting
    assert ctrl.runtime[lit.area].activation["result"] == "turned_on"  # restored
    all_off(hass, lit)
    for e in (lit.lamp, lit.desk, lit.plug, lit.strip):
        hass.states.async_set(e, "off", CT)
    await advance(hass, freezer, 120)
    set_lux(hass, 600)
    await advance(hass, freezer, 30)
    await occupy(hass, lit, freezer)
    assert ctrl.runtime[lit.area].activation["result"] == "not_needed"  # the new visit is recorded
