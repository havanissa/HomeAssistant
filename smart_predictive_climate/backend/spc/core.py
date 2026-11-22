"""The App's core: configuration, acquisition, learning, prediction, simulation and publication.

``BaseCore`` (``base.py``) owns the infrastructure (configuration store, Home Assistant connection, companion
link); this class adds the climate work:

* the tracked entities follow the configuration; their history is read and kept (``history.Acquisition``);
* models are trained in a worker thread after the history is read and every six hours (``engine.Engine``);
* predictions are recomputed every minute and on every relevant change, and published through the companion
  as native entities (``snapshot``).

Observation and recommendation only: nothing in this App can command a device. The Home Assistant client's
allowlist has no service call, and the companion registers no service.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
from datetime import UTC, datetime
import logging
import time
from typing import Any

from . import config as C, forecast as F, thermal_model as tm
from .base import BaseCore
from .const import APP_NAME, APP_VERSION
from .discovery import Index
from .engine import STATUS_TEXT, Engine, fingerprint, window_specs
from .history import Acquisition
from .store import ConfigError

_LOGGER = logging.getLogger(__name__)

PREDICT_EVERY_S = 60.0
TRAIN_CHECK_S = 900.0
ENTITY_KEYS = ("predicted_temperature", "heating_rate", "cooling_rate", "estimated_warmup_minutes",
               "recommended_preheat_start", "recommended_target_temperature", "solar_gain", "prediction_confidence",
               "preheat_recommended", "model_status")
CONFIDENCE_OPTIONS = ["none", "low", "moderate", "high"]
MODEL_STATUS_OPTIONS = list(STATUS_TEXT)


def utcnow() -> float:
    return time.time()


def _iso(t: float | None) -> str | None:
    return None if t is None else datetime.fromtimestamp(t, UTC).isoformat(timespec="seconds")


class AppCore(BaseCore):
    def __init__(self, settings: Any, buffer: Any = None) -> None:
        super().__init__(settings, buffer)
        data = settings.data_dir
        self.acq = Acquisition(data, None)
        self.engine = Engine(data, self.acq)
        self.rooms_view: dict[str, dict[str, Any]] = {}
        self._tasks: list[asyncio.Task[Any]] = []
        self.store.add_listener(self._on_config_saved)

    # ------------------------------------------------------------- life cycle
    async def on_start(self) -> None:
        await asyncio.to_thread(self.acq.load)
        await asyncio.to_thread(self.engine.load)
        self.acq.ha = self.ha
        self.acq.add_listener(self._on_acquisition)
        self.ha.add_listener(self._on_ha)
        self._apply_config()
        loop = asyncio.get_running_loop()
        self._tasks = [loop.create_task(self._ticker(), name="spc-ticker"),
                       loop.create_task(self._trainer(), name="spc-trainer")]
        _LOGGER.info("%s %s started (%d rooms)", APP_NAME, APP_VERSION, len(self.store.config["rooms"]))

    async def on_stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        await self.acq.stop()

    def _apply_config(self) -> None:
        config = self.store.config
        self.acq.configure(C.tracked_entities(config), config["settings"]["history_days"])
        if self.ha is not None and self.ha.connected:
            asyncio.get_running_loop().create_task(self.acq.follow_forecast(config["settings"]["weather"]))

    def _on_config_saved(self, reason: str) -> None:
        with contextlib.suppress(RuntimeError):
            self._apply_config()
            asyncio.get_running_loop().create_task(self._predict_and_publish())

    def _on_ha(self, kind: str, payload: Any) -> None:
        if kind == "status":
            if isinstance(payload, dict) and payload.get("connected"):
                self.acq.connected()
                asyncio.get_running_loop().create_task(self.acq.follow_forecast(self.store.config["settings"]["weather"]))
                asyncio.get_running_loop().create_task(self._predict_and_publish())
            elif isinstance(payload, dict) and not payload.get("connected"):
                self.acq.disconnected()
        elif kind == "state":
            entity_id, state = payload
            self.acq.on_state(entity_id, state)

    def _on_acquisition(self, kind: str) -> None:
        if kind == "backfill" and self.acq.status.get("state") == "done":
            asyncio.get_running_loop().create_task(self._train(force=False))
        if kind == "forecast":
            asyncio.get_running_loop().create_task(self._predict_and_publish())

    async def _ticker(self) -> None:
        while True:
            await asyncio.sleep(PREDICT_EVERY_S)
            try:
                self.acq.tick()
                await asyncio.to_thread(self.acq.flush)
                await self._predict_and_publish()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the loop must survive
                _LOGGER.exception("Periodic update failed")

    async def _trainer(self) -> None:
        await asyncio.sleep(30)
        while True:
            try:
                if self.acq.status.get("state") in ("done", "idle", "interrupted", "error") and self.ha and self.ha.connected:
                    await self._train(force=False)
                    if datetime.now(UTC).hour == 3:
                        await asyncio.to_thread(self.acq.store.prune, self.store.config["settings"]["history_days"] + 14,
                                                utcnow())
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Training round failed")
            await asyncio.sleep(TRAIN_CHECK_S)

    async def _train(self, *, force: bool, area_id: str | None = None) -> None:
        config = {**copy.deepcopy(self.store.config), "_app_version": APP_VERSION}
        await asyncio.to_thread(self.acq.flush)
        await self.engine.train_all(config, self._ha_config(), force=force, only={area_id} if area_id else None)
        await asyncio.to_thread(self.engine.learn_occupancy, self.store.config, self.engine.tz(self._ha_config()))
        await self._predict_and_publish()

    def _ha_config(self) -> dict[str, Any]:
        return dict(self.ha.config) if self.ha is not None else {}

    # ---------------------------------------------------------- prediction
    async def _predict_and_publish(self) -> None:
        if self.ha is None or not self.ha.connected:
            return
        config = self.store.config
        states = self.ha.states
        ha_config = self._ha_config()
        now = utcnow()
        views = {}
        for area_id, room in config["rooms"].items():
            if not room["enabled"]:
                continue
            try:
                views[area_id] = self.engine.predict_room(config, area_id, states, ha_config, now=now)
            except Exception as err:  # noqa: BLE001 - one room must not stop the others
                _LOGGER.exception("Prediction for %s failed", area_id)
                views[area_id] = {"area_id": area_id, "name": room["name"], "prediction": {
                    "available": False, "code": "error", "reason": f"{type(err).__name__}: {err}"},
                    "model_status": self.engine.model_status(config, area_id)}
        self.rooms_view = views
        if self.link is not None:
            try:
                self.link.publish(self.snapshot())
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Publishing failed")

    def snapshot(self) -> dict[str, Any]:
        """The published entities of every enabled room (values ``None`` = unknown; never a guessed value)."""
        config = self.store.config
        rooms: dict[str, Any] = {}
        for area_id, view in self.rooms_view.items():
            room = config["rooms"].get(area_id)
            if room is None:
                continue
            rooms[area_id] = {"name": room["name"][:80], "entities": entities_of(view, config)}
        return {"rooms": rooms}

    # -------------------------------------------------------------- reading
    def index(self) -> Index:
        ha = self.ha
        if ha is None:
            return Index({}, [], [], [])
        return Index(ha.states, ha.entity_registry, ha.devices, ha.areas)

    def overview(self) -> dict[str, Any]:
        config = self.store.config
        index = self.index()
        cards = {}
        for area_id, room in config["rooms"].items():
            view = self.rooms_view.get(area_id) or {}
            pred = view.get("prediction") or {}
            now = view.get("now") or {}
            status = self.engine.model_status(config, area_id)
            cards[area_id] = {
                "area_id": area_id, "name": room["name"], "enabled": room["enabled"],
                "area": index.areas.get(area_id), "temp_c": now.get("temp_c"), "source": now.get("source"),
                "target_c": view.get("target_now"), "target_reason": view.get("target_reason"),
                "predicted_c": pred.get("predicted_1h"), "heating_rate": pred.get("heating_rate"),
                "cooling_rate": pred.get("cooling_rate"), "preheat_start": pred.get("preheat_start"),
                "state": pred.get("state"), "confidence": status.get("confidence"), "status": status,
                "available": pred.get("available", False), "reason": pred.get("reason"),
                "heating_now": now.get("heating"), "window_open": now.get("window_open"),
                "observation_only": not room["thermostats"],
            }
        groups = []
        in_zone = set()
        for zone_id, zone in sorted(config["zones"].items(), key=lambda z: z[1]["name"].casefold()):
            members = [cards[a] for a in zone["rooms"] if a in cards]
            in_zone.update(zone["rooms"])
            groups.append({"kind": "zone", "id": zone_id, "name": zone["name"], "rooms": members,
                           "connections": zone["connections"]})
        others = [c for a, c in sorted(cards.items(), key=lambda kv: kv[1]["name"].casefold()) if a not in in_zone]
        if others:
            groups.append({"kind": "rooms", "id": None, "name": "Rooms", "rooms": others})
        unconfigured = [r for r in index.rooms(config["rooms"]) if not r["configured"]]
        return {"groups": groups, "unconfigured": unconfigured, "acquisition": self.acq.status,
                "forecast": {"entity_id": self.acq.forecast.get("entity_id"), "received_at": _iso(self.acq.forecast.get("received_at")),
                             "hours": len(self.acq.current_forecast())},
                "settings": config["settings"]}

    def room_view(self, area_id: str) -> dict[str, Any]:
        config = self.store.config
        room = config["rooms"].get(area_id)
        if room is None:
            raise ConfigError("unknown_room", "This room is not configured", 404)
        index = self.index()
        doc = self.engine.models.docs.get(area_id)
        model_info = None
        if doc:
            spc = doc.get("spc") or {}
            model_info = {
                "status": doc.get("status"), "preliminary": doc.get("preliminary"), "trained_at": doc.get("trained_at"),
                "structure": doc.get("structure"), "parameters": doc.get("parameters"), "dynamics": doc.get("dynamics"),
                "confidence": doc.get("confidence"), "validation": _short_validation(doc.get("validation")),
                "uncertainty": doc.get("uncertainty"), "explanations": doc.get("explanations"),
                "evidence": doc.get("evidence"), "data": doc.get("data"),
                "characteristics": spc.get("characteristics"), "window_loss": spc.get("window_loss"),
                "solar_basis": spc.get("solar_basis"),
                "current": doc.get("fingerprint_hash") == fingerprint(config, area_id)}
        entities = [e for e in C.tracked_entities({**config, "rooms": {area_id: room}, "zones": {}})]
        coverage = self.acq.coverage()
        missing = missing_information(config, area_id, index)
        return {"area_id": area_id, "room": room, "area": index.areas.get(area_id),
                "view": self.rooms_view.get(area_id), "model": model_info,
                "status": self.engine.model_status(config, area_id),
                "coverage": {e: coverage.get(e) for e in entities}, "names": {e: index.name(e) for e in entities},
                "missing": missing, "occupancy": self.engine.occupancy.get(area_id),
                "connections": [{**c, "name": config["rooms"][c["other"]]["name"]} for c in C.connections_of(config, area_id)],
                "zone": (C.zone_of(config, area_id) or (None, None))[1], "revision": self.store.revision,
                "published": entities_of(self.rooms_view.get(area_id) or {}, config) if self.rooms_view.get(area_id) else None}

    def room_history(self, area_id: str, hours: float) -> dict[str, Any]:
        """Measured series of a room for the graph: temperature, setpoint, heating, outdoor (5-minute grid,
        gaps kept as gaps), plus hourly long-term statistics before the detailed history where they exist."""
        config = self.store.config
        if area_id not in config["rooms"]:
            raise ConfigError("unknown_room", "This room is not configured", 404)
        hours = max(6.0, min(24.0 * 30, hours))
        now = utcnow()
        grid = self.engine.prepare(config, area_id, self._ha_config(), days=max(1, int(hours / 24 + 1)), now=now)
        start = now - hours * 3600
        idx = [i for i, t in enumerate(grid.times) if t >= start]
        step = max(1, len(idx) // 600)
        idx = idx[::step]
        room = config["rooms"][area_id]
        stats = {}
        for entity_id in [room["temperature"], *room["secondary"]]:
            if entity_id and self.acq.statistics.get(entity_id):
                stats[entity_id] = [r for r in self.acq.statistics[entity_id] if r[0] >= start]
        return {"times": [_iso(grid.times[i]) for i in idx], "temp": [_r(grid.temp[i]) for i in idx],
                "setpoint": [_r(grid.setpoint[i]) for i in idx], "heating": [_r(grid.heat[i]) for i in idx],
                "outdoor": [_r(grid.outdoor[i]) for i in idx], "window": [grid.window[i] for i in idx],
                "statistics": {e: [[_iso(r[0]), r[1], r[2], r[3]] for r in rows] for e, rows in stats.items()},
                "kind": {"temp": "measured", "setpoint": "measured", "heating": "measured", "outdoor": "measured",
                         "statistics": "hourly mean (long-term statistics)"}}

    def simulate(self, area_id: str, scenario: Any) -> dict[str, Any]:
        """A "what if" for one room. Uses the room's learned model; returns trajectories and sends nothing."""
        config = self.store.config
        room = config["rooms"].get(area_id)
        if room is None:
            raise ConfigError("unknown_room", "This room is not configured", 404)
        sc = scenario if isinstance(scenario, dict) else {}
        doc = self.engine.models.docs.get(area_id)
        model = tm.load_model(doc, allow_preliminary=True) if doc else tm.Unavailable("no_model", "No model yet")
        if isinstance(model, tm.Unavailable):
            return {"available": False, "reason": model.text}
        hours = int(_num(sc.get("hours"), 1, 48, 12))
        now = utcnow()
        ha_config = self._ha_config()
        states = self.ha.states if self.ha else {}
        ha_unit = ((ha_config.get("unit_system") or {}).get("temperature")) or "°C"
        cur = self.engine.current(config, area_id, states, ha_unit, now)
        start_c = sc.get("start_c")
        start_c = _num(start_c, -10, 40, None) if start_c not in (None, "") else cur["temp_c"]
        if start_c is None:
            return {"available": False, "reason": "The room temperature is not known; enter a start temperature"}
        view = self.engine.predict_room(config, area_id, states, ha_config, now=now)
        ctx = F.Context(now=now, tz=self.engine.tz(ha_config), temp_c=start_c, outdoor_c=None,
                        location=self.engine.location(ha_config), windows=window_specs(room), covers=cur["covers"],
                        heating_now=cur["heating"], heating_history=[cur["heating"]] * 24 if cur["heating"] is not None else [])
        outdoor_now = None
        outdoor_entity = C.outdoor_of(config, area_id)
        if outdoor_entity:
            state = states.get(outdoor_entity) or {}
            attrs = state.get("attributes") or {}
            from .units import to_celsius  # noqa: PLC0415
            outdoor_now = to_celsius(attrs.get("temperature"), attrs.get("temperature_unit") or ha_unit) \
                if outdoor_entity.startswith("weather.") else to_celsius(state.get("state"), attrs.get("unit_of_measurement"))
        ctx.outdoor_c = outdoor_now
        ctx.outdoor_forecast = self.acq.current_forecast() if config["settings"]["weather"] else []
        for conn in C.connections_of(config, area_id):
            ctx.neighbours[conn["other"]] = ((self.rooms_view.get(conn["other"]) or {}).get("now") or {}).get("temp_c")
        steps = F.horizon_steps(hours)
        solar_basis = ((doc or {}).get("spc") or {}).get("solar_basis", "geometry")
        outdoor_c = sc.get("outdoor_c")
        outdoor_c = None if outdoor_c in (None, "") else _num(outdoor_c, -40, 45, None)
        sky = sc.get("cloud_coverage")
        sky = None if sky in (None, "") else _num(sky, 0, 100, None)
        base_plan = F.plan(model, ctx, steps, solar_basis=solar_basis)
        plan = F.plan(model, ctx, steps, solar_basis=solar_basis, outdoor_offset=_num(sc.get("outdoor_offset"), -20, 20, 0.0),
                      outdoor_override=outdoor_c, sky_override=sky)
        if model.reference == "outdoor" and any(i.outdoor_c is None for i in plan.inputs):
            return {"available": False, "reason": "The outdoor temperature is unknown: enter one for the simulation"}

        def as_now(pl: Any) -> list[float]:
            """The heating as the thermostats run it now: on until their setpoint, as in the prediction."""
            heat = cur["heating"] or 0.0
            if heat > 0 and cur["setpoint_c"] is not None:
                return F.thermostat_schedule(model, ctx, pl, heat, cur["setpoint_c"])
            return [heat] * steps

        def schedule(spec: dict[str, Any]) -> list[float]:
            mode = spec.get("heating", "as_now")
            if mode == "off":
                return [0.0] * steps
            if mode == "on":
                return [1.0] * steps
            if mode == "window":
                a = int(_num(spec.get("heat_from_min"), 0, hours * 60, 0) / F.STEP_MIN)
                b = int(_num(spec.get("heat_until_min"), 0, hours * 60, hours * 60) / F.STEP_MIN)
                return [1.0 if a <= i < b else 0.0 for i in range(steps)]
            return as_now(plan)

        window_loss = self.engine.window_effects.get(area_id) or {}
        loss = window_loss.get("value_per_h") if window_loss.get("available") else F.ASSUMED_WINDOW_LOSS
        extra = None
        if sc.get("window_open"):
            a = int(_num(sc.get("window_from_min"), 0, hours * 60, 0) / F.STEP_MIN)
            b = int(_num(sc.get("window_until_min"), 0, hours * 60, 60) / F.STEP_MIN)
            extra = [loss if a <= i < b else 0.0 for i in range(steps)]
        baseline = F.run(model, ctx, base_plan, as_now(base_plan), start_c=start_c)
        what_if = F.run(model, ctx, plan, schedule(sc), extra_loss=extra, start_c=start_c)
        if isinstance(what_if, tm.Unavailable) or isinstance(baseline, tm.Unavailable):
            bad = what_if if isinstance(what_if, tm.Unavailable) else baseline
            return {"available": False, "reason": bad.text}
        target = _num(sc.get("target_c"), 5, 30, None) if sc.get("target_c") not in (None, "") else view.get("target_now")
        reach = None
        if target is not None:
            for i, v in enumerate(what_if.temps):
                if v >= target - F.REACHED_C:
                    reach = i * F.STEP_MIN
                    break
        times = [_iso(now + i * F.STEP_MIN * 60) for i in range(steps + 1)]
        sched = schedule(sc)
        return {"available": True, "kind": "hypothetical", "times": times[::2],
                "baseline": [_r(v) for v in baseline.temps][::2],
                "scenario": [_r(v) for v in what_if.temps][::2], "low": [_r(v) for v in what_if.low][::2],
                "high": [_r(v) for v in what_if.high][::2], "heating": [sched[min(i, steps - 1)] for i in range(0, steps + 1, 2)],
                "target_c": target, "reach_min": reach, "start_c": start_c,
                "inputs": {"outdoor": "hypothetical" if outdoor_c is not None or sc.get("outdoor_offset") else plan.outdoor_basis,
                           "sky": plan.sky_basis, "window_loss": "learned" if window_loss.get("available") else "assumed"},
                "warnings": list(dict.fromkeys([*what_if.warnings, *(["The open-window effect is an assumed value "
                                                                     f"({F.ASSUMED_WINDOW_LOSS}/h): this room's data has "
                                                                     "not shown it yet"] if extra and not window_loss.get("available") else [])]))}

    # ------------------------------------------------------------ changes
    def save_room(self, area_id: Any, room: Any, expected_revision: Any, by: str | None) -> dict[str, Any]:
        if not isinstance(area_id, str):
            raise ConfigError("invalid", "Unknown room")
        index = self.index()
        if self.ha is not None and self.ha.connected and area_id not in index.areas and area_id not in self.store.config["rooms"]:
            raise ConfigError("unknown_area", "This area does not exist in Home Assistant", 404)
        new = copy.deepcopy(self.store.config)
        new["rooms"][area_id] = room
        added = area_id not in self.store.config["rooms"]
        return self.store.save(new, expected_revision=expected_revision,
                               reason=f"room {'added' if added else 'changed'}: {area_id}", by=by)

    def delete_room(self, area_id: Any, expected_revision: Any, by: str | None) -> dict[str, Any]:
        if area_id not in self.store.config["rooms"]:
            raise ConfigError("unknown_room", "This room is not configured", 404)
        new = copy.deepcopy(self.store.config)
        del new["rooms"][area_id]
        for zone_id, zone in list(new["zones"].items()):
            zone["rooms"] = [a for a in zone["rooms"] if a != area_id]
            zone["connections"] = [c for c in zone["connections"] if area_id not in (c["a"], c["b"])]
            if len(zone["rooms"]) < 2:
                del new["zones"][zone_id]
        result = self.store.save(new, expected_revision=expected_revision, reason=f"room removed: {area_id}", by=by)
        self.rooms_view.pop(area_id, None)
        if self.link is not None:
            self.link.mark_deleted(area_id)
        # The observations and the model stay: adding the room again later continues where it left off.
        return result

    def save_zone(self, zone_id: Any, zone: Any, expected_revision: Any, by: str | None) -> dict[str, Any]:
        new = copy.deepcopy(self.store.config)
        if zone_id in (None, "", "new"):
            zone_id = C.new_zone_id()
        elif zone_id not in new["zones"]:
            raise ConfigError("unknown_zone", "This thermal zone does not exist", 404)
        new["zones"][zone_id] = zone
        result = self.store.save(new, expected_revision=expected_revision, reason=f"zone saved: {zone_id}", by=by)
        return {**result, "zone_id": zone_id}

    def delete_zone(self, zone_id: Any, expected_revision: Any, by: str | None) -> dict[str, Any]:
        if zone_id not in self.store.config["zones"]:
            raise ConfigError("unknown_zone", "This thermal zone does not exist", 404)
        new = copy.deepcopy(self.store.config)
        del new["zones"][zone_id]
        return self.store.save(new, expected_revision=expected_revision, reason=f"zone removed: {zone_id}", by=by)

    def save_settings(self, settings: Any, expected_revision: Any, by: str | None) -> dict[str, Any]:
        new = copy.deepcopy(self.store.config)
        new["settings"] = settings
        return self.store.save(new, expected_revision=expected_revision, reason="settings changed", by=by)

    async def retrain(self, area_id: Any) -> dict[str, Any]:
        if area_id not in self.store.config["rooms"]:
            raise ConfigError("unknown_room", "This room is not configured", 404)
        self._require_ha()
        await self._train(force=True, area_id=area_id)
        return {"training": self.engine.training.get(area_id), "status": self.engine.model_status(self.store.config, area_id)}

    def review_import(self, document: Any) -> dict[str, Any]:
        new = self.store.parse_import(document)
        before, after = self.store.config["rooms"], new["rooms"]
        changes = []
        for area_id in sorted(set(before) | set(after)):
            if area_id not in before:
                changes.append({"type": "added", "name": after[area_id]["name"]})
            elif area_id not in after:
                changes.append({"type": "removed", "name": before[area_id]["name"]})
            elif before[area_id] != after[area_id]:
                changes.append({"type": "changed", "name": after[area_id]["name"]})
        if self.store.config["zones"] != new["zones"]:
            changes.append({"type": "zones", "name": "Thermal zones"})
        if self.store.config["settings"] != new["settings"]:
            changes.append({"type": "settings", "name": "Settings"})
        return {"changes": changes, "revision": self.store.revision,
                "note": "Observations and learned models are kept; a room that is removed keeps its history and "
                        "continues when it is added again."}

    def apply_import(self, document: Any, expected_revision: Any, by: str | None, reason: str = "import") -> dict[str, Any]:
        return self.store.save(self.store.parse_import(document), expected_revision=expected_revision, reason=reason, by=by)

    # ---------------------------------------------------------- diagnostics
    def diagnostics(self) -> dict[str, Any]:
        config = self.store.config
        return {
            "app": {"name": APP_NAME, "version": APP_VERSION, "generated_at": _iso(utcnow())},
            "connection": self.connection(),
            "configuration": {"revision": self.store.revision, "rooms": len(config["rooms"]), "zones": len(config["zones"]),
                              "settings": config["settings"], "recovery": self.store.recovery},
            "acquisition": {**self.acq.status, "coverage": self.acq.coverage(), "store": self.acq.store.stats(),
                            "forecast": {"entity_id": self.acq.forecast.get("entity_id"),
                                         "received_at": _iso(self.acq.forecast.get("received_at")),
                                         "hours": len(self.acq.forecast.get("hourly") or [])}},
            "models": {a: {"status": d.get("status"), "trained_at": d.get("trained_at"),
                           "confidence": (d.get("confidence") or {}).get("level"),
                           "validation": _short_validation(d.get("validation"))} for a, d in self.engine.models.docs.items()},
            "training": self.engine.training,
            "rooms": {a: {"model_status": v.get("model_status"), "now": v.get("now"),
                          "prediction": {k: (v.get("prediction") or {}).get(k) for k in
                                         ("available", "code", "reason", "state", "preheat_start", "predicted_1h",
                                          "confidence", "inputs", "warnings")}} for a, v in self.rooms_view.items()},
            "time_zone": str(self.engine.tz(self._ha_config())),
            "log": self.buffer.entries("info")[-150:] if self.buffer else [],
        }


