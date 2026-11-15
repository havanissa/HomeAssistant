"""Phase 4.6.0 (0.6.1): window manager, adjustable presets and explicitly confirmed manual curtain control.

The covers are simulated like the house's Zigbee2MQTT Aqara E1 / IKEA FYRTUR
motors: a command is accepted, then the device reports "opening"/"closing",
an intermediate position and its final position later, each with a context of
its own. A cover can be told to fail, to ignore a command, to travel slowly or
to drop off the network. Nothing here talks to real hardware.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path
import re
from typing import Any

from aiohttp.client_exceptions import WSMessageTypeError
import pytest

from homeassistant.core import Context, HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import area_registry as ar, entity_registry as er
from homeassistant.helpers.event import async_call_later
from homeassistant.setup import async_setup_component

from custom_components.smart_occupancy.curtains import config as cc, solar
from custom_components.smart_occupancy.curtains.config import CoverLayer, CurtainConfig, Window, with_layers
from custom_components.smart_occupancy.curtains.discovery import guess_kind, window_key
from custom_components.smart_occupancy.supervision import (
    AUTOMATIC_CURTAIN_CONTROL,
    ControlNotPermittedError,
    CurtainOrigin,
    assert_curtain_command_permitted,
)

from .conftest import Pilot, pilot_options, set_moods, setup_entry
from .test_daylight_runtime import advance
from .test_lighting_runtime import WS_LOGGER

STOVA_SHEER = "cover.gardinur_stova_vindeyga_ljos"
STOVA_BLACKOUT = "cover.gardinur_stova_vindeyga_myrk"
KOKUR_SHEER = "cover.gardinur_kokur_vindeyga_ljost"
KOKUR_BLACKOUT = "cover.gardinur_kokur_vindeyga_myrk"
KOKUR_EAST = "cover.gardinur_kokur_vindeyga_eystur_myrk"
KAMAR = "cover.gardinur_kamar_erva_vindeyga_myrk"
SCREEN = "cover.trappa_screen"          # open / close only (no positions)
COVER_GROUP = "cover.kokur_all"         # a cover group with the two Køkur south covers
FULL = 15                               # open, close, set position, stop (every cover in the house)
OPEN_CLOSE = 3


@dataclass
class House:
    stova: str
    kokur: str
    kamar: str
    trappa: str


class Covers:
    """cover.* services that behave like Zigbee2MQTT covers (see the module docstring)."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.calls: list[ServiceCall] = []
        self.fail: set[str] = set()
        self.ignore: set[str] = set()
        self.travel: dict[str, float] = {}   # seconds a movement takes (0 = reports at once)
        self.drop: set[str] = set()          # becomes unavailable instead of moving
        for service in ("set_cover_position", "open_cover", "close_cover", "stop_cover"):
            hass.services.async_register("cover", service, self._handle)

    def services(self, entity_id: str | None = None) -> list[tuple[str, str, Any]]:
        return [(c.service, c.data["entity_id"], c.data.get("position")) for c in self.calls
                if entity_id is None or c.data["entity_id"] == entity_id]

    async def _handle(self, call: ServiceCall) -> None:
        self.calls.append(call)
        entity_id = call.data["entity_id"]
        if entity_id in self.fail:
            raise HomeAssistantError(f"Publish to zigbee2mqtt/{entity_id} failed")
        if entity_id in self.ignore:
            return
        if entity_id in self.drop:
            self.hass.states.async_set(entity_id, "unavailable", self.hass.states.get(entity_id).attributes,
                                       context=Context())
            return
        target = {"open_cover": 100, "close_cover": 0}.get(call.service, call.data.get("position"))
        start = self.position(entity_id)
        seconds = self.travel.get(entity_id, 0)
        if not seconds:
            self.report(entity_id, target)
            return
        moving = "opening" if target > (start or 0) else "closing"
        self.report(entity_id, start, moving)
        half = None if start is None else round((start + target) / 2)
        async_call_later(self.hass, seconds / 2, callback(lambda _n: self.report(entity_id, half, moving)))
        async_call_later(self.hass, seconds, callback(lambda _n: self.report(entity_id, target)))

    def position(self, entity_id: str) -> int | None:
        return self.hass.states.get(entity_id).attributes.get("current_position")

    def report(self, entity_id: str, position: int | None, moving: str | None = None) -> None:
        """A device report: its own context (not the caller's), like Zigbee2MQTT."""
        old = self.hass.states.get(entity_id)
        attrs = dict(old.attributes)
        if attrs.get("supported_features", 0) & 4 and position is not None:
            attrs["current_position"] = position
        state = moving or ("closed" if position == 0 else "open")
        self.hass.states.async_set(entity_id, state, attrs, context=Context())


def set_cover(hass: HomeAssistant, entity_id: str, position: int | None, features: int = FULL,
              state: str | None = None, name: str | None = None, context: Context | None = None) -> None:
    attrs: dict[str, Any] = {"supported_features": features, "friendly_name": name or entity_id.split(".")[1]}
    if features & 4 and position is not None:
        attrs["current_position"] = position
    hass.states.async_set(entity_id, state or ("closed" if position == 0 else "open"), attrs, context=context)


@pytest.fixture
async def house(hass: HomeAssistant, pilot: Pilot) -> House:
    area_reg = ar.async_get(hass)
    ent_reg = er.async_get(hass)
    stova = area_reg.async_create("Stova")
    kokur = area_reg.async_create("Køkur")
    kamar = area_reg.async_create("Kamar Erva")
    trappa = area_reg.async_create("Trappa")
    layout = {STOVA_SHEER: (stova.id, 0), STOVA_BLACKOUT: (stova.id, 50), KOKUR_SHEER: (kokur.id, 0),
              KOKUR_BLACKOUT: (kokur.id, 51), KOKUR_EAST: (kokur.id, 100), KAMAR: (kamar.id, 36),
              SCREEN: (trappa.id, 0)}
    for entity_id, (area_id, position) in layout.items():
        entry = ent_reg.async_get_or_create("cover", "mqtt", entity_id, suggested_object_id=entity_id.split(".")[1])
        ent_reg.async_update_entity(entry.entity_id, area_id=area_id)
        set_cover(hass, entity_id, position, OPEN_CLOSE if entity_id == SCREEN else FULL)
    await hass.async_block_till_done()
    return House(stova.id, kokur.id, kamar.id, trappa.id)


