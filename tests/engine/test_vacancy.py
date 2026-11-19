"""Vacancy lighting (0.3.8): what automatic lighting does when a room becomes vacant.

Lights are simulated by light services that report the requested state back
(like a real integration), or that fail. Occupancy comes from the real engine
with the default timing (radar = Maintain: vacancy after the 600 s inactivity
timeout). Nothing here talks to real devices.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import Context, HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError

from custom_components.smart_occupancy.const import VacancyAction, VacantOthers
from custom_components.smart_occupancy.daylight import RoomDaylight
from custom_components.smart_occupancy.lighting_config import RoomVacancy, migrate_store
from custom_components.smart_occupancy.models import ProfileSetting

from .conftest import setup_entry
from .test_decision_log import log, obey
from .test_lighting_runtime import MAIN_OPTS, Lit, advance, occupy, options, set_moods
from .test_lighting_runtime import lit  # noqa: F401  (fixture)
from .test_switches import WS, ctrl_of

ALWAYS = RoomDaylight.from_dict({"auto_on": "always"})  # switch-on regardless of daylight (no gate in these tests)


def ids(call: ServiceCall) -> list[str]:
    value = call.data["entity_id"]
    return [value] if isinstance(value, str) else list(value)


def sent(calls: list[ServiceCall], service: str) -> set[str]:
    return {e for c in calls if c.service == service for e in ids(c)}


async def ready(hass: HomeAssistant, lit: Lit, freezer, **room: Any):
    """Occupied, Automatic, Dagur applied (every managed light on). Returns (entry, controller, service calls)."""
    seen = obey(hass)
    entry = await setup_entry(hass, options(lit))
    ctrl = ctrl_of(entry)
    await occupy(hass, lit, freezer)
    ctrl.store.async_set(ctrl.with_room(lit.area, control_enabled=True, daylight=ALWAYS, **room))
    await hass.async_block_till_done()
    await advance(hass, freezer)
    for e in (lit.group, lit.lamp, lit.desk, lit.strip, lit.plug):
        assert hass.states.is_state(e, "on"), e
    seen.clear()
    return entry, ctrl, seen


async def leave(hass: HomeAssistant, lit: Lit, freezer) -> None:
    """Every sensor clears; past the radar's 30 s dropout bridge the vacancy timeout runs."""
    hass.states.async_set(lit.pir, "off")
    hass.states.async_set(lit.radar, "off")
    await hass.async_block_till_done()
    await advance(hass, freezer, 35)


def state(ctrl, lit: Lit) -> str:
    return ctrl.coordinator.occupancy.result(lit.area).state.value


# ------------------------------------------------------------------ Turn off
async def test_turn_off_after_the_vacancy_timeout(hass: HomeAssistant, lit: Lit, freezer) -> None:
    """Scenario C: everything clears; the timeout is respected; the lights go off once; the log explains it."""
    _entry, ctrl, seen = await ready(hass, lit, freezer)
    await leave(hass, lit, freezer)
    assert state(ctrl, lit) == "possible_vacancy"
    d = log(ctrl, lit.area)[0]
    assert (d["action"], d["status"], d["kind"]) == ("Vacancy timeout started", "noted", "vacancy_wait"), d
    assert "will be switched off at " in d["explanation"] and "(after 10 min without activity)" in d["explanation"]
    # Not before the vacancy timeout.
    await advance(hass, freezer, 300)
    assert seen == [] and state(ctrl, lit) == "possible_vacancy"
    await advance(hass, freezer, 400)
    assert state(ctrl, lit) == "vacant"
    # One command per light or group (the spots are switched through their group), ambient none here.
    assert sent(seen, "turn_off") == {lit.group, lit.lamp, lit.desk, lit.strip, lit.plug}
    assert sent(seen, "turn_on") == set() and len(seen) == 1
    for e in (lit.group, lit.lamp, lit.desk, lit.strip, lit.plug):
        assert hass.states.is_state(e, "off")
    entries = log(ctrl, lit.area)
    d = entries[0]
    assert (d["action"], d["status"], d["trigger"], d["mode"]) == ("Lights off", "executed", "vacant", "automatic"), d
    assert d["explanation"].startswith("Vacant: no activity for 10 min: ") and "switched off" in d["explanation"]
    assert d["details"]["vacancy_action"] == "Turn off"
    assert d["details"]["verification"] == "Every target reported the requested state."
    assert entries[1]["action"] == "Vacancy timeout expired"  # the wait entry, updated in place
    diag = ctrl.room_diagnostics(lit.area)["vacancy"]
    assert diag["action"] == "turn_off" and diag["last"]["result"] == "executed" and diag["deferred"] == []
    # Nothing more happens while the room stays vacant.
    seen.clear()
    await advance(hass, freezer, 3600)
    assert seen == []


