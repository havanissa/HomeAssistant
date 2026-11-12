"""Occupied and Vacant lighting profiles (0.4.0) in a running Home Assistant.

Lights are simulated by light services that report the requested state back
with the caller's context (``FakeLights``). Occupancy comes from the real
engine (radar = Maintain: vacancy after the 600 s inactivity timeout).
Daylight cases use the outdoor sensor times the room's daylight factor, which
the room's own lamps cannot disturb. Nothing here talks to real devices.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import Context, HomeAssistant

from custom_components.smart_occupancy.const import (
    DaylightCondition,
    LightAction,
    Profile,
    VacancyAction,
    VacantOthers,
)
from custom_components.smart_occupancy.daylight import DaylightConfig, RoomDaylight
from custom_components.smart_occupancy.lighting_config import (
    LightingConfig,
    RoomVacancy,
    VacantDefaults,
    migrate_store,
    set_profile,
)
from custom_components.smart_occupancy.models import ProfileSetting
from custom_components.smart_occupancy.profiles import resolve

from .conftest import setup_entry
from .test_adaptive_runtime import FakeLights, lights  # noqa: F401  (fixture)
from .test_daylight_runtime import DAY, OUTDOOR, set_lux
from .test_decision_log import log
from .test_lighting_runtime import Lit, advance, lit, occupy, options, set_moods  # noqa: F401
from .test_switches import WS, ctrl_of

ALWAYS = RoomDaylight.from_dict({"auto_on": "always"})
# Daylight from the outdoor sensor x 0.05: 600 lx outside = 30 lx in the room (dark), 20000 lx = 1000 lx (bright).
FACTOR = RoomDaylight(factor=0.05, stable_seconds=0)
ON = LightAction.ON
OFF = LightAction.OFF


def on(brightness: int, kelvin: int | None = None, **extra: Any) -> ProfileSetting:
    return ProfileSetting(action=ON, brightness_pct=brightness, color_temp_kelvin=kelvin, **extra)


# Stova-like Vacant profile: downlights off, the LED strip (decorative) dim, lamps not set (others: Off).
VACANT = {
    "Dagur": {"downlights": ProfileSetting(action=OFF), "led_strips": on(20, 2200)},
    "Kvøld": {"led_strips": on(30, 2200)},
}


async def setup(hass: HomeAssistant, lit: Lit, freezer, lights: FakeLights, *, daylight: RoomDaylight = ALWAYS,
                vacancy: RoomVacancy | None = None, vacant: dict | None = None, active: bool = True,
                glob: LightingConfig | None = None, **room: Any):
    """Occupied, the room's mood (Dagur) applied: every managed light on. Returns (entry, controller)."""
    entry = await setup_entry(hass, options(lit))
    ctrl = ctrl_of(entry)
    if glob is not None:
        ctrl.store.async_set(glob)
    await occupy(hass, lit, freezer)
    ctrl.store.async_set(ctrl.with_room(
        lit.area, control_enabled=True, daylight=daylight,
        vacancy=vacancy or RoomVacancy(VacancyAction.VACANT_MOOD),
        vacant_overrides=VACANT if vacant is None else vacant, **room))
    await hass.async_block_till_done()
    await advance(hass, freezer)
    if not active:
        ctrl.store.async_set(ctrl.with_room(lit.area, control_enabled=False))
        await hass.async_block_till_done()
    lights.clear()
    return entry, ctrl


async def leave(hass: HomeAssistant, lit: Lit, freezer) -> None:
    """Every sensor clears; the 10 min vacancy timeout runs out."""
    hass.states.async_set(lit.pir, "off")
    hass.states.async_set(lit.radar, "off")
    await hass.async_block_till_done()
    await advance(hass, freezer, 35)
    await advance(hass, freezer, 700)


def state(ctrl, lit: Lit) -> str:
    return ctrl.coordinator.occupancy.result(lit.area).state.value


def commands(lights: FakeLights, entity_id: str) -> list[tuple[str, dict[str, Any]]]:
    out = []
    for call in lights.calls:
        ids = call.data["entity_id"]
        if entity_id in ([ids] if isinstance(ids, str) else ids):
            out.append((call.service, {k: v for k, v in call.data.items() if k != "entity_id"}))
    return out


def brightness(hass: HomeAssistant, entity_id: str) -> int:
    return round(hass.states.get(entity_id).attributes["brightness"] * 100 / 255)