@pytest.fixture
def covers(hass: HomeAssistant) -> Covers:
    return Covers(hass)


class Api:
    """The card's websocket calls. Reconnects when the harness closed the socket after a long time jump."""

    def __init__(self, client, connect=None) -> None:
        self.client = client
        self.connect = connect
        self.n = 0

    async def __call__(self, command: str, **data: Any) -> dict[str, Any]:
        self.n += 1
        payload = {"id": self.n, "type": f"smart_occupancy/curtains/{command}", **data}
        for attempt in (1, 2):
            try:
                if self.client.closed:
                    raise ConnectionResetError
                await self.client.send_json(payload)
                while True:
                    msg = await self.client.receive_json()
                    if msg.get("id") == self.n and msg.get("type") == "result":
                        return msg
            except (ConnectionResetError, WSMessageTypeError):
                if self.connect is None or attempt == 2:
                    raise
                self.client = await self.connect()
        raise AssertionError("unreachable")

    async def ok(self, command: str, **data: Any) -> Any:
        res = await self(command, **data)
        assert res["success"], res
        return res["result"]

    async def error(self, command: str, **data: Any) -> dict[str, Any]:
        res = await self(command, **data)
        assert not res["success"], res
        return res["error"]


async def start(hass: HomeAssistant, pilot: Pilot, hass_ws_client):
    entry = await setup_entry(hass, pilot_options(pilot))
    async def connect():
        return await hass_ws_client(hass)

    return entry, entry.runtime_data.curtains, Api(await connect(), connect)


async def make_stova(api: Api, house: House) -> str:
    """Stova Suður: the semitransparent and the blackout layer, facing south."""
    created = await api.ok("window/create", name="Stova Suður", area_id=house.stova, direction="S", layers=[
        {"entity_id": STOVA_SHEER, "kind": "sheer"}, {"entity_id": STOVA_BLACKOUT, "kind": "blackout"}])
    return created["window_id"]


async def window(api: Api, window_id: str) -> dict[str, Any]:
    model = await api.ok("model")
    return next(w for w in model["windows"] if w["window_id"] == window_id)


def layer_ids(w: dict[str, Any]) -> dict[str, str]:
    return {x["entity_id"]: x["layer_id"] for x in w["layers"]}


def preset(w: dict[str, Any], name: str) -> dict[str, Any]:
    return next(p for p in w["presets"] if p["name"] == name)


async def confirm(api: Api, w: dict[str, Any], p: dict[str, Any]) -> dict[str, Any]:
    """Preview (what the confirmation shows), then apply exactly that."""
    plan = await api.ok("preview", window_id=w["window_id"], preset_id=p["preset_id"])
    return await api.ok("apply", window_id=w["window_id"], preset_id=p["preset_id"], confirm=True,
                        expected=plan["positions"])


# ---------------------------------------------------------- window management
async def test_create_edit_and_delete_windows(hass, pilot, house, covers, hass_ws_client, hass_storage) -> None:
    entry, mgr, api = await start(hass, pilot, hass_ws_client)
    single = (await api.ok("window/create", name=" Kamar  Erva ", area_id=house.kamar, direction="E",
                           layers=[{"entity_id": KAMAR, "kind": "blackout"}]))["window_id"]
    stova = await make_stova(api, house)
    model = await api.ok("model")
    assert [r["name"] for r in model["rooms"]] == ["Kamar Erva", "Stova"]  # grouped by room
    w = await window(api, single)
    assert w["name"] == "Kamar Erva" and w["area_name"] == "Kamar Erva" and w["mode"] == "observing"
    assert [x["entity_id"] for x in w["layers"]] == [KAMAR]
    assert w["window_id"].startswith("w_") and w["layers"][0]["layer_id"].startswith("l_")

    # Edit: name, room, direction, flags. The ids stay.
    await api.ok("window/update", window_id=single, name="Kamar Erva Vindeyga", area_id=house.stova, bearing=113.5,
                 sleep_protection=True, privacy=True)
    w = await window(api, single)
    assert (w["name"], w["area_id"], w["bearing"], w["direction"], w["compass"]) == (
        "Kamar Erva Vindeyga", house.stova, 113.5, None, "SE")
    assert w["sleep_protection"] and w["privacy"] and not w["solar_protection"]
    model = await api.ok("model")
    assert [r["name"] for r in model["rooms"]] == ["Stova"] and len(model["rooms"][0]["windows"]) == 2  # one room, two windows

    await api.ok("window/remove", window_id=single)
    model = await api.ok("model")
    assert [w["window_id"] for w in model["windows"]] == [stova]
    await mgr.async_flush()
    stored = hass_storage["smart_occupancy.curtains"]["data"]
    assert [w["window_id"] for w in stored["windows"]] == [stova]
    assert covers.calls == []  # creating, editing and removing windows never moves anything


async def test_room_and_orientation_validation(hass, pilot, house, covers, hass_ws_client) -> None:
    _e, _m, api = await start(hass, pilot, hass_ws_client)
    stova = await make_stova(api, house)
    for direction, bearing in (("N", 0), ("NE", 45), ("E", 90), ("SE", 135), ("S", 180), ("SW", 225), ("W", 270),
                               ("NW", 315)):
        await api.ok("window/update", window_id=stova, direction=direction)
        w = await window(api, stova)
        assert (w["direction"], w["effective_bearing"], w["compass"]) == (direction, bearing, direction)
    await api.ok("window/update", window_id=stova, bearing=200)
    assert (await window(api, stova))["direction"] is None  # an exact angle replaces the compass point
    assert (await api.error("window/update", window_id=stova, direction="SSE"))["code"] == "invalid_direction"
    assert (await api.error("window/update", window_id=stova, bearing=361))["code"] == "invalid_bearing"
    assert (await api.error("window/update", window_id=stova, area_id="no_such_room"))["code"] == "unknown_area"
    assert (await api.error("window/update", window_id=stova, name="  "))["code"] == "invalid_name"
    assert (await api.error("window/create", name="X", layers=[{"entity_id": "light.lamp"}]))["code"] == "invalid_cover"
    assert (await api.error("window/create", name="X", layers=[{"entity_id": "cover.nope"}]))["code"] == "unknown_cover"
    err = await api.error("window/create", name="X", layers=[{"entity_id": KAMAR, "kind": "lace"}])
    assert err["code"] == "invalid_kind"
    await api.ok("window/update", window_id=stova, direction=None, bearing=None)
    w = await window(api, stova)
    assert w["effective_bearing"] is None and w["exposure"]["status"] == "no_direction"


