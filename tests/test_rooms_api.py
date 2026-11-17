"""Rooms configuration API (0.6.9) used by the Smart Lighting App (Phase A2).

The App edits the existing config entry options through these commands. The
tests prove: one authoritative configuration (the options), unrelated keys and
sensor settings preserved, validation reused, stale writes refused, disruptive
changes confirmed, the options flow and the API interchangeable, and no device
command from any configuration change (including the reload it causes).
"""

from __future__ import annotations

import copy
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import area_registry as ar, entity_registry as er

from custom_components.smart_occupancy.const import VERSION
from custom_components.smart_occupancy.rooms_api import build_room, diff_room, revision_of

from .conftest import setup_entry
from .test_decision_log import obey
from .test_lighting_runtime import WS_LOGGER, Lit, advance, ctrl_of, lit, occupy, options  # noqa: F401
from .test_switches import WS

GET = "smart_occupancy/rooms/get"
REVIEW = "smart_occupancy/rooms/review"
SET = "smart_occupancy/rooms/set"
RESTORE_REVIEW = "smart_occupancy/rooms/restore/review"
RESTORE = "smart_occupancy/rooms/restore"


async def client(hass: HomeAssistant, hass_ws_client, user=None) -> WS:
    if user is None:
        return WS(await hass_ws_client(hass))
    refresh = await hass.auth.async_create_refresh_token(user, "https://example.com/app")
    return WS(await hass_ws_client(hass, access_token=hass.auth.async_create_access_token(refresh)))


def editable(room: dict[str, Any]) -> dict[str, Any]:
    """The part of a stored room the App sends back."""
    return {k: copy.deepcopy(room.get(k)) for k in
            ("enabled", "sensors", "lights", "reference_occupancy_entity", "adjacent_rooms")
            if k in room} | {"adjacent_rooms": list(room.get("adjacent_rooms") or [])}


async def model(ws: WS) -> dict[str, Any]:
    reply = await ws(type=GET)
    assert reply["success"], reply
    return reply["result"]


def full_options(lit: Lit) -> dict[str, Any]:
    opts = options(lit, timing={"inactivity_timeout": 600},
                   lights=[lit.lamp], mood_overrides={}, light_overrides={}, adjacent_rooms=[])
    opts["rooms"][lit.area]["sensors"][1]["inverted"] = True  # radar inverted
    opts["rooms"][lit.area]["sensors"][1]["enabled"] = False  # and excluded in the Presence tab
    opts["rooms"][lit.other] = {"enabled": False, "sensors": [], "lights": [lit.other_lamp]}
    opts["occupancy"] = {"correlation_window": 12}
    return opts


# ---------------------------------------------------------------- pure parts
def test_build_room_keeps_settings_and_order() -> None:
    old = {"enabled": True, "sensors": [
        {"entity_id": "binary_sensor.a", "kind": "pir", "role": "maintain", "inverted": True},
        {"entity_id": "binary_sensor.b", "kind": "radar", "role": "maintain", "inverted": False, "enabled": False},
    ], "lights": ["light.x", "light.y"], "timing": {"inactivity_timeout": 400}, "custom_key": 1}
    edit = {"enabled": True, "lights": ["light.z", "light.y", "light.x"], "adjacent_rooms": [],
            "sensors": [{"entity_id": "binary_sensor.c", "kind": "door"},
                        {"entity_id": "binary_sensor.b", "kind": "radar"},
                        {"entity_id": "binary_sensor.a", "kind": "radar"}]}
    new = build_room("room", old, edit)
    assert [s["entity_id"] for s in new["sensors"]] == ["binary_sensor.a", "binary_sensor.b", "binary_sensor.c"]
    a, b, c = new["sensors"]
    assert (a["kind"], a["role"], a["inverted"]) == ("radar", "maintain", True)  # kind changed: default role
    assert (b["role"], b["inverted"], b["enabled"]) == ("maintain", False, False)  # exclusion kept
    assert (c["kind"], c["role"], c["inverted"]) == ("door", "trigger", False)
    assert "enabled" not in c
    assert new["lights"] == ["light.x", "light.y", "light.z"]
    assert new["timing"] == {"inactivity_timeout": 400} and new["custom_key"] == 1
    changes = {(c["type"], c.get("entity_id")) for c in diff_room("room", old, new)}
    assert changes == {("sensor_added", "binary_sensor.c"), ("sensor_kind", "binary_sensor.a"),
                       ("light_added", "light.z")}  # radar's default role is the old role: no role change
    assert diff_room("room", old, old) == []
    assert diff_room("room", old, None) == [{"type": "room_removed", "area_id": "room"}]
    assert revision_of(None) != revision_of({}) and revision_of({"a": 1, "b": 2}) == revision_of({"b": 2, "a": 1})


