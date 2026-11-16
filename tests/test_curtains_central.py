"""Phase 4.6.4 addendum (0.6.6): Global Schedule and Global Modes are the only curtain configuration.

Old room / window / cover mode overrides and room / window schedules (0.6.3-0.6.5) stay active until a reviewed
migration converts them; the conversion must give every window the same positions, ambiguous or inexact items
need a deliberate choice, and only then are the old settings removed. Afterwards nothing outside Global Modes
and Global Schedule can change a window's preset or timing. Simulated Zigbee2MQTT covers only.
"""

from __future__ import annotations

import copy
from typing import Any

from custom_components.smart_occupancy.curtains import migration, modes
from custom_components.smart_occupancy.curtains.config import (
    MODES,
    NO_ACTION,
    CoverLayer,
    CurtainConfig,
    Window,
    with_layers,
)

from .test_curtains import KAMAR, STOVA_BLACKOUT, STOVA_SHEER, covers, house, set_cover  # noqa: F401
from .test_curtains_auto import LUX, admin_ws, begin, enable, faroe, global_modes, kamar, stova  # noqa: F401


def _stova(area: str) -> dict[str, Any]:
    layers = (CoverLayer("l_sheer", STOVA_SHEER, "sheer"), CoverLayer("l_dark", STOVA_BLACKOUT, "blackout"))
    w = with_layers(Window("w_stova", "Stova Suður", area_id=area, mode="automatic", privacy=True), layers)
    return w.as_dict()


def _kamar(area: str) -> dict[str, Any]:
    w = with_layers(Window("w_kamar", "Kamar Erva", area_id=area, sleep_protection=True),
                    (CoverLayer("l_k", KAMAR, "blackout"),))
    return w.as_dict()


def store_0_6_5(house) -> dict[str, Any]:
    """A 0.6.5 store with every kind of old setting (no "centralized" flag)."""
    stova_w = _stova(house.stova)
    stova_w["conditions"].update(morning="p_full_shade",      # hides the Global Morning (Half shade): conflict
                                 glare=NO_ACTION,             # cannot be expressed globally: must be accepted
                                 lamp="p_evening_privacy")    # + a cover override: needs a new cover preset
    stova_w["cover_modes"] = {"l_sheer": {"night": "c_closed", "lamp": "c_open"}}
    stova_w["auto"] = {"night_end_workday": "06:30"}         # same as the Global Schedule: simply removed
    kamar_w = _kamar(house.kamar)
    return {"windows": [stova_w, kamar_w], "defaults": {"lux_entity": LUX, "min_interval": 0},
            "rooms": {house.kamar: {"night_start_workday": "21:00"}},          # differs: a choice is required
            "room_modes": {house.stova: {"evening_privacy": NO_ACTION}},
            "modes": {"morning": [{"window_id": "w_stova", "layer_id": None, "preset_id": "p_half_shade"}],
                      "night": [{"window_id": "w_stova", "layer_id": None, "preset_id": "p_blackout"},
                                {"window_id": "w_kamar", "layer_id": None, "preset_id": "p_closed"}],
                      "day": [{"window_id": "w_stova", "layer_id": None, "preset_id": "p_open"},
                              {"window_id": "w_kamar", "layer_id": None, "preset_id": "p_open"}],
                      "sun": [{"window_id": "w_stova", "layer_id": None, "preset_id": "p_full_shade"}]}}


def positions(config: CurtainConfig, window_id: str, mode: str, legacy: bool) -> dict[str, int | None]:
    eff = modes.effective(config, config.windows[window_id], mode, legacy=legacy)
    return dict(eff.preset.positions) if eff.has_target else {}


