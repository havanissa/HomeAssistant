"""Websocket API of the occupancy forecast (0.5.0): the room's forecast details and its settings.

Reads (any user):

* ``smart_occupancy/forecast/room`` - everything the Presence tab shows about
  a room's forecast: the live forecast with its explanation, learning status
  and history coverage, hourly / weekly patterns from the learned daily
  summaries, movement statistics, prequential accuracy against the schedule
  and persistence baselines, settings (room and global) and the forecast log.

Writes (admin only; the forecast settings store, no reload):

* ``smart_occupancy/forecast/settings`` - global defaults (``area_id`` null)
  or room overrides (a value; ``null`` = use the global default;
  ``reset_all`` for a room).
* ``smart_occupancy/forecast/connection`` - add, change or remove a room
  connection, or go back to the suggested ones.
* ``smart_occupancy/forecast/learn`` - read new history now.
* ``smart_occupancy/forecast/rebuild`` - forget what was learned and learn
  again from the Recorder.

Nothing here controls a device: forecasts are observation only.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN
from .forecast import ForecastManager, subject
from .forecast_model import (
    ADJACENT_CONTEXT,
    CONFIDENCE_LEVELS,
    DAY_PARTS,
    HORIZONS,
    HOURS,
    accuracy,
)
from .forecast_settings import (
    CONFIDENCE_THRESHOLDS,
    CONNECTION_KINDS,
    CONNECTION_WORDS,
    GLOBAL_NUMBERS,
    MAX_CONTEXTS,
    Connection,
    ForecastDefaults,
    RoomForecastSettings,
)
from .occupancy import friendly_name

_CONTEXT_DOMAINS = ("person", "input_boolean", "binary_sensor", "group", "device_tracker", "schedule", "switch")
_MAX_CANDIDATES = 400
# Learned movements this frequent between rooms that are not connected are offered as a connection.
SUGGEST_MIN_MOVES = 5


def _manager(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> ForecastManager | None:
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, "Smart Occupancy & Lighting is not loaded")
        return None
    return entries[0].runtime_data.forecast


def _r(value: float | None, digits: int = 3) -> float | None:
    return None if value is None else round(value, digits)


def context_candidates(hass: HomeAssistant) -> list[dict[str, Any]]:
    ent_reg = er.async_get(hass)
    out = []
    for state in hass.states.async_all(_CONTEXT_DOMAINS):
        entry = ent_reg.async_get(state.entity_id)
        if entry and entry.platform == DOMAIN:
            continue
        out.append({"entity_id": state.entity_id, "name": state.name,
                    "workday": bool(entry and entry.platform == "workday")})
    out.sort(key=lambda c: (not c["workday"], not c["entity_id"].startswith("person."), c["name"].casefold()))
    return out[:_MAX_CANDIDATES]


def _patterns(m: ForecastManager, area_id: str) -> dict[str, Any]:
    """Hourly and weekly occupancy from the learned days (real data; None where nothing was observed)."""
    occ = [[0.0] * HOURS for _ in range(7)]
    obs = [[0.0] * HOURS for _ in range(7)]
    entries = [0.0] * HOURS
    days_with = 0
    for day in m.days.values():
        room = day.rooms.get(area_id)
        if room is None or not room.observed_seconds:
            continue
        days_with += 1
        o, v, e = room.occ, room.vac, room.ent
        for h in range(HOURS):
            occ[day.dow][h] += o[h]
            obs[day.dow][h] += o[h] + v[h]
            entries[h] += e[h]
    weekly = [[_r(occ[d][h] / obs[d][h]) if obs[d][h] >= 1800 else None for h in range(HOURS)] for d in range(7)]
    observed_hours = [[round(obs[d][h] / 3600, 1) for h in range(HOURS)] for d in range(7)]
    raw_hour = []
    for h in range(HOURS):
        o = sum(occ[d][h] for d in range(7))
        x = sum(obs[d][h] for d in range(7))
        raw_hour.append(_r(o / x) if x >= 1800 else None)
    model = m.model.rooms.get(area_id) if m.model else None
    return {
        "days": days_with,
        "weekly": weekly, "weekly_observed_hours": observed_hours,
        "hourly_observed": raw_hour,
        "hourly_entries": [_r(entries[h] / days_with, 2) if days_with else None for h in range(HOURS)],
        "hourly_learned": [_r(x) for x in model.occupancy.hour] if model else None,
        "hourly_workday": [_r(x) for x in model.occupancy.daytype[True]] if model else None,
        "hourly_day_off": [_r(x) for x in model.occupancy.daytype[False]] if model else None,
    }


def _movement(m: ForecastManager, area_id: str) -> dict[str, Any]:
    name = m.name
    if m.model is None:
        return {"next": [], "previous": [], "arrivals": 0}
    nxt = [{"area_id": p.target, "name": name(p.target), "probability": _r(p.probability),
            "count": round(p.count, 1), "median_delay": round(p.median_delay),
            "by_part": {DAY_PARTS[part][2]: _r(p.by_part.get((part, True))) for part in range(len(DAY_PARTS))}}
           for p in m.model.outgoing(area_id)][:6]
    prev = [{"area_id": p.source, "name": name(p.source), "share": _r(share), "count": round(p.count, 1),
             "median_delay": round(p.median_delay)} for p, share in m.model.incoming(area_id)][:6]
    return {"next": nxt, "previous": prev, "arrivals": round(m.model.arrivals.get(area_id, 0.0), 1)}


def _connections(m: ForecastManager, area_id: str) -> dict[str, Any]:
    conns = m.connections()
    configured = m.settings.connections is not None
    mine = [c for c in conns if area_id in (c.a, c.b)]
    pairs = {c.pair for c in conns}
    suggestions: list[dict[str, Any]] = []
    if configured:  # otherwise the suggested connections are the ones in use
        for c in m.suggested_connections():
            if area_id in (c.a, c.b) and c.pair not in pairs:
                suggestions.append({**c.as_dict(), "reason": "configuration"})
    if m.model is not None:
        counts: dict[frozenset[str], float] = {}
        for (a, b), p in m.model.pairs.items():
            if area_id in (a, b) and a != b:
                counts[frozenset((a, b))] = counts.get(frozenset((a, b)), 0.0) + p.count
        for pair, n in sorted(counts.items(), key=lambda x: -x[1]):
            if n >= SUGGEST_MIN_MOVES and pair not in pairs and not any(s["a"] in pair and s["b"] in pair
                                                                         for s in suggestions):
                a, b = sorted(pair)
                suggestions.append({"a": a, "b": b, "kind": "door", "reason": "learned", "moves": round(n, 1)})
    def item(c: Connection) -> dict[str, Any]:
        other = c.b if c.a == area_id else c.a
        return {**c.as_dict(), "other": other, "other_name": m.name(other), "kind_name": CONNECTION_WORDS[c.kind]}
    return {
        "configured": configured,
        "items": [item(c) for c in mine],
        "suggestions": [{**s, "other": (o := s["b"] if s["a"] == area_id else s["a"]), "other_name": m.name(o)}
                        for s in suggestions][:6],
        "kinds": [{"key": k, "name": CONNECTION_WORDS[k]} for k in CONNECTION_KINDS],
        "rooms": [{"area_id": a, "name": m.name(a)} for a in sorted(m.coordinator.config.enabled_rooms, key=m.name)
                  if a != area_id],
    }


def _accuracy(m: ForecastManager, area_id: str, threshold: float, horizon: int) -> dict[str, Any]:
    by_horizon = {str(h): m.accuracy(area_id, h).as_dict(threshold) for h in HORIZONS}
    daily = []
    for key in sorted(m.days):
        room = m.days[key].rooms.get(area_id)
        ev = room.ev.get(horizon) if room else None
        if ev and ev[0]:
            daily.append({"date": key, "forecasts": int(ev[0]), "brier": _r(ev[1] / ev[0], 4),
                          "brier_schedule": _r(ev[2] / ev[0], 4), "brier_persistence": _r(ev[3] / ev[0], 4)})
    house = accuracy(m.days.values(), None, horizon).as_dict(threshold)
    return {"by_horizon": by_horizon, "daily": daily[-30:], "house": house, "horizon": horizon,
            "method": "Prequential evaluation: each completed day is forecast every 15 min with a model of the earlier days "
                      "only, and compared with the occupancy that followed"}


def _contexts(m: ForecastManager, area_id: str) -> list[dict[str, Any]]:
    model = m.model.rooms.get(area_id) if m.model else None
    auto = m.settings.defaults.contexts is None
    out = []
    for key in m.contexts() + [ADJACENT_CONTEXT]:
        f = model.contexts.get(key) if model else None
        state = m.hass.states.get(key) if key != ADJACENT_CONTEXT else None
        out.append({"key": key, "name": "A connected room is occupied" if key == ADJACENT_CONTEXT else m.name(key),
                    "automatic": auto or key == ADJACENT_CONTEXT, "state": state.state if state else None,
                    "factor": f.as_dict() if f else None})
    return out


def room_detail(hass: HomeAssistant, m: ForecastManager, area_id: str) -> dict[str, Any]:
    eff = m.settings.effective(area_id)
    rf = m.room(area_id)
    model = m.model.rooms.get(area_id) if m.model else None
    stats = model.stats if model else None
    g = m.settings.defaults
    workday = m.workday_entity()
    return {
        "area_id": area_id,
        "name": m.name(area_id),
        "observation_only": True,
        "now": rf.as_dict(m.name) if rf else None,
        "learning": {
            **m.status.as_dict(),
            "days_learned": len(m.days),
            "today_included": m.today is not None,
            "window_days": g.window_days,
            "room_days": stats.days if stats else 0,
            "room_weekdays": stats.weekdays if stats else 0,
            "room_first_date": stats.first_date if stats else None,
            "room_last_date": stats.last_date if stats else None,
            "room_observed_hours": round(stats.observed_hours, 1) if stats else 0,
            "room_entries": int(stats.entries) if stats else 0,
            "model_days": m.model.days if m.model else 0,
        },
        "patterns": _patterns(m, area_id),
        "movement": _movement(m, area_id),
        "connections": _connections(m, area_id),
        "accuracy": _accuracy(m, area_id, eff.probability_threshold / 100, eff.horizon_minutes),
        "contexts": _contexts(m, area_id),
        "settings": {
            "room": m.settings.room(area_id).as_dict(),
            "effective": {"enabled": eff.enabled, "horizon_minutes": eff.horizon_minutes,
                          "probability_threshold": eff.probability_threshold,
                          "confidence_threshold": eff.confidence_threshold},
            "overridden": sorted(eff.overridden),
            "global": g.as_dict(),
            "limits": {k: {"default": d, "min": lo, "max": hi} for k, (d, lo, hi) in GLOBAL_NUMBERS.items()},
            "horizons": list(HORIZONS),
            "confidence_options": list(CONFIDENCE_THRESHOLDS),
            "confidence_levels": list(CONFIDENCE_LEVELS),
            "automatic_contexts": [{"entity_id": e, "name": m.name(e)} for e in m.automatic_contexts()],
            "contexts": [{"entity_id": e, "name": m.name(e)} for e in m.contexts()],
            "workday_entity": workday, "workday_name": friendly_name(hass, workday) if workday else None,
            "workday_automatic": g.workday_entity is None,
            "max_contexts": MAX_CONTEXTS,
        },
        "log": m.coordinator.lighting.decisions.model(subject(area_id)),
        "house_log": m.coordinator.lighting.decisions.model("forecast:house"),
    }


# ---------------------------------------------------------------- commands
@websocket_api.websocket_command({vol.Required("type"): "smart_occupancy/forecast/room", vol.Required("area_id"): str})
@callback
def ws_forecast_room(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    if (m := _manager(hass, connection, msg)) is None:
        return
    if msg["area_id"] not in m.coordinator.config.enabled_rooms:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, f"Room {msg['area_id']} is not enabled")
        return
    detail = room_detail(hass, m, msg["area_id"])
    detail["context_candidates"] = context_candidates(hass)
    connection.send_result(msg["id"], detail)


_GLOBAL_SCHEMA = vol.Schema({
    vol.Optional("enabled"): bool,
    **{vol.Optional(k): vol.All(vol.Coerce(int), vol.Range(min=lo, max=hi)) for k, (_d, lo, hi) in GLOBAL_NUMBERS.items()},
    vol.Optional("horizon_minutes"): vol.All(vol.Coerce(int), vol.In(HORIZONS)),
    vol.Optional("confidence_threshold"): vol.In(CONFIDENCE_THRESHOLDS),
    vol.Optional("contexts"): vol.Any(None, vol.All([str], vol.Length(max=MAX_CONTEXTS))),
    vol.Optional("workday_entity"): vol.Any(None, str),
})
_ROOM_SCHEMA = vol.Schema({
    vol.Optional("enabled"): vol.Any(None, bool),
    vol.Optional("horizon_minutes"): vol.Any(None, vol.All(vol.Coerce(int), vol.In(HORIZONS))),
    vol.Optional("probability_threshold"): vol.Any(None, vol.All(vol.Coerce(int), vol.Range(min=5, max=99))),
    vol.Optional("confidence_threshold"): vol.Any(None, vol.In(CONFIDENCE_THRESHOLDS)),
})


@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/forecast/settings",
    vol.Optional("area_id"): vol.Any(None, str),
    vol.Optional("values", default={}): dict,
    vol.Optional("reset_all", default=False): bool,
})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_forecast_settings(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    if (m := _manager(hass, connection, msg)) is None:
        return
    area_id = msg.get("area_id")
    settings = m.settings
    try:
        if area_id is None:
            values = _GLOBAL_SCHEMA(msg["values"])
            if "contexts" in values and values["contexts"] is not None:
                bad = [e for e in values["contexts"] if hass.states.get(e) is None]
                if bad:
                    raise vol.Invalid(f"Unknown entity: {', '.join(bad)}")
            if values.get("workday_entity") and hass.states.get(values["workday_entity"]) is None:
                raise vol.Invalid(f"Unknown entity: {values['workday_entity']}")
            data = {**settings.defaults.as_dict(), **values}
            settings = replace(settings, defaults=ForecastDefaults.from_dict(data))
        else:
            if area_id not in m.coordinator.config.enabled_rooms:
                connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, f"Room {area_id} is not enabled")
                return
            if msg["reset_all"]:
                room = RoomForecastSettings()
            else:
                values = _ROOM_SCHEMA(msg["values"])
                room = RoomForecastSettings.from_dict({**settings.room(area_id).as_dict(), **values})
            settings = settings.with_room(area_id, room)
    except vol.Invalid as err:
        connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT, str(err))
        return
    await m.async_set_settings(settings)
    connection.send_result(msg["id"], {"settings": settings.as_dict()})


@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/forecast/connection",
    vol.Required("action"): vol.In(["add", "remove", "reset"]),
    vol.Optional("a"): str,
    vol.Optional("b"): str,
    vol.Optional("kind", default="door"): vol.In(CONNECTION_KINDS),
})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_forecast_connection(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Room connections. The first change starts from the suggested ones; ``reset`` goes back to them."""
    if (m := _manager(hass, connection, msg)) is None:
        return
    rooms = m.coordinator.config.enabled_rooms
    if msg["action"] == "reset":
        await m.async_set_settings(replace(m.settings, connections=None))
        connection.send_result(msg["id"], {"connections": [c.as_dict() for c in m.connections()]})
        return
    a, b = msg.get("a"), msg.get("b")
    if not a or not b or a == b or a not in rooms or b not in rooms:
        connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT, "Two different enabled rooms are needed")
        return
    current = {c.pair: c for c in m.connections()}
    pair = frozenset((a, b))
    if msg["action"] == "add":
        current[pair] = Connection.make(a, b, msg["kind"])
    else:
        current.pop(pair, None)
    await m.async_set_settings(replace(m.settings, connections=tuple(current.values())))
    connection.send_result(msg["id"], {"connections": [c.as_dict() for c in m.connections()]})


@websocket_api.websocket_command({vol.Required("type"): "smart_occupancy/forecast/learn"})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_forecast_learn(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    if (m := _manager(hass, connection, msg)) is None:
        return
    await m.async_learn()
    connection.send_result(msg["id"], {"status": m.status.as_dict()})


@websocket_api.websocket_command({vol.Required("type"): "smart_occupancy/forecast/rebuild"})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_forecast_rebuild(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    if (m := _manager(hass, connection, msg)) is None:
        return
    await m.async_rebuild()
    connection.send_result(msg["id"], {"status": m.status.as_dict(), "days_learned": len(m.days)})


FORECAST_HANDLERS = (ws_forecast_room, ws_forecast_settings, ws_forecast_connection, ws_forecast_learn,
                     ws_forecast_rebuild)