# ------------------------------------------------------------- presets
async def test_single_cover_defaults_and_custom_half_open(hass, pilot, house, covers, hass_ws_client) -> None:
    _e, _m, api = await start(hass, pilot, hass_ws_client)
    wid = (await api.ok("window/create", name="Kamar Erva", area_id=house.kamar,
                        layers=[{"entity_id": KAMAR, "kind": "blackout"}]))["window_id"]
    w = await window(api, wid)
    layer = w["layers"][0]["layer_id"]
    assert [(p["name"], p["positions"][layer]) for p in w["presets"]] == [
        ("Fully open", 100), ("Half open", 50), ("Fully closed", 0)]
    half = preset(w, "Half open")
    await api.ok("preset/save", window_id=wid, preset_id=half["preset_id"], positions={layer: 35})
    w = await window(api, wid)
    assert preset(w, "Half open")["positions"][layer] == 35 and preset(w, "Half open")["preset_id"] == half["preset_id"]
    assert w["conditions"]["morning"] == half["preset_id"]  # defaults follow the preset's id, not its value
    assert covers.calls == []


async def test_multi_cover_defaults_and_preset_library(hass, pilot, house, covers, hass_ws_client) -> None:
    _e, _m, api = await start(hass, pilot, hass_ws_client)
    wid = await make_stova(api, house)
    w = await window(api, wid)
    ids = layer_ids(w)
    sheer, blackout = ids[STOVA_SHEER], ids[STOVA_BLACKOUT]
    assert [(p["name"], p["positions"][sheer], p["positions"][blackout]) for p in w["presets"]] == [
        ("Fully open", 100, 100), ("Half shade", 50, 100), ("Full shade", 0, 100), ("Evening privacy", 0, 60),
        ("Full blackout", 100, 0)]
    assert w["conditions"] == {
        "morning": "p_half_shade", "day": "p_open", "sun": "p_full_shade", "evening_privacy": "p_evening_privacy",
        "night": "p_blackout", "sleep": "p_blackout", "lamp": "p_half_shade", "manual_override": None}

    # A new custom preset with two independent positions, one cover left unchanged in another.
    tv = (await api.ok("preset/save", window_id=wid, name="TV", positions={sheer: 20, blackout: 70}))["preset_id"]
    keep = (await api.ok("preset/save", window_id=wid, name="Only sheer", positions={sheer: 0, blackout: None}))
    await api.ok("preset/save", window_id=wid, preset_id=tv, name="Sjónvarp")
    copy = (await api.ok("preset/duplicate", window_id=wid, preset_id=tv))["preset_id"]
    w = await window(api, wid)
    assert preset(w, "Sjónvarp")["positions"] == {sheer: 20, blackout: 70}
    assert preset(w, "Sjónvarp (copy)")["positions"] == {sheer: 20, blackout: 70}
    assert preset(w, "Sjónvarp (copy)")["preset_id"] == copy != tv
    assert preset(w, "Only sheer")["positions"] == {sheer: 0, blackout: None}
    assert (await api.error("preset/save", window_id=wid, name="sjónvarp"))["code"] == "duplicate_name"

    # Invalid positions are refused, nothing is clamped or guessed.
    for bad in (101, -1, 50.5, "50", True):
        err = await api.error("preset/save", window_id=wid, preset_id=tv, positions={sheer: bad})
        assert err["code"] == "invalid_position", bad
    assert (await api.error("preset/save", window_id=wid, preset_id=tv, positions={"l_nope": 1}))["code"] == \
        "unknown_layer"

    # Select, then delete: the selection and the conditions that used it become "Not set".
    await api.ok("preset/select", window_id=wid, preset_id="p_blackout")
    await api.ok("preset/delete", window_id=wid, preset_id="p_blackout")
    w = await window(api, wid)
    assert w["selected_preset"] is None and w["conditions"]["night"] is None and w["conditions"]["sleep"] is None
    assert "Full blackout" not in [p["name"] for p in w["presets"]]
    await api.ok("preset/delete", window_id=wid, preset_id=keep["preset_id"])
    assert covers.calls == []


async def test_condition_mapping_is_per_window_and_never_runs(hass, pilot, house, covers, hass_ws_client) -> None:
    _e, mgr, api = await start(hass, pilot, hass_ws_client)
    stova = await make_stova(api, house)
    kamar = (await api.ok("window/create", name="Kamar", layers=[{"entity_id": KAMAR}]))["window_id"]
    await api.ok("window/update", window_id=stova, conditions={"morning": "p_open", "manual_override": "p_full_shade"})
    assert (await window(api, stova))["conditions"]["morning"] == "p_open"
    assert (await window(api, kamar))["conditions"]["morning"] == "p_half"
    err = await api.error("window/update", window_id=kamar, conditions={"morning": "p_half_shade"})
    assert err["code"] == "unknown_preset"  # a preset of another window
    assert (await api.error("window/update", window_id=kamar, conditions={"lunch": None}))["code"] == \
        "invalid_condition"
    set_moods(hass, "Kvøld", "Auto", "Kvøld")  # a lighting mood change is not a curtain condition
    await hass.async_block_till_done()
    assert covers.calls == []