async def test_reviewed_migration_keeps_behaviour_and_removes_old_settings(hass, pilot, house, covers, admin_ws,
                                                                          freezer, faroe, hass_storage) -> None:
    stored = store_0_6_5(house)
    hass_storage["smart_occupancy.curtains"] = {"version": 1, "minor_version": 4, "key": "smart_occupancy.curtains",
                                                "data": copy.deepcopy(stored)}
    set_cover(hass, STOVA_SHEER, 100)
    set_cover(hass, STOVA_BLACKOUT, 100)
    entry, r = await begin(hass, pilot, freezer, covers, admin_ws, "2026-11-19T13:00:00+00:00", lux=3000,
                           elevation=5)
    api = r.api
    old = r.mgr.config
    # Until the migration: the old settings still decide, exactly as in 0.6.5 (nothing changes silently).
    assert not old.centralized
    assert old.rules_for(old.windows["w_kamar"])[0]["night_start_workday"] == "21:00"
    assert positions(old, "w_stova", "morning", legacy=False) == {"l_sheer": 50, "l_dark": 100}   # hidden global
    assert positions(old, "w_stova", "morning", legacy=True) == {"l_sheer": 0, "l_dark": 100}     # in force now
    assert modes.effective(old, old.windows["w_stova"], "morning").preset_id == "p_full_shade"
    model = await api.ok("model")
    assert model["migration"]["pending"] and model["migration"]["required"] == 2 and not model["centralized"]
    review = await api.ok("migration/change", dry_run=True)
    items = {x["id"]: x for x in review["items"]}
    assert set(review["missing"]) == {"inexact:w_stova:glare", f"schedule:room:{house.kamar}:night_start_workday"}
    assert items["conflict:w_stova:morning"]["choices"] == ["keep", "global"]
    assert items["morning:w_kamar"]["default"] == "keep"
    assert items["schedule:window:w_stova:night_end_workday"]["detail"].endswith("removed")
    assert review["changes"] == [] and review["impact"] == []
    err = await api.error("migration/change")
    assert err["code"] == "choice_required"
    assert not r.mgr.config.centralized and covers.calls == []
    choices = {"inexact:w_stova:glare": "accept", f"schedule:room:{house.kamar}:night_start_workday": "adopt"}
    review = await api.ok("migration/change", choices=choices, dry_run=True)
    assert not review["missing"] and not review["needs_confirm"] and review["impact"] == []
    assert "Global Schedule: Night from (normal evenings) → 21:00 (from Kamar Erva)" in review["changes"]
    assert any("new Blackout preset “Evening privacy” (60 %)" in line for line in review["changes"])
    await api.ok("migration/change", choices=choices)
    new = r.mgr.config
    assert new.centralized and new.room_modes == {} and new.rooms == {}
    assert all(v is None for w in new.windows.values() for v in w.conditions.values())
    assert all(not w.cover_modes for w in new.windows.values())
    assert new.windows["w_stova"].auto == {} and new.defaults["night_start_workday"] == "21:00"
    # Every converted mapping gives exactly the positions it gave before (glare: the accepted fallback).
    for window_id in ("w_stova", "w_kamar"):
        for mode in MODES:
            if (window_id, mode) in {("w_stova", "glare")}:
                continue
            before = positions(old, window_id, mode, legacy=True)
            after = positions(new, window_id, mode, legacy=False)
            if mode == "morning" and window_id == "w_kamar":
                before = positions(old, "w_kamar", "day", legacy=True)   # kept: Daytime preset from daylight
            assert after == before, (window_id, mode, before, after)
    assert new.assignment("morning", "w_stova", None).preset_id == "p_full_shade"          # "keep" (default)
    night = {a.layer_id: a.preset_id for a in new.modes["night"] if a.window_id == "w_stova"}
    assert night == {"l_sheer": "c_closed", "l_dark": "c_closed"}                           # per cover, exact
    dark = new.windows["w_stova"].layer("l_dark")
    lamp = {a.layer_id: a.preset_id for a in new.modes["lamp"] if a.window_id == "w_stova"}
    assert lamp["l_sheer"] == "c_open" and dark.preset(lamp["l_dark"]).position == 60
    assert dark.preset(lamp["l_dark"]).name == "Evening privacy"
    assert not any(a.window_id == "w_stova" for a in new.modes.get("evening_privacy", ()))   # room No action
    assert new.assignment("morning", "w_kamar", None).preset_id == "p_open"
    # The page is clean now, and it survives a restart. Nothing moved.
    model = await api.ok("model")
    assert not model["migration"]["pending"] and model["schedule"]["overrides"] == []
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    reloaded = entry.runtime_data.curtains.config
    assert reloaded.centralized and reloaded.as_dict() == new.as_dict()
    assert hass_storage["smart_occupancy.curtains"]["data"]["centralized"] is True
    assert covers.calls == []


