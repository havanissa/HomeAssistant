"""The whole App in-process: configuration → Recorder backfill → learning → prediction → published snapshot.

A synthetic room (``thermal_sim``, known truth) is served as the Recorder history of a fake Home Assistant;
the real ``AppCore`` reads it in bounded windows, learns, predicts and builds the companion snapshot.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest

from spc import core as core_mod, engine as engine_mod, history as history_mod, protocol as P
from spc.core import AppCore
from spc.ha import HomeAssistantError
from spc.publisher import CompanionLink
from spc.settings import load_settings
from thermal_sim import Scenario, Sim, Truth

from conftest import KAMAR, STOVA

START = datetime(2026, 1, 5, tzinfo=UTC).timestamp()
DAYS = 24
NOW = START + DAYS * 86400
ROOM_T, TRV, OUT = "sensor.kamar_temperature", "climate.kamar_trv", "sensor.outdoor_temperature"
READS = {"history/history_during_period", "recorder/statistics_during_period", "recorder/list_statistic_ids",
         "get_config", "get_states", "config/auth/list"}


def rows_of(sim: Sim) -> dict[str, list[dict[str, Any]]]:
    """The sim's records as Home Assistant's compressed history rows."""
    out: dict[str, list[dict[str, Any]]] = {}
    ids = {sim.ids["room"]: ROOM_T, sim.ids["trv"]: TRV, sim.ids["outdoor"]: OUT}
    for rec in sim.records:
        entity = ids.get(rec["e"])
        if entity is None:
            continue
        if rec["k"] == "thermostat":
            a = rec["a"]
            attrs = {"current_temperature": a["cur"], "temperature": a["tgt"], "hvac_action": a["action"]}
        else:
            attrs = {"unit_of_measurement": "°C"}
        out.setdefault(entity, []).append({"s": rec["s"], "a": attrs, "lu": rec["t"]})
    return out


def serve_history(ha: Any, rows: dict[str, list[dict[str, Any]]], fail_after: list[int] | None = None) -> None:
    """Answer ``history/history_during_period`` like the Recorder: the window's rows plus the state at its start."""
    original = ha.command
    count = [0]

    async def command(command_type: str, **params: Any) -> Any:
        if command_type != "history/history_during_period":
            return await original(command_type, **params)
        ha._check(command_type, params)  # noqa: SLF001 - the allowlist, as the real client
        count[0] += 1
        if fail_after and count[0] == fail_after[0]:
            raise ConnectionError("connection lost")
        start = datetime.fromisoformat(params["start_time"]).timestamp()
        end = datetime.fromisoformat(params["end_time"]).timestamp()
        out = {}
        for entity in params["entity_ids"]:
            series = rows.get(entity, [])
            before = [r for r in series if r["lu"] <= start]
            inside = [r for r in series if start < r["lu"] < end]
            window = ([{**before[-1], "lu": start}] if before else []) + inside
            if window:
                out[entity] = window
        return out

    ha.command = command


def set_now(states: dict[str, Any], ha: Any, sim: Sim, *, temp: str | None = None) -> None:
    last = {}
    for rec in sim.records:
        last[rec["e"]] = rec
    at = datetime.fromtimestamp(NOW - 60, UTC)
    ha.set(ROOM_T, temp if temp is not None else str(last[sim.ids["room"]]["v"]), at, name="Kamar temperature",
           attributes={"unit_of_measurement": "°C", "device_class": "temperature"})
    a = last[sim.ids["trv"]]["a"]
    ha.set(TRV, "heat", at, name="Kamar TRV", attributes={"current_temperature": a["cur"], "temperature": a["tgt"],
                                                          "hvac_action": a["action"]})
    ha.set(OUT, str(last[sim.ids["outdoor"]]["v"]), at, name="Outdoor",
           attributes={"unit_of_measurement": "°C", "device_class": "temperature"})


ROOM = {"name": "Kamar Suður", "temperature": ROOM_T, "thermostats": [TRV], "outdoor": OUT,
        "comfort": {"day_c": 21.0, "weekday": {"start": "06:30", "end": "22:30"}, "weekend": {"start": "06:30", "end": "22:30"}}}


@pytest.fixture
def sim() -> Sim:
    return Sim(Scenario(start=START, days=DAYS, seed=5, truth=Truth())).run()


