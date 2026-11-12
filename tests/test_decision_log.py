"""Decision log (0.3.7): what automatic lighting and wall switches decided, why, and whether it worked.

Lights, switches and the broker are simulated: light services either obey
(they set the light's state, like a real integration reporting back), do
nothing (a light that never reports), or fail (Home Assistant error).
"""

from __future__ import annotations

import logging
from typing import Any

from pytest_homeassistant_custom_component.common import async_mock_service

from homeassistant.core import Context, HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError

from custom_components.smart_occupancy.const import DECISION_LOG_SIZE, LIGHTING_STATE_STORE_KEY
from custom_components.smart_occupancy.decisions import Check

from .conftest import setup_entry
from .test_lighting_runtime import WS_LOGGER, Calls, Lit, activate, advance, occupy, options, set_moods
from .test_lighting_runtime import calls, lit  # noqa: F401  (fixtures)
from .test_switches import WS, controlled, ctrl_of, mqtt, press, settle  # noqa: F401
from .test_adaptive_runtime import home, lights  # noqa: F401  (fixtures)

EDITOR = "smart_occupancy/lighting/editor"


def obey(hass: HomeAssistant) -> list[ServiceCall]:
    """Light services that change the light's state (the light reports the requested state)."""
    seen: list[ServiceCall] = []

    async def handler(call: ServiceCall) -> None:
        seen.append(call)
        ids = call.data["entity_id"]
        for entity_id in [ids] if isinstance(ids, str) else ids:
            state = hass.states.get(entity_id)
            attrs = dict(state.attributes) if state else {}
            if call.service == "turn_on":
                if "brightness_pct" in call.data:
                    attrs["brightness"] = round(call.data["brightness_pct"] * 255 / 100)
                if "color_temp_kelvin" in call.data:
                    attrs.update(color_temp_kelvin=call.data["color_temp_kelvin"], color_mode="color_temp")
            hass.states.async_set(entity_id, "on" if call.service == "turn_on" else "off", attrs,
                                  context=call.context)

    hass.services.async_register("light", "turn_on", handler)
    hass.services.async_register("light", "turn_off", handler)
    return seen


def log(ctrl, subject: str) -> list[dict[str, Any]]:
    return ctrl.decisions.entries(subject)


async def editor(ws: WS, area: str) -> dict[str, Any]:
    reply = await ws(type=EDITOR, area_id=area)
    assert reply["success"], reply
    return reply["result"]["room"]


# ------------------------------------------------------------ Observing
async def test_observing_records_what_would_happen(hass: HomeAssistant, lit: Lit, calls: Calls, hass_ws_client,
                                                   freezer) -> None:
    """1, 2, 6, 10, 13: Observing evaluates and logs, sends nothing; one visit is one entry; every session agrees."""
    entry = await setup_entry(hass, options(lit))
    ctrl = ctrl_of(entry)
    ws, other = WS(await hass_ws_client(hass)), WS(await hass_ws_client(hass))
    assert (await editor(ws, lit.area))["decisions"] == {"latest": None, "entries": [], "size": DECISION_LOG_SIZE}

    await occupy(hass, lit, freezer)  # PIR motion, then radar: the visit starts and is confirmed
    entries = log(ctrl, lit.area)
    assert len(entries) == 1, entries  # occupancy confirmed after the first motion is the same visit
    d = entries[0]
    assert (d["action"], d["status"], d["mode"], d["module"], d["trigger"]) == (
        "Lights on", "observed", "observing", "lighting", "motion")
    assert d["explanation"].startswith("Room occupied (binary_sensor.kno_pir detected motion)")
    assert "would switch on" in d["explanation"] and d["explanation"].endswith("Observing: nothing was sent.")
    assert d["details"]["trigger_entity"] == lit.pir and d["details"]["trigger_state"] == "on"
    assert d["details"]["mood"] == "Dagur" and d["details"]["daylight"] == "uncertain"
    assert any(s.startswith("light.kno_lamp: change") for s in d["details"]["targets"])
    assert d["at"].endswith("+00:00")  # timezone-aware
    assert calls.all == []

    # Sensor flicker within the visit (radar holds): no new entry.
    hass.states.async_set(lit.pir, "off")
    await advance(hass, freezer, 5)
    hass.states.async_set(lit.pir, "on")
    await advance(hass, freezer, 5)
    assert len(log(ctrl, lit.area)) == 1

    # A mood change in an Observing room: what the new mood would do.
    set_moods(hass, "Hygge", "Auto", "Hygge")
    await advance(hass, freezer, 3)
    d = log(ctrl, lit.area)[0]
    assert (d["action"], d["status"], d["trigger"]) == ("Apply Hygge", "observed", "mood_change")
    assert d["explanation"].startswith("Mood changed to Hygge: would ") and "switch off light.kno_spots" in d["explanation"]
    assert d["details"]["trigger_entity"] == "input_select.main_mood" and d["details"]["trigger_state"] == "Hygge"
    assert calls.all == []

    # Backend data, identical for every browser session (newest first).
    a, b = await editor(ws, lit.area), await editor(other, lit.area)
    assert a["decisions"] == b["decisions"] and a["decisions"]["latest"] == a["decisions"]["entries"][0]
    assert [e["action"] for e in a["decisions"]["entries"]] == ["Apply Hygge", "Lights on"]
    # Decisions are not entity attributes (no Recorder history of them).
    assert not any("decisions" in s.attributes for s in hass.states.async_all())