# ------------------------------------------------------------- discovery
async def test_discovery_proposals_and_single_owner(hass, pilot, house, covers, hass_ws_client) -> None:
    _e, _m, api = await start(hass, pilot, hass_ws_client)
    assert guess_kind(STOVA_SHEER) == "sheer" and guess_kind(KOKUR_SHEER) == "sheer"
    assert guess_kind(KOKUR_BLACKOUT) == "blackout" and guess_kind(SCREEN) == "custom"
    assert window_key(KOKUR_SHEER) == window_key(KOKUR_BLACKOUT) != window_key(KOKUR_EAST)
    model = await api.ok("model")
    by_name = {p["name"]: p for p in model["proposals"]}
    assert set(by_name) >= {"Stova", "Køkur", "Køkur Eystur", "Kamar Erva", "Trappa Screen"}, by_name
    assert [(c["entity_id"], c["kind"]) for c in by_name["Køkur"]["covers"]] == [
        (KOKUR_SHEER, "sheer"), (KOKUR_BLACKOUT, "blackout")]
    assert by_name["Køkur"]["area_id"] == house.kokur
    assert [c["entity_id"] for c in by_name["Køkur Eystur"]["covers"]] == [KOKUR_EAST]
    cands = {c["entity_id"]: c for c in model["candidates"]}
    assert cands[SCREEN]["caps"]["position"] is False and cands[KAMAR]["caps"]["position"] is True
    assert model["windows"] == []  # proposals are never created on their own
    assert covers.calls == []

    # The user corrects a proposal: the east cover goes with the south pair? No - one cover, one window.
    kokur = (await api.ok("window/create", name="Køkur Suður", area_id=house.kokur, layers=[
        {"entity_id": KOKUR_SHEER, "kind": "sheer"}, {"entity_id": KOKUR_BLACKOUT, "kind": "blackout"}]))["window_id"]
    err = await api.error("window/create", name="Again", layers=[{"entity_id": KOKUR_BLACKOUT}])
    assert err["code"] == "cover_in_use" and "Køkur Suður" in err["message"]
    err = await api.error("window/create", name="Twice", layers=[{"entity_id": KAMAR}, {"entity_id": KAMAR}])
    assert err["code"] == "duplicate_cover"
    # A cover group whose members already belong to a window is the same physical cover.
    hass.states.async_set(COVER_GROUP, "open", {"entity_id": [KOKUR_SHEER, KOKUR_BLACKOUT], "supported_features": FULL,
                                                "current_position": 25})
    err = await api.error("window/create", name="Group", layers=[{"entity_id": COVER_GROUP}])
    assert err["code"] == "cover_in_use"
    model = await api.ok("model")
    assert {c["entity_id"]: c["assigned_to"] for c in model["candidates"]}[KOKUR_SHEER] == kokur
    assert "Køkur" not in {p["name"] for p in model["proposals"]}
    assert "Køkur Eystur" in {p["name"] for p in model["proposals"]}


# -------------------------------------------------------------- matching
async def test_position_matching_and_custom(hass, pilot, house, covers, hass_ws_client) -> None:
    _e, _m, api = await start(hass, pilot, hass_ws_client)
    wid = await make_stova(api, house)
    w = await window(api, wid)
    sheer, blackout = layer_ids(w)[STOVA_SHEER], layer_ids(w)[STOVA_BLACKOUT]
    # Live: sheer 0, blackout 50 -> no preset (Evening privacy is 0 / 60).
    assert w["current"] == {"status": "custom", "preset_id": None, "text": "Custom position"}
    set_cover(hass, STOVA_BLACKOUT, 58)  # within the default tolerance (3) of 60
    w = await window(api, wid)
    assert (w["current"]["status"], w["current"]["preset_id"]) == ("preset", "p_evening_privacy")
    assert preset(w, "Evening privacy")["matches"] and not preset(w, "Full shade")["matches"]
    # A preset that leaves a cover unchanged matches whatever that cover does; the more specific one wins.
    await api.ok("preset/save", window_id=wid, name="Sheer closed", positions={sheer: 0, blackout: None})
    w = await window(api, wid)
    assert w["current"]["preset_id"] == "p_evening_privacy" and preset(w, "Sheer closed")["matches"]
    # ...unless the person selected the less specific one.
    await api.ok("preset/select", window_id=wid, preset_id=preset(w, "Sheer closed")["preset_id"])
    assert (await window(api, wid))["current"]["text"] == "Sheer closed"
    set_cover(hass, STOVA_SHEER, 12)
    assert (await window(api, wid))["current"]["text"] == "Custom position"
    set_cover(hass, STOVA_SHEER, 12, state="opening")
    assert (await window(api, wid))["current"]["status"] == "moving"
    set_cover(hass, STOVA_SHEER, None, state="unavailable")
    set_cover(hass, STOVA_BLACKOUT, None, state="unavailable")
    assert (await window(api, wid))["current"]["status"] == "unknown"
    assert covers.calls == []


# --------------------------------------------------------- preview / apply
async def test_preview_never_moves_anything(hass, pilot, house, covers, hass_ws_client, service_calls) -> None:
    _e, _m, api = await start(hass, pilot, hass_ws_client)
    wid = await make_stova(api, house)
    w = await window(api, wid)
    plan = await api.ok("preview", window_id=wid, preset_id="p_full_shade")
    items = {i["entity_id"]: i for i in plan["items"]}
    assert (items[STOVA_SHEER]["decision"], items[STOVA_SHEER]["target"]) == ("already", 0)  # 0 -> 0
    assert (items[STOVA_BLACKOUT]["decision"], items[STOVA_BLACKOUT]["service"], items[STOVA_BLACKOUT]["target"]) == (
        "move", "set_cover_position", 100)
    assert plan["moves"] == 1 and plan["positions"] == preset(w, "Full shade")["positions"]
    await hass.async_block_till_done()
    assert covers.calls == [] and not [c for c in service_calls if c.data["domain"] == "cover"]


async def test_apply_needs_admin_confirmation_and_the_confirmed_targets(hass, pilot, house, covers, hass_ws_client,
                                                                        hass_read_only_access_token, caplog) -> None:
    caplog.set_level(logging.CRITICAL, logger=WS_LOGGER)  # rejected messages are logged as errors by HA
    _e, mgr, api = await start(hass, pilot, hass_ws_client)
    wid = await make_stova(api, house)
    w = await window(api, wid)
    p = preset(w, "Full shade")
    err = await api.error("apply", window_id=wid, preset_id=p["preset_id"], confirm=False, expected=p["positions"])
    assert err["code"] == "not_permitted" and "confirmation" in err["message"]
    stale = {**p["positions"], layer_ids(w)[STOVA_BLACKOUT]: 90}
    err = await api.error("apply", window_id=wid, preset_id=p["preset_id"], confirm=True, expected=stale)
    assert err["code"] == "preset_changed"
    user = Api(await hass_ws_client(hass, access_token=hass_read_only_access_token))
    assert (await user.error("apply", window_id=wid, preset_id=p["preset_id"], confirm=True,
                             expected=p["positions"]))["code"] == "unauthorized"
    assert (await user.error("preset/save", window_id=wid, name="X"))["code"] == "unauthorized"
    assert (await user.ok("preview", window_id=wid, preset_id=p["preset_id"]))["moves"] == 1  # reading is allowed
    await api.ok("window/update", window_id=wid, mode="off")
    err = await api.error("apply", window_id=wid, preset_id=p["preset_id"], confirm=True, expected=p["positions"])
    assert err["code"] == "not_permitted" and "Off" in err["message"]
    assert covers.calls == []
    # Observing: a person's confirmed request is allowed.
    await api.ok("window/update", window_id=wid, mode="observing")
    await confirm(api, w, p)
    assert covers.services() == [("set_cover_position", STOVA_BLACKOUT, 100)]