# ------------------------------------------------------------------ helpers
def _num(value: Any, low: float, high: float, default: Any) -> Any:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        return default
    return max(low, min(high, float(value)))


def _r(value: Any, digits: int = 2) -> Any:
    return None if value is None or value != value else round(value, digits)


def _short_validation(val: Any) -> Any:
    if not isinstance(val, dict):
        return None
    return {"usable_hours": val.get("usable_hours"), "horizons": val.get("horizons"), "conditions": val.get("conditions"),
            "headline": val.get("headline")}


def missing_information(config: dict[str, Any], area_id: str, index: Index) -> list[dict[str, str]]:
    """What the room needs (required), would profit from (suggested) or uses but cannot get now."""
    room = config["rooms"][area_id]
    out = []
    if not room["temperature"]:
        out.append({"level": "required" if not room["secondary"] and not room["thermostats"] else "suggested",
                    "text": "Choose an independent room temperature sensor. A thermostat's own reading is used meanwhile; "
                            "it measures next to the heater" if room["thermostats"] else "Choose the room temperature sensor"})
    if not C.outdoor_of(config, area_id):
        out.append({"level": "suggested", "text": "No outdoor temperature: heat loss to outdoors cannot be learned and "
                                                  "predictions hold the room's own average as the reference"})
    if not room["thermostats"]:
        out.append({"level": "info", "text": "Observation only: without a thermostat the heating response cannot be "
                                             "learned; the room's passive behaviour is still predicted"})
    elif not any(((index.states.get(t) or {}).get("attributes") or {}).get("hvac_action") is not None
                 for t in room["thermostats"]):
        out.append({"level": "suggested", "text": "The thermostats do not report whether they are heating (hvac_action): "
                                                  "heating activity is then inferred from setpoints, which is less precise"})
    if not room["windows"]:
        out.append({"level": "suggested", "text": "No windows described: solar gain cannot be learned"})
    if not room["openings"]:
        out.append({"level": "info", "text": "No window or door sensors: time with an open window cannot be left out of "
                                             "learning"})
    if not config["settings"]["weather"]:
        out.append({"level": "suggested", "text": "No weather entity: no outdoor temperature or cloud forecast, so "
                                                  "the present outdoor temperature is assumed to hold"})
    for entity_id in C.tracked_entities({**config, "rooms": {area_id: room}, "zones": {}}):
        if index.states and not index.exists(entity_id):
            out.append({"level": "warning", "text": f"{entity_id} does not exist in Home Assistant"})
    return out