async def mood(hass: HomeAssistant, freezer, name: str) -> None:
    set_moods(hass, name, "Auto", name)
    await hass.async_block_till_done()
    await advance(hass, freezer)


# ------------------------------------------------------------------ resolution
def test_vacant_profile_resolves_by_itself_through_the_same_levels() -> None:
    """Global -> room -> light, with the mood chain; a Vacant entry never inherits from an Occupied one."""
    config = LightingConfig(global_profiles={"Kvøld": {"lamps": on(80, 2700)}})
    config = set_profile(config, "global", "Kvøld", "lamps", on(15, 2200, transition=4), profile=Profile.VACANT)
    config = set_profile(config, "room", "Kvøld", "lamps", ProfileSetting(brightness_pct=25), "stova",
                         profile=Profile.VACANT)
    config = set_profile(config, "light", "Kvøld", "light.lego", ProfileSetting(daylight=DaylightCondition.ALWAYS),
                         "stova", profile=Profile.VACANT)
    occupied = resolve(config, "Kvøld", "stova", "light.lego", ["lamps"])
    vacant = resolve(config, "Kvøld", "stova", "light.lego", ["lamps"], profile=Profile.VACANT)
    assert (occupied.brightness_pct, occupied.daylight) == (80, None)
    assert (vacant.action, vacant.brightness_pct, vacant.color_temp_kelvin, vacant.transition) == (ON, 25, 2200, 4)
    assert vacant.in_daylight and vacant.sources["brightness_pct"]["level"] == "room"
    assert vacant.sources["color_temp_kelvin"] == {"level": "global", "mood": "Kvøld", "target": "lamps"}
    # Nothing set for this mood: Ignore (the room's "others" setting decides), not the Occupied profile.
    assert resolve(config, "Dagur", "stova", None, ["lamps"], profile=Profile.VACANT).action == LightAction.IGNORE
    # Round trip through the store.
    again = LightingConfig.from_dict(config.as_dict())
    assert again == config and again.room("stova").vacant_light_overrides["Kvøld"]["light.lego"].daylight == "always"


def test_migration_keeps_a_0_3_8_vacant_mood_and_adds_empty_vacant_profiles() -> None:
    data = {"rooms": {
        "legacy": {"vacancy": {"action": "vacant_mood", "mood": "Nátt"}},
        "off": {"vacancy": {"action": "turn_off", "mood": "Nátt"}},
        "new": {}}}
    out = migrate_store(1, 5, data)
    config = LightingConfig.from_dict(out)
    assert config.room("legacy").vacancy == RoomVacancy(VacancyAction.VACANT_MOOD, "Nátt", VacantOthers.MOOD)
    assert config.vacant_others("legacy") == VacantOthers.MOOD  # unchanged behaviour: Nátt's settings, lit lights
    assert config.room("off").vacancy.others is None and config.vacant_others("off") == VacantOthers.OFF
    assert config.room("new").vacant_overrides == {} and config.global_vacant == {}
    assert config.vacant == VacantDefaults() and out["vacant"] == {"others": "off", "on_below": None}
    # "Like a mood" without a mood falls back to Off.
    broken = LightingConfig.from_dict({"rooms": {"r": {"vacancy": {"action": "vacant_mood", "others": "mood"}}}})
    assert broken.vacant_others("r") == VacantOthers.OFF


# ------------------------------------------------------------ occupied -> vacant
async def test_occupied_to_vacant_goes_directly_to_the_vacant_profile(hass: HomeAssistant, lit: Lit, freezer,
                                                                       lights: FakeLights) -> None:
    """Spots off, the strip from 50 % straight to 20 % (never off first), lamps not set: off (others)."""
    _entry, ctrl = await setup(hass, lit, freezer, lights)
    await leave(hass, lit, freezer)
    assert state(ctrl, lit) == "vacant"
    assert commands(lights, lit.strip) == [("turn_on", {"brightness_pct": 20, "color_temp_kelvin": 2200})]
    for e in (lit.group, lit.lamp, lit.desk, lit.plug):
        assert commands(lights, e) == [("turn_off", {})], e
    assert brightness(hass, lit.strip) == 20 and hass.states.is_state(lit.lamp, "off")
    d = log(ctrl, lit.area)[0]
    assert (d["action"], d["status"]) == ("Apply Vacant profile (Dagur)", "executed"), d
    assert "light.kno_strip adjusted" in d["explanation"] and "(Vacant profile (Dagur))" in d["explanation"]
    targets = " | ".join(d["details"]["targets"])
    assert "does not set it (others: Off)" in targets
    assert ctrl.room_diagnostics(lit.area)["vacancy"]["vacant_profile_in_charge"] is True