# ------------------------------------------------------- Automatic, verified
async def test_automatic_execution_is_verified(hass: HomeAssistant, lit: Lit, freezer) -> None:
    """7: executed only when the lights report the requested state; otherwise sent, not confirmed."""
    sent = obey(hass)
    entry = await setup_entry(hass, options(lit))
    ctrl = ctrl_of(entry)
    await occupy(hass, lit, freezer)
    await activate(hass, entry, lit, freezer)
    d = log(ctrl, lit.area)[0]
    assert (d["action"], d["trigger"], d["mode"]) == ("Apply Dagur", "activated", "automatic") and sent
    assert d["explanation"].startswith("Automatic lighting was switched on for this room: ")
    assert d["status"] == "executed", d  # every light reported the requested state at once
    assert d["details"]["verification"] == "Every target reported the requested state."
    assert any(s.startswith("light.turn_on light.kno_lamp") for s in d["details"]["service"])

    # Lights that accept the command but never report it: sent, then "not confirmed" after the verify time.
    async_mock_service(hass, "light", "turn_on")
    async_mock_service(hass, "light", "turn_off")
    set_moods(hass, "Hygge", "Auto", "Hygge")
    await advance(hass, freezer, 2)
    d = log(ctrl, lit.area)[0]
    assert (d["action"], d["status"]) == ("Apply Hygge", "dispatched") and "verification" not in d["details"]
    await advance(hass, freezer, 8)  # 5 s plus the profile's 2 s transition
    d = log(ctrl, lit.area)[0]
    assert d["status"] == "dispatched" and "not confirmed" in d["details"]["verification"]
    assert d["details"]["result"].endswith("sent and accepted")


async def test_failed_service_calls_are_reported(hass: HomeAssistant, lit: Lit, freezer) -> None:
    """9: Home Assistant's error is recorded; nothing is shown as executed."""
    async def broken(call: ServiceCall) -> None:
        raise HomeAssistantError("Zigbee coordinator offline")

    hass.services.async_register("light", "turn_on", broken)
    hass.services.async_register("light", "turn_off", broken)
    entry = await setup_entry(hass, options(lit))
    ctrl = ctrl_of(entry)
    await occupy(hass, lit, freezer)
    await activate(hass, entry, lit, freezer)
    d = log(ctrl, lit.area)[0]
    assert d["status"] == "failed", d
    assert "Failed: " in d["explanation"] and "Zigbee coordinator offline" in d["explanation"]
    assert d["details"]["errors"][lit.lamp] == "Zigbee coordinator offline"
    await advance(hass, freezer, 10)
    assert log(ctrl, lit.area)[0]["status"] == "failed"  # never upgraded by verification


