"""Learning and prediction for the configured rooms: from the observation store to a published snapshot.

``Engine`` turns the configuration into the learner's ``RoomSpec`` per room, trains each room's model in a worker
thread (one model per room - a room belongs to at most one thermal zone, so overlapping models cannot exist),
keeps the models in ``ModelStore`` and computes the predictions from the newest observations, the weather
forecast and the comfort schedule. It never sends anything to a device; its output is data.

Thermal zones. A connection between two rooms makes each the other's candidate neighbour: the learner then
tests whether exchange with it is measurable (``Σ c_j (T_j − T)``) and keeps the term only if it is. With a
door sensor on the connection, the neighbour's influence is zero while the door is closed (the closed door is a
barrier). Without measurable exchange the rooms are modelled independently - the conservative assumption - and
the room says so. Every room keeps its own temperature, model, target and prediction.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
import hashlib
import json
import logging
from pathlib import Path
import time
from typing import Any

from . import config as C, forecast as F, thermal_model as tm
from .fit import learn
from .history import Acquisition
from .model_store import ModelStore
from .prep import GRID_S, RoomGrid, RoomSpec, StoreView, WindowSpec, heating_of, prepare_room
from .solar import to_compass
from .units import HEATING_ACTIONS, as_float, climate_unit, to_celsius

_LOGGER = logging.getLogger(__name__)

TRAIN_EVERY_S = 6 * 3600.0
STALE_TEMPERATURE_S = 3 * 3600.0   # a room reading older than this is not "now"
OCCUPANCY_MIN_DAYS = 14
STATUS_TEXT = {
    "not_configured": "No temperature source is configured",
    "collecting": "Collecting observations",
    "waiting": "Waiting for the first training",
    "training": "Learning",
    "insufficient": "Not enough usable data yet",
    "preliminary": "Preliminary model (less history than the minimum)",
    "low": "Model learned, low confidence",
    "moderate": "Model learned, moderate confidence",
    "high": "Model learned, high confidence",
    "unidentifiable": "No reliable model can be identified from the data",
    "disabled": "Room disabled",
}


def utcnow() -> float:
    return time.time()


def fingerprint(config: dict[str, Any], area_id: str) -> str:
    """What a room's model depends on. A change of anything else (targets, names, occupancy) keeps the model."""
    room = config["rooms"][area_id]
    neighbours = sorted((c["other"], c["strength"], c["door"]) for c in C.connections_of(config, area_id))
    data = {"temperature": room["temperature"], "secondary": room["secondary"], "thermostats": room["thermostats"],
            "outdoor": C.outdoor_of(config, area_id), "openings": [o["entity_id"] for o in room["openings"]],
            "windows": [{k: w[k] for k in ("orientation", "area_m2", "shading", "obstructions", "covers")}
                        for w in room["windows"]],
            "sunlight": room["sunlight"], "weather": config["settings"]["weather"], "neighbours": neighbours}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:16]


def window_specs(room: dict[str, Any]) -> list[WindowSpec]:
    out = []
    for w in room["windows"]:
        if w["orientation"] is None:
            continue
        out.append(WindowSpec(
            window_id=w["id"], bearing=to_compass(w["orientation"]),
            obstructions=[(to_compass(o["from"]), to_compass(o["to"]), o["elevation"]) for o in w["obstructions"]],
            layers=[(c, "blackout") for c in w["covers"]], area_m2=w["area_m2"], shading=w["shading"]))
    return out


def room_spec(config: dict[str, Any], area_id: str, location: tuple[float, float] | None) -> RoomSpec:
    room = config["rooms"][area_id]
    primary = room["temperature"]
    secondary = list(room["secondary"])
    if primary is None and not secondary:
        # Only the thermostats measure: their own reading stands in for the room (said so in the room view).
        primary = None
    thermostats = [*room["thermostats"], *(e for e in room["running"] if e.startswith("climate."))]
    weather = config["settings"]["weather"]
    return RoomSpec(area_id=area_id, primary=primary, secondary=secondary, thermostats=list(dict.fromkeys(thermostats)),
                    openings=[o["entity_id"] for o in room["openings"]], outdoor=C.outdoor_of(config, area_id),
                    stale_s=STALE_TEMPERATURE_S, windows=window_specs(room),
                    neighbours=[c["other"] for c in C.connections_of(config, area_id)],
                    own_thermostats=list(room["thermostats"]), location=location,
                    weather=weather if weather != C.outdoor_of(config, area_id) else None,
                    sun_sensors=list(room["sunlight"]))