async def test_others_leave_keeps_lights_without_a_vacant_setting(hass: HomeAssistant, lit: Lit, freezer,
                                                                  lights: FakeLights) -> None:
    _entry, ctrl = await setup(hass, lit, freezer, lights,
                               vacancy=RoomVacancy(VacancyAction.VACANT_MOOD, others=VacantOthers.LEAVE))
    await leave(hass, lit, freezer)
    assert commands(lights, lit.lamp) == [] and hass.states.is_state(lit.lamp, "on")
    assert commands(lights, lit.group) == [("turn_off", {})]  # the profile sets Off
    plan = ctrl.vacancy_plan(lit.area)
    assert {i.entity_id: i.reason_code for i in plan.items}[lit.lamp] == "vacant_unset"


# ------------------------------------------------------------- mood changes
async def test_mood_change_while_vacant_applies_the_new_vacant_profile(hass: HomeAssistant, lit: Lit, freezer,
                                                                        lights: FakeLights) -> None:
    """No sensor event: Main Mood alone re-evaluates the vacant room. Identical commands are not repeated."""
    _entry, ctrl = await setup(hass, lit, freezer, lights)
    await leave(hass, lit, freezer)
    lights.clear()
    await mood(hass, freezer, "Nátt")  # nothing set in Nátt: others Off
    assert commands(lights, lit.strip) == [("turn_off", {})]
    assert lights.count(lit.lamp) == 0  # already off: nothing resent
    d = log(ctrl, lit.area)[0]
    assert d["explanation"].startswith("Mood changed to Nátt while the room is vacant"), d
    lights.clear()
    await mood(hass, freezer, "Kvøld")  # the strip is switched on in the empty room (decorative)
    assert commands(lights, lit.strip) == [("turn_on", {"brightness_pct": 30, "color_temp_kelvin": 2200})]
    assert len(lights.calls) == 1
    assert "light.kno_strip switched on" in log(ctrl, lit.area)[0]["explanation"]
    # The same mood again (e.g. Main Mood re-written by an automation): nothing is sent.
    lights.clear()
    await mood(hass, freezer, "Kvøld")
    await advance(hass, freezer, 30)
    assert lights.calls == []


async def test_turn_off_and_leave_never_run_a_vacant_profile(hass: HomeAssistant, lit: Lit, freezer,
                                                             lights: FakeLights) -> None:
    _entry, ctrl = await setup(hass, lit, freezer, lights, vacancy=RoomVacancy(VacancyAction.TURN_OFF))
    await leave(hass, lit, freezer)
    assert commands(lights, lit.strip) == [("turn_off", {})]
    lights.clear()
    await mood(hass, freezer, "Kvøld")
    await advance(hass, freezer, 30)
    assert lights.calls == []  # the Vacant profile (strip on in Kvøld) is not used by Turn off
    ctrl.store.async_set(ctrl.with_room(lit.area, vacancy=RoomVacancy(VacancyAction.LEAVE)))
    await mood(hass, freezer, "Dagur")
    await advance(hass, freezer, 30)
    assert lights.calls == []


async def test_ambient_lights_follow_the_occupied_profile_in_the_same_plan(hass: HomeAssistant, lit: Lit, freezer,
                                                                           lights: FakeLights) -> None:
    """An ambient light (Occupied profile) and the Vacant profile never control the same light."""
    _entry, ctrl = await setup(hass, lit, freezer, lights, mood_overrides={
        "Dagur": {"lamps": ProfileSetting(persistent=True)}, "Kvøld": {"lamps": ProfileSetting(persistent=True)}},
        vacant={"Dagur": {"lamps": ProfileSetting(action=OFF)}, "Kvøld": {"lamps": ProfileSetting(action=OFF)}})
    await leave(hass, lit, freezer)
    assert commands(lights, lit.lamp) == []  # ambient: kept, the Vacant profile's Off does not apply
    lights.clear()
    await mood(hass, freezer, "Kvøld")
    assert commands(lights, lit.lamp) == [("turn_on", {"brightness_pct": 75, "color_temp_kelvin": 2700})]
    entries = [d for d in log(ctrl, lit.area)[:2] if d["trigger"] == "mood_change"]
    assert len(entries) == 1 and entries[0]["kind"] == "vacancy"  # one decision for the whole room