async def test_curtain_gate_by_origin_and_mode(hass, pilot, house, covers, hass_ws_client, hass_storage) -> None:
    """0.6.2: automatic commands only for windows in Automatic; Automatic only through its confirmed enable."""
    assert AUTOMATIC_CURTAIN_CONTROL is True
    for mode in ("observing", "off"):
        with pytest.raises(ControlNotPermittedError):
            assert_curtain_command_permitted(CurtainOrigin.AUTOMATIC, confirmed=True, window_mode=mode, window_id="w")
    assert_curtain_command_permitted(CurtainOrigin.AUTOMATIC, confirmed=False, window_mode="automatic",
                                     window_id="w")
    assert_curtain_command_permitted(CurtainOrigin.MANUAL_CONFIRMED, confirmed=True, window_mode="observing",
                                     window_id="w")
    with pytest.raises(ControlNotPermittedError):
        assert_curtain_command_permitted(CurtainOrigin.MANUAL_CONFIRMED, confirmed=False, window_mode="automatic",
                                         window_id="w")
    _e, mgr, api = await start(hass, pilot, hass_ws_client)
    wid = await make_stova(api, house)
    # Automatic is never set by a plain update (nor at creation): it has its own confirmed action.
    assert (await api.error("window/update", window_id=wid, mode="automatic"))["code"] == "confirm_required"
    assert (await api.error("window/create", name="X", mode="automatic"))["code"] == "confirm_required"
    with pytest.raises(ControlNotPermittedError):
        mgr.begin(mgr.config.windows[wid], mgr.config.windows[wid].preset("p_open"), CurtainOrigin.AUTOMATIC)
    # A stored "automatic" (enabled earlier) loads as Automatic; an unknown mode as Observing.
    stored = mgr.config.as_dict()
    stored["windows"][0]["mode"] = "automatic"
    assert CurtainConfig.from_dict(stored).windows[wid].mode == "automatic"
    stored["windows"][0]["mode"] = "turbo"
    assert CurtainConfig.from_dict(stored).windows[wid].mode == "observing"
    assert covers.calls == []


async def test_multi_cover_preset_with_delayed_reports(hass, pilot, house, covers, hass_ws_client, freezer) -> None:
    """Stova Suður: a custom preset with two independent positions, verified per cover."""
    _e, mgr, api = await start(hass, pilot, hass_ws_client)
    wid = await make_stova(api, house)
    w = await window(api, wid)
    sheer, blackout = layer_ids(w)[STOVA_SHEER], layer_ids(w)[STOVA_BLACKOUT]
    pid = (await api.ok("preset/save", window_id=wid, name="Afternoon", positions={sheer: 40, blackout: 85}))[
        "preset_id"]
    await api.ok("preset/select", window_id=wid, preset_id=pid)
    covers.travel = {STOVA_SHEER: 20, STOVA_BLACKOUT: 30}
    op = await confirm(api, await window(api, wid), preset(await window(api, wid), "Afternoon"))
    assert op["status"] == "running" and len(op["items"]) == 2
    assert sorted(covers.services()) == [("set_cover_position", STOVA_SHEER, 40),
                                         ("set_cover_position", STOVA_BLACKOUT, 85)]  # one call per cover
    w = await window(api, wid)
    assert w["running"] and w["current"]["status"] == "moving"
    assert {i["entity_id"]: i["status"] for i in w["last"]["items"]} == {STOVA_SHEER: "moving", STOVA_BLACKOUT: "moving"}
    # A second preset for the same window (or cover) is refused instead of contradicting the first.
    err = await api.error("apply", window_id=wid, preset_id="p_open", confirm=True,
                          expected=preset(w, "Fully open")["positions"])
    assert err["code"] == "busy"
    for _ in range(11):
        await advance(hass, freezer)
    w = await window(api, wid)
    assert {i["entity_id"]: i["status"] for i in w["last"]["items"]} == {STOVA_SHEER: "reached",
                                                                          STOVA_BLACKOUT: "moving"}
    for _ in range(6):
        await advance(hass, freezer)
    w = await window(api, wid)
    last = w["last"]
    assert last["status"] == "succeeded" and not w["running"]
    assert {i["entity_id"]: (i["status"], i["final_position"]) for i in last["items"]} == {
        STOVA_SHEER: ("reached", 40), STOVA_BLACKOUT: ("reached", 85)}
    assert w["current"] == {"status": "preset", "preset_id": pid, "text": "Afternoon", "also": []}
    assert "applied" in last["explanation"]
    assert w["manual"] is None  # our own movement (reported with the device's context) is not a manual change
    assert len(covers.calls) == 2  # nothing repeated