# ------------------------------------------------------------------- reading
async def test_get_reads_the_existing_options(hass: HomeAssistant, lit: Lit, hass_ws_client) -> None:
    entry = await setup_entry(hass, full_options(lit))
    data = await model(await client(hass, hass_ws_client))
    assert data["loaded"] and data["writable"] and data["entry_id"] == entry.entry_id
    assert data["integration_version"] == VERSION and data["api_version"] == 1
    assert set(data["rooms"]) == {lit.area, lit.other}
    room = data["rooms"][lit.area]
    assert room["config"] == entry.options["rooms"][lit.area]  # exactly what is stored
    assert room["revision"] == revision_of(entry.options["rooms"][lit.area])
    assert room["occupancy"]["state"] and room["occupancy"]["signals"]
    assert room["lighting"]["automatic"] is False and room["lighting"]["managed"]
    assert data["rooms"][lit.other]["occupancy"] is None  # disabled room: no engine, no invented values
    kinds = {k["value"]: k for k in data["schema"]["kinds"]}
    assert kinds["pir"]["domains"] == ["binary_sensor"] and kinds["other"]["domains"] is None
    assert {r["value"] for r in data["schema"]["roles"]} == {"trigger", "maintain", "confirm_vacancy", "ignore"}


async def test_commands_are_admin_only(hass: HomeAssistant, lit: Lit, hass_ws_client, hass_read_only_user,
                                       caplog) -> None:
    caplog.set_level(logging.CRITICAL, logger=WS_LOGGER)
    await setup_entry(hass, full_options(lit))
    ws = await client(hass, hass_ws_client, hass_read_only_user)
    for msg in ({"type": GET}, {"type": REVIEW, "area_id": lit.area, "room": None},
                {"type": SET, "area_id": lit.area, "room": None, "expected_revision": "x"},
                {"type": RESTORE_REVIEW, "rooms": {}}, {"type": RESTORE, "rooms": {}, "expected_revision": "x"}):
        reply = await ws(**msg)
        assert not reply["success"] and reply["error"]["code"] == "unauthorized", msg


# ------------------------------------------------------------------- writing
async def test_review_then_set_changes_only_that_room(hass: HomeAssistant, lit: Lit, hass_ws_client,
                                                     service_calls) -> None:
    opts = full_options(lit)
    entry = await setup_entry(hass, opts)
    before = copy.deepcopy(dict(entry.options))
    ws = await client(hass, hass_ws_client)
    room = (await model(ws))["rooms"][lit.area]
    edit = editable(room["config"])
    edit["sensors"] = [{"entity_id": s["entity_id"], "kind": s["kind"]} for s in edit["sensors"]]  # settings omitted
    edit["lights"] = [lit.lamp, lit.desk]
    review = await ws(type=REVIEW, area_id=lit.area, room=edit, expected_revision=room["revision"])
    assert review["success"], review
    r = review["result"]
    assert r["changes"] == [{"type": "light_added", "entity_id": lit.desk}]
    assert r["errors"] == [] and r["stale"] is False and r["apply"] == "reload"
    assert r["requires_confirmation"] == []  # Observing room: nothing automatic follows
    assert dict(entry.options) == before  # a review changes nothing

    reply = await ws(type=SET, area_id=lit.area, room=edit, expected_revision=room["revision"])
    assert reply["success"], reply
    await hass.async_block_till_done()
    after = dict(entry.options)
    assert after["rooms"][lit.area]["lights"] == [lit.lamp, lit.desk]
    radar = after["rooms"][lit.area]["sensors"][1]
    assert (radar["inverted"], radar["enabled"]) == (True, False)  # existing sensor settings preserved
    assert after["rooms"][lit.area]["timing"] == {"inactivity_timeout": 600}
    assert {k: v for k, v in after.items() if k != "rooms"} == {k: v for k, v in before.items() if k != "rooms"}
    assert after["rooms"][lit.other] == before["rooms"][lit.other]
    assert reply["result"]["room_revision"] == revision_of(after["rooms"][lit.area]) != room["revision"]
    assert entry.state is ConfigEntryState.LOADED
    assert lit.desk in entry.runtime_data.config.room(lit.area).lights  # the reloaded integration uses it
    assert [c for c in service_calls if c.data["domain"] == "light"] == []