@pytest.fixture
def patched(monkeypatch):
    monkeypatch.setattr(core_mod, "utcnow", lambda: NOW)
    monkeypatch.setattr(engine_mod, "utcnow", lambda: NOW)
    monkeypatch.setattr(history_mod, "PAUSE_S", 0.0)


async def make(tmp_path, ha, sim, *, history: dict | None = None, fail_after=None) -> AppCore:
    settings = load_settings({"SPC_DATA_DIR": str(tmp_path), "SPC_ALLOWED_CLIENTS": "*", "SPC_OPTIONS": str(tmp_path / "o.json")})
    core = AppCore(settings)
    core.ha = ha
    core.link = CompanionLink(ha, settings.data_dir, app_version="0.1.0")
    core.acq.now = lambda: NOW
    serve_history(ha, history if history is not None else rows_of(sim), fail_after)
    await core.on_start()
    return core


async def until(predicate, timeout: float = 60.0) -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not predicate():
        assert loop.time() < end, "timed out"
        await asyncio.sleep(0.05)


async def configured(tmp_path, ha, sim, **kw) -> AppCore:
    core = await make(tmp_path, ha, sim, **kw)
    set_now(ha.states, ha, sim)
    ha.connect()
    core.save_room(KAMAR, ROOM, core.store.revision, "admin")
    core.save_settings({**core.store.config["settings"], "history_days": 30}, core.store.revision, "admin")
    await until(lambda: core.acq.status["state"] in ("done", "interrupted"))
    return core


async def test_backfill_learn_predict_publish(tmp_path, ha, sim, patched):
    core = await configured(tmp_path, ha, sim)
    try:
        status = core.acq.status
        assert status["state"] == "done" and status["windows"] >= DAYS * 2
        calls = [p for c, p in ha.calls if c == "history/history_during_period"]
        assert all(len(p["entity_ids"]) <= history_mod.ENTITIES_PER_CALL for p in calls)
        spans = {datetime.fromisoformat(p["end_time"]).timestamp() - datetime.fromisoformat(p["start_time"]).timestamp()
                 for p in calls}
        assert max(spans) <= history_mod.WINDOW_HOURS * 3600          # bounded windows only
        coverage = core.acq.coverage()
        assert coverage[ROOM_T]["days"] == pytest.approx(DAYS, abs=0.6)
        assert coverage[ROOM_T]["recorder_limit"] is not None       # nothing older than the sim: never asked again
        # learning
        await core._train(force=True)  # noqa: SLF001
        doc = core.engine.models.docs[KAMAR]
        params = {k: v["value"] for k, v in doc["parameters"].items() if v.get("available")}
        assert params["heating_c_per_h"] == pytest.approx(1.6, rel=0.12)
        assert params["k_per_h"] == pytest.approx(0.04, rel=0.35)
        status = core.engine.model_status(core.store.config, KAMAR)
        assert status["state"] in ("moderate", "high")
        # prediction and the published snapshot
        view = core.rooms_view[KAMAR]
        pred = view["prediction"]
        assert pred["available"] and pred["inputs"]["outdoor"] == "measured_held" and pred["predicted_1h"] is not None
        snapshot = core.snapshot()
        rooms, deleted = P.check_snapshot(snapshot)
        entities = snapshot["rooms"][KAMAR]["entities"]
        assert set(entities) == set(core_mod.ENTITY_KEYS) and not deleted
        assert entities["heating_rate"]["value"] is not None and entities["prediction_confidence"]["value"] in ("moderate", "high")
        assert entities["preheat_recommended"]["value"] in (True, False)
        assert entities["model_status"]["value"] == status["state"]
        # the room view of the interface
        room = core.room_view(KAMAR)
        assert room["model"]["characteristics"]["warming"] and room["model"]["validation"]["horizons"]["60"]["available"]
        hist = core.room_history(KAMAR, 72)
        assert len(hist["times"]) == len(hist["temp"]) > 100
        sim_result = core.simulate(KAMAR, {"heating": "on", "hours": 6, "target_c": 22})
        assert sim_result["available"] and sim_result["kind"] == "hypothetical" and sim_result["reach_min"] is not None
        # what was sent to Home Assistant: reads only
        assert {c for c, _p in ha.calls} <= READS | {f"{P.DOMAIN}/status", "manifest/get"}
    finally:
        await core.on_stop()