async def test_choosing_the_hidden_global_assignment_is_previewed(hass, pilot, house, covers, admin_ws, freezer,
                                                                   faroe, hass_storage) -> None:
    stored = store_0_6_5(house)
    hass_storage["smart_occupancy.curtains"] = {"version": 1, "minor_version": 4, "key": "smart_occupancy.curtains",
                                                "data": stored}
    set_cover(hass, STOVA_SHEER, 0)
    set_cover(hass, STOVA_BLACKOUT, 100)
    _e, r = await begin(hass, pilot, freezer, covers, admin_ws, "2026-11-19T09:20:00+00:00", lux=500, elevation=3)
    await r.settle()
    await r.walk(11)
    assert r.condition("w_stova") == "morning" and r.pos(STOVA_SHEER) == 0    # the old override: Full shade
    choices = {"inexact:w_stova:glare": "accept", f"schedule:room:{house.kamar}:night_start_workday": "follow",
               "conflict:w_stova:morning": "global"}
    review = await r.api.ok("migration/change", choices=choices, dry_run=True)
    entry = review["impact"][0]
    assert review["needs_confirm"] and entry["name"] == "Stova Suður" and entry["moves"] == 1
    assert entry["changes"][0]["after"] == "Morning: Half shade"
    assert (await r.api.error("migration/change", choices=choices))["code"] == "confirm_required"
    await r.api.ok("migration/change", choices=choices, confirm=True, policy="next_change")
    await r.walk(5)
    assert covers.calls == [] and r.auto("w_stova")["status"]["code"] == "deferred"
    assert r.mgr.config.rules_for(r.mgr.config.windows["w_kamar"])[0]["night_start_workday"] == "22:30"


async def test_nothing_to_migrate_centralizes_on_load(hass, pilot, house, covers, admin_ws, freezer, faroe,
                                                      hass_storage) -> None:
    stored = {"windows": [_stova(house.stova)], "defaults": {"lux_entity": LUX},
              "modes": {m: [{"window_id": "w_stova", "layer_id": None, "preset_id": "p_open"}]
                        for m in ("morning", "day")}}
    hass_storage["smart_occupancy.curtains"] = {"version": 1, "minor_version": 4, "key": "smart_occupancy.curtains",
                                                "data": stored}
    _e, r = await begin(hass, pilot, freezer, covers, admin_ws, "2026-11-19T13:00:00+00:00", lux=3000, elevation=5)
    assert r.mgr.config.centralized and r.mgr.config.windows["w_stova"].mode == "automatic"
    await r.mgr.async_flush()
    assert hass_storage["smart_occupancy.curtains"]["data"]["centralized"] is True
    assert not (await r.api.ok("model"))["migration"]["pending"]