async def test_manual_mood_and_observing(hass: HomeAssistant, lit: Lit, freezer, lights: FakeLights) -> None:
    _entry, ctrl = await setup(hass, lit, freezer, lights)
    await leave(hass, lit, freezer)
    lights.clear()
    set_moods(hass, "Kvøld", "Manual", "Manual", main_opts=["Morgun", "Dagur", "Kvøld", "Nátt", "Manual"],
              manual_opts=["Morgun", "Dagur", "Kvøld", "Nátt", "Manual"])
    await hass.async_block_till_done()
    await advance(hass, freezer, 30)
    assert lights.calls == []
    # Observing: the new mood's Vacant profile is evaluated and logged, nothing is sent.
    ctrl.store.async_set(ctrl.with_room(lit.area, control_enabled=False))
    await mood(hass, freezer, "Nátt")
    assert lights.calls == []
    d = log(ctrl, lit.area)[0]
    assert d["status"] == "observed" and "would switch off light.kno_strip" in d["explanation"], d


# ------------------------------------------------------------ vacant -> occupied
async def test_vacant_to_occupied_restores_the_occupied_profile_directly(hass: HomeAssistant, lit: Lit, freezer,
                                                                          lights: FakeLights) -> None:
    _entry, ctrl = await setup(hass, lit, freezer, lights)
    await leave(hass, lit, freezer)
    lights.clear()
    await occupy(hass, lit, freezer)
    assert state(ctrl, lit) == "confirmed_occupancy"
    # The strip goes from 20 % to Dagur's 50 % (no off first); everything else is switched on; one command each.
    assert commands(lights, lit.strip) == [("turn_on", {"brightness_pct": 50, "color_temp_kelvin": 3500})]
    for e in (lit.group, lit.lamp, lit.desk, lit.plug):
        assert len(commands(lights, e)) == 1 and commands(lights, e)[0][0] == "turn_on", e
    assert ctrl.room_diagnostics(lit.area)["vacancy"]["vacant_profile_in_charge"] is False


# --------------------------------------------------------------------- daylight
async def outdoor(hass: HomeAssistant, freezer, value: float) -> None:
    """A new outdoor reading, held long enough for the smoothing window and the debounce."""
    set_lux(hass, value, OUTDOOR)
    await hass.async_block_till_done()
    for _ in range(4):
        await advance(hass, freezer, 120)


async def daylight_setup(hass, lit, freezer, lights, outdoor: float, **kw):
    freezer.move_to(DAY)
    set_lux(hass, outdoor, OUTDOOR)
    await hass.async_block_till_done()
    base = await setup_entry(hass, options(lit))
    ctrl = ctrl_of(base)
    glob = ctrl.lighting.__class__(**{**{f: getattr(ctrl.lighting, f) for f in ctrl.lighting.__dataclass_fields__},
                                      "daylight": DaylightConfig(outdoor_entity=OUTDOOR)})
    assert await hass.config_entries.async_unload(base.entry_id)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_setup(base.entry_id)
    await hass.async_block_till_done()
    ctrl = ctrl_of(base)
    ctrl.store.async_set(glob)
    await occupy(hass, lit, freezer)
    ctrl.store.async_set(ctrl.with_room(lit.area, control_enabled=True, daylight=FACTOR,
                                        vacancy=kw.pop("vacancy", RoomVacancy(VacancyAction.VACANT_MOOD)),
                                        vacant_overrides=kw.pop("vacant", VACANT), **kw))
    await hass.async_block_till_done()
    await advance(hass, freezer)
    lights.clear()
    return base, ctrl