def entities_of(view: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """The room's published entities. Every value is ``None`` (unknown) unless it was actually computed; a
    recommendation is only given when the model is reliable enough (Settings: ``recommend_from``)."""
    pred = view.get("prediction") or {}
    status = view.get("model_status") or {}
    available = bool(pred.get("available"))
    reliable = available and pred.get("reliable", False)
    reason = pred.get("reason") or ("; ".join(pred.get("explanation") or []) or None)
    common = {"computed_at": view.get("computed_at"), "reason": (reason or "")[:250] or None}

    def sensor(name: str, value: Any, *, device_class: str | None = None, unit: str | None = None,
               state_class: str | None = None, icon: str | None = None, attributes: dict[str, Any] | None = None,
               category: str | None = None) -> dict[str, Any]:
        return {"platform": "sensor", "name": name, "value": value, "device_class": device_class, "unit": unit,
                "state_class": state_class, "icon": icon, "attributes": {**common, **(attributes or {})},
                "category": category}

    confidence = pred.get("confidence") if available else None
    hourly = pred.get("hourly") or []
    out = {
        "predicted_temperature": sensor(
            "Predicted temperature", pred.get("predicted_1h") if available else None, device_class="temperature",
            unit="°C", state_class="measurement",
            attributes={"horizon": "1 h, heating as now" + (
                            f" (until the thermostat setpoint, {(pred.get('inputs') or {}).get('setpoint_c')} °C)"
                            if (pred.get("inputs") or {}).get("heating_course") == "until_setpoint" else ""),
                        "band_80": pred.get("predicted_1h_band"),
                        "in_3h": hourly[3]["expected"] if len(hourly) > 3 else None,
                        "passive_in_6h": hourly[6]["passive"] if len(hourly) > 6 else None,
                        "outdoor_input": (pred.get("inputs") or {}).get("outdoor"),
                        "sky_input": (pred.get("inputs") or {}).get("sky"), "kind": "model"}),
        "heating_rate": sensor("Heating rate", pred.get("heating_rate") if available else None, unit="°C/h",
                               state_class="measurement", icon="mdi:thermometer-chevron-up",
                               attributes={"meaning": "warming with full heating, at the present temperature and conditions"}),
        "cooling_rate": sensor("Cooling rate", pred.get("cooling_rate") if available else None, unit="°C/h",
                               state_class="measurement", icon="mdi:thermometer-chevron-down",
                               attributes={"meaning": "change without heating (negative = cooling)"}),
        "estimated_warmup_minutes": sensor("Estimated warm-up", pred.get("warmup_min") if available else None,
                                           device_class="duration", unit="min", icon="mdi:timer-sand",
                                           attributes={"target_c": (pred.get("requirement") or {}).get("target_c")}),
        "recommended_preheat_start": sensor(
            "Recommended preheat start", pred.get("preheat_start") if reliable and pred.get("preheat_needed") else None,
            device_class="timestamp",
            attributes={"expected_start": pred.get("preheat_start_expected") if reliable else None,
                        "target_time": (pred.get("requirement") or {}).get("t"),
                        "target_c": (pred.get("requirement") or {}).get("target_c"), "state": pred.get("state"),
                        "recommendation": True, "reliable": reliable}),
        "recommended_target_temperature": sensor(
            "Recommended target temperature", pred.get("recommended_target") if available else view.get("target_now"),
            device_class="temperature", unit="°C", icon="mdi:target",
            attributes={"source": "comfort schedule", "why": view.get("target_reason"), "recommendation": True}),
        "solar_gain": sensor("Expected solar gain", pred.get("solar_gain_c") if available else None, unit="°C",
                             icon="mdi:weather-sunny",
                             attributes={"kind": "estimate", "sky": (pred.get("inputs") or {}).get("sky")}),
        "prediction_confidence": sensor(
            "Prediction confidence", confidence if confidence in CONFIDENCE_OPTIONS else None, device_class="enum",
            attributes={"options": CONFIDENCE_OPTIONS, "score": status.get("score"), "reliable": reliable,
                        "warnings": [w[:200] for w in (pred.get("warnings") or [])[:4]]}),
        "model_status": sensor(
            "Model status", status.get("state") if status.get("state") in MODEL_STATUS_OPTIONS else None,
            device_class="enum", category="diagnostic",
            attributes={"options": MODEL_STATUS_OPTIONS, "text": status.get("text"), "trained_at": status.get("trained_at")}),
    }
    # The binary recommendation: on = start heating now to meet the coming comfort target. Unknown (not off)
    # whenever there is no reliable prediction: an unavailable forecast must never read as "no need to heat".
    on = None
    if reliable and pred.get("preheat_needed") is not None:
        on = bool(pred.get("preheat_needed")) and pred.get("state") in ("preheat_now", "below_target", "late") or (
            pred.get("state") == "preheat_later" and _due(pred.get("preheat_start"), view.get("computed_at")))
    out["preheat_recommended"] = {
        "platform": "binary_sensor", "name": "Preheat recommended", "value": on, "device_class": "heat",
        "unit": None, "state_class": None, "icon": None, "category": None,
        "attributes": {**common, "state": pred.get("state"), "start": pred.get("preheat_start") if reliable else None,
                       "target_c": (pred.get("requirement") or {}).get("target_c"), "recommendation": True,
                       "reliable": reliable}}
    for entity in out.values():
        entity["attributes"] = {k: v for k, v in entity["attributes"].items() if v is not None}
    return out


def _due(start: Any, now: Any) -> bool:
    if not isinstance(start, str) or not isinstance(now, str):
        return False
    return datetime.fromisoformat(start) <= datetime.fromisoformat(now)