async def test_no_hidden_override_after_centralization(hass, pilot, house, covers, admin_ws, freezer, faroe,
                                                       hass_storage) -> None:
    """Old override data left in a centralized store (e.g. edited by hand) is never used."""
    stova_w = _stova(house.stova)
    stova_w["conditions"].update(day="p_blackout")
    stova_w["auto"] = {"night_start_workday": "18:00"}
    stored = {"windows": [stova_w], "defaults": {"lux_entity": LUX}, "centralized": True,
              "room_modes": {house.stova: {"day": NO_ACTION}},
              "modes": {"day": [{"window_id": "w_stova", "layer_id": None, "preset_id": "p_open"}]}}
    hass_storage["smart_occupancy.curtains"] = {"version": 1, "minor_version": 4, "key": "smart_occupancy.curtains",
                                                "data": stored}
    set_cover(hass, STOVA_SHEER, 0)
    set_cover(hass, STOVA_BLACKOUT, 0)
    _e, r = await begin(hass, pilot, freezer, covers, admin_ws, "2026-11-19T13:00:00+00:00", lux=3000, elevation=5)
    cfg = r.mgr.config
    assert cfg.rules_for(cfg.windows["w_stova"])[0]["night_start_workday"] == "22:30"
    await r.settle()
    await r.walk(11)
    d = r.auto("w_stova")["decision"]
    assert (d["condition"], d["preset_id"], d["source"]) == ("day", "p_open", "global")
    assert (r.pos(STOVA_SHEER), r.pos(STOVA_BLACKOUT)) == (100, 100)
    model = await r.api.ok("model")
    assert mode_rows(model, "day")[0]["hidden"] == []


def mode_rows(model: dict[str, Any], key: str) -> list[dict[str, Any]]:
    return next(m for m in model["global_modes"]["modes"] if m["key"] == key)["rows"]


async def test_global_schedule_and_modes_drive_every_window(hass, pilot, house, covers, admin_ws, freezer,
                                                            faroe) -> None:
    """One schedule for the house; each window uses its own Global Modes presets; a window excluded from Morning
    keeps its position during Morning and opens with Daytime; one command per cover, never two."""
    set_cover(hass, STOVA_SHEER, 100)
    set_cover(hass, STOVA_BLACKOUT, 0)
    set_cover(hass, KAMAR, 0)
    _e, r = await begin(hass, pilot, freezer, covers, admin_ws, "2026-11-19T07:30:00+00:00", lux=0, elevation=-6,
                        sleep=False)
    api = r.api
    await api.ok("schedule/change", scope="global", values={"night_end_workday": "07:00", "morning_mode":
                                                            "until_time", "day_from_workday": "10:00"})
    s = await stova(api, house, assign=False)
    k = await kamar(api, house, assign=False)
    await global_modes(api, s, {"morning": "p_half_shade", "day": "p_open", "night": "p_blackout"})
    await global_modes(api, k, {"day": "p_open", "night": "p_closed"})        # excluded from Morning
    await r.settle()
    await enable(api, s)
    await enable(api, k)
    r.lux(400)
    r.sun(1.0, 130)
    await r.walk(11)
    assert r.condition(s) == "morning" and r.condition(k) == "morning"
    assert (r.pos(STOVA_SHEER), r.pos(STOVA_BLACKOUT), r.pos(KAMAR)) == (50, 100, 0)
    assert "No preset is set for Morning" in r.auto(k)["decision"]["text"]
    await r.until("10:01")
    assert r.condition(s) == "day" and r.condition(k) == "day"
    assert (r.pos(STOVA_SHEER), r.pos(STOVA_BLACKOUT), r.pos(KAMAR)) == (100, 100, 100)
    per_cover: dict[str, list[int]] = {}
    for call in covers.calls:
        per_cover.setdefault(call.data["entity_id"], []).append(call.data.get("position"))
    assert per_cover == {STOVA_SHEER: [50, 100], STOVA_BLACKOUT: [100], KAMAR: [100]}
    model = await api.ok("model")
    win = {w["window_id"]: w for w in model["windows"]}
    assert win[s]["effective"]["day"]["text"] == "Fully open — Global Daytime"
    assert win[k]["effective"]["morning"]["text"] == "No preset assigned"


def test_plan_is_pure_and_empty_when_centralized() -> None:
    assert migration.plan(CurtainConfig()) == []
    assert migration.plan(CurtainConfig(centralized=False)) == []