async def test_service_selection_and_open_close_only_covers(hass, pilot, house, covers, hass_ws_client) -> None:
    _e, _m, api = await start(hass, pilot, hass_ws_client)
    wid = (await api.ok("window/create", name="Trappa", area_id=house.trappa,
                        layers=[{"entity_id": SCREEN}, {"entity_id": KAMAR}]))["window_id"]
    w = await window(api, wid)
    screen, kamar = layer_ids(w)[SCREEN], layer_ids(w)[KAMAR]
    assert any("can only open or close fully" in p["text"] for p in w["problems"])  # Half open = 50 on the screen
    op = await confirm(api, w, preset(w, "Fully open"))
    assert covers.services() == [("open_cover", SCREEN, None), ("set_cover_position", KAMAR, 100)]
    assert op["status"] == "succeeded"
    covers.calls.clear()
    op = await confirm(api, w, preset(w, "Half open"))
    items = {i["entity_id"]: i for i in op["items"]}
    assert items[SCREEN]["status"] == "unsupported" and "0 % or 100 %" in items[SCREEN]["detail"]
    assert covers.services() == [("set_cover_position", KAMAR, 50)]
    assert op["status"] == "partial"
    covers.calls.clear()
    pid = (await api.ok("preset/save", window_id=wid, name="Dark", positions={screen: 0, kamar: 0}))["preset_id"]
    op = await confirm(api, await window(api, wid), {"preset_id": pid, "positions": {screen: 0, kamar: 0}})
    assert covers.services() == [("close_cover", SCREEN, None), ("set_cover_position", KAMAR, 0)]
    assert op["status"] == "succeeded"


async def test_already_correct_covers_get_no_command(hass, pilot, house, covers, hass_ws_client) -> None:
    _e, _m, api = await start(hass, pilot, hass_ws_client)
    wid = await make_stova(api, house)
    w = await window(api, wid)
    set_cover(hass, STOVA_SHEER, 49)
    set_cover(hass, STOVA_BLACKOUT, 98)
    op = await confirm(api, w, preset(w, "Half shade"))  # 50 / 100: both within 3
    assert covers.calls == [] and op["status"] == "nothing_to_do"
    assert {i["status"] for i in op["items"]} == {"already"}
    op = await confirm(api, w, preset(w, "Full shade"))  # sheer 49 -> 0, blackout already
    assert covers.services() == [("set_cover_position", STOVA_SHEER, 0)]
    assert op["status"] == "succeeded"
    # Applying the same preset again sends nothing.
    covers.calls.clear()
    op = await confirm(api, w, preset(w, "Full shade"))
    assert covers.calls == [] and op["status"] == "nothing_to_do"


async def test_missing_and_unavailable_covers(hass, pilot, house, covers, hass_ws_client, freezer) -> None:
    _e, mgr, api = await start(hass, pilot, hass_ws_client)
    wid = await make_stova(api, house)
    w = await window(api, wid)
    set_cover(hass, STOVA_BLACKOUT, None, state="unavailable")
    w = await window(api, wid)
    assert any("is unavailable" in p["text"] for p in w["problems"])
    op = await confirm(api, w, preset(w, "Half shade"))
    items = {i["entity_id"]: i for i in op["items"]}
    assert items[STOVA_BLACKOUT]["status"] == "unavailable"
    assert covers.services() == [("set_cover_position", STOVA_SHEER, 50)]  # never to an unavailable cover
    assert op["status"] == "partial"
    # A cover that drops off the network instead of moving.
    set_cover(hass, STOVA_BLACKOUT, 50)
    covers.calls.clear()
    covers.drop = {STOVA_BLACKOUT}
    op = await confirm(api, await window(api, wid), preset(w, "Full blackout"))
    for _ in range(62):
        await advance(hass, freezer)
    w = await window(api, wid)
    items = {i["entity_id"]: i for i in w["last"]["items"]}
    assert items[STOVA_BLACKOUT]["status"] == "unavailable" and items[STOVA_SHEER]["status"] == "reached"
    assert w["last"]["status"] == "partial"
    # A removed entity is reported, never commanded.
    hass.states.async_remove(STOVA_SHEER)
    w = await window(api, wid)
    assert w["layers"][0]["missing"] and any("does not exist" in p["text"] for p in w["problems"])
    covers.calls.clear()
    covers.drop = set()
    set_cover(hass, STOVA_BLACKOUT, 50)
    op = await confirm(api, w, preset(w, "Fully open"))
    assert {i["entity_id"]: i["status"] for i in op["items"]}[STOVA_SHEER] == "missing"
    assert covers.services() == [("set_cover_position", STOVA_BLACKOUT, 100)]


async def test_partial_failure_and_timeout(hass, pilot, house, covers, hass_ws_client, freezer) -> None:
    _e, mgr, api = await start(hass, pilot, hass_ws_client)
    wid = await make_stova(api, house)
    w = await window(api, wid)
    covers.fail = {STOVA_SHEER}
    op = await confirm(api, w, preset(w, "Half shade"))
    items = {i["entity_id"]: i for i in op["items"]}
    assert items[STOVA_SHEER]["status"] == "failed" and "zigbee2mqtt" in items[STOVA_SHEER]["detail"]
    assert items[STOVA_BLACKOUT]["status"] == "reached"
    assert items[STOVA_BLACKOUT]["detail"] == "Reported 100 % (target 100 %)"  # the verified result, not "sent"
    assert op["status"] == "partial" and "partly succeeded" in op["explanation"]
    # A cover that accepts the command but never moves: timed out, not repeated.
    covers.fail = set()
    covers.ignore = {STOVA_BLACKOUT}
    covers.calls.clear()
    op = await confirm(api, await window(api, wid), preset(w, "Full blackout"))  # sheer 0 -> 100, blackout -> 0
    assert op["status"] == "running"
    for _ in range(59):
        await advance(hass, freezer)
    assert (await window(api, wid))["running"]
    for _ in range(2):
        await advance(hass, freezer)
    w = await window(api, wid)
    items = {i["entity_id"]: i for i in w["last"]["items"]}
    assert items[STOVA_BLACKOUT]["status"] == "timeout" and "Still at 100 %" in items[STOVA_BLACKOUT]["detail"]
    assert w["last"]["status"] == "partial" and not w["running"]
    assert len(covers.services(STOVA_BLACKOUT)) == 1
    # Every cover timing out is "timeout" (both are at 100 now; Evening privacy is 0 / 60).
    covers.ignore = {STOVA_SHEER, STOVA_BLACKOUT}
    op = await confirm(api, w, preset(w, "Evening privacy"))
    assert op["status"] == "running"
    for _ in range(61):
        await advance(hass, freezer)
    assert (await window(api, wid))["last"]["status"] == "timeout"
    assert len((await window(api, wid))["history"]) == 3