# ------------------------------------------------------------- blocked
async def test_blocked_actions_and_manual_overrides(hass: HomeAssistant, lit: Lit, freezer) -> None:
    """8: manual overrides, automatic switch-on off and Manual mood are explained."""
    obey(hass)
    entry = await setup_entry(hass, options(lit))
    ctrl = ctrl_of(entry)
    await occupy(hass, lit, freezer)
    await activate(hass, entry, lit, freezer, manual_override_minutes=30)
    # A person switches the lamp off in the app: noted, respected.
    hass.states.async_set(lit.lamp, "off", hass.states.get(lit.lamp).attributes, context=Context(user_id="person"))
    await advance(hass, freezer, 1)
    d = log(ctrl, lit.area)[0]
    assert (d["action"], d["status"], d["kind"]) == ("Respect manual change", "noted", "override")
    assert d["explanation"] == ("light.kno_lamp was switched off from the Home Assistant app: automatic lighting "
                                "leaves it as set for 30 min.")
    # The next mood leaves the lamp alone, and says so.
    set_moods(hass, "Kvøld", "Auto", "Kvøld")
    await advance(hass, freezer, 2)
    d = log(ctrl, lit.area)[0]
    assert d["action"] == "Apply Kvøld" and "Not changed: manual override on light.kno_lamp." in d["explanation"]
    assert any(o.startswith("light.kno_lamp: ui, until ") for o in d["details"]["manual_overrides"])

    # Automatic switch-on is off: a new visit keeps the lights off, blocked, with the reason.
    for e in (lit.group, lit.desk, lit.strip, lit.plug):
        hass.states.async_set(e, "off", hass.states.get(e).attributes)
    hass.states.async_set(lit.pir, "off")
    hass.states.async_set(lit.radar, "off")
    await advance(hass, freezer, 3600)
    ctrl.store.async_set(ctrl.with_room(lit.area, daylight=ctrl.lighting.room(lit.area).daylight.__class__.from_dict(
        {"auto_on": "never"})))
    await advance(hass, freezer, 2)
    await occupy(hass, lit, freezer)
    d = log(ctrl, lit.area)[0]
    assert (d["action"], d["status"]) == ("Keep lights off", "blocked"), d
    assert d["trigger"] in ("motion", "occupancy_confirmed")
    assert d["explanation"].endswith("but automatic switch-on is off for this room.")
    assert d["details"]["auto_on"] == "never"

    # Manual mood: the next visit is suppressed and says why.
    hass.states.async_set(lit.pir, "off")
    hass.states.async_set(lit.radar, "off")
    await advance(hass, freezer, 3600)
    set_moods(hass, "Dagur", "Manual", "Manual", main_opts=["Dagur", "Kvøld", "Manual"])
    await advance(hass, freezer, 2)
    await occupy(hass, lit, freezer)
    d = log(ctrl, lit.area)[0]
    assert (d["status"], d["mode"]) == ("blocked", "paused"), d
    assert "Main Mood is Manual: automatic lighting is paused and nothing is sent." in d["explanation"]


# ----------------------------------------------- bounded, ordered, isolated
async def test_bounded_ordered_isolated_and_deduplicated(hass: HomeAssistant, lit: Lit, calls: Calls,
                                                         freezer) -> None:
    """3, 4, 5, 10: ten per room, newest first, rooms independent, repeats folded into one entry."""
    entry = await setup_entry(hass, options(lit))
    log_ = ctrl_of(entry).decisions
    for i in range(DECISION_LOG_SIZE + 5):
        log_.record("room_a", module="lighting", trigger="t", kind="k", action=f"A{i}", explanation=f"a {i}",
                    mode="observing", status="observed", key=("a", i))
        await advance(hass, freezer, 1)
    log_.record("room_b", module="lighting", trigger="t", kind="k", action="B", explanation="b", mode="automatic",
                status="no_change", key=("b",))
    a = log_.entries("room_a")
    assert len(a) == DECISION_LOG_SIZE and [d["action"] for d in a] == [f"A{i}" for i in range(14, 4, -1)]
    assert [d["at"] for d in a] == sorted((d["at"] for d in a), reverse=True)
    assert [d["action"] for d in log_.entries("room_b")] == ["B"]
    # The same decision again: one entry, counted, latest time; a different result is a new entry.
    first = log_.entries("room_b")[0]
    await advance(hass, freezer, 30)
    log_.record("room_b", module="lighting", trigger="t", kind="k", action="B", explanation="b again",
                mode="automatic", status="no_change", key=("b",))
    b = log_.entries("room_b")
    assert len(b) == 1 and b[0]["count"] == 2 and b[0]["first_at"] == first["at"] and b[0]["at"] > first["at"]
    assert b[0]["explanation"] == "b again" and b[0]["id"] == first["id"]
    log_.record("room_b", module="lighting", trigger="t", kind="k", action="B", explanation="b", mode="automatic",
                status="blocked", key=("b",))
    assert len(log_.entries("room_b")) == 2
    assert log_.entries("room_a")[0]["action"] == "A14"  # untouched by room_b