async def test_restart_resumes_and_keeps_the_model(tmp_path, ha, sim, patched):
    core = await configured(tmp_path, ha, sim)
    await core._train(force=True)  # noqa: SLF001
    await core.on_stop()
    ha.calls.clear()
    again = await make(tmp_path, ha, sim)
    try:
        assert KAMAR in again.engine.models.docs                      # the model survives the restart
        again.acq.configure(again.acq.tracked, 30)
        assert again.acq.plan(NOW + 3600) and all(why == "downtime" for *_x, why in again.acq.plan(NOW + 3600))
        assert again.acq.plan(NOW) == []                              # nothing older is read twice
    finally:
        await again.on_stop()


async def test_interrupted_backfill_continues_where_it_stopped(tmp_path, ha, sim, patched):
    core = await configured(tmp_path, ha, sim, fail_after=[20])
    try:
        assert core.acq.status["state"] == "interrupted"
        read_before = core.acq.status["windows"]
        assert 0 < read_before < 30
        core.acq.schedule_backfill()
        await until(lambda: core.acq.status["state"] == "done")
        calls = [p for c, p in ha.calls if c == "history/history_during_period"]
        starts = [p["start_time"] for p in calls]
        # every window was asked once, except the one that failed
        assert len(starts) - len(set(starts)) <= 1
        assert core.acq.coverage()[ROOM_T]["days"] == pytest.approx(DAYS, abs=0.6)
    finally:
        await core.on_stop()


async def test_missing_and_unavailable_inputs_publish_unknown_never_a_guess(tmp_path, ha, sim, patched):
    core = await configured(tmp_path, ha, sim)
    try:
        await core._train(force=True)  # noqa: SLF001
        ha.set(ROOM_T, "unavailable", datetime.fromtimestamp(NOW - 30, UTC), attributes={"unit_of_measurement": "°C"})
        await core._predict_and_publish()  # noqa: SLF001
        entities = core.snapshot()["rooms"][KAMAR]["entities"]
        P.check_snapshot(core.snapshot())
        # the thermostat's own reading is NOT silently used when a room sensor is configured
        assert core.rooms_view[KAMAR]["prediction"]["code"] == "no_temperature"
        assert entities["predicted_temperature"]["value"] is None
        assert entities["preheat_recommended"]["value"] is None        # unknown, never "off"
        assert entities["recommended_preheat_start"]["value"] is None
        assert entities["model_status"]["value"] is not None
        # the outdoor sensor gone: a model that needs it cannot predict
        set_now(ha.states, ha, sim)
        ha.set(OUT, "unknown", datetime.fromtimestamp(NOW - 30, UTC), attributes={"unit_of_measurement": "°C"})
        await core._predict_and_publish()  # noqa: SLF001
        assert core.rooms_view[KAMAR]["prediction"]["code"] == "no_outdoor"
        # recommendations need the configured confidence
        set_now(ha.states, ha, sim)
        core.save_settings({**core.store.config["settings"], "recommend_from": "high"}, core.store.revision, "admin")
        doc = core.engine.models.docs[KAMAR]
        doc["confidence"]["level"] = "low"
        core.engine.models.docs[KAMAR] = doc
        from spc import thermal_model as tm  # noqa: PLC0415
        original = tm.load_model

        def low(d, **kw):
            m = original(d, **kw)
            return tm.ThermalModel(**{**m.__dict__, "confidence": "low"}) if isinstance(m, tm.ThermalModel) else m
        engine_mod.tm.load_model = low
        try:
            await core._predict_and_publish()  # noqa: SLF001
        finally:
            engine_mod.tm.load_model = original
        entities = core.snapshot()["rooms"][KAMAR]["entities"]
        assert core.rooms_view[KAMAR]["prediction"]["available"]
        assert entities["predicted_temperature"]["value"] is not None   # the prediction itself is published
        assert entities["preheat_recommended"]["value"] is None         # but no recommendation
        assert entities["recommended_preheat_start"]["value"] is None
    finally:
        await core.on_stop()