# ---------------------------------------------------------- manual changes
async def test_manual_change_is_recorded_not_acted_on(hass, pilot, house, covers, hass_ws_client, freezer) -> None:
    _e, mgr, api = await start(hass, pilot, hass_ws_client)
    wid = await make_stova(api, house)
    # NSPanel / device button: a report with the device's context and no user.
    set_cover(hass, STOVA_BLACKOUT, 0, context=Context())
    w = await window(api, wid)
    assert w["manual"]["entity_id"] == STOVA_BLACKOUT and (w["manual"]["from"], w["manual"]["to"]) == (50, 0)
    assert w["manual"]["source"] == "outside"
    # A person in Home Assistant.
    for _ in range(70):
        await advance(hass, freezer)
    set_cover(hass, STOVA_SHEER, 30, context=Context(user_id="abc"))
    w = await window(api, wid)
    assert w["manual"]["entity_id"] == STOVA_SHEER and w["manual"]["source"] == "person"
    # Restarts and reconnects (unavailable -> position) are not movements.
    await api.ok("manual/clear", window_id=wid)
    set_cover(hass, STOVA_SHEER, None, state="unavailable")
    set_cover(hass, STOVA_SHEER, 30)
    assert (await window(api, wid))["manual"] is None
    # Applying a preset supersedes it; its own reports are not manual.
    set_cover(hass, STOVA_SHEER, 31, context=Context())
    assert (await window(api, wid))["manual"] is not None
    w = await window(api, wid)
    await confirm(api, w, preset(w, "Full shade"))
    assert (await window(api, wid))["manual"] is None
    assert len(covers.calls) == 2


# ---------------------------------------------------------- restart / safety
async def test_restart_persists_everything_and_moves_nothing(hass, pilot, house, covers, hass_ws_client, freezer,
                                                             service_calls) -> None:
    entry, mgr, api = await start(hass, pilot, hass_ws_client)
    wid = await make_stova(api, house)
    w = await window(api, wid)
    sheer = layer_ids(w)[STOVA_SHEER]
    await api.ok("preset/save", window_id=wid, preset_id="p_half_shade", positions={sheer: 35})
    await api.ok("preset/select", window_id=wid, preset_id="p_half_shade")
    await api.ok("window/update", window_id=wid, solar={"exposure_width": 55, "min_elevation": 4, "obstructions": [
        {"from_azimuth": 60, "to_azimuth": 120, "min_elevation": 12}]}, solar_protection=True)
    covers.travel = {STOVA_SHEER: 40}
    await confirm(api, w, preset(await window(api, wid), "Half shade"))
    assert mgr.running(wid)
    calls = len(covers.calls)
    assert await hass.config_entries.async_reload(entry.entry_id)  # stops while the sheer is still moving
    await hass.async_block_till_done()
    for _ in range(25):
        await advance(hass, freezer)
    mgr = entry.runtime_data.curtains
    api = Api(await hass_ws_client(hass))
    w = await window(api, wid)
    assert preset(w, "Half shade")["positions"][sheer] == 35 and w["selected_preset"] == "p_half_shade"
    assert w["solar"] == {"exposure_width": 55, "min_elevation": 4.0, "obstructions": [
        {"from_azimuth": 60.0, "to_azimuth": 120.0, "min_elevation": 12.0}]} and w["solar_protection"]
    assert w["last"]["status"] == "interrupted" and not w["running"]
    assert {i["entity_id"]: i["status"] for i in w["last"]["items"]}[STOVA_SHEER] == "interrupted"
    assert len(covers.calls) == calls  # nothing was resumed or sent again on restart
    cover_events = [c for c in service_calls if c.data["domain"] == "cover"]
    assert len(cover_events) == calls


async def test_setup_with_stored_windows_sends_nothing(hass, pilot, house, covers, hass_storage, service_calls) -> None:
    layers = (CoverLayer("l_1", STOVA_SHEER, "sheer"), CoverLayer("l_2", STOVA_BLACKOUT, "blackout"))
    stored = CurtainConfig({"w_1": with_layers(Window("w_1", "Stova Suður", house.stova, direction="S"), layers)})
    data = stored.as_dict()
    data["windows"][0]["selected_preset"] = "p_open"
    hass_storage["smart_occupancy.curtains"] = {"version": 1, "minor_version": 1, "key": "smart_occupancy.curtains",
                                                "data": data}
    hass_storage["smart_occupancy.curtains_state"] = {"version": 1, "key": "smart_occupancy.curtains_state", "data": {
        "last": {"w_1": {"op_id": "op_1", "window_id": "w_1", "preset_id": "p_open", "preset_name": "Fully open",
                         "status": "running", "items": [{"entity_id": STOVA_SHEER, "status": "sent", "target": 100}]}}}}
    entry = await setup_entry(hass, pilot_options(pilot))
    mgr = entry.runtime_data.curtains
    assert mgr.config.windows["w_1"].selected_preset == "p_open"
    assert mgr.last["w_1"]["status"] == "interrupted"
    await hass.async_block_till_done()
    assert covers.calls == [] and not [c for c in service_calls if c.data["domain"] == "cover"]


async def test_lighting_moods_are_never_read() -> None:
    """The curtain code does not read or write a lighting mood (no mood helper, no lighting mood module)."""
    for path in (Path(__file__).parents[1] / "custom_components/smart_occupancy/curtains").glob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert not re.search(r"mood", source.split('"""', 2)[-1], re.IGNORECASE), path.name
        assert "input_select" not in source and "select_option" not in source, path.name


async def test_rename_keeps_presets(hass, pilot, house, covers, hass_ws_client) -> None:
    _e, mgr, api = await start(hass, pilot, hass_ws_client)
    wid = await make_stova(api, house)
    before = (await window(api, wid))["presets"]
    er.async_get(hass).async_update_entity(STOVA_SHEER, new_entity_id="cover.stova_sheer")
    await hass.async_block_till_done()
    w = await window(api, wid)
    assert [x["entity_id"] for x in w["layers"]] == ["cover.stova_sheer", STOVA_BLACKOUT]
    assert w["presets"] == before