async def test_check_confirms_state_and_brightness(hass: HomeAssistant) -> None:
    hass.states.async_set("light.x", "on", {"brightness": 128})
    assert Check("light.x", "on", 50).reached(hass)
    assert not Check("light.x", "on", 80).reached(hass)
    assert not Check("light.x", "off").reached(hass)
    hass.states.async_set("light.x", "unavailable")
    assert not Check("light.x", "on").reached(hass)


# -------------------------------------------------------------- persistence
async def test_log_survives_reload_and_mode_changes(hass: HomeAssistant, lit: Lit, calls: Calls, hass_ws_client,
                                                    hass_storage, freezer) -> None:
    """11: kept in the existing state store; a restart and mode changes keep it."""
    entry = await setup_entry(hass, options(lit))
    ctrl = ctrl_of(entry)
    ws = WS(await hass_ws_client(hass))
    entities = len(hass.states.async_all())
    await occupy(hass, lit, freezer)
    set_moods(hass, "Hygge", "Auto", "Hygge")
    await advance(hass, freezer, 3)
    before = log(ctrl, lit.area)
    assert len(before) == 2
    assert len(hass.states.async_all()) == entities  # no entity or helper per decision
    await advance(hass, freezer, 11)  # the state store's delayed save
    stored = hass_storage[LIGHTING_STATE_STORE_KEY]["data"]["decisions"]
    assert [d["id"] for d in stored["logs"][lit.area]] == [d["id"] for d in reversed(before)]
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    ctrl = ctrl_of(entry)
    assert log(ctrl, lit.area) == before
    # Observing -> Automatic -> Observing: history kept, both changes logged.
    assert (await ws(type="smart_occupancy/lighting/room/activate", area_id=lit.area, confirm=True))["success"]
    await advance(hass, freezer, 2)
    assert (await ws(type="smart_occupancy/lighting/room/deactivate", area_id=lit.area))["success"]
    await advance(hass, freezer, 2)
    after = log(ctrl, lit.area)
    assert [d["action"] for d in after[:2]] == ["Set to Observing", "Apply Hygge"]
    assert after[1]["trigger"] == "activated" and after[2:] == before


# ----------------------------------------------------------------- switches
async def test_switch_decisions_follow_the_switch_mode(hass: HomeAssistant, mqtt, lit: Lit, hass_ws_client,
                                                       freezer, caplog) -> None:
    """12, 14: Observing / Active presses, no action, failures; repeats folded; switch logs separate from rooms."""
    caplog.set_level(logging.CRITICAL, logger=WS_LOGGER)
    sent = obey(hass)
    entry = await setup_entry(hass, options(lit))
    ws = WS(await hass_ws_client(hass))
    await occupy(hass, lit, freezer)
    switch_id = await controlled(hass, ws, lit, freezer, entry,
                                 on_right={"action": "set_brightness", "brightness": 40, "target": lit.lamp},
                                 off_left={"action": "none"})
    ctrl = ctrl_of(entry)
    subject = f"{lit.area}/{switch_id}"
    room_before = log(ctrl, lit.area)

    # Active (room Observing): executed, verified from the lamp's state, override noted in the room's log.
    sent.clear()
    press(hass, "on_right")
    await settle(hass, freezer)
    assert len(sent) == 1
    d = log(ctrl, subject)[0]
    assert (d["module"], d["mode"], d["action"], d["status"]) == ("switch", "active", "Set brightness 40 % (light.kno_lamp)",
                                                                 "executed"), d
    assert d["explanation"].startswith("Button ON right: light.kno_lamp set to 40 %")
    assert "leaves these lights as set for 60 min" in d["explanation"]
    assert d["details"]["button"] == "on_right" and d["details"]["input"] == "zigbee2mqtt"
    assert d["target"] == "light.kno_lamp"
    r = log(ctrl, lit.area)[0]
    assert (r["action"], r["status"]) == ("Respect manual change", "noted") and r["details"]["switch"] == "avb_ksu"
    assert len(log(ctrl, lit.area)) == len(room_before) + 1  # one room entry per press, not per light report

    # The same press again soon after: one entry, counted.
    await advance(hass, freezer, 2)
    press(hass, "on_right")
    await settle(hass, freezer)
    entries = log(ctrl, subject)
    assert entries[0]["count"] == 2 and len([e for e in entries if e["action"].startswith("Set brightness")]) == 1

    # A button without an action.
    press(hass, "off_left")
    await settle(hass, freezer)
    d = log(ctrl, subject)[0]
    assert (d["action"], d["status"]) == ("No action", "no_action")
    assert d["explanation"] == "Button OFF left: no action is assigned to this button (registered as a manual action)."

    # Home Assistant rejects the command: failed, with its error.
    async def broken(call: ServiceCall) -> None:
        raise HomeAssistantError("Zigbee coordinator offline")

    hass.services.async_register("light", "turn_on", broken)
    await advance(hass, freezer, 1)
    press(hass, "on_right")
    await settle(hass, freezer)
    d = log(ctrl, subject)[0]
    assert d["status"] == "failed" and "Zigbee coordinator offline" in d["explanation"]

    # Observing: registered, nothing executed, and the log says what would have run.
    obey(hass)
    assert (await ws(type="smart_occupancy/lighting/switch/mode", area_id=lit.area, switch_id=switch_id,
                     mode="listen"))["success"]
    sent = obey(hass)
    await advance(hass, freezer, 1)  # beyond the switch's debounce time
    press(hass, "on_right")
    await settle(hass, freezer)
    assert sent == []
    d = log(ctrl, subject)[0]
    assert (d["mode"], d["status"]) == ("observing", "observed")
    assert "Set brightness 40 % (light.kno_lamp) is assigned but not executed" in d["explanation"]

    # The switch card's model carries the switch's own log.
    room = await editor(ws, lit.area)
    item = next(x for x in room["switches"]["items"] if x["id"] == switch_id)
    assert item["decisions"]["latest"] == d and len(item["decisions"]["entries"]) <= DECISION_LOG_SIZE
    assert all(e["module"] == "lighting" for e in room["decisions"]["entries"])

    # Removing the switch removes its log; the room keeps its own.
    assert (await ws(type="smart_occupancy/lighting/switch/remove", area_id=lit.area, switch_id=switch_id))["success"]
    await settle(hass, freezer)
    assert log(ctrl, subject) == [] and log(ctrl, lit.area)