async def test_recorder_errors_leave_gaps_not_values(tmp_path, ha, sim, patched):
    ha.errors["history/history_during_period"] = HomeAssistantError("unknown_error", "Recorder busy")
    core = await make(tmp_path, ha, sim, history={})
    try:
        set_now(ha.states, ha, sim)
        # with the error injected, serve_history's wrapper would answer: make the fake refuse instead
        async def refuse(command_type: str, **params: Any) -> Any:
            if command_type == "history/history_during_period":
                raise HomeAssistantError("unknown_error", "Recorder busy")
            return None
        ha.command = refuse
        ha.connect()
        core.save_room(KAMAR, ROOM, core.store.revision, "admin")
        await until(lambda: core.acq.status["state"] in ("done", "interrupted"))
        assert "did not answer" in (core.acq.status.get("recorder") or "")
        assert core.acq.store.gaps.get(ROOM_T)                      # the unanswered windows are gaps
    finally:
        await core.on_stop()


async def test_configuration_changes_keep_observations_and_models(tmp_path, ha, sim, patched):
    core = await configured(tmp_path, ha, sim)
    try:
        await core._train(force=True)  # noqa: SLF001
        records = core.acq.store.stats()
        trained = core.engine.models.docs[KAMAR]["trained_at"]
        # a soft change (comfort target): the model stays current
        room = {**core.store.config["rooms"][KAMAR], "comfort": {**core.store.config["rooms"][KAMAR]["comfort"], "day_c": 22.0}}
        core.save_room(KAMAR, room, core.store.revision, "admin")
        assert core.engine.models.docs[KAMAR]["fingerprint_hash"] == engine_mod.fingerprint(core.store.config, KAMAR)
        # removing the room keeps its history and model; adding it again continues
        core.delete_room(KAMAR, core.store.revision, "admin")
        assert KAMAR in core.engine.models.docs and core.acq.store.stats()["files"] == records["files"]
        core.save_room(KAMAR, ROOM, core.store.revision, "admin")
        await core._predict_and_publish()  # noqa: SLF001
        assert core.engine.models.docs[KAMAR]["trained_at"] == trained
        assert core.rooms_view[KAMAR]["prediction"]["available"]
    finally:
        await core.on_stop()


async def test_zone_neighbour_and_overview(tmp_path, ha, sim, patched):
    core = await configured(tmp_path, ha, sim)
    try:
        ha.set("sensor.stova_temperature", "20.5", datetime.fromtimestamp(NOW - 60, UTC), name="Stova",
               attributes={"unit_of_measurement": "°C", "device_class": "temperature"})
        core.save_room(STOVA, {"name": "Stova", "temperature": "sensor.stova_temperature"}, core.store.revision, "admin")
        result = core.save_zone("new", {"name": "Downstairs", "rooms": [KAMAR, STOVA],
                                        "connections": [{"a": KAMAR, "b": STOVA, "strength": "open"}]},
                                core.store.revision, "admin")
        overview = core.overview()
        zone = next(g for g in overview["groups"] if g["kind"] == "zone")
        assert zone["id"] == result["zone_id"] and [r["area_id"] for r in zone["rooms"]] == [KAMAR, STOVA]
        assert any(a["area_id"] == "gongd" for a in overview["unconfigured"])
        # a room cannot be in two zones (one room, one model)
        with pytest.raises(Exception, match="not valid"):
            core.save_zone("new", {"name": "Again", "rooms": [KAMAR, STOVA]}, core.store.revision, "admin")
    finally:
        await core.on_stop()


async def test_a_backfill_requested_while_one_is_finishing_is_not_dropped(tmp_path):
    """A reconnect can ask for a read while the previous one is still finishing (flushing after an interruption):
    one more pass must follow, not nothing."""
    acq = history_mod.Acquisition(tmp_path, None)
    passes, release = [], asyncio.Event()

    async def backfill() -> None:
        passes.append(1)
        if len(passes) == 1:
            await release.wait()

    acq.backfill = backfill
    acq.schedule_backfill()
    await asyncio.sleep(0)
    acq.schedule_backfill()          # while the first pass is still running
    acq.schedule_backfill()          # several requests: still one more pass
    release.set()
    await acq._task  # noqa: SLF001
    assert len(passes) == 2