async def test_stale_and_invalid_writes_are_refused(hass: HomeAssistant, lit: Lit, hass_ws_client, caplog) -> None:
    caplog.set_level(logging.CRITICAL, logger=WS_LOGGER)
    entry = await setup_entry(hass, full_options(lit))
    ws = await client(hass, hass_ws_client)
    room = (await model(ws))["rooms"][lit.area]
    stored = copy.deepcopy(dict(entry.options))

    def edited(**changes: Any) -> dict[str, Any]:
        return {**editable(room["config"]), **changes}

    hass.states.async_set("sensor.lux", "12", {"device_class": "illuminance"})
    own = next(e.entity_id for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id))
    bad = {
        "duplicate_sensor": edited(sensors=[*room["config"]["sensors"], {"entity_id": lit.pir, "kind": "pir"}]),
        "sensor_domain": edited(sensors=[{"entity_id": "sensor.lux", "kind": "pir"}]),
        "entity_missing": edited(sensors=[{"entity_id": "binary_sensor.nope", "kind": "pir"}]),
        "not_a_light": edited(lights=["switch.relay"]),
        "duplicate_light": edited(lights=[lit.lamp, lit.lamp]),
        "reference_domain": edited(reference_occupancy_entity=lit.lamp),
        "own_entity": edited(sensors=[{"entity_id": own, "kind": "other"}]),
        "adjacent_unknown": edited(adjacent_rooms=["no_such_area"]),
    }
    for code, edit in bad.items():
        review = (await ws(type=REVIEW, area_id=lit.area, room=edit, expected_revision=room["revision"]))["result"]
        assert code in {e["code"] for e in review["errors"]}, (code, review["errors"])
        reply = await ws(type=SET, area_id=lit.area, room=edit, expected_revision=room["revision"])
        assert not reply["success"] and reply["error"]["code"] == "invalid_room", code
    # Malformed input never reaches validation.
    for edit in ({"enabled": "yes", "sensors": [], "lights": []}, {"enabled": True, "sensors": [{"kind": "pir"}],
                 "lights": []}, {"enabled": True, "sensors": [], "lights": ["not an id"]}):
        reply = await ws(type=REVIEW, area_id=lit.area, room=edit)
        assert not reply["success"] and reply["error"]["code"] == "invalid_room"
    assert dict(entry.options) == stored

    # Changed in the integration's own room editor meanwhile: the App's write is stale.
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    flow = await hass.config_entries.options.async_configure(flow["flow_id"], {"next_step_id": "rooms"})
    flow = await hass.config_entries.options.async_configure(flow["flow_id"], {"area": lit.area})
    flow = await hass.config_entries.options.async_configure(flow["flow_id"], {
        "enabled": True, "pir": [lit.pir], "radar": [lit.radar], "lights": [lit.lamp, lit.strip], "reset": False})
    flow = await hass.config_entries.options.async_configure(flow["flow_id"], {lit.pir: "trigger",
                                                                                lit.radar: "maintain"})
    flow = await hass.config_entries.options.async_configure(flow["flow_id"], {})
    assert flow["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    reply = await ws(type=SET, area_id=lit.area, room=edited(lights=[lit.desk]), expected_revision=room["revision"])
    assert not reply["success"] and reply["error"]["code"] == "stale_revision"
    fresh = (await model(ws))["rooms"][lit.area]
    assert fresh["config"]["lights"] == [lit.lamp, lit.strip]  # the options flow's change is what the App reads
    assert fresh["revision"] != room["revision"]
    review = (await ws(type=REVIEW, area_id=lit.area, room=edited(lights=[lit.desk]),
                       expected_revision=room["revision"]))["result"]
    assert review["stale"] is True
    reply = await ws(type=SET, area_id=lit.area, room={**editable(fresh["config"]), "lights": [lit.desk]},
                     expected_revision=fresh["revision"])
    assert reply["success"], reply


async def test_existing_missing_entity_may_stay(hass: HomeAssistant, lit: Lit, hass_ws_client) -> None:
    """A deleted sensor that is already assigned can be kept (reported as a problem); new ones must exist."""
    opts = full_options(lit)
    opts["rooms"][lit.area]["sensors"].append({"entity_id": "binary_sensor.gone", "kind": "pir", "role": "trigger"})
    await setup_entry(hass, opts)
    ws = await client(hass, hass_ws_client)
    room = (await model(ws))["rooms"][lit.area]
    edit = {**editable(room["config"]), "lights": [lit.lamp, lit.desk]}
    review = (await ws(type=REVIEW, area_id=lit.area, room=edit, expected_revision=room["revision"]))["result"]
    assert review["errors"] == []
    assert "entity_missing" in {p["type"] for p in review["problems_after"]}  # the integration's own check


async def test_disruptive_changes_need_confirmation(hass: HomeAssistant, lit: Lit, hass_ws_client, freezer,
                                                    caplog) -> None:
    caplog.set_level(logging.CRITICAL, logger=WS_LOGGER)
    seen = obey(hass)
    entry = await setup_entry(hass, full_options(lit))
    ctrl = ctrl_of(entry)
    await occupy(hass, lit, freezer)
    ctrl.store.async_set(ctrl.with_room(lit.area, control_enabled=True))  # Automatic lighting
    await hass.async_block_till_done()
    await advance(hass, freezer)
    seen.clear()
    ws = await client(hass, hass_ws_client)
    room = (await model(ws))["rooms"][lit.area]
    assert room["lighting"]["automatic"] is True
    edit = {**editable(room["config"]), "lights": [lit.lamp, lit.desk]}
    review = (await ws(type=REVIEW, area_id=lit.area, room=edit, expected_revision=room["revision"]))["result"]
    assert review["requires_confirmation"] == ["automatic_lighting"]
    reply = await ws(type=SET, area_id=lit.area, room=edit, expected_revision=room["revision"])
    assert not reply["success"] and reply["error"]["code"] == "confirmation_required"
    reply = await ws(type=SET, area_id=lit.area, room=edit, expected_revision=room["revision"],
                     confirmed=["automatic_lighting"])
    assert reply["success"], reply
    await hass.async_block_till_done()
    for _ in range(10):  # the reload itself sends nothing, also not in the following seconds
        await advance(hass, freezer, 2)
    assert seen == []

    ws = await client(hass, hass_ws_client)
    room = (await model(ws))["rooms"][lit.area]
    disable = {**editable(room["config"]), "enabled": False}
    review = (await ws(type=REVIEW, area_id=lit.area, room=disable, expected_revision=room["revision"]))["result"]
    assert review["requires_confirmation"] == ["room_disabled"]
    reply = await ws(type=SET, area_id=lit.area, room=disable, expected_revision=room["revision"],
                     confirmed=review["requires_confirmation"])
    assert reply["success"], reply
    await hass.async_block_till_done()
    assert lit.area not in entry.runtime_data.config.enabled_rooms
    ws = await client(hass, hass_ws_client)
    room = (await model(ws))["rooms"][lit.area]
    enable = {**editable(room["config"]), "enabled": True}
    review = (await ws(type=REVIEW, area_id=lit.area, room=enable, expected_revision=room["revision"]))["result"]
    assert review["requires_confirmation"] == ["automatic_lighting_resumes"]  # activation is still stored
    review = (await ws(type=REVIEW, area_id=lit.area, room=None, expected_revision=room["revision"]))["result"]
    assert "room_removed" in review["requires_confirmation"]
    assert "room_settings_removed" in review["requires_confirmation"]  # its timing override goes too
    assert seen == []


async def test_sensor_exclusion_is_applied_live(hass: HomeAssistant, lit: Lit, hass_ws_client) -> None:
    entry = await setup_entry(hass, full_options(lit))
    coordinator = entry.runtime_data
    ws = await client(hass, hass_ws_client)
    room = (await model(ws))["rooms"][lit.area]
    edit = editable(room["config"])
    edit["sensors"][1]["enabled"] = True
    review = (await ws(type=REVIEW, area_id=lit.area, room=edit, expected_revision=room["revision"]))["result"]
    assert review["apply"] == "live" and review["changes"][0]["type"] == "sensor_enabled"
    assert (await ws(type=SET, area_id=lit.area, room=edit, expected_revision=room["revision"]))["success"]
    await hass.async_block_till_done()
    assert entry.runtime_data is coordinator  # not reloaded, like the Presence tab
    assert "enabled" not in entry.options["rooms"][lit.area]["sensors"][1]


async def test_new_room_for_an_unconfigured_area(hass: HomeAssistant, lit: Lit, hass_ws_client) -> None:
    entry = await setup_entry(hass, full_options(lit))
    hall = ar.async_get(hass).async_create("Gongd")
    ws = await client(hass, hass_ws_client)
    data = await model(ws)
    assert hall.id not in data["rooms"]
    edit = {"enabled": True, "sensors": [{"entity_id": lit.radar, "kind": "radar"}], "lights": [],
            "adjacent_rooms": [lit.area]}
    review = (await ws(type=REVIEW, area_id=hall.id, room=edit, expected_revision=revision_of(None)))["result"]
    assert [c["type"] for c in review["changes"]] == ["room_added", "room_enabled", "sensor_added", "adjacent_added"]
    assert "signal_shared" in {p["type"] for p in review["problems_new"]}  # the radar is in two rooms now
    reply = await ws(type=SET, area_id=hall.id, room=edit, expected_revision=revision_of(None))
    assert reply["success"], reply
    await hass.async_block_till_done()
    assert entry.options["rooms"][hall.id]["sensors"] == [
        {"entity_id": lit.radar, "kind": "radar", "role": "maintain", "inverted": False}]
    assert hall.id in entry.runtime_data.config.enabled_rooms


async def test_backup_restore(hass: HomeAssistant, lit: Lit, hass_ws_client, caplog) -> None:
    caplog.set_level(logging.CRITICAL, logger=WS_LOGGER)
    entry = await setup_entry(hass, full_options(lit))
    ws = await client(hass, hass_ws_client)
    data = await model(ws)
    backup = {a: r["config"] for a, r in data["rooms"].items()}
    room = data["rooms"][lit.area]
    assert (await ws(type=SET, area_id=lit.area, room={**editable(room["config"]), "lights": []},
                     expected_revision=room["revision"]))["success"]
    await hass.async_block_till_done()
    data = await model(ws)
    review = await ws(type=RESTORE_REVIEW, rooms=backup, expected_revision=data["revision"])
    assert review["success"], review
    r = review["result"]
    assert [x["area_id"] for x in r["rooms"]] == [lit.area]
    assert r["rooms"][0]["changes"] == [{"type": "light_added", "entity_id": lit.lamp}]
    assert r["requires_confirmation"] == ["restore"]
    reply = await ws(type=RESTORE, rooms=backup, expected_revision=data["revision"])
    assert reply["error"]["code"] == "confirmation_required"
    reply = await ws(type=RESTORE, rooms=backup, expected_revision="0000", confirmed=["restore"])
    assert reply["error"]["code"] == "stale_revision"
    reply = await ws(type=RESTORE, rooms=backup, expected_revision=data["revision"], confirmed=["restore"])
    assert reply["success"], reply
    await hass.async_block_till_done()
    assert entry.options["rooms"] == backup
    for broken in ({lit.area: {"sensors": [{"entity_id": "x"}]}}, {lit.area: "nope"}, {"": {}}):
        reply = await ws(type=RESTORE_REVIEW, rooms=broken)
        assert not reply["success"] and reply["error"]["code"] == "invalid_backup", broken


async def test_writes_refused_while_not_loaded(hass: HomeAssistant, lit: Lit, hass_ws_client, caplog) -> None:
    caplog.set_level(logging.CRITICAL, logger=WS_LOGGER)
    entry = await setup_entry(hass, full_options(lit))
    ws = await client(hass, hass_ws_client)
    room = (await model(ws))["rooms"][lit.area]
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    data = await model(ws)
    assert data["loaded"] is False and data["writable"] is False and data["rooms"][lit.area]["config"]
    reply = await ws(type=SET, area_id=lit.area, room=editable(room["config"]), expected_revision=room["revision"])
    assert not reply["success"] and reply["error"]["code"] == "not_loaded"
    # Set up again (as after a restart): the stored configuration is unchanged and readable.
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert (await model(ws))["rooms"][lit.area]["revision"] == room["revision"]