# ------------------------------------------------- gradual updates, vacancy
async def test_gradual_updates_fold_and_manual_mood_drop(hass: HomeAssistant, home, lights, freezer) -> None:
    """10: global adjustment updates are one (counted) entry; a change dropped by Manual mood is logged."""
    from .test_adaptive_runtime import ready, set_moods as moods, set_sources

    entry = await ready(hass, home, lights, freezer)
    ctrl = ctrl_of(entry)
    for value in (70, 80, 90):
        await advance(hass, freezer, 61)
        set_sources(hass, value, 3200)
        await hass.async_block_till_done()
        await advance(hass, freezer, 3)
    entries = [d for d in log(ctrl, home.area) if d["kind"] == "adaptive"]
    assert len(entries) == 1 and entries[0]["count"] == 3, entries
    d = entries[0]
    assert d["action"] == "Follow global adjustment" and d["status"] in ("executed", "dispatched")
    assert d["explanation"].startswith("The global light adjustment changed: ") and "90 %" in d["explanation"]
    # Manual starts while a mood change is queued: the change is dropped, and the log says so.
    await advance(hass, freezer, 61)
    moods(hass, "Hygge")
    await hass.async_block_till_done()
    moods(hass, "Manual")
    await hass.async_block_till_done()
    await advance(hass, freezer, 5)
    d = log(ctrl, home.area)[0]
    assert (d["action"], d["status"], d["mode"]) == ("Queued change dropped", "blocked", "paused")
    assert d["explanation"] == "Main Mood became Manual: the queued lighting change (mood changed) was not sent."


async def test_vacancy_is_explained(hass: HomeAssistant, lit: Lit, calls: Calls, freezer) -> None:
    """Lights left on when a room empties are explained (Smart Occupancy has no vacancy switch-off)."""
    entry = await setup_entry(hass, options(lit))
    ctrl = ctrl_of(entry)
    await occupy(hass, lit, freezer)
    hass.states.async_set(lit.pir, "off")
    hass.states.async_set(lit.radar, "off")
    await advance(hass, freezer, 3600)
    d = log(ctrl, lit.area)[0]
    assert (d["action"], d["status"], d["trigger"]) == ("Leave lights on", "no_change", "vacant"), d
    assert d["explanation"].startswith("Vacant: no activity for ")
    assert "light.kno_spots is left on: Smart Occupancy does not switch lights off when a room becomes vacant." \
        in d["explanation"]
    assert calls.all == []
