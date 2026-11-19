"""Phase 4.6.3 (0.6.4): Global Curtain Modes.

The Global Modes page is a central way to configure which preset a condition uses; the automatic controller of
0.6.2/0.6.3 still decides the condition of every window on its own. Covered here: the six modes, assigning
windows and single covers, cover presets, layered presets by reference, stable ids, "no assignment means no
action", Inherit versus No action, global < room < window < cover, the 0.6.3 migration, overlap validation,
that editing never moves anything, Observing, arbitration, the sun fallbacks, missing presets, renames,
restarts, the reviewed save with its policies and 24-hour texts. Simulated Zigbee2MQTT covers only.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
import logging
import re
from typing import Any

from homeassistant.core import Context
from homeassistant.util import dt as dt_util

from custom_components.smart_occupancy.curtains import logic, modes, rules
from custom_components.smart_occupancy.curtains.auto import AutoCurtains
from custom_components.smart_occupancy.curtains.config import (
    COMMON_MODES,
    NO_ACTION,
    Assignment,
    CoverLayer,
    CurtainConfig,
    Window,
    default_conditions,
    with_layers,
)

from .test_curtains import (  # noqa: F401  (fixtures)
    KAMAR,
    KOKUR_BLACKOUT,
    KOKUR_EAST,
    KOKUR_SHEER,
    STOVA_BLACKOUT,
    STOVA_SHEER,
    Api,
    covers,
    house,
    set_cover,
)
from .test_lighting_runtime import WS_LOGGER
from .test_curtains_auto import (  # noqa: F401  (fixtures)
    FAROE,
    LUX,
    admin_ws,
    begin,
    enable,
    faroe,
    stova,
)

TZ = dt_util.get_time_zone(FAROE)


def at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=TZ)


# ======================================================================== pure helpers
def stova_window(**changes: Any) -> Window:
    layers = (CoverLayer("l_sheer", STOVA_SHEER, "sheer"), CoverLayer("l_dark", STOVA_BLACKOUT, "blackout"))
    return replace(with_layers(Window("w_stova", "Stova Suður", area_id="stova"), layers), **changes)


def kamar_window(**changes: Any) -> Window:
    return replace(with_layers(Window("w_kamar", "Kamar Erva", area_id="kamar"),
                               (CoverLayer("l_k", KAMAR, "blackout"),)), **changes)


def config(*windows: Window, **changes: Any) -> CurtainConfig:
    return replace(CurtainConfig(windows={w.window_id: w for w in windows}), **changes)


def assign(**mapping: list[tuple[str, str | None, str]]) -> dict[str, tuple[Assignment, ...]]:
    return {m: tuple(Assignment(*x) for x in items) for m, items in mapping.items()}


def test_six_common_modes_and_presets_defined_by_the_target() -> None:
    assert COMMON_MODES == ("morning", "day", "evening_privacy", "night", "sleep", "sun")
    from custom_components.smart_occupancy.curtains.config import MODE_WORDS
    assert [MODE_WORDS[m] for m in COMMON_MODES] == ["Morning", "Daytime", "Evening", "Night", "Sleep",
                                                     "Sun Protection"]
    w = stova_window()
    # Every single cover has its own editable presets with stable ids; the window keeps its layered library.
    for layer in w.layers:
        assert [(p.preset_id, p.name, p.position) for p in layer.presets] == [
            ("c_open", "Fully open", 100), ("c_half", "Half open", 50), ("c_closed", "Fully closed", 0)]
    assert [p.preset_id for p in w.presets] == ["p_open", "p_half_shade", "p_full_shade", "p_evening_privacy",
                                                "p_blackout"]
    # A new window has no mapping: no preset is chosen for it by default.
    assert all(v is None for v in w.conditions.values()) and w.cover_modes == {}
    for mode in COMMON_MODES:
        assert modes.effective(config(w), w, mode).state == "nothing"


def test_whole_window_and_single_cover_targets() -> None:
    w = stova_window()
    cfg = config(w, modes=assign(morning=[("w_stova", None, "p_half_shade")], day=[("w_stova", "l_sheer", "c_half")]))
    morning = modes.effective(cfg, w, "morning")
    # A whole-window assignment is exactly the window's preset: both layers, by reference.
    assert morning.has_target and morning.preset_id == "p_half_shade" and morning.source == "global"
    assert dict(morning.preset.positions) == {"l_sheer": 50, "l_dark": 100}
    assert modes.describe(morning, "morning") == "Half shade — Global Morning"
    # A single cover assignment changes only that cover: the blackout layer is left alone.
    day = modes.effective(cfg, w, "day")
    assert dict(day.preset.positions) == {"l_sheer": 50, "l_dark": None}
    assert day.preset.name == "Sheer: Half open" and day.unassigned() == ["Blackout"]
    # Nothing at all assigned: no action, nothing invented.
    assert modes.effective(cfg, w, "night").state == "nothing"
    assert modes.describe(modes.effective(cfg, w, "night"), "night") == "No preset assigned"


def test_inherit_no_action_and_the_four_levels() -> None:
    """The four levels of 0.6.4 still decide for a store that is not migrated yet (unchanged behaviour); once
    centralized (0.6.6) only Global Modes counts, so no old override can hide a Global Modes assignment."""
    w = stova_window()
    base = config(w, modes=assign(morning=[("w_stova", None, "p_half_shade")]), centralized=False)
    stale = replace(w, conditions={**w.conditions, "morning": "p_full_shade"},
                    cover_modes={"l_sheer": {"morning": "c_closed"}})
    central = replace(base, centralized=True, room_modes={"stova": {"morning": NO_ACTION}}).with_window(stale)
    assert (modes.effective(central, stale, "morning").source,
            modes.effective(central, stale, "morning").preset_id) == ("global", "p_half_shade")
    assert modes.hidden_by(central, stale, "morning", None) == []

    def eff(cfg: CurtainConfig, window: Window = w):
        return modes.effective(cfg, window, "morning")

    assert (eff(base).source, eff(base).preset_id) == ("global", "p_half_shade")
    room = replace(base, room_modes={"stova": {"morning": "p_open"}})
    assert (eff(room).source, eff(room).preset_id) == ("room", "p_open")                 # room > global
    win = replace(w, conditions={**w.conditions, "morning": "p_full_shade"})
    assert (eff(room.with_window(win), win).source, eff(room.with_window(win), win).preset_id) == (
        "window", "p_full_shade")                                                         # window > room
    cov = replace(win, cover_modes={"l_sheer": {"morning": "c_closed"}})
    mixed = eff(room.with_window(cov), cov)
    assert dict(mixed.preset.positions) == {"l_sheer": 0, "l_dark": 100} and mixed.source == "mixed"  # cover > window
    # Explicit No action is not missing configuration: it stops the lookup (the global one is hidden).
    none = replace(w, conditions={**w.conditions, "morning": NO_ACTION})
    stopped = eff(base.with_window(none), none)
    assert stopped.state == "no_action" and stopped.source == "window"
    assert modes.describe(stopped, "morning") == "No action — Explicit window override"
    hidden = modes.hidden_by(base.with_window(none), none, "morning", None)
    assert [(h["level"], h["label"]) for h in hidden] == [("window", "No action")]
    # Inherit again ("Use Global Setting") = the global assignment applies.
    back = replace(none, conditions={**none.conditions, "morning": None})
    assert eff(base.with_window(back), back).preset_id == "p_half_shade"
    # A room No action stops it for every window of the room that has nothing more specific.
    assert eff(replace(base, room_modes={"stova": {"morning": NO_ACTION}})).state == "no_action"


def test_glare_and_heat_fall_back_to_sun_protection_per_cover() -> None:
    w = stova_window()
    cfg = config(w, modes=assign(sun=[("w_stova", None, "p_full_shade")], glare=[("w_stova", "l_sheer", "c_half")],
                                 morning=[("w_stova", None, "p_half_shade")]), centralized=False)
    heat = modes.effective(cfg, w, "heat")
    assert heat.preset_id == "p_full_shade" and heat.fallback                  # nothing for heat: Sun Protection
    glare = modes.effective(cfg, w, "glare")
    assert dict(glare.preset.positions) == {"l_sheer": 50, "l_dark": 100}      # sheer own, blackout from Sun
    assert [t.fallback for t in glare.layers] == [False, True]
    # An explicit No action for glare is not "nothing": no fallback.
    no = replace(w, conditions={**w.conditions, "glare": NO_ACTION})
    assert modes.effective(cfg.with_window(no), no, "glare").state == "no_action"
    # Useful solar heating keeps the Morning / Daytime mapping; nothing else falls back to another mode.
    assert modes.effective(cfg, w, "solar_heating", "morning").preset_id == "p_half_shade"
    assert modes.effective(cfg, w, "solar_heating", "day").state == "nothing"
    assert modes.effective(cfg, w, "night").state == "nothing"


def _ev(now: str, **changes: Any) -> logic.Evidence:
    ev = logic.Evidence(now=at(now), lux=600.0, lux_status="ok", lux_entity=LUX, daylight=True, elevation=6.0,
                        exposure_text="x")
    for key, value in changes.items():
        setattr(ev, key, value)
    return ev


def _decide(cfg: CurtainConfig, w: Window, ev: logic.Evidence, values=None, hold=None) -> logic.Decision:
    return logic.decide(AutoCurtains.flags_for(cfg, w), values or rules.defaults(), ev, hold=hold, active_since={},
                        morning_since=None)


def test_arbitration_sleep_night_lamp_and_manual_hold_with_global_modes() -> None:
    w = stova_window(sleep_protection=True, privacy=True)
    cfg = config(w, modes=assign(day=[("w_stova", None, "p_open")], night=[("w_stova", None, "p_blackout")],
                                 sleep=[("w_stova", None, "p_blackout")], lamp=[("w_stova", "l_sheer", "c_closed")]))
    day = _decide(cfg, w, _ev("2026-11-19T13:00", sleep="off"))
    assert (day.condition, day.preset_id, day.target.source) == ("day", "p_open", "global")
    lamp = _decide(cfg, w, _ev("2026-11-19T13:00", sleep="off", lamp="on"))
    assert lamp.condition == "lamp" and dict(lamp.target.preset.positions) == {"l_sheer": 0, "l_dark": None}
    night = _decide(cfg, w, _ev("2026-11-19T23:00", sleep="off", lamp="on", lux=0.0, daylight=False,
                                elevation=-30.0))
    assert night.condition == "night" and night.preset_id == "p_blackout" and night.lamp_bypassed
    asleep = _decide(cfg, w, _ev("2026-11-19T13:00", sleep="on", lamp="on"))
    assert asleep.condition == "sleep" and asleep.preset_id == "p_blackout"
    held = _decide(cfg, w, _ev("2026-11-19T13:00", sleep="off"),
                   hold={"source": "manual", "created": at("2026-11-19T12:55"), "policy": "until_released",
                         "phase": "day"})
    assert held.condition == "manual_override" and held.preset_id is None and not held.target.has_target
    # Without any lamp assignment the lamp does not take over.
    no_lamp = replace(cfg, modes={k: v for k, v in cfg.modes.items() if k != "lamp"})
    assert _decide(no_lamp, w, _ev("2026-11-19T13:00", sleep="off", lamp="on")).condition == "day"


def test_morning_nothing_versus_no_action_and_no_fallback() -> None:
    """0.6.6: the Global Schedule decides when Morning is: a window without a Morning preset keeps its position
    during Morning (it is excluded from that mode). Before the migration (0.6.2-0.6.5) nothing for Morning started
    the Daytime preset directly, and an explicit No action kept Morning as a phase that changes nothing."""
    w = stova_window()
    central = config(w, modes=assign(day=[("w_stova", None, "p_open")]))
    d = _decide(central, w, _ev("2026-11-19T09:00"))
    assert d.condition == "morning" and d.preset_id is None
    assert d.text.endswith("No preset is set for Morning: the covers are kept as they are")
    cfg = replace(central, centralized=False)
    assert _decide(cfg, w, _ev("2026-11-19T09:00")).condition == "day"
    no = replace(w, conditions={**w.conditions, "morning": NO_ACTION})
    d = _decide(cfg.with_window(no), no, _ev("2026-11-19T09:00"))
    assert d.condition == "morning" and d.preset_id is None and d.target.state == "no_action"
    assert d.text.endswith("Morning is set to No action (Window override): the covers are kept as they are")
    # A winning protective mode without an assignment keeps the position; nothing lower is used instead.
    n = _decide(cfg, w, _ev("2026-11-19T23:00", lux=0.0, daylight=False, elevation=-30.0))
    assert n.condition == "night" and n.preset_id is None
    assert n.text.endswith("No preset is set for Night: the covers are kept as they are")


def test_migrated_0_6_3_mappings_decide_exactly_as_before() -> None:
    """A 0.6.3 window mapping becomes the window's overrides; with no Global Modes it resolves to the same
    preset (and positions) for every condition, and a null mapping stays "keep the position"."""
    w = stova_window()
    legacy = {**default_conditions(w.presets), "night": None, "glare": None}
    migrated = Window.from_dict({**w.as_dict(), "conditions": legacy})
    cfg = config(migrated, centralized=False)
    for condition in ("morning", "day", "sun", "evening_privacy", "night", "sleep", "lamp", "glare", "heat"):
        old = logic.preset_for(logic.Flags(False, False, False, legacy), condition, None)
        new = modes.effective(cfg, migrated, condition)
        assert new.preset_id == old, condition
        if old:
            assert dict(new.preset.positions) == dict(migrated.preset(old).positions)
            assert new.source == "window"
    assert modes.effective(cfg, migrated, "night").state == "nothing"


# ======================================================================== the card API
async def trappa(api: Api, house) -> str:
    created = await api.ok("window/create", name="Trappa Erva", area_id=house.trappa, direction="N",
                           layers=[{"entity_id": KOKUR_EAST, "kind": "blackout"}])
    return created["window_id"]


def ops(*items: dict[str, Any]) -> list[dict[str, Any]]:
    return list(items)


def a(mode: str, window_id: str, preset_id: str, layer_id: str | None = None, **extra: Any) -> dict[str, Any]:
    return {"op": "assign", "mode": mode, "window_id": window_id, "layer_id": layer_id, "preset_id": preset_id,
            **extra}


def mode_of(model: dict[str, Any], key: str) -> dict[str, Any]:
    return next(m for m in model["global_modes"]["modes"] if m["key"] == key)


async def test_acceptance_morning_and_daytime_through_the_existing_controller(hass, pilot, house, covers, admin_ws,
                                                                              freezer, faroe) -> None:
    """The user's acceptance test: Morning (Stova Suður → Half shade, Trappa Erva → Half open), Daytime (both
    Fully open); the automatic controller decides each window and applies the resolved preset; editing Half
    shade from 50 % to 35 % changes the Global Morning assignment; an unassigned condition changes nothing."""
    set_cover(hass, STOVA_SHEER, 100)
    set_cover(hass, STOVA_BLACKOUT, 0)
    set_cover(hass, KOKUR_EAST, 0)
    _e, r = await begin(hass, pilot, freezer, covers, admin_ws, "2026-11-19T09:20:00+00:00", lux=0, elevation=2.0)
    api = r.api
    s = await stova(api, house, assign=False)
    t = await trappa(api, house)
    model = await api.ok("model")
    assert [m["word"] for m in model["global_modes"]["modes"] if not m["advanced"]] == [
        "Morning", "Daytime", "Evening", "Night", "Sleep", "Sun Protection"]
    assert all(m["count"] == 0 for m in model["global_modes"]["modes"])
    targets = {(x["name"], x["type"]) for x in model["global_modes"]["targets"]}
    assert {("Stova Suður", "window"), ("Stova Suður — Sheer", "cover"), ("Stova Suður — Blackout", "cover"),
            ("Trappa Erva", "single")} <= targets
    # A draft is validated on a copy: nothing is saved and nothing moves.
    draft = await api.ok("modes/change", ops=ops(a("morning", s, "p_half_shade")), dry_run=True)
    assert draft["saved"] is False and mode_of(draft, "morning")["count"] == 1
    assert r.mgr.config.modes == {} and covers.calls == []
    saved = await api.ok("modes/change", ops=ops(a("morning", s, "p_half_shade"), a("morning", t, "p_half"),
                                                 a("day", s, "p_open"), a("day", t, "p_open")))
    assert saved["saved"] and not saved["needs_confirm"]
    assert saved["changes"][0] == "Morning: Stova Suður → Half shade (Global Modes)"
    model = await api.ok("model")
    rows = mode_of(model, "morning")["rows"]
    assert [(x["name"], x["type_word"], x["preset_id"], x["effective"]) for x in rows] == [
        ("Stova Suður", "Whole window", "p_half_shade", "Half shade — Global Morning"),
        ("Trappa Erva", "Cover", "p_half", "Half open — Global Morning")]
    assert rows[0]["status"]["mode_word"] == "Observing"
    await r.settle()
    await enable(api, s)
    await enable(api, t)
    assert covers.calls == []                                 # dark: waiting for daylight
    r.lux(400)
    await r.walk(11)
    assert r.condition(s) == "morning" and r.condition(t) == "morning"
    assert (r.pos(STOVA_SHEER), r.pos(STOVA_BLACKOUT), r.pos(KOKUR_EAST)) == (50, 100, 50)
    d = r.auto(s)["decision"]
    assert (d["preset_name"], d["source_word"]) == ("Half shade", "Global Mode")
    moved = next(x for x in r.auto(s)["log"] if x["action"] == "moved")
    assert moved["summary"] == "Morning. Stova Suður → Half shade (Global Mode)." and moved["source"] == "global"
    # Window Settings: Half shade 50 % → 35 %. The Global Morning assignment refers to the preset, so it follows.
    sheer = next(x["layer_id"] for x in (await api.ok("model"))["windows"][0]["layers"] if x["entity_id"] ==
                 STOVA_SHEER)
    await api.ok("preset/save", window_id=s, preset_id="p_half_shade", positions={sheer: 35})
    assert r.mgr.config.modes["morning"][0].preset_id == "p_half_shade"
    model = await api.ok("model")
    win = next(w for w in model["windows"] if w["window_id"] == s)
    assert win["effective"]["morning"]["positions"][sheer] == 35
    await r.walk(11)                                          # the minimum interval between moves, then 35 %
    assert r.pos(STOVA_SHEER) == 35 and r.pos(STOVA_BLACKOUT) == 100
    # Morning (120 min) ends: Daytime → Fully open for both.
    await r.walk(110, step=120)
    assert r.condition(s) == "day" and (r.pos(STOVA_SHEER), r.pos(STOVA_BLACKOUT), r.pos(KOKUR_EAST)) == (100, 100,
                                                                                                         100)
    # Dusk: Evening privacy wins for Stova Suður, but nothing is assigned to Evening: it stays where it is.
    calls = len(covers.calls)
    await r.until("15:30", step=300)
    r.lux(10)
    r.sun(-5.0, 230)
    await r.walk(12)
    assert r.condition(s) == "evening_privacy" and len(covers.calls) == calls
    assert (r.pos(STOVA_SHEER), r.pos(STOVA_BLACKOUT)) == (100, 100)
    keep = r.auto(s)["log"][0]
    assert keep["summary"] == "Evening. Stova Suður has no assignment; leaving positions unchanged."
    assert "No preset is set for Evening privacy" in r.auto(s)["decision"]["text"]


async def test_single_cover_assignment_moves_only_that_cover_and_overlaps_are_refused(
        hass, pilot, house, covers, admin_ws, freezer, faroe) -> None:
    set_cover(hass, STOVA_SHEER, 100)
    set_cover(hass, STOVA_BLACKOUT, 30)
    _e, r = await begin(hass, pilot, freezer, covers, admin_ws, "2026-11-19T12:10:00+00:00", lux=3000, elevation=6)
    api = r.api
    s = await stova(api, house, assign=False)
    w = r.mgr.config.windows[s]
    sheer, dark = (x.layer_id for x in w.layers)
    await api.ok("modes/change", ops=ops(a("day", s, "c_half", sheer)))
    # The whole window and one of its covers cannot both be assigned in one mode.
    err = await api.error("modes/change", ops=ops(a("day", s, "p_open")), dry_run=True)
    assert err["code"] == "overlap" and "replace it" in err["message"]
    err = await api.error("modes/change", ops=ops(a("day", s, "c_open", sheer), a("day", s, "p_open")))
    assert err["code"] == "overlap" and r.mgr.config.modes["day"] == (Assignment(s, sheer, "c_half"),)
    # A cover preset is not a window preset and vice versa; a single-cover window is assigned as itself.
    assert (await api.error("modes/change", ops=ops(a("day", s, "p_open", dark))))["code"] == "unknown_preset"
    assert (await api.error("modes/change", ops=ops(a("day", s, "c_half"))))["code"] == "unknown_preset"
    k = (await api.ok("window/create", name="Kamar", layers=[{"entity_id": KAMAR}]))["window_id"]
    klayer = r.mgr.config.windows[k].layers[0].layer_id
    assert (await api.error("modes/change", ops=ops(a("day", k, "c_half", klayer))))["code"] == "single_cover"
    await r.settle()
    await r.walk(11)                                          # daylight stable for 10 minutes
    await enable(api, s)
    await hass.async_block_till_done()
    # Only the sheer moves; the blackout layer is never commanded.
    assert covers.services() == [("set_cover_position", STOVA_SHEER, 50)]
    assert r.pos(STOVA_BLACKOUT) == 30
    d = r.auto(s)["decision"]
    assert d["target"]["positions"] == {sheer: 50, dark: None} and d["preset_name"] == "Sheer: Half open"
    assert next(x for x in r.auto(s)["log"] if x["action"] == "moved")["summary"] == (
        "Daytime. Stova Suður → Sheer: Half open (Global Mode). Blackout unchanged (no assignment).")
    # Replace the cover assignment by the whole window: at most one target per physical cover.
    preview = await api.ok("modes/change", ops=ops(a("day", s, "p_open", replace=True)), dry_run=True)
    assert preview["needs_confirm"] and preview["impact"][0]["moves"] == 2
    await api.ok("modes/change", ops=ops(a("day", s, "p_open", replace=True)), confirm=True, policy="now")
    await hass.async_block_till_done()
    assert r.mgr.config.modes["day"] == (Assignment(s, None, "p_open"),)
    # "Now" still goes through every safety gate: here the minimum interval since the last move.
    assert r.auto(s)["status"]["code"] == "dwell" and len(covers.calls) == 1
    await r.walk(11)
    assert covers.services()[1:] == [("set_cover_position", STOVA_SHEER, 100),
                                     ("set_cover_position", STOVA_BLACKOUT, 100)]


async def test_review_confirm_and_application_policies(hass, pilot, house, covers, admin_ws, freezer, faroe,
                                                       hass_ws_client, hass_read_only_user, caplog) -> None:
    caplog.set_level(logging.CRITICAL, logger=WS_LOGGER)  # HA logs the refused non-admin message as an error
    set_cover(hass, STOVA_SHEER, 100)
    set_cover(hass, STOVA_BLACKOUT, 100)
    set_cover(hass, KOKUR_SHEER, 100)
    set_cover(hass, KOKUR_BLACKOUT, 100)
    _e, r = await begin(hass, pilot, freezer, covers, admin_ws, "2026-11-19T13:00:00+00:00", lux=3000, elevation=5)
    api = r.api
    s = await stova(api, house)                               # Global Modes: the 0.6.3 defaults (day = Fully open)
    kokur = (await api.ok("window/create", name="Køkur Suður", area_id=house.kokur, layers=[
        {"entity_id": KOKUR_SHEER, "kind": "sheer"}, {"entity_id": KOKUR_BLACKOUT, "kind": "blackout"}]))["window_id"]
    await api.ok("modes/change", ops=ops(a("day", kokur, "p_open")))
    await r.settle()
    await r.walk(11)
    await enable(api, s)                                      # Køkur stays Observing
    await r.walk(2)
    assert r.condition(s) == "day" and covers.calls == []     # already Fully open
    change = ops(a("day", s, "p_half_shade"), a("day", kokur, "p_half_shade"))
    preview = await api.ok("modes/change", ops=change, dry_run=True)
    assert preview["needs_confirm"]
    first, second = preview["impact"]
    assert (first["name"], first["mode"], first["active"], first["moves"], first["needs_confirm"]) == (
        "Stova Suður", "automatic", True, 1, True)
    assert first["changes"] == [{"mode": "day", "word": "Daytime", "before": "Fully open — Global Daytime",
                                 "after": "Half shade — Global Daytime", "targets_change": True}]
    assert first["condition"] == "day" and first["before"] == "Fully open" and first["after"] == "Half shade"
    assert (second["name"], second["mode"], second["needs_confirm"]) == ("Køkur Suður", "observing", False)
    # Saving without the confirmation is refused, server side; a user who is not an administrator cannot save.
    assert (await api.error("modes/change", ops=change))["code"] == "confirm_required"
    refresh = await hass.auth.async_create_refresh_token(hass_read_only_user, "https://example.com/app")
    viewer = Api(await hass_ws_client(hass, access_token=hass.auth.async_create_access_token(refresh)))
    assert (await viewer.error("modes/change", ops=change, dry_run=True))["code"] == "unauthorized"
    assert r.mgr.config.modes["day"][0].preset_id == "p_open"
    # Policy "at the next change of condition": saved, nothing moves now.
    await api.ok("modes/change", ops=change, confirm=True, policy="next_change")
    await r.walk(30)
    assert covers.calls == [] and r.mgr.config.modes["day"][0].preset_id == "p_half_shade"
    status = r.auto(s)["status"]
    assert status["code"] == "deferred" and "applies at the next change of condition" in status["text"]
    assert re.search(r"changed at \d{2}:\d{2}:", status["text"]) and not re.search(r"\b(AM|PM)\b", status["text"])
    assert r.auto(s)["log"][1]["action"] == "config" and "Daytime: Fully open — Global Daytime → Half shade" in \
        r.auto(s)["log"][1]["text"]
    assert r.auto(kokur)["status"]["action"] == "would_move"   # Observing only explains
    # The condition changes (dusk: Evening privacy): the window follows its mapping normally again.
    r.lux(10)
    r.sun(-5.0, 230)
    await r.until("15:40", step=300)
    assert r.condition(s) == "evening_privacy" and r.auto(s)["defer"] is None
    assert (r.pos(STOVA_SHEER), r.pos(STOVA_BLACKOUT)) == (0, 60)
    # Policy "now": the controller applies it through its normal safety gates.
    calls = len(covers.calls)
    await api.ok("modes/change", ops=ops(a("evening_privacy", s, "p_blackout")), confirm=True, policy="now")
    await hass.async_block_till_done()
    assert (r.pos(STOVA_SHEER), r.pos(STOVA_BLACKOUT)) == (100, 0) and len(covers.calls) == calls + 2
    assert all(c.data["entity_id"] in (STOVA_SHEER, STOVA_BLACKOUT) for c in covers.calls)  # Køkur never moved


async def test_manual_hold_is_kept_when_a_mapping_changes(hass, pilot, house, covers, admin_ws, freezer,
                                                          faroe) -> None:
    set_cover(hass, STOVA_SHEER, 100)
    set_cover(hass, STOVA_BLACKOUT, 100)
    _e, r = await begin(hass, pilot, freezer, covers, admin_ws, "2026-11-19T12:30:00+00:00", lux=3000, elevation=6)
    s = await stova(r.api, house)
    await r.settle()
    await r.walk(11)
    await enable(r.api, s)
    await r.walk(3)
    set_cover(hass, STOVA_SHEER, 20, context=Context())       # moved by hand
    await r.walk(1)
    assert r.condition(s) == "manual_override"
    preview = await r.api.ok("modes/change", ops=ops(a("day", s, "p_full_shade")), dry_run=True)
    entry = preview["impact"][0]
    assert entry["hold"] and not entry["active"] and not preview["needs_confirm"]
    await r.api.ok("modes/change", ops=ops(a("day", s, "p_full_shade")))
    await r.walk(20)
    assert covers.calls == [] and r.pos(STOVA_SHEER) == 20 and r.condition(s) == "manual_override"
    await r.api.ok("resume", window_id=s)                     # Resume applies the new Daytime preset
    await hass.async_block_till_done()
    assert (r.pos(STOVA_SHEER), r.pos(STOVA_BLACKOUT)) == (0, 100)


async def test_observing_and_editing_never_move_anything(hass, pilot, house, covers, admin_ws, freezer, faroe,
                                                         service_calls) -> None:
    set_cover(hass, STOVA_SHEER, 0)
    set_cover(hass, STOVA_BLACKOUT, 0)
    _e, r = await begin(hass, pilot, freezer, covers, admin_ws, "2026-11-19T09:20:00+00:00", lux=500, elevation=3)
    s = await stova(r.api, house)
    await r.settle()
    await r.walk(11)
    assert r.condition(s) == "morning" and r.auto(s)["status"]["action"] == "would_move"
    assert "(Morning, Global Mode)" in r.auto(s)["status"]["text"]
    for preset in ("p_open", "p_full_shade", "p_blackout"):
        await r.api.ok("modes/change", ops=ops(a("morning", s, preset)), dry_run=True)
        await r.api.ok("modes/change", ops=ops(a("morning", s, preset)))
        await r.api.ok("model")
        await r.walk(1)
    await r.walk(130, step=300)
    assert covers.calls == [] and not [c for c in service_calls if c.domain == "cover"]


async def test_upgrade_from_0_6_3_keeps_windows_as_they_were_until_migrated(hass, pilot, house, covers, admin_ws,
                                                                            freezer, faroe, hass_storage) -> None:
    """A 0.6.3 store (minor 3): every window keeps its mode (none is enabled) and its mapping, which stays active
    (shown as window overrides) until the reviewed migration; Automatic windows decide and move exactly as
    before; the migration moves them to Global Modes with the same effect and without moving anything."""
    stova_dict = stova_window(mode="automatic", privacy=True, area_id=house.stova).as_dict()
    for key in ("cover_modes",):
        stova_dict.pop(key)
    for layer in stova_dict["layers"]:
        layer.pop("presets")
    stova_dict["conditions"] = {**default_conditions(stova_window().presets), "manual_override": None}
    kamar_dict = kamar_window(area_id=house.kamar).as_dict()
    kamar_dict.pop("cover_modes")
    kamar_dict["layers"][0].pop("presets")
    kamar_dict["conditions"] = {"morning": None, "day": "p_open", "night": "p_closed"}
    stored = {"windows": [stova_dict, kamar_dict], "settings": {}, "defaults": {"lux_entity": LUX, "min_interval": 0},
              "rooms": {}}
    hass_storage["smart_occupancy.curtains"] = {"version": 1, "minor_version": 3, "key": "smart_occupancy.curtains",
                                                "data": stored}
    set_cover(hass, STOVA_SHEER, 0)
    set_cover(hass, STOVA_BLACKOUT, 100)
    _e, r = await begin(hass, pilot, freezer, covers, admin_ws, "2026-11-19T13:00:00+00:00", lux=3000, elevation=5)
    cfg = r.mgr.config
    assert cfg.windows["w_stova"].mode == "automatic" and cfg.windows["w_kamar"].mode == "observing"
    assert cfg.modes == {} and cfg.windows["w_stova"].conditions["day"] == "p_open"
    model = await r.api.ok("model")
    assert model["global_modes"]["overrides"] == 9 and mode_of(model, "day")["count"] == 0
    assert {(o["name"], o["word"], o["label"]) for o in mode_of(model, "day")["others"]} == {
        ("Stova Suður", "Window override", "Fully open"), ("Kamar Erva", "Window override", "Fully open")}
    stova_model = next(w for w in model["windows"] if w["window_id"] == "w_stova")
    assert stova_model["effective"]["day"]["text"] == "Fully open — Window override"
    assert next(w for w in model["windows"] if w["window_id"] == "w_kamar")["effective"]["morning"]["state"] == \
        "nothing"                                             # a null mapping never picks a preset
    await r.settle()
    await r.walk(11)
    d = r.auto("w_stova")["decision"]
    assert (d["condition"], d["preset_id"], d["source"]) == ("day", "p_open", "window")   # as 0.6.3 decided
    assert covers.services() == [("set_cover_position", STOVA_SHEER, 100)]
    assert r.auto("w_kamar")["status"]["action"] in ("would_move", "at_target")           # Observing stays
    # The migration: reviewed first; the effective presets stay the same, so nothing would move.
    assert model["migration"]["pending"] and model["migration"]["required"] == 0
    preview = await r.api.ok("migration/change", dry_run=True)
    assert not preview["needs_confirm"] and preview["impact"] == [] and not preview["missing"]
    assert "Daytime: Stova Suður → Sheer 100 %, Blackout 100 % (Global Modes)" in preview["changes"]
    kinds = {x["id"]: x["kind"] for x in preview["items"]}
    assert kinds["morning:w_kamar"] == "morning"   # Kamar had no Morning preset: keeps its Daytime preset
    await r.api.ok("migration/change")
    await r.walk(5)
    cfg = r.mgr.config
    assert cfg.centralized and all(v is None for w in cfg.windows.values() for v in w.conditions.values())
    assert cfg.assignment("day", "w_stova", None).preset_id == "p_open"
    assert cfg.assignment("night", "w_kamar", None).preset_id == "p_closed"
    assert cfg.assignment("morning", "w_kamar", None).preset_id == "p_open"   # as before: Daytime from daylight
    assert len(covers.calls) == 1 and r.auto("w_stova")["decision"]["source"] == "global"


async def test_missing_presets_renames_and_replaced_covers(hass, pilot, house, covers, admin_ws, freezer, faroe,
                                                           hass_storage) -> None:
    window = stova_window(area_id=house.stova).as_dict()
    stored = {"windows": [window], "defaults": {"lux_entity": LUX},
              "modes": {"morning": [{"window_id": "w_stova", "layer_id": None, "preset_id": "p_gone"}],
                        "day": [{"window_id": "w_stova", "layer_id": "l_sheer", "preset_id": "c_half"},
                                {"window_id": "w_stova", "layer_id": None, "preset_id": "p_open"},  # overlaps: dropped
                                {"window_id": "w_nope", "layer_id": None, "preset_id": "p_open"}]}}  # no window
    hass_storage["smart_occupancy.curtains"] = {"version": 1, "minor_version": 4, "key": "smart_occupancy.curtains",
                                                "data": stored}
    _e, r = await begin(hass, pilot, freezer, covers, admin_ws, "2026-11-19T13:00:00+00:00", lux=3000, elevation=5)
    api = r.api
    cfg = r.mgr.config
    assert cfg.modes["day"] == (Assignment("w_stova", "l_sheer", "c_half"),)
    # A missing preset is reported and acts as No action: it is never re-pointed to another preset.
    morning = modes.effective(cfg, cfg.windows["w_stova"], "morning")
    assert morning.state == "no_action" and morning.problems
    model = await api.ok("model")
    row = mode_of(model, "morning")["rows"][0]
    assert row["missing"] and any("no longer exists" in t for t in mode_of(model, "morning")["warnings"])
    review = await api.ok("auto/preview", window_id="w_stova")
    assert any("p_gone no longer exists" in t for t in review["warnings"])
    # Renaming the window, a preset and a cover entity keeps every reference (stable ids).
    await api.ok("modes/change", ops=ops(a("morning", "w_stova", "p_half_shade")))
    await api.ok("window/update", window_id="w_stova", name="Stova Suður (stór)")
    await api.ok("preset/save", window_id="w_stova", preset_id="p_half_shade", name="Hálvt skugga")
    await api.ok("cover_preset/save", window_id="w_stova", layer_id="l_sheer", preset_id="c_half", name="Hálvt",
                 position=40)
    assert r.mgr.rename_entity(STOVA_SHEER, "cover.stova_ljos_nytt")
    w = r.mgr.config.windows["w_stova"]
    assert w.layer("l_sheer").entity_id == "cover.stova_ljos_nytt"
    eff = modes.effective(r.mgr.config, w, "morning")
    assert (eff.preset.name, dict(eff.preset.positions)) == ("Hálvt skugga", {"l_sheer": 50, "l_dark": 100})
    day = modes.effective(r.mgr.config, w, "day")
    assert day.preset.name == "Sheer: Hálvt" and day.preset.positions["l_sheer"] == 40
    # Replacing the cover entity of a layer (same layer id) keeps its presets and assignments.
    set_cover(hass, KOKUR_SHEER, 0)
    await api.ok("window/update", window_id="w_stova", layers=[
        {"layer_id": "l_sheer", "entity_id": KOKUR_SHEER, "kind": "sheer"},
        {"layer_id": "l_dark", "entity_id": STOVA_BLACKOUT, "kind": "blackout"}])
    w = r.mgr.config.windows["w_stova"]
    assert w.layer("l_sheer").entity_id == KOKUR_SHEER and w.layer("l_sheer").preset("c_half").position == 40
    assert r.mgr.config.modes["day"] == (Assignment("w_stova", "l_sheer", "c_half"),)
    # A removed cover entity is reported on the Global Modes page.
    hass.states.async_remove(STOVA_BLACKOUT)
    await hass.async_block_till_done()
    model = await api.ok("model")
    assert any(f"{STOVA_BLACKOUT} does not exist" in t for t in mode_of(model, "morning")["warnings"])
    # Deleting presets removes the assignments that used them.
    await api.ok("preset/delete", window_id="w_stova", preset_id="p_half_shade")
    await api.ok("cover_preset/delete", window_id="w_stova", layer_id="l_sheer", preset_id="c_half")
    cfg = r.mgr.config
    assert "morning" not in cfg.modes and "day" not in cfg.modes and cfg.windows["w_stova"].cover_modes == {}
    assert covers.calls == []


async def test_cover_presets_are_adjustable_and_validated(hass, pilot, house, covers, admin_ws, freezer,
                                                          faroe) -> None:
    _e, r = await begin(hass, pilot, freezer, covers, admin_ws, "2026-11-19T13:00:00+00:00", lux=3000, elevation=5)
    api = r.api
    s = await stova(api, house, assign=False)
    sheer = r.mgr.config.windows[s].layers[0].layer_id
    await api.ok("modes/change", ops=ops(a("morning", s, "c_half", sheer)))
    await api.ok("cover_preset/save", window_id=s, layer_id=sheer, preset_id="c_half", position=35)
    await api.ok("cover_preset/save", window_id=s, layer_id=sheer, preset_id="c_half", name="Mostly closed")
    new = (await api.ok("cover_preset/save", window_id=s, layer_id=sheer, name="TV", position=15))["preset_id"]
    layer = r.mgr.config.windows[s].layer(sheer)
    assert [(p.preset_id, p.name, p.position) for p in layer.presets] == [
        ("c_open", "Fully open", 100), ("c_half", "Mostly closed", 35), ("c_closed", "Fully closed", 0),
        (new, "TV", 15)]
    eff = modes.effective(r.mgr.config, r.mgr.config.windows[s], "morning")
    assert eff.preset.positions[sheer] == 35                   # the assignment follows the edited preset
    for bad in (101, -1, 50.5, True):
        err = await api.error("cover_preset/save", window_id=s, layer_id=sheer, preset_id=new, position=bad)
        assert err["code"] == "invalid_position", bad
    err = await api.error("cover_preset/save", window_id=s, layer_id=sheer, name="tv", position=10)
    assert err["code"] == "duplicate_name"
    assert (await api.error("cover_preset/save", window_id=s, layer_id="l_x", name="X", position=1))["code"] == \
        "unknown_layer"
    model = await api.ok("model")
    win = next(w for w in model["windows"] if w["window_id"] == s)
    assert [p["name"] for p in win["layers"][0]["presets"]] == ["Fully open", "Mostly closed", "Fully closed", "TV"]
    assert covers.calls == []


async def test_matrix_and_restart_persistence(hass, pilot, house, covers, admin_ws, freezer, faroe,
                                             hass_storage) -> None:
    set_cover(hass, STOVA_SHEER, 100)
    set_cover(hass, STOVA_BLACKOUT, 100)
    entry, r = await begin(hass, pilot, freezer, covers, admin_ws, "2026-11-19T13:00:00+00:00", lux=3000,
                           elevation=5)
    api = r.api
    s = await stova(api, house)
    sheer = r.mgr.config.windows[s].layers[0].layer_id
    await api.ok("modes/change", ops=ops(a("sleep", s, "c_open", sheer, replace=True)))
    # 0.6.6: no room, window or cover overrides: refused, with a pointer to Global Modes.
    for op in ({"op": "set", "level": "room", "mode": "night", "area_id": house.stova, "value": NO_ACTION},
               {"op": "set", "level": "cover", "mode": "sleep", "window_id": s, "layer_id": sheer, "value": "c_open"},
               {"op": "set", "level": "window", "mode": "lamp", "window_id": s, "value": NO_ACTION},
               {"op": "adopt", "window_id": s}):
        assert (await api.error("modes/change", ops=ops(op)))["code"] == "managed_globally"
    model = await api.ok("model")
    rows = {(x["kind"], x.get("window_id"), x.get("layer_id")): x for x in model["global_modes"]["matrix"]}
    window_row = rows[("window", s, None)]
    assert (window_row["cells"]["day"]["label"], window_row["cells"]["day"]["source"]) == ("Fully open", "global")
    assert window_row["cells"]["sleep"]["label"] == "Per cover"
    cover_row = rows[("cover", s, sheer)]
    assert (cover_row["cells"]["sleep"]["label"], cover_row["cells"]["sleep"]["source"]) == ("Fully open", "global")
    assert cover_row["cells"]["day"]["label"] == "100 %" and cover_row["cells"]["day"]["inherited"]
    # Defer a change, then restart: configuration and the deferred application survive; nothing is replayed.
    await r.settle()
    await r.walk(11)
    await enable(api, s)
    await r.walk(2)
    assert r.condition(s) == "day"
    await api.ok("modes/change", ops=ops(a("day", s, "p_full_shade")), confirm=True, policy="next_change")
    await r.walk(1)
    before = r.mgr.config
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    r.mgr = entry.runtime_data.curtains
    assert r.mgr.config.as_dict() == before.as_dict()
    assert r.mgr.auto.runtimes[s].defer["condition"] == "day"
    await r.walk(13)                                          # sensors settle: Waiting, then Daytime again
    assert r.condition(s) == "day"
    assert covers.calls == [] and r.auto(s)["status"]["code"] == "deferred"
    saved = hass_storage["smart_occupancy.curtains"]
    assert saved["data"]["centralized"] is True and saved["data"]["room_modes"] == {}
    assert saved["data"]["modes"]["day"] == [{"window_id": s, "layer_id": None, "preset_id": "p_full_shade"}]


async def test_occupancy_lighting_and_forecast_are_untouched(hass, pilot, house, covers, admin_ws, freezer, faroe,
                                                             service_calls) -> None:
    """Global Modes only configure curtains: no light, switch or helper is touched and no lighting mood is read."""
    _e, r = await begin(hass, pilot, freezer, covers, admin_ws, "2026-11-19T13:00:00+00:00", lux=3000, elevation=5)
    s = await stova(r.api, house)
    await r.api.ok("modes/change", ops=ops(a("day", s, "p_half_shade")))
    await r.walk(5)
    assert all(c.domain == "cover" for c in service_calls)
    source = (modes.__file__, logic.__file__)
    for path in source:
        text = open(path, encoding="utf-8").read()
        assert "mood" not in text.lower().replace("lighting mood", "")


def test_24_hour_texts_in_new_messages() -> None:
    """Every new time in a text is HH:mm (the card formats ISO times with its shared 24-hour helper)."""
    w = stova_window()
    cfg = config(w, modes=assign(night=[("w_stova", None, "p_blackout")]))
    d = _decide(cfg, w, _ev("2026-11-19T22:45", lux=0.0, daylight=False, elevation=-30.0))
    assert d.text == "Night protection active until 06:30"
    assert not re.search(r"\b(AM|PM|am|pm)\b", d.text)