async def test_decorative_light_follows_daylight_while_vacant(hass: HomeAssistant, lit: Lit, freezer,
                                                              lights: FakeLights) -> None:
    """Bright: the strip is not needed in the empty room (off). Dark: switched on. Bright again: off."""
    _entry, ctrl = await daylight_setup(hass, lit, freezer, lights, 20000)
    for e in (lit.group, lit.spot_1, lit.spot_2, lit.lamp, lit.desk, lit.plug, lit.strip):
        hass.states.async_set(e, "off", hass.states.get(e).attributes)
    await advance(hass, freezer, 2)
    lights.clear()
    await leave(hass, lit, freezer)
    assert state(ctrl, lit) == "vacant" and lights.calls == []  # bright: nothing switched on in the empty room
    item = next(i for i in ctrl.vacancy_plan(lit.area).items if i.entity_id == lit.strip)
    assert (item.decision, item.reason_code) == ("blocked", "daylight"), item.reason
    await outdoor(hass, freezer, 600)  # 30 lx in the room: dark
    assert commands(lights, lit.strip) == [("turn_on", {"brightness_pct": 20, "color_temp_kelvin": 2200})]
    assert len(lights.calls) == 1
    assert log(ctrl, lit.area)[0]["explanation"].startswith("It became dark while the room is vacant"), \
        log(ctrl, lit.area)[0]
    lights.clear()
    await outdoor(hass, freezer, 20000)
    assert commands(lights, lit.strip) == [("turn_off", {})]
    assert "only in the dark" in " ".join(log(ctrl, lit.area)[0]["details"]["targets"])
    # Between the thresholds (60 lx, the hysteresis band 50-100 lx) nothing changes back and forth.
    lights.clear()
    await outdoor(hass, freezer, 1200)
    assert lights.calls == []


async def test_daylight_exception_and_own_threshold(hass: HomeAssistant, lit: Lit, freezer,
                                                    lights: FakeLights) -> None:
    """A window lamp that is on regardless of daylight; the Vacant profile's own (lower) threshold."""
    vacant = {"Dagur": {"led_strips": on(20, 2200, daylight=DaylightCondition.ALWAYS), "lamps": on(10)}}
    _entry, ctrl = await daylight_setup(hass, lit, freezer, lights, 800,  # 40 lx: dark for the room (50)
                                        vacant=vacant,
                                        vacancy=RoomVacancy(VacancyAction.VACANT_MOOD, on_below=20))
    for e in (lit.group, lit.spot_1, lit.spot_2, lit.lamp, lit.desk, lit.plug, lit.strip):
        hass.states.async_set(e, "off", hass.states.get(e).attributes)
    await advance(hass, freezer, 2)
    lights.clear()
    await leave(hass, lit, freezer)
    # 40 lx is not dark at the Vacant profile's 20 lx: the lamps stay off; the strip's exception switches it on.
    assert set(lights.by_entity()) == {lit.strip}
    gate = ctrl.daylight.vacant_gate(lit.area)
    assert (gate.on_below, gate.decision.value) == (20, "bright")
    assert ctrl.daylight_gate(lit.area).decision.value == "dark"  # the room's own decision is unchanged
    await outdoor(hass, freezer, 300)  # 15 lx
    assert {e for e, (s, _) in lights.by_entity().items() if s == "turn_on"} == {lit.strip, lit.lamp, lit.desk,
                                                                                lit.plug}


# ------------------------------------------------- editing, preview, test, copy
async def test_editing_never_sends_and_preview_shows_both_profiles(hass: HomeAssistant, lit: Lit, freezer,
                                                                    lights: FakeLights, hass_ws_client) -> None:
    _entry, ctrl = await setup(hass, lit, freezer, lights)
    await leave(hass, lit, freezer)
    lights.clear()
    ws = WS(await hass_ws_client(hass))
    reply = await ws(type="smart_occupancy/lighting/profile/set", profile="vacant", scope="room", area_id=lit.area,
                     mood="Dagur", target="lamps", setting={"action": "on", "brightness_pct": 15})
    assert reply["success"], reply
    reply = await ws(type="smart_occupancy/lighting/vacant/set", area_id=lit.area, others="leave", on_below=40)
    assert reply["success"], reply
    await advance(hass, freezer, 30)
    assert lights.calls == []  # configuration editing changes no physical light
    # Vacant: ambient is not a Vacant setting.
    reply = await ws(type="smart_occupancy/lighting/profile/set", profile="vacant", scope="room", area_id=lit.area,
                     mood="Dagur", target="lamps", setting={"action": "on", "persistent": True})
    assert not reply["success"] and "Occupied profile" in reply["error"]["message"]
    # The editor model of the Vacant profile.
    model = (await ws(type="smart_occupancy/lighting/editor", area_id=lit.area, mood="Dagur",
                      profile="vacant"))["result"]
    room = model["room"]
    assert model["profile"] == "vacant" and room["profile"] == "vacant" and room["now_profile"] == "vacant"
    lamps = next(t for t in room["types"] if t["target"] == "lamps")
    assert lamps["own"]["brightness_pct"] == 15 and lamps["effective"]["action"] == "on"
    assert room["vacant"]["effective"] == {"others": "leave", "on_below": 40}
    assert room["vacant"]["sources"] == {"others": "room", "on_below": "room"} and room["vacant"]["in_charge"]
    assert room["vacant"]["room_moods"] == ["Dagur", "Kvøld"]
    # Preview: the Vacant profile as if vacant, the Occupied one as if occupied; nothing is sent.
    vac = (await ws(type="smart_occupancy/lighting/preview", area_id=lit.area, mood="Dagur",
                    profile="vacant"))["result"]["plans"][0]
    items = {i["entity_id"]: i for i in vac["items"]}
    assert vac["profile"] == "vacant" and items[lit.lamp]["decision"] == "change"
    assert items[lit.lamp]["data"] == {"brightness_pct": 15}
    occ = (await ws(type="smart_occupancy/lighting/preview", area_id=lit.area, mood="Dagur",
                    profile="occupied"))["result"]["plans"][0]
    assert {i["entity_id"]: i["decision"] for i in occ["items"]}[lit.lamp] == "change"
    now = (await ws(type="smart_occupancy/lighting/preview", area_id=lit.area))["result"]["plans"][0]
    assert now["profile"] == "vacant"
    await advance(hass, freezer, 5)
    assert lights.calls == []
    # The next trigger uses the edited profile.
    await mood(hass, freezer, "Kvøld")
    await mood(hass, freezer, "Dagur")
    assert commands(lights, lit.lamp)[-1] == ("turn_on", {"brightness_pct": 15})