async def test_return_before_the_timeout_cancels(hass: HomeAssistant, lit: Lit, freezer) -> None:
    """Scenario D: presence returns during the timeout: nothing is switched off (and nothing on again)."""
    _entry, ctrl, seen = await ready(hass, lit, freezer)
    for _ in range(2):
        await leave(hass, lit, freezer)
        await advance(hass, freezer, 120)
        hass.states.async_set(lit.pir, "on")
        await hass.async_block_till_done()
        await advance(hass, freezer, 2)
        assert state(ctrl, lit) == "confirmed_occupancy"
        hass.states.async_set(lit.pir, "off")
        await hass.async_block_till_done()
        await advance(hass, freezer, 2)
        hass.states.async_set(lit.radar, "on")
        await hass.async_block_till_done()
        await advance(hass, freezer, 2)
    assert seen == []
    waits = [d for d in log(ctrl, lit.area) if d["kind"] == "vacancy_wait"]
    # Four interruptions (PIR off with the radar off, then the radar back, twice) are one counted entry,
    # not a log full of sensor updates.
    assert len(waits) == 1 and waits[0]["count"] == 4, waits
    assert waits[0]["action"] == "Vacancy cancelled"
    assert waits[0]["explanation"].startswith("Occupancy returned after ")
    assert "nothing is switched off" in waits[0]["explanation"]
    await advance(hass, freezer, 3600)  # radar holds the room
    assert seen == [] and state(ctrl, lit) == "confirmed_occupancy"


# --------------------------------------------------------------- Vacant mood
async def test_vacant_mood_applies_a_profile_to_lights_that_are_on(hass: HomeAssistant, lit: Lit, freezer) -> None:
    """The 0.3.8 vacant mood, kept by rooms that used it (others: like a mood). Nátt: downlights off, lamps
    10 % 2200 K, LED strips not set (left as they are); nothing switched on."""
    _entry, ctrl, seen = await ready(hass, lit, freezer,
                                     vacancy=RoomVacancy(VacancyAction.VACANT_MOOD, "Nátt", VacantOthers.MOOD))
    hass.states.async_set(lit.desk, "off", hass.states.get(lit.desk).attributes, context=Context(user_id="person"))
    await advance(hass, freezer, 2)
    seen.clear()
    await leave(hass, lit, freezer)
    assert "the vacant mood Nátt will be applied to " in log(ctrl, lit.area)[0]["explanation"]
    await advance(hass, freezer, 700)
    assert sent(seen, "turn_off") == {lit.group}
    on = {e: c.data for c in seen if c.service == "turn_on" for e in ids(c)}
    assert set(on) == {lit.lamp}, on  # the desk lamp is off: an empty room gets no light switched on
    assert on[lit.lamp]["brightness_pct"] == 10 and on[lit.lamp]["color_temp_kelvin"] == 2200
    assert hass.states.is_state(lit.strip, "on") and hass.states.is_state(lit.desk, "off")
    d = log(ctrl, lit.area)[0]
    assert (d["action"], d["status"]) == ("Apply vacant mood Nátt", "executed"), d
    assert d["details"]["vacant_mood"] == "Nátt"
    targets = " | ".join(d["details"]["targets"])
    assert "Stays off: the vacant mood Nátt does not switch lights on" in targets
    assert "Left as it is: the vacant mood Nátt does not set it" in targets
    # Someone arrives: the room's mood (Dagur) is applied again.
    seen.clear()
    await occupy(hass, lit, freezer)
    assert hass.states.get(lit.lamp).attributes["brightness"] == round(75 * 255 / 100)


