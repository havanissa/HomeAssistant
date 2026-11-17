"""Presence API (0.6.10) used by the Smart Lighting App (Phase A3.1).

The App shows and configures the existing occupancy engine through these
commands; the engine stays in the integration. The tests prove: the live view
is the engine's own state (no invented values), timing inheritance (global
defaults -> room overrides -> restore default), server-side validation, stale
writes refused while unrelated scopes stay writable, consequences for
Automatic rooms confirmed, the decision right after saving predicted from a
copy of the engine (and equal to what really happens), live apply keeps
occupancy, unrelated options kept, the bounded presence log, diagnostics
(measured vs heuristic), and no device command from any of it.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant

from custom_components.smart_occupancy.const import TIMING_PARAMETERS, VERSION
from custom_components.smart_occupancy.presence_log import LOG_SIZE, PresenceLog, presence_log
from custom_components.smart_occupancy.rooms_api import revision_of

from .conftest import Pilot, pilot_options, setup_entry
from .test_decision_log import obey
from .test_lighting_runtime import WS_LOGGER, Lit, advance, ctrl_of, lit, occupy, options  # noqa: F401
from .test_rooms_api import client

GET = "smart_occupancy/presence/get"
REVIEW = "smart_occupancy/presence/review"
SET = "smart_occupancy/presence/set"
OLD_TIMING = "smart_occupancy/lighting/presence/timing"


async def get(ws, area_id: str | None = None) -> dict[str, Any]:
    reply = await ws(type=GET, **({"area_id": area_id} if area_id else {}))
    assert reply["success"], reply
    return reply["result"]


async def review(ws, area_id: str | None, change: dict[str, Any], revision: str | None = None) -> dict[str, Any]:
    reply = await ws(type=REVIEW, area_id=area_id, change=change, expected_revision=revision)
    assert reply["success"], reply
    return reply["result"]


def pilot_opts(p: Pilot, **extra: Any) -> dict[str, Any]:
    opts = pilot_options(p)
    opts.update(extra)
    return opts


async def with_unknown_keys(hass: HomeAssistant, entry) -> None:
    """Keys this version does not know (a newer version's, or a hand edit) must survive every write."""
    opts = dict(entry.options)
    opts["future_option"] = 7
    opts["rooms"] = {**opts["rooms"], **{a: dict(r) for a, r in opts["rooms"].items()}}
    for room in opts["rooms"].values():
        room["custom_key"] = {"kept": True}
    hass.config_entries.async_update_entry(entry, options=opts)
    await hass.async_block_till_done()


# ------------------------------------------------------------------- reading
async def test_get_is_the_engines_own_view(hass: HomeAssistant, pilot: Pilot, hass_ws_client, freezer) -> None:
    entry = await setup_entry(hass, pilot_opts(pilot))
    ws = await client(hass, hass_ws_client)
    hass.states.async_set(pilot.pir, "on")
    await hass.async_block_till_done()
    data = await get(ws)
    assert data["integration_version"] == VERSION and data["api_version"] == 1 and data["writable"]
    assert data["global"]["revision"] == revision_of(entry.options.get("occupancy"))
    assert {t["key"] for t in data["timing_schema"]} == set(TIMING_PARAMETERS)
    stuck = next(t for t in data["timing_schema"] if t["key"] == "trigger_stuck_after")
    assert stuck["zero"] and stuck["min_nonzero"] == 60
    assert {s["value"] for s in data["states"]} == {"vacant", "possible_occupancy", "confirmed_occupancy",
                                                     "possible_vacancy", "uncertain"}
    room = data["rooms"][pilot.area]
    engine = entry.runtime_data.occupancy.engines[pilot.area]
    assert room["occupancy"]["state"] == engine.result.state.value == "possible_occupancy"
    assert room["occupancy"]["confidence"]["score"] == engine.result.confidence.score
    assert room["revision"] == revision_of(entry.options["rooms"][pilot.area])
    signals = {s["entity_id"]: s for s in room["signals"]}
    pir = signals[pilot.pir]
    assert pir["status"] == "active" and pir["effect"] == "trigger" and pir["since"]
    # Confirmation by sustained activity: since + correlation window (10 s), from the engine's rule.
    assert pir["deadlines"]["sustained_at"] and pir["deadlines"]["stuck_at"]
    assert signals[pilot.door]["effect"] == "door" and signals[pilot.radar]["effect"] == "presence"
    nxt = room["explain"]["next"][0]
    assert nxt["code"] == "await_confirmation" and nxt["at"] == pir["deadlines"]["sustained_at"]
    assert room["entities"]["state"] and room["entities"]["state"].startswith("sensor.")
    assert room["timing"][0]["key"] == "inactivity_timeout" and room["timing"][0]["overridden"] is False
    assert room["automation"]["lighting_automatic"] is False
    assert data["log"][0]["kind"] == "start"
    # After the window the engine confirms it; the log has the transition with its evidence.
    await advance(hass, freezer, 11)
    data = await get(ws, pilot.area)
    assert set(data["rooms"]) == {pilot.area}
    assert data["rooms"][pilot.area]["occupancy"]["state"] == "confirmed_occupancy"
    transitions = [e for e in data["log"] if e["kind"] == "transition"]
    assert [(e["previous_state"], e["state"]) for e in transitions][-2:] == [
        ("vacant", "possible_occupancy"), ("possible_occupancy", "confirmed_occupancy")]
    assert transitions[-1]["evidence"][0]["entity_id"] == pilot.pir


async def test_commands_are_admin_only(hass: HomeAssistant, pilot: Pilot, hass_ws_client, hass_read_only_user,
                                       caplog) -> None:
    caplog.set_level(logging.CRITICAL, logger=WS_LOGGER)
    await setup_entry(hass, pilot_opts(pilot))
    ws = await client(hass, hass_ws_client, hass_read_only_user)
    for msg in ({"type": GET}, {"type": REVIEW, "change": {}},
                {"type": SET, "change": {"timing": {"inactivity_timeout": 300}}, "expected_revision": "x"}):
        reply = await ws(**msg)
        assert not reply["success"] and reply["error"]["code"] == "unauthorized", msg


def test_log_is_bounded() -> None:
    log = PresenceLog()
    for i in range(LOG_SIZE + 50):
        log.add("transition", "a" if i % 2 else "b", state="vacant")
    assert len(log.entries) == LOG_SIZE and log.seq == LOG_SIZE + 50
    assert all(e["area_id"] == "a" for e in log.view("a"))
    log.add("start", None)
    assert log.view("a")[-1]["kind"] == "start"  # global entries belong to every room


# ------------------------------------------------------------------ timing
async def test_global_defaults_inheritance_and_overrides(hass: HomeAssistant, pilot: Pilot, hass_ws_client,
                                                         freezer) -> None:
    seen = obey(hass)
    entry = await setup_entry(hass, pilot_opts(pilot))
    await with_unknown_keys(hass, entry)
    coordinator = entry.runtime_data
    ws = await client(hass, hass_ws_client)
    data = await get(ws)
    rev = data["global"]["revision"]
    plan = await review(ws, None, {"timing": {"inactivity_timeout": 900, "dropout_grace": 30}}, rev)
    assert plan["scope"] == "global" and plan["apply"] == "live" and not plan["stale"]
    assert plan["changes"] == [{"type": "timing", "scope": "global", "key": "inactivity_timeout", "old": 600,
                                "new": 900}]  # 30 is already the default: not a change
    assert {r["area_id"] for r in plan["affected_rooms"]} == {pilot.area, pilot.hall}
    assert plan["requires_confirmation"] == []  # no Automatic room, no decision changes
    reply = await ws(type=SET, area_id=None, change={"timing": {"inactivity_timeout": 900}}, expected_revision=rev)
    assert reply["success"], reply
    await hass.async_block_till_done()
    assert entry.runtime_data is coordinator  # applied live, no reload
    assert entry.options["occupancy"]["inactivity_timeout"] == 900
    assert entry.options["future_option"] == 7 and entry.options["rooms"][pilot.area]["custom_key"] == {"kept": True}
    assert coordinator.occupancy.engines[pilot.hall].timing.inactivity_timeout == 900  # inherits

    # A room override; the other room keeps inheriting.
    data = await get(ws)
    room_rev = data["rooms"][pilot.area]["revision"]
    plan = await review(ws, pilot.area, {"timing": {"inactivity_timeout": 300}}, room_rev)
    assert plan["changes"] == [{"type": "timing", "scope": "room", "key": "inactivity_timeout", "old": None,
                                "new": 300, "old_effective": 900, "new_effective": 300}]
    assert [r["area_id"] for r in plan["affected_rooms"]] == [pilot.area]
    assert (await ws(type=SET, area_id=pilot.area, change={"timing": {"inactivity_timeout": 300}},
                     expected_revision=room_rev))["success"]
    await hass.async_block_till_done()
    data = await get(ws)
    rows = {r["key"]: r for r in data["rooms"][pilot.area]["timing"]}
    assert rows["inactivity_timeout"] == {**rows["inactivity_timeout"], "value": 300, "global": 900,
                                          "overridden": True}
    assert entry.options["rooms"][pilot.area]["custom_key"] == {"kept": True}
    # A global change no longer affects the overriding room.
    plan = await review(ws, None, {"timing": {"inactivity_timeout": 1200}}, data["global"]["revision"])
    assert [r["area_id"] for r in plan["affected_rooms"]] == [pilot.hall]
    # Restore the default (null) for one key, then reset everything.
    room_rev = data["rooms"][pilot.area]["revision"]
    assert (await ws(type=SET, area_id=pilot.area, change={"timing": {"inactivity_timeout": None,
                                                                      "correlation_window": 20}},
                     expected_revision=room_rev))["success"]
    await hass.async_block_till_done()
    assert entry.options["rooms"][pilot.area]["timing"] == {"correlation_window": 20}
    data = await get(ws)
    assert (await ws(type=SET, area_id=pilot.area, change={"reset_timing": True},
                     expected_revision=data["rooms"][pilot.area]["revision"]))["success"]
    await hass.async_block_till_done()
    assert entry.options["rooms"][pilot.area]["timing"] == {}
    # Global null = the engine's default.
    data = await get(ws)
    assert (await ws(type=SET, area_id=None, change={"timing": {"inactivity_timeout": None}},
                     expected_revision=data["global"]["revision"]))["success"]
    await hass.async_block_till_done()
    assert entry.options["occupancy"]["inactivity_timeout"] == TIMING_PARAMETERS["inactivity_timeout"][0]
    log = [e for e in presence_log(hass).view() if e["kind"] == "config"]
    assert log and all(e["source"] == "app" and e["applied"] == "live" for e in log)
    assert log[0]["area_id"] is None and log[0]["changes"][0]["key"] == "inactivity_timeout"
    assert entry.runtime_data is coordinator
    assert seen == []


async def test_invalid_values_are_refused(hass: HomeAssistant, pilot: Pilot, hass_ws_client, caplog) -> None:
    caplog.set_level(logging.CRITICAL, logger=WS_LOGGER)
    entry = await setup_entry(hass, pilot_opts(pilot))
    before = dict(entry.options)
    ws = await client(hass, hass_ws_client)
    data = await get(ws)
    rev = data["global"]["revision"]
    for values, code in (({"inactivity_timeout": 10}, "timing_range"), ({"correlation_window": 0}, "timing_range"),
                         ({"trigger_stuck_after": 30}, "timing_stuck"), ({"startup_hold": 999999}, "timing_range")):
        plan = await review(ws, None, {"timing": values}, rev)
        assert [e["code"] for e in plan["errors"]] == [code], values
        assert plan["changes"] == [] and plan["apply"] == "none"
        reply = await ws(type=SET, area_id=None, change={"timing": values}, expected_revision=rev)
        assert not reply["success"] and reply["error"]["code"] == "invalid_room", values
    assert (await review(ws, None, {"timing": {"trigger_stuck_after": 0}}, rev))["errors"] == []  # 0 = off
    for change in ({"timing": {"not_a_setting": 5}}, {"sensors": []}, {"timing": {"inactivity_timeout": "x"}}):
        reply = await ws(type=REVIEW, area_id=None, change=change)
        assert not reply["success"] and reply["error"]["code"] == "invalid_change", change
    reply = await ws(type=REVIEW, area_id="not_a_room", change={"timing": {}})
    assert not reply["success"] and reply["error"]["code"] == "not_configured"
    # Sensor validation is the Rooms API's: a missing entity and a wrong domain are errors.
    room_rev = data["rooms"][pilot.area]["revision"]
    sensors = [s | {} for s in data["rooms"][pilot.area]["sensors"]]
    plan = await review(ws, pilot.area, {"sensors": [*sensors, {"entity_id": "binary_sensor.nope", "kind": "pir"}]},
                        room_rev)
    assert [e["code"] for e in plan["errors"]] == ["entity_missing"]
    plan = await review(ws, pilot.area, {"sensors": [*sensors, {"entity_id": pilot.light, "kind": "pir"}]}, room_rev)
    assert [e["code"] for e in plan["errors"]] == ["sensor_domain"]
    assert dict(entry.options) == before


async def test_stale_writes_and_concurrent_scopes(hass: HomeAssistant, pilot: Pilot, hass_ws_client,
                                                  caplog) -> None:
    caplog.set_level(logging.CRITICAL, logger=WS_LOGGER)
    entry = await setup_entry(hass, pilot_opts(pilot))
    ws = await client(hass, hass_ws_client)
    data = await get(ws)
    room_rev, global_rev = data["rooms"][pilot.area]["revision"], data["global"]["revision"]
    # Someone else changes the room through the older Presence tab command (the room card).
    assert (await ws(type=OLD_TIMING, area_id=pilot.area, values={"exit_confirmation": 120}))["success"]
    await hass.async_block_till_done()
    plan = await review(ws, pilot.area, {"timing": {"inactivity_timeout": 300}}, room_rev)
    assert plan["stale"]
    reply = await ws(type=SET, area_id=pilot.area, change={"timing": {"inactivity_timeout": 300}},
                     expected_revision=room_rev)
    assert not reply["success"] and reply["error"]["code"] == "stale_revision"
    assert entry.options["rooms"][pilot.area]["timing"] == {"exit_confirmation": 120}
    # Another room and the global defaults are not affected by that edit: still writable with their revisions.
    assert (await ws(type=SET, area_id=pilot.hall, change={"timing": {"inactivity_timeout": 300}},
                     expected_revision=data["rooms"][pilot.hall]["revision"]))["success"]
    await hass.async_block_till_done()
    assert (await ws(type=SET, area_id=None, change={"timing": {"correlation_window": 15}},
                     expected_revision=global_rev))["success"]
    await hass.async_block_till_done()
    log = [e for e in presence_log(hass).view() if e["kind"] == "config"]
    assert log[0]["source"] == "integration" and log[0]["changes"][0]["type"] == "timing_override"


# ------------------------------------------------------------- consequences
async def test_automatic_rooms_need_confirmation(hass: HomeAssistant, lit: Lit, hass_ws_client, freezer,
                                                 caplog) -> None:
    caplog.set_level(logging.CRITICAL, logger=WS_LOGGER)
    seen = obey(hass)
    entry = await setup_entry(hass, options(lit))
    ctrl = ctrl_of(entry)
    await occupy(hass, lit, freezer)
    ctrl.store.async_set(ctrl.with_room(lit.area, control_enabled=True))
    await hass.async_block_till_done()
    await advance(hass, freezer)
    seen.clear()
    ws = await client(hass, hass_ws_client)
    data = await get(ws)
    assert data["rooms"][lit.area]["automation"]["lighting_automatic"] is True
    rev = data["global"]["revision"]
    plan = await review(ws, None, {"timing": {"inactivity_timeout": 1200}}, rev)
    assert plan["requires_confirmation"] == ["automatic_rooms"]
    reply = await ws(type=SET, area_id=None, change={"timing": {"inactivity_timeout": 1200}}, expected_revision=rev)
    assert not reply["success"] and reply["error"]["code"] == "confirmation_required"
    reply = await ws(type=SET, area_id=None, change={"timing": {"inactivity_timeout": 1200}}, expected_revision=rev,
                     confirmed=["automatic_rooms"])
    assert reply["success"], reply
    for _ in range(5):
        await advance(hass, freezer, 2)
    assert seen == []  # occupied stays occupied: nothing switched


async def test_prediction_equals_what_happens(hass: HomeAssistant, pilot: Pilot, hass_ws_client, freezer,
                                              caplog) -> None:
    """A shorter vacancy timeout while vacancy is pending: the review predicts Vacant at once, and it is."""
    caplog.set_level(logging.CRITICAL, logger=WS_LOGGER)
    entry = await setup_entry(hass, pilot_opts(pilot))
    hass.states.async_set(pilot.radar, "on")
    hass.states.async_set(pilot.pir, "on")
    await hass.async_block_till_done()
    await advance(hass, freezer, 12)
    hass.states.async_set(pilot.radar, "off")
    hass.states.async_set(pilot.pir, "off")
    await hass.async_block_till_done()
    for _ in range(75):  # 150 s quiet (past the 120 s start-up hold): vacancy pending, 600 s needed
        await advance(hass, freezer, 2)
    engine = entry.runtime_data.occupancy.engines[pilot.area]
    assert engine.result.state.value == "possible_vacancy" and engine.result.occupied
    ws = await client(hass, hass_ws_client)
    data = await get(ws, pilot.area)
    room = data["rooms"][pilot.area]
    assert room["explain"]["next"][0]["code"] == "await_vacancy"
    assert room["explain"]["next"][0]["at"] == room["occupancy"]["vacancy_at"]
    assert room["explain"]["vacancy_path"] == "normal"
    plan = await review(ws, pilot.area, {"timing": {"inactivity_timeout": 60}}, room["revision"])
    prediction = next(i for i in plan["impacts"] if i["code"] == "prediction")["predictions"][0]
    assert (prediction["state"], prediction["new_state"], prediction["new_occupied"]) == (
        "possible_vacancy", "vacant", False)
    assert "decision_changes_now" in plan["requires_confirmation"]  # occupied -> vacant at once
    assert engine.result.state.value == "possible_vacancy"  # the review changed nothing
    # A longer timeout only moves the pending vacancy.
    plan_later = await review(ws, pilot.area, {"timing": {"inactivity_timeout": 1200}}, room["revision"])
    assert "vacancy_moves" in {i["code"] for i in plan_later["impacts"]}
    assert plan_later["requires_confirmation"] == []
    reply = await ws(type=SET, area_id=pilot.area, change={"timing": {"inactivity_timeout": 60}},
                     expected_revision=room["revision"], confirmed=plan["requires_confirmation"])
    assert reply["success"], reply
    await hass.async_block_till_done()
    assert engine is entry.runtime_data.occupancy.engines[pilot.area]  # live: the same engine
    assert (engine.result.state.value, engine.result.occupied) == ("vacant", False)


async def test_sensor_changes_live_or_reload(hass: HomeAssistant, pilot: Pilot, hass_ws_client, freezer) -> None:
    entry = await setup_entry(hass, pilot_opts(pilot))
    await with_unknown_keys(hass, entry)
    coordinator = entry.runtime_data
    hass.states.async_set(pilot.radar, "on")
    await hass.async_block_till_done()
    await advance(hass, freezer, 12)
    ws = await client(hass, hass_ws_client)
    room = (await get(ws))["rooms"][pilot.area]
    sensors = [dict(s) for s in room["sensors"]]
    pc = next(s for s in sensors if s["entity_id"] == pilot.pc)
    pc["role"] = "confirm_vacancy"
    pc["inverted"] = True
    plan = await review(ws, pilot.area, {"sensors": sensors}, room["revision"])
    assert plan["apply"] == "live"
    assert {c["type"] for c in plan["changes"]} == {"sensor_role", "sensor_inverted"}
    # The inverted PC sensor (off) now counts as active: the prediction shows it.
    prediction = next(i for i in plan["impacts"] if i["code"] == "prediction")["predictions"][0]
    assert prediction["new_state"] == "confirmed_occupancy"
    assert (await ws(type=SET, area_id=pilot.area, change={"sensors": sensors},
                     expected_revision=room["revision"]))["success"]
    await hass.async_block_till_done()
    assert entry.runtime_data is coordinator
    stored = {s["entity_id"]: s for s in entry.options["rooms"][pilot.area]["sensors"]}
    assert stored[pilot.pc] == {"entity_id": pilot.pc, "kind": "pc_activity", "role": "confirm_vacancy",
                                "inverted": True}
    track = coordinator.occupancy.engines[pilot.area].tracks[pilot.pc]
    assert track.value.value == "active"  # re-read with the inverted flag
    assert coordinator.occupancy.engines[pilot.area].result.occupied is True  # occupancy kept

    # Removing a sensor reloads; occupancy is restored, the unknown key kept.
    room = (await get(ws))["rooms"][pilot.area]
    sensors = [s for s in room["sensors"] if s["entity_id"] != pilot.door]
    plan = await review(ws, pilot.area, {"sensors": sensors}, room["revision"])
    assert plan["apply"] == "reload" and plan["changes"][0]["type"] == "sensor_removed"
    assert (await ws(type=SET, area_id=pilot.area, change={"sensors": sensors},
                     expected_revision=room["revision"]))["success"]
    await hass.async_block_till_done()
    assert entry.runtime_data is not coordinator
    assert entry.options["rooms"][pilot.area]["custom_key"] == {"kept": True}
    assert entry.runtime_data.occupancy.engines[pilot.area].result.occupied is True
    log = presence_log(hass).view(pilot.area)
    assert [e["kind"] for e in log][-2:] == ["config", "start"] and log[-2]["applied"] == "reload"


async def test_configuration_survives_a_restart(hass: HomeAssistant, pilot: Pilot, hass_ws_client) -> None:
    entry = await setup_entry(hass, pilot_opts(pilot))
    ws = await client(hass, hass_ws_client)
    data = await get(ws)
    assert (await ws(type=SET, area_id=None, change={"timing": {"exit_confirmation": 90}},
                     expected_revision=data["global"]["revision"]))["success"]
    assert (await ws(type=SET, area_id=pilot.area, change={"timing": {"correlation_window": 20}},
                     expected_revision=data["rooms"][pilot.area]["revision"]))["success"]
    await hass.async_block_till_done()
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    engines = entry.runtime_data.occupancy.engines
    assert engines[pilot.area].timing.correlation_window == 20 and engines[pilot.area].timing.exit_confirmation == 90
    assert engines[pilot.hall].timing.correlation_window == 10 and engines[pilot.hall].timing.exit_confirmation == 90


# -------------------------------------------------------------- diagnostics
async def test_diagnostics_measured_and_heuristic(hass: HomeAssistant, pilot: Pilot, hass_ws_client,
                                                  freezer) -> None:
    opts = pilot_opts(pilot, occupancy={"trigger_stuck_after": 60, "exit_confirmation": 900})
    sensors = opts["rooms"][pilot.area]["sensors"]
    sensors[1]["inverted"] = True  # radar with device class occupancy, inverted
    sensors[2]["role"] = "trigger"  # PC activity as Trigger
    hass.states.async_set(pilot.radar, "off", {"device_class": "occupancy", "friendly_name": "Radar"})
    await setup_entry(hass, opts)
    hass.states.async_set(pilot.pir, "on")
    await hass.async_block_till_done()
    for _ in range(35):
        await advance(hass, freezer, 2)
    hass.states.async_set(pilot.pc, "busy_state_nobody_knows")
    await hass.async_block_till_done()
    ws = await client(hass, hass_ws_client)
    diags = (await get(ws))["rooms"][pilot.area]["diagnostics"]
    by_code = {d["code"]: d for d in diags}
    assert by_code["signal_stuck"]["basis"] == "measured" and by_code["signal_stuck"]["entity_id"] == pilot.pir
    assert by_code["signal_stuck"]["setting"] == "trigger_stuck_after"
    assert by_code["state_not_understood"]["entity_id"] == pilot.pc
    assert by_code["inverted_presence"]["basis"] == "heuristic"
    assert by_code["presence_as_trigger"]["entity_id"] == pilot.pc
    assert by_code["fast_slower"]["target"] == "timing" and by_code["fast_slower"]["setting"] == "exit_confirmation"
    assert all(d["level"] in ("error", "warning", "info") and d["title"] for d in diags)
    hall = {d["code"] for d in (await get(ws))["rooms"][pilot.hall]["diagnostics"]}
    assert {"no_presence_sensor", "single_signal"} <= hall


async def test_not_running_shows_the_stored_configuration(hass: HomeAssistant, pilot: Pilot, hass_ws_client,
                                                          caplog) -> None:
    caplog.set_level(logging.CRITICAL, logger=WS_LOGGER)
    entry = await setup_entry(hass, pilot_opts(pilot, occupancy={"inactivity_timeout": 900}))
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    ws = await client(hass, hass_ws_client)
    data = await get(ws)
    assert data["loaded"] is False and data["writable"] is False
    room = data["rooms"][pilot.area]  # still listed as configured: no engine, nothing invented
    assert room["configured"] and room["occupancy"] is None and room["signals"] == []
    assert room["revision"] == revision_of(entry.options["rooms"][pilot.area])
    assert {r["key"]: r["value"] for r in room["timing"]}["inactivity_timeout"] == 900
    reply = await ws(type=SET, area_id=None, change={"timing": {"inactivity_timeout": 300}},
                     expected_revision=data["global"]["revision"])
    assert not reply["success"] and reply["error"]["code"] == "not_loaded"


async def test_rooms_api_changes_are_logged_as_the_apps(hass: HomeAssistant, pilot: Pilot, hass_ws_client) -> None:
    entry = await setup_entry(hass, pilot_opts(pilot))
    ws = await client(hass, hass_ws_client)
    room = (await ws(type="smart_occupancy/rooms/get"))["result"]["rooms"][pilot.area]
    edit = {k: room["config"].get(k) for k in ("enabled", "sensors", "lights", "reference_occupancy_entity")}
    edit["adjacent_rooms"] = []
    edit["sensors"] = [dict(s) for s in edit["sensors"]]
    edit["sensors"][1]["enabled"] = False
    reply = await ws(type="smart_occupancy/rooms/set", area_id=pilot.area, room=edit, expected_revision=room["revision"])
    assert reply["success"], reply
    await hass.async_block_till_done()
    log = [e for e in presence_log(hass).view() if e["kind"] == "config"]
    assert log[-1]["source"] == "app" and log[-1]["changes"][0]["type"] == "sensor_enabled"
    assert entry.state.value == "loaded"