async def test_explicit_test_sends_and_keeps_the_lights(hass: HomeAssistant, lit: Lit, freezer,
                                                        lights: FakeLights, hass_ws_client) -> None:
    """Test is the only way editing sends commands: explicit, confirmed, kept as a manual override."""
    _entry, ctrl = await setup(hass, lit, freezer, lights, active=False)  # Observing: tests work too
    ws = WS(await hass_ws_client(hass))
    msg = {"type": "smart_occupancy/lighting/profile/test", "area_id": lit.area, "profile": "vacant", "mood": "Dagur"}
    reply = await ws(**msg, confirm=True)  # (without "confirm": true the command is refused by its schema)
    assert reply["success"], reply
    result = reply["result"]
    assert set(result["sent"]) == {lit.group, lit.lamp, lit.desk, lit.plug, lit.strip}
    assert commands(lights, lit.strip) == [("turn_on", {"brightness_pct": 20, "color_temp_kelvin": 2200})]
    rt = ctrl.runtime[lit.area]
    assert rt.overrides[lit.strip]["source"] == "profile_test" and rt.overrides[lit.strip]["until"]
    d = log(ctrl, lit.area)[0]
    assert d["action"] == "Test vacant profile Dagur" and "A test is a person's change" in d["explanation"], d
    # The lights' own reports are not new manual overrides (one entry, not one per light).
    await advance(hass, freezer, 10)
    assert sum(1 for x in log(ctrl, lit.area) if x["kind"] == "override") == 0
    # Testing the Occupied profile restores Dagur.
    lights.clear()
    reply = await ws(type="smart_occupancy/lighting/profile/test", area_id=lit.area, profile="occupied",
                     mood="Dagur", confirm=True)
    assert reply["success"] and commands(lights, lit.strip) == [
        ("turn_on", {"brightness_pct": 50, "color_temp_kelvin": 3500})]