async def test_leave_unchanged_is_explained(hass: HomeAssistant, lit: Lit, freezer) -> None:
    _entry, ctrl, seen = await ready(hass, lit, freezer, vacancy=RoomVacancy(VacancyAction.LEAVE))
    await leave(hass, lit, freezer)
    assert log(ctrl, lit.area)[0]["kind"] != "vacancy_wait"  # nothing will happen: no countdown entry
    await advance(hass, freezer, 700)
    assert seen == []
    d = log(ctrl, lit.area)[0]
    assert (d["action"], d["status"]) == ("Leave lights unchanged", "suppressed"), d
    assert "Lights left unchanged: room vacancy behavior is configured to Leave Unchanged." in d["explanation"]
    assert "stay on" in d["explanation"]


async def test_ambient_lights_keep_following_the_mood(hass: HomeAssistant, lit: Lit, freezer) -> None:
    _entry, ctrl, seen = await ready(hass, lit, freezer,
                                     mood_overrides={"Dagur": {"led_strips": ProfileSetting(persistent=True)}})
    await leave(hass, lit, freezer)
    await advance(hass, freezer, 700)
    assert lit.strip not in sent(seen, "turn_off") and lit.lamp in sent(seen, "turn_off")
    assert hass.states.is_state(lit.strip, "on")
    assert "Ambient: light.kno_strip keeps following Dagur." in log(ctrl, lit.area)[0]["explanation"]


# ------------------------------------------------------------ manual overrides
async def test_manual_overrides_and_the_vacancy_action(hass: HomeAssistant, lit: Lit, freezer) -> None:
    """Scenario E. An override from while the room was occupied ends with the vacancy (as before); one made
    in the already quiet room is respected until it expires, then the vacancy action runs for that light."""
    _entry, ctrl, seen = await ready(hass, lit, freezer, manual_override_minutes=30)
    attrs = hass.states.get(lit.desk).attributes
    hass.states.async_set(lit.desk, "on", {**attrs, "brightness": 30}, context=Context(user_id="person"))
    await advance(hass, freezer, 2)
    rt = ctrl.runtime[lit.area]
    assert lit.desk in rt.overrides
    await leave(hass, lit, freezer)
    await advance(hass, freezer, 120)
    # In the empty room, a person switches the lamp to 20 % from the app.
    attrs = hass.states.get(lit.lamp).attributes
    hass.states.async_set(lit.lamp, "on", {**attrs, "brightness": 51}, context=Context(user_id="person"))
    await advance(hass, freezer, 2)
    seen.clear()
    await advance(hass, freezer, 600)
    assert state(ctrl, lit) == "vacant"
    # The desk lamp's override ended with the vacancy; the lamp's (made in the empty room) is kept.
    assert set(rt.overrides) == {lit.lamp}
    assert lit.desk in sent(seen, "turn_off") and lit.lamp not in sent(seen, "turn_off")
    assert hass.states.is_state(lit.lamp, "on")
    d = log(ctrl, lit.area)[0]
    assert d["action"] == "Lights off" and d["status"] == "executed", d
    assert "Not changed: manual override on light.kno_lamp." in d["explanation"]
    assert "1 manual override ended with the vacancy." in d["explanation"]
    assert rt.vacancy_deferred == {lit.lamp}
    # The override expires: the room is still vacant, so the lamp is switched off now.
    seen.clear()
    await advance(hass, freezer, 30 * 60)
    assert sent(seen, "turn_off") == {lit.lamp}
    d = log(ctrl, lit.area)[0]
    assert (d["action"], d["trigger"], d["status"]) == ("Lights off", "override_expired", "executed"), d
    assert d["explanation"].startswith("The manual override ended while the room is vacant: ")
    assert rt.vacancy_deferred == set()