# ------------------------------------------------------------------- solar
def test_exposure_geometry_and_obstructions() -> None:
    settings = cc.SolarSettings(exposure_width=70, min_elevation=3, obstructions=(cc.Obstruction(60, 120, 12),))
    east = 90.0

    def status(az, el, bearing=east):
        return solar.exposure(bearing, solar.SunNow(az, el, None), settings)["status"]

    assert status(100, 20) == "exposed"
    assert status(100, 8) == "obstructed"           # the hill east of the house
    assert status(140, 8) == "exposed"              # past the hill
    assert status(170, 30) == "outside_sector"      # 80° off the bearing
    assert status(90, 2) == "too_low"
    assert status(90, -5) == "below_horizon"
    assert status(10, 20, bearing=None) == "no_direction"
    assert solar.exposure(east, solar.SunNow(None, 20, None), settings)["status"] == "unknown"
    assert status(350, 20, bearing=0) == "exposed" and solar.in_arc(5, 350, 20)  # wraps through north


def test_sun_angle_calibration_is_validated_not_assumed() -> None:
    samples: list[tuple[float, float]] = []
    for az in range(120, 240, 5):
        solar.add_sample(samples, az, az - 113 + (0.5 if az % 10 else -0.5))
    cal = solar.calibrate(samples)
    assert cal["status"] == "validated" and cal["sign"] == 1 and abs(cal["offset"] + 113) < 1
    assert abs(cal["zero_azimuth"] - 113) < 1 and "0° = azimuth 113°" in solar.relation_text(cal)
    # A mirrored sensor is recognised as such, not assumed to be a shifted azimuth.
    mirrored = [(az, 180 - az) for az, _ in samples]
    assert solar.calibrate(mirrored)["sign"] == -1 and solar.calibrate(mirrored)["status"] == "validated"
    # Too little sun movement: not validated.
    assert solar.calibrate(samples[:3])["status"] == "insufficient"
    # Not a straight relation (e.g. a value clamped to the window's sector).
    clamped = [(az, max(-30, min(30, az - 180))) for az, _ in samples]
    assert solar.calibrate(clamped)["status"] == "inconsistent"


async def test_solar_display_and_settings_never_move(hass, pilot, house, covers, hass_ws_client, freezer,
                                                     service_calls) -> None:
    _e, mgr, api = await start(hass, pilot, hass_ws_client)
    wid = await make_stova(api, house)
    await api.ok("window/update", window_id=wid, solar_protection=True)
    for az in range(150, 216, 3):
        hass.states.async_set("sun.sun", "above_horizon", {"azimuth": az, "elevation": 9.5})
        hass.states.async_set("sensor.sun_angle", str(az - 180))
        await hass.async_block_till_done()
    model = await api.ok("model")
    assert model["sun"]["azimuth"] == 213 and model["sun"]["elevation"] == 9.5 and model["sun"]["sun_angle"] == 33
    assert model["sun"]["calibration"]["status"] == "validated"
    assert model["sun"]["calibration"]["zero_azimuth"] == 180
    w = next(x for x in model["windows"] if x["window_id"] == wid)
    assert w["exposure"]["status"] == "exposed" and w["exposure"]["offset"] == 33
    await api.ok("window/update", window_id=wid, solar={"exposure_width": 30, "min_elevation": 2})
    assert (await window(api, wid))["exposure"]["status"] == "outside_sector"
    assert (await api.error("window/update", window_id=wid, solar={"exposure_width": 120}))["code"] == \
        "invalid_exposure_width"
    assert (await api.error("window/update", window_id=wid, solar={"min_elevation": 80}))["code"] == \
        "invalid_elevation"
    bad = {"obstructions": [{"from_azimuth": 60, "to_azimuth": 400, "min_elevation": 5}]}
    assert (await api.error("window/update", window_id=wid, solar=bad))["code"] == "invalid_obstruction"
    # Standard azimuth / elevation from two sensors instead of sun.sun.
    hass.states.async_set("sensor.sun_solar_azimuth", "181.4")
    hass.states.async_set("sensor.sun_solar_elevation", "-2")
    await api.ok("settings", azimuth_entity="sensor.sun_solar_azimuth", elevation_entity="sensor.sun_solar_elevation")
    w = await window(api, wid)
    assert w["exposure"]["status"] == "below_horizon"
    assert (await api.error("settings", tolerance=20))["code"] == "invalid_tolerance"
    await api.ok("settings", tolerance=5, verify_timeout=180, sun_angle_entity=None)
    model = await api.ok("model")
    assert model["settings"]["tolerance"] == 5 and model["sun"]["sun_angle"] is None
    await hass.async_block_till_done()
    assert covers.calls == [] and not [c for c in service_calls if c.data["domain"] == "cover"]


# --------------------------------------------------------------- conflicts
async def test_competing_automations_are_reported_and_untouched(hass, pilot, house, covers, hass_ws_client) -> None:
    assert await async_setup_component(hass, "automation", {"automation": [
        {"id": "cover_stova_sudur", "alias": "cover_stova_sudur",
         "triggers": [{"trigger": "state", "entity_id": "input_select.gardinur_state"}],
         "actions": [{"action": "cover.set_cover_position", "target": {"entity_id": STOVA_BLACKOUT},
                      "data": {"position": 50}}]},
        {"id": "stova_kokur_gardinur", "alias": "stova_kokur_gardinur", "initial_state": False,
         "triggers": [{"trigger": "state", "entity_id": "input_boolean.sol"}],
         "actions": [{"action": "cover.close_cover", "target": {"area_id": house.stova}}]},
        {"id": "lights_only", "alias": "lights_only",
         "triggers": [{"trigger": "state", "entity_id": "input_boolean.sol"}],
         "actions": [{"action": "light.turn_on", "target": {"area_id": house.stova}}]},
    ]})
    await hass.async_block_till_done()
    _e, _m, api = await start(hass, pilot, hass_ws_client)
    wid = await make_stova(api, house)
    w = await window(api, wid)
    found = {x["entity_id"]: [(c["automation"], c["enabled"], c["via"]) for c in x["conflicts"]] for x in w["layers"]}
    assert found[STOVA_BLACKOUT] == [("automation.cover_stova_sudur", True, "entity"),
                                     ("automation.stova_kokur_gardinur", False, "area")]
    assert found[STOVA_SHEER] == [("automation.stova_kokur_gardinur", False, "area")]
    assert any("enabled automation(s) may also move" in p["text"] for p in w["problems"])
    assert hass.states.get("automation.cover_stova_sudur").state == "on"  # never changed
    assert hass.states.get("automation.stova_kokur_gardinur").state == "off"
    assert covers.calls == []