class Engine:
    def __init__(self, data_dir: Path | None, acquisition: Acquisition) -> None:
        self.acq = acquisition
        self.models = ModelStore(data_dir / "models" if data_dir else None)
        self.training: dict[str, dict[str, Any]] = {}     # area -> {state, started_at, finished_at, error, ...}
        self.predictions: dict[str, dict[str, Any]] = {}
        self.window_effects: dict[str, dict[str, Any]] = {}
        self.occupancy: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[Any] | None = None

    def load(self) -> None:
        self.models.load()
        for area, doc in self.models.docs.items():
            extra = doc.get("spc") or {}
            if isinstance(extra.get("window_loss"), dict):
                self.window_effects[area] = extra["window_loss"]

    # ------------------------------------------------------------- context
    @staticmethod
    def location(ha_config: dict[str, Any]) -> tuple[float, float] | None:
        lat, lon = as_float(ha_config.get("latitude")), as_float(ha_config.get("longitude"))
        return (lat, lon) if lat is not None and lon is not None else None

    @staticmethod
    def tz(ha_config: dict[str, Any]) -> Any:
        name = ha_config.get("time_zone")
        if name:
            try:
                from zoneinfo import ZoneInfo  # noqa: PLC0415
                return ZoneInfo(name)
            except Exception:  # noqa: BLE001 - no tz database: UTC, and the diagnostics say so
                _LOGGER.warning("Time zone %s is not known to the App; UTC is used", name)
        return UTC

    def view(self) -> StoreView:
        s = self.acq.store
        return StoreView(s.dir, {k: dict(v) for k, v in s.coverage.items()}, {k: [list(g) for g in v] for k, v in s.gaps.items()},
                         {k: list(v) for k, v in s.aliases.items()})

    # ------------------------------------------------------------ training
    def prepare(self, config: dict[str, Any], area_id: str, ha_config: dict[str, Any], *, days: int | None = None,
                now: float | None = None) -> RoomGrid:
        """The room's prepared grid over the learning period, with its zone neighbours (doors as barriers)."""
        now = now if now is not None else utcnow()
        days = days or config["settings"]["history_days"]
        start = (now - days * 86400.0) // GRID_S * GRID_S
        end = now // GRID_S * GRID_S
        location, tz = self.location(ha_config), ha_config.get("time_zone")
        view = self.view()
        spec = room_spec(config, area_id, location)
        grid = prepare_room(spec, view, start, end, tz=tz)
        neighbours: dict[str, list[float | None]] = {}
        for conn in C.connections_of(config, area_id):
            other = conn["other"]
            other_spec = room_spec(config, other, location)
            other_spec = replace(other_spec, thermostats=[], own_thermostats=[], openings=[], windows=[])
            series = list(prepare_room(other_spec, view, start, end, tz=tz).temp)
            if conn["door"]:
                door = prepare_room(RoomSpec(other, None, [], [], [conn["door"]], None), view, start, end, tz=tz).window
                series = [grid.temp[i] if door[i] in ("closed", "settling") and grid.temp[i] is not None else v
                          for i, v in enumerate(series)]
            neighbours[other] = series
        # Strongest connections first: the learner tests at most three neighbours, in this order.
        order = {c["other"]: -c["factor"] for c in C.connections_of(config, area_id)}
        grid.neighbours = dict(sorted(neighbours.items(), key=lambda kv: order.get(kv[0], 0)))
        return grid

    def train(self, config: dict[str, Any], area_id: str, ha_config: dict[str, Any], *, now: float | None = None
              ) -> dict[str, Any]:
        """Fit one room (blocking: run it in a thread). Returns a short result for the training status."""
        now = now if now is not None else utcnow()
        grid = self.prepare(config, area_id, ha_config, now=now)
        result = learn(grid, min_history_days=config["settings"]["min_history_days"], now=now)
        if result.doc is None:
            return {"result": result.diagnostic.get("status", "insufficient"), "reason": result.diagnostic.get("reason"),
                    "evidence": result.diagnostic.get("evidence")}
        doc = result.doc
        doc["fingerprint_hash"] = fingerprint(config, area_id)
        doc["app_version"] = config.get("_app_version", "")
        model = tm.load_model(doc, allow_preliminary=True)
        extra: dict[str, Any] = {"solar_basis": grid.solar_basis}
        if isinstance(model, tm.ThermalModel):
            extra["window_loss"] = F.window_loss(model, grid)
            extra["characteristics"] = F.characteristics(model)
            self.window_effects[area_id] = extra["window_loss"]
        doc["spc"] = extra
        self.models.save(doc)
        return {"result": doc["status"], "reason": None, "confidence": doc["confidence"]["level"]}

    async def train_all(self, config: dict[str, Any], ha_config: dict[str, Any], *, force: bool = False,
                        only: set[str] | None = None) -> None:
        async with self._lock:
            now = utcnow()
            for area_id, room in config["rooms"].items():
                if not room["enabled"] or (only is not None and area_id not in only):
                    continue
                state = self.training.get(area_id) or {}
                doc = self.models.docs.get(area_id)
                fresh = doc is not None and doc.get("fingerprint_hash") == fingerprint(config, area_id)
                if not force and fresh and state.get("finished_at") and now - state["finished_at"] < TRAIN_EVERY_S:
                    continue
                self.training[area_id] = {**state, "state": "training", "started_at": now}
                try:
                    outcome = await asyncio.to_thread(self.train, config, area_id, ha_config, now=now)
                except Exception as err:  # noqa: BLE001 - one room must not stop the others
                    _LOGGER.exception("Training %s failed", area_id)
                    outcome = {"result": "error", "reason": f"{type(err).__name__}: {err}"}
                self.training[area_id] = {"state": "done", "started_at": now, "finished_at": utcnow(), **outcome}
            for area_id in list(self.training):
                if area_id not in config["rooms"]:
                    self.training.pop(area_id, None)

    # ----------------------------------------------------------- prediction
    def current(self, config: dict[str, Any], area_id: str, states: dict[str, Any], ha_unit: str, now: float
                ) -> dict[str, Any]:
        """The room's readings now: temperature (and where it comes from), heating, windows, covers."""
        room = config["rooms"][area_id]

        def fresh_temp(entity_id: str | None) -> float | None:
            """A usable reading now: numeric, in a temperature unit, and reported within STALE_TEMPERATURE_S
            (``last_reported`` moves when a device repeats an unchanged value; without it ``last_updated``)."""
            if not entity_id:
                return None
            state = states.get(entity_id) or {}
            value = to_celsius(state.get("state"), (state.get("attributes") or {}).get("unit_of_measurement"))
            seen = _stamp(state.get("last_reported")) or _stamp(state.get("last_updated"))
            if value is None or seen is None:
                return None
            if now - seen > STALE_TEMPERATURE_S and entity_id not in self.acq._last_live:  # noqa: SLF001
                return None
            return value

        temp, source = None, None
        for entity_id in [room["temperature"], *room["secondary"]]:
            temp = fresh_temp(entity_id)
            if temp is not None:
                source = entity_id
                break
        thermostat_temp = None
        heating = []
        setpoints = []
        for entity_id in room["thermostats"]:
            state = states.get(entity_id) or {}
            attrs = state.get("attributes") or {}
            unit = climate_unit(attrs, ha_unit)
            cur = to_celsius(attrs.get("current_temperature"), unit)
            if cur is not None and thermostat_temp is None:
                thermostat_temp = cur
            action = attrs.get("hvac_action")
            if action is not None:
                heating.append(1.0 if action in HEATING_ACTIONS else 0.0)
            target = to_celsius(attrs.get("temperature"), unit)
            if target is not None and state.get("state") not in ("off", "unavailable"):
                setpoints.append(target)
        if temp is None and thermostat_temp is not None and not room["temperature"] and not room["secondary"]:
            temp, source = thermostat_temp, f"{room['thermostats'][0]} (its own measurement)"
        openings = [(states.get(o["entity_id"]) or {}).get("state") for o in room["openings"]]
        window_open = True if "on" in openings else None if any(v not in ("on", "off") for v in openings) else False
        covers = {}
        for w in room["windows"]:
            for cover in w["covers"]:
                attrs = (states.get(cover) or {}).get("attributes") or {}
                pos = as_float(attrs.get("current_position"))
                if pos is None:
                    pos = {"open": 100.0, "closed": 0.0}.get((states.get(cover) or {}).get("state"))
                covers[cover] = pos
        problems = []
        if temp is not None and thermostat_temp is not None and abs(temp - thermostat_temp) > 3.0 and source != \
                f"{room['thermostats'][0] if room['thermostats'] else ''} (its own measurement)":
            problems.append(f"The room sensor ({temp:.1f} °C) and the thermostat's own measurement "
                            f"({thermostat_temp:.1f} °C) differ by more than 3 °C; the room sensor is used")
        if temp is None and (room["temperature"] or room["secondary"]):
            problems.append("The room temperature sensor has no current value (unavailable, not a number or not "
                            "reported for more than 3 hours)")
        return {"temp_c": temp, "source": source, "thermostat_c": thermostat_temp, "problems": problems,
                "heating": sum(heating) / len(heating) if heating else None, "setpoint_c": max(setpoints) if setpoints else None,
                "window_open": window_open, "covers": covers}

    def away(self, room: dict[str, Any], states: dict[str, Any]) -> bool | None:
        entity_id = room["comfort"]["away_entity"]
        if not entity_id:
            return None
        state = (states.get(entity_id) or {}).get("state")
        if state in (None, "unavailable", "unknown"):
            return None
        if entity_id.split(".")[0] in ("person", "device_tracker"):
            return state != "home"
        return state == "on"

    def predict_room(self, config: dict[str, Any], area_id: str, states: dict[str, Any], ha_config: dict[str, Any],
                     *, now: float | None = None) -> dict[str, Any]:
        now = now if now is not None else utcnow()
        room = config["rooms"][area_id]
        ha_unit = ((ha_config.get("unit_system") or {}).get("temperature")) or "°C"
        cur = self.current(config, area_id, states, ha_unit, now)
        doc = self.models.docs.get(area_id)
        status = self.model_status(config, area_id)
        base = {"area_id": area_id, "name": room["name"], "now": cur, "model_status": status,
                "computed_at": datetime.fromtimestamp(now, UTC).isoformat(timespec="seconds")}
        model = tm.load_model(doc, allow_preliminary=True) if doc else tm.Unavailable("no_model", STATUS_TEXT[status["state"]])
        tz = self.tz(ha_config)
        steps = F.horizon_steps(config["settings"]["horizon_hours"])
        times = [now + i * F.STEP_MIN * 60.0 for i in range(steps + 1)]
        targets, why = F.comfort_targets(room["comfort"], times, tz, away=self.away(room, states),
                                         occupancy=self.occupancy.get(area_id, {}).get("profile")
                                         if room["occupancy_learning"] else None)
        base["target_now"] = targets[0]
        base["target_reason"] = why[0]
        if isinstance(model, tm.Unavailable):
            return {**base, "prediction": {"available": False, "code": model.code, "reason": model.text}}
        outdoor_entity = C.outdoor_of(config, area_id)
        outdoor = None
        if outdoor_entity:
            state = states.get(outdoor_entity) or {}
            attrs = state.get("attributes") or {}
            outdoor = to_celsius(attrs.get("temperature"), attrs.get("temperature_unit") or ha_unit) \
                if outdoor_entity.startswith("weather.") else to_celsius(state.get("state"), attrs.get("unit_of_measurement"))
        weather = config["settings"]["weather"]
        cloud = as_float(((states.get(weather) or {}).get("attributes") or {}).get("cloud_coverage")) if weather else None
        neighbours, barriers = {}, {}
        for conn in C.connections_of(config, area_id):
            other_cur = self.current(config, conn["other"], states, ha_unit, now)
            neighbours[conn["other"]] = other_cur["temp_c"]
            if conn["door"]:
                barriers[conn["other"]] = (states.get(conn["door"]) or {}).get("state") == "off"
        history = [cur["heating"]] * 24 if cur["heating"] is not None else []
        ctx = F.Context(now=now, tz=tz, temp_c=cur["temp_c"], temp_source=cur["source"], outdoor_c=outdoor,
                        outdoor_forecast=self.acq.current_forecast() if weather else [], cloud_now=cloud,
                        heating_now=cur["heating"], heating_history=history, setpoint_c=cur["setpoint_c"],
                        location=self.location(ha_config),
                        windows=window_specs(room), covers=cur["covers"], window_open=cur["window_open"],
                        neighbours=neighbours, barriers=barriers, targets=targets, target_reasons=why)
        solar_basis = ((doc or {}).get("spc") or {}).get("solar_basis", "geometry")
        result = F.predict(model, ctx, horizon_h=config["settings"]["horizon_hours"], solar_basis=solar_basis,
                           recommend_from=config["settings"]["recommend_from"])
        if doc and doc.get("fingerprint_hash") != fingerprint(config, area_id):
            result.setdefault("warnings", []).append("The room's inputs changed after this model was learned; it is "
                                                     "being retrained (the old model is used until then)")
        return {**base, "prediction": result, "context": {"window_open": cur["window_open"], "away": self.away(room, states)}}

    def model_status(self, config: dict[str, Any], area_id: str) -> dict[str, Any]:
        room = config["rooms"][area_id]
        if not room["enabled"]:
            state = "disabled"
        elif not (room["temperature"] or room["secondary"] or room["thermostats"]):
            state = "not_configured"
        else:
            doc = self.models.docs.get(area_id)
            training = self.training.get(area_id) or {}
            if training.get("state") == "training" and doc is None:
                state = "training"
            elif doc is None:
                state = training.get("result") if training.get("result") in ("collecting", "insufficient") else \
                    "waiting" if not training else "insufficient"
            elif doc.get("status") == "unidentifiable":
                state = "unidentifiable"
            elif doc.get("preliminary"):
                state = "preliminary"
            else:
                state = (doc.get("confidence") or {}).get("level") or "low"
                state = state if state in ("low", "moderate", "high") else "low"
        doc = self.models.docs.get(area_id) or {}
        return {"state": state, "text": STATUS_TEXT.get(state, state), "trained_at": doc.get("trained_at"),
                "confidence": (doc.get("confidence") or {}).get("level"),
                "score": (doc.get("confidence") or {}).get("score"),
                "training": self.training.get(area_id), "reason": (self.training.get(area_id) or {}).get("reason")}

    # ----------------------------------------------------------- occupancy
    def learn_occupancy(self, config: dict[str, Any], tz: Any, now: float | None = None) -> None:
        """Weekly usage pattern of rooms whose occupancy learning is switched on (shares per hour, weekdays and
        weekends apart). Only from the selected occupancy entities' own history; nothing is detected here."""
        now = now if now is not None else utcnow()
        for area_id, room in config["rooms"].items():
            if not (room["enabled"] and room["occupancy_learning"] and room["occupancy"]):
                self.occupancy.pop(area_id, None)
                continue
            start = now - 56 * 86400.0
            view = self.view()
            grid = prepare_room(RoomSpec(area_id, None, [], [], list(room["occupancy"]), None), view, start, now // GRID_S * GRID_S,
                                tz=None)
            buckets = {"weekday": [[0, 0] for _ in range(24)], "weekend": [[0, 0] for _ in range(24)]}
            days = set()
            for t, state in zip(grid.times, grid.window, strict=True):
                if state not in ("open", "closed", "settling"):
                    continue
                local = datetime.fromtimestamp(t, UTC).astimezone(tz)
                bucket = buckets["weekend" if local.weekday() >= 5 else "weekday"][local.hour]
                bucket[1] += 1
                bucket[0] += 1 if state == "open" else 0
                days.add(local.date())
            profile = {k: [round(a / b, 2) if b else 0.0 for a, b in v] for k, v in buckets.items()}
            self.occupancy[area_id] = {"days": len(days), "ready": len(days) >= OCCUPANCY_MIN_DAYS,
                                       "profile": profile if len(days) >= OCCUPANCY_MIN_DAYS else None,
                                       "learned_at": datetime.fromtimestamp(now, UTC).isoformat(timespec="seconds")}


def _stamp(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).timestamp()


def heating_series(grid: RoomGrid) -> list[float | None]:
    return heating_of(grid)