async def test_manual_mood_blocks_the_vacancy_action(hass: HomeAssistant, lit: Lit, freezer) -> None:
    _entry, ctrl, seen = await ready(hass, lit, freezer)
    set_moods(hass, "Dagur", "Manual", "Manual", main_opts=[*MAIN_OPTS, "Manual"])
    await advance(hass, freezer, 2)
    await leave(hass, lit, freezer)
    await advance(hass, freezer, 700)
    assert seen == []
    d = log(ctrl, lit.area)[0]
    assert (d["action"], d["status"], d["mode"]) == ("Lights off", "blocked", "paused"), d
    assert "Main Mood is Manual: automatic lighting is paused and nothing is switched off." in d["explanation"]


async def test_switching_to_observing_during_the_timeout(hass: HomeAssistant, lit: Lit, freezer) -> None:
    """The mode at the moment of vacancy counts: Observing records what would happen and sends nothing."""
    _entry, ctrl, seen = await ready(hass, lit, freezer)
    await leave(hass, lit, freezer)
    ctrl.store.async_set(ctrl.with_room(lit.area, control_enabled=False))
    await advance(hass, freezer, 700)
    assert seen == []
    d = log(ctrl, lit.area)[0]
    assert (d["action"], d["status"], d["mode"]) == ("Lights off", "observed", "observing"), d
    assert "Observing: nothing was sent." in d["explanation"]


# ---------------------------------------------- unavailable and failing targets
async def test_unavailable_and_missing_targets(hass: HomeAssistant, lit: Lit, freezer) -> None:
    """An unavailable light is reported and handled when it comes back (if the room is still vacant)."""
    _entry, ctrl, seen = await ready(hass, lit, freezer)
    group_attrs = hass.states.get(lit.group).attributes
    hass.states.async_set(lit.group, "unavailable", group_attrs)
    hass.states.async_remove(lit.plug)  # missing (e.g. its integration is not loaded)
    await advance(hass, freezer, 2)
    await leave(hass, lit, freezer)
    await advance(hass, freezer, 700)
    assert sent(seen, "turn_off") == {lit.lamp, lit.desk, lit.strip}
    d = log(ctrl, lit.area)[0]
    assert (d["action"], d["status"]) == ("Lights off", "partial"), d
    assert "Not changed: " in d["explanation"] and "unavailable" in d["explanation"]
    assert d["details"]["errors"] == {lit.group: "unavailable", lit.plug: "missing"}
    rt = ctrl.runtime[lit.area]
    assert rt.vacancy_deferred == {lit.group, lit.plug}
    # The group comes back (still on): it is switched off now.
    seen.clear()
    hass.states.async_set(lit.group, "on", group_attrs)
    await advance(hass, freezer, 2)
    assert sent(seen, "turn_off") == {lit.group}
    d = log(ctrl, lit.area)[0]
    assert (d["trigger"], d["status"]) == ("reconnected", "executed"), d
    assert rt.vacancy_deferred == {lit.plug}
    # Someone arrives: nothing deferred is due any more.
    await occupy(hass, lit, freezer)
    assert rt.vacancy_deferred == set()


async def test_failed_switch_off_is_reported(hass: HomeAssistant, lit: Lit, freezer) -> None:
    _entry, ctrl, _seen = await ready(hass, lit, freezer)

    async def broken(call: ServiceCall) -> None:
        raise HomeAssistantError("Zigbee coordinator offline")

    hass.services.async_register("light", "turn_off", broken)
    await leave(hass, lit, freezer)
    await advance(hass, freezer, 700)
    d = log(ctrl, lit.area)[0]
    assert (d["action"], d["status"]) == ("Lights off", "failed"), d
    assert "Failed: " in d["explanation"] and "Zigbee coordinator offline" in d["explanation"]