async def test_copy_and_reset(hass: HomeAssistant, lit: Lit, freezer, lights: FakeLights, hass_ws_client) -> None:
    _entry, ctrl = await setup(hass, lit, freezer, lights, vacant={},
                               light_overrides={"Kvøld": {lit.desk: ProfileSetting(brightness_pct=40)}},
                               mood_overrides={"Kvøld": {"lamps": ProfileSetting(persistent=True)}})
    ws = WS(await hass_ws_client(hass))
    # Occupied Kvøld -> Vacant Kvøld as a starting point: what the room's light types get now, single lights too.
    reply = await ws(type="smart_occupancy/lighting/profile/copy", area_id=lit.area, from_profile="occupied",
                     from_mood="Kvøld", to_profile="vacant", to_mood="Kvøld")
    assert reply["success"], reply
    room = ctrl.lighting.room(lit.area)
    copied = room.vacant_overrides["Kvøld"]
    assert copied["lamps"].action == ON and copied["lamps"].brightness_pct == 75
    assert copied["lamps"].persistent is None  # ambient is not copied into a Vacant profile
    assert copied["downlights"].brightness_pct == 50 and copied["led_strips"].action == LightAction.IGNORE
    assert "decor" not in copied  # no such light in this room
    assert room.vacant_light_overrides["Kvøld"][lit.desk].brightness_pct == 40
    assert lights.calls == []
    # Mood to mood within one profile.
    reply = await ws(type="smart_occupancy/lighting/profile/copy", area_id=lit.area, from_profile="vacant",
                     from_mood="Kvøld", to_profile="vacant", to_mood="Nátt")
    assert reply["success"] and ctrl.lighting.room(lit.area).vacant_overrides["Nátt"]["lamps"].brightness_pct == 75
    reply = await ws(type="smart_occupancy/lighting/profile/copy", area_id=lit.area, from_profile="vacant",
                     from_mood="Nátt", to_profile="vacant", to_mood="nátt")
    assert not reply["success"]
    # Reset: the room's Vacant Kvøld entries are removed (it inherits the global Vacant profile again).
    reply = await ws(type="smart_occupancy/lighting/profile/reset", area_id=lit.area, profile="vacant", mood="Kvøld")
    assert reply["success"]
    room = ctrl.lighting.room(lit.area)
    assert "Kvøld" not in room.vacant_overrides and "Kvøld" not in room.vacant_light_overrides
    assert "Nátt" in room.vacant_overrides and room.mood_overrides["Kvøld"]  # nothing else changed
    # The global level: Occupied Dagur -> global Vacant Dagur.
    reply = await ws(type="smart_occupancy/lighting/profile/copy", from_profile="occupied", from_mood="Dagur",
                     to_profile="vacant", to_mood="Dagur")
    assert reply["success"] and ctrl.lighting.global_vacant["Dagur"]["lamps"].brightness_pct == 75
    assert lights.calls == []


async def test_global_vacant_defaults(hass: HomeAssistant, lit: Lit, freezer, lights: FakeLights,
                                      hass_ws_client) -> None:
    """Global Vacant profile and defaults; a room without its own values follows them."""
    _entry, ctrl = await setup(hass, lit, freezer, lights, vacant={})
    ws = WS(await hass_ws_client(hass))
    reply = await ws(type="smart_occupancy/lighting/profile/set", profile="vacant", scope="global", mood="Dagur",
                     target="led_strips", setting={"action": "on", "brightness_pct": 5})
    assert reply["success"], reply
    assert (await ws(type="smart_occupancy/lighting/vacant/set", others="leave"))["success"]
    assert not (await ws(type="smart_occupancy/lighting/vacant/set", others="mood"))["success"]
    assert not (await ws(type="smart_occupancy/lighting/vacant/set", area_id=lit.area, others="mood"))["success"]
    assert ctrl.lighting.vacant_others(lit.area) == VacantOthers.LEAVE
    await leave(hass, lit, freezer)
    assert commands(lights, lit.strip) == [("turn_on", {"brightness_pct": 5})]
    assert commands(lights, lit.lamp) == []  # others: Leave (global default)
    model = (await ws(type="smart_occupancy/lighting/editor", area_id=lit.area, profile="vacant",
                      mood="Dagur"))["result"]
    strip = next(t for t in model["room"]["types"] if t["target"] == "led_strips")
    assert strip["own"] is None and strip["effective"]["sources"]["brightness_pct"]["level"] == "global"
    assert model["room"]["vacant"]["sources"]["others"] == "global"
    assert model["vacant_defaults"] == {"others": "leave", "on_below": None}


async def test_manual_override_in_the_empty_room_is_respected(hass: HomeAssistant, lit: Lit, freezer,
                                                              lights: FakeLights) -> None:
    _entry, ctrl = await setup(hass, lit, freezer, lights)
    await leave(hass, lit, freezer)
    # A person switches the strip off from the app in the empty room: the next mood change leaves it.
    attrs = hass.states.get(lit.strip).attributes
    hass.states.async_set(lit.strip, "off", attrs, context=Context(user_id="person"))
    await advance(hass, freezer, 2)
    lights.clear()
    await mood(hass, freezer, "Kvøld")
    assert commands(lights, lit.strip) == []
    item = next(i for i in ctrl.vacancy_plan(lit.area).items if i.entity_id == lit.strip)
    assert item.reason_code == "manual_override"