async def test_lights_already_off_are_not_logged_again(hass: HomeAssistant, lit: Lit, freezer) -> None:
    _entry, ctrl, seen = await ready(hass, lit, freezer)
    for e in (lit.group, lit.spot_1, lit.spot_2, lit.lamp, lit.desk, lit.strip, lit.plug):
        hass.states.async_set(e, "off", hass.states.get(e).attributes, context=Context(user_id="person"))
    await advance(hass, freezer, 2)
    seen.clear()
    before = len(log(ctrl, lit.area))
    await leave(hass, lit, freezer)
    await advance(hass, freezer, 700)
    assert seen == []
    # No "vacancy timeout started" (no light was on), no vacancy entry beyond the overrides that ended.
    new = log(ctrl, lit.area)[: len(log(ctrl, lit.area)) - before]
    assert all(d["kind"] != "vacancy_wait" for d in new)


# ---------------------------------------------------- settings and migration
def test_store_migration_gives_turn_off_and_keeps_existing_values() -> None:
    data = {"rooms": {"a": {"control_enabled": True}, "b": {"vacancy": {"action": "leave", "mood": None}}}}
    out = migrate_store(1, 3, data)
    assert out["rooms"]["a"]["vacancy"] == {"action": "turn_off", "mood": None, "others": None, "on_below": None}
    assert out["rooms"]["b"]["vacancy"] == {"action": "leave", "mood": None, "others": None, "on_below": None}
    assert out["rooms"]["a"]["control_enabled"] is True  # everything else is kept
    # A 1.3 store loads with the new default; unknown values fall back to it too.
    assert RoomVacancy.from_dict({"action": "bogus"}).action == VacancyAction.TURN_OFF
    assert RoomVacancy.from_dict({"action": "vacant_mood", "mood": " Nátt "}).mood == "Nátt"


async def test_upgrade_of_a_stored_room(hass: HomeAssistant, lit: Lit, hass_storage) -> None:
    hass_storage["smart_occupancy.lighting"] = {
        "version": 1, "minor_version": 3, "key": "smart_occupancy.lighting",
        "data": {"rooms": {lit.area: {"control_enabled": True, "mood_override": "Kvøld", "switches": []}}},
    }
    entry = await setup_entry(hass, options(lit))
    room = ctrl_of(entry).lighting.room(lit.area)
    assert room.vacancy.action == VacancyAction.TURN_OFF
    assert room.control_enabled and room.mood_override == "Kvøld"


async def test_vacancy_setting_over_the_websocket(hass: HomeAssistant, lit: Lit, hass_ws_client) -> None:
    entry = await setup_entry(hass, options(lit))
    ctrl = ctrl_of(entry)
    ws = WS(await hass_ws_client(hass))
    room_set = "smart_occupancy/lighting/room/set"
    # 0.4.0: Vacant mood applies the room's Vacant profile of the current mood; no mood has to be chosen.
    assert (await ws(type=room_set, area_id=lit.area, vacancy_action="vacant_mood"))["success"]
    assert ctrl.lighting.room(lit.area).vacancy == RoomVacancy(VacancyAction.VACANT_MOOD)
    reply = await ws(type=room_set, area_id=lit.area, vacancy_action="vacant_mood", vacancy_mood="Manual")
    assert not reply["success"]
    assert (await ws(type=room_set, area_id=lit.area, vacancy_action="vacant_mood", vacancy_mood="Nátt"))["success"]
    assert ctrl.lighting.room(lit.area).vacancy == RoomVacancy(VacancyAction.VACANT_MOOD, "Nátt")
    # Another action keeps the chosen mood, so switching back restores it.
    assert (await ws(type=room_set, area_id=lit.area, vacancy_action="leave"))["success"]
    assert ctrl.lighting.room(lit.area).vacancy == RoomVacancy(VacancyAction.LEAVE, "Nátt")
    model = (await ws(type="smart_occupancy/lighting/editor", area_id=lit.area))["result"]["room"]
    assert model["vacancy"]["action"] == "leave" and model["vacancy"]["mood"] == "Nátt"
    assert model["diagnostics"]["vacancy"]["action"] == "leave"
    # The room's mood-lighting sensor shows it as an attribute that is never recorded.
    from custom_components.smart_occupancy.sensor import RoomMoodLightingSensor

    assert "vacancy" in RoomMoodLightingSensor._unrecorded_attributes
