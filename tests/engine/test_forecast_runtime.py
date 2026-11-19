"""Occupancy forecast (0.5.0) in Home Assistant: learning, entities, settings, logging, observation only.

The Recorder is SIMULATED here (a fake history fetcher fed with synthetic
histories); one test uses the real Recorder to check the query and its
conversion. Nothing here is a real Home Assistant observation.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.smart_occupancy.const import DECISION_LOG_SIZE, DOMAIN
from custom_components.smart_occupancy.forecast import HOUSE_SUBJECT, subject

from .conftest import Pilot, pilot_options, setup_entry
from .test_lighting_runtime import WS_LOGGER
from .test_switches import WS

ROOM_WS = "smart_occupancy/forecast/room"
SETTINGS_WS = "smart_occupancy/forecast/settings"
CONNECTION_WS = "smart_occupancy/forecast/connection"


class FakeRecorder:
    """Serves a synthetic history like recorder.history.get_significant_states (with the start-time state)."""

    def __init__(self, history: dict[str, list[tuple[float, str]]]) -> None:
        self.history = history
        self.calls: list[tuple[list[str], datetime, datetime]] = []

    async def __call__(self, entity_ids: list[str], start: datetime, end: datetime):
        self.calls.append((list(entity_ids), start, end))
        s, e = start.timestamp(), end.timestamp()
        out: dict[str, list[tuple[float, str]]] = {}
        for entity_id in entity_ids:
            rows = self.history.get(entity_id, [])
            before = [r for r in rows if r[0] <= s]
            inside = [r for r in rows if s < r[0] < e]
            items = ([(s, before[-1][1])] if before else []) + inside
            if items:
                out[entity_id] = items
        return out

    def oldest(self) -> float | None:
        times = [r[0] for rows in self.history.values() for r in rows]
        return min(times) if times else None


def local_noon(days_ahead: int = 2) -> datetime:
    tz = dt_util.get_default_time_zone()
    d = (dt_util.now() + timedelta(days=days_ahead)).date()
    return datetime(d.year, d.month, d.day, 12, 0, tzinfo=tz)


def office_history(p: Pilot, now: datetime, days: int, *, at=(12, 10), start_days: int | None = None
                   ) -> dict[str, list[tuple[float, str]]]:
    """SIMULATED: every day at ``at`` someone walks through the hall into Kamar Norður and stays for 2 h."""
    tz = dt_util.get_default_time_zone()
    first = now - timedelta(days=start_days or days)
    h: dict[str, list[tuple[float, str]]] = {e: [(first.timestamp() - 3600, "off")]
                                             for e in (p.pir, p.radar, p.door, p.pc, p.hall_pir)}
    for i in range(1, days + 1):
        d = (now - timedelta(days=i)).date()
        t = datetime(d.year, d.month, d.day, at[0], at[1], tzinfo=tz).timestamp() + (i % 3) * 300
        h[p.hall_pir] += [(t, "on"), (t + 60, "off")]
        h[p.pir] += [(t + 20, "on"), (t + 80, "off")]
        h[p.radar] += [(t + 25, "on"), (t + 2 * 3600, "off")]
    return {e: sorted(v) for e, v in h.items()}


async def client(hass: HomeAssistant, hass_ws_client, hass_admin_user) -> WS:
    """A websocket client whose token was issued after the clock moved (tokens expire in frozen time)."""
    refresh = await hass.auth.async_create_refresh_token(hass_admin_user, "https://example.com/app")
    return WS(await hass_ws_client(hass, access_token=hass.auth.async_create_access_token(refresh)))


async def start(hass: HomeAssistant, p: Pilot, freezer, history, *, now: datetime | None = None):
    freezer.move_to(now or local_noon())
    entry = await setup_entry(hass, pilot_options(p))
    manager = entry.runtime_data.forecast
    fake = FakeRecorder(history)
    manager.fetch_history = fake
    manager.oldest_history = fake.oldest
    return entry, manager, fake


def fstate(hass: HomeAssistant, domain: str, unique: str):
    entity_id = er.async_get(hass).async_get_entity_id(domain, DOMAIN, unique)
    assert entity_id, unique
    return hass.states.get(entity_id)


# ------------------------------------------------------------------ history
async def test_learns_from_history_and_publishes(hass: HomeAssistant, pilot: Pilot, freezer, hass_ws_client,
                                                 hass_admin_user, service_calls) -> None:
    now = local_noon()
    entry, m, fake = await start(hass, pilot, freezer, office_history(pilot, now, 12))
    await m.async_learn()
    await hass.async_block_till_done()
    assert m.status.state == "ready"
    assert len(m.days) == 12
    # One query per learned day plus today; only the configured signals are requested.
    assert len(fake.calls) == 13
    assert set(fake.calls[0][0]) == {pilot.pir, pilot.radar, pilot.door, pilot.pc, pilot.hall_pir}
    # The PC activity signal is a context automatically (existing room configuration).
    assert m.contexts() == [pilot.pc]
    area = pilot.area
    sensor = fstate(hass, "sensor", f"{DOMAIN}_room_{area}_forecast")
    assert sensor.entity_id == "sensor.smart_occupancy_kamar_nordur_forecast"
    assert sensor.attributes["status"] == "ok"
    assert sensor.attributes["observation_only"] is True
    p30 = sensor.attributes["probability_30_min"]
    assert p30 > 60 and int(sensor.state) < p30  # the visit usually starts at 12:10-12:20
    assert fstate(hass, "sensor", f"{DOMAIN}_room_{area}_forecast_confidence").state == "moderate"
    predicted = fstate(hass, "binary_sensor", f"{DOMAIN}_room_{area}_predicted")
    assert predicted.entity_id == "binary_sensor.smart_occupancy_kamar_nordur_predicted"
    # Room override: 30 minutes ahead, threshold 60 % -> predicted.
    ws = await client(hass, hass_ws_client, hass_admin_user)
    reply = await ws(type=SETTINGS_WS, area_id=area, values={"horizon_minutes": 30})
    assert reply["success"], reply
    await hass.async_block_till_done()
    assert fstate(hass, "binary_sensor", f"{DOMAIN}_room_{area}_predicted").state == "on"
    reply = await ws(type=SETTINGS_WS, area_id=area, values={"probability_threshold": 99})
    await hass.async_block_till_done()
    assert fstate(hass, "binary_sensor", f"{DOMAIN}_room_{area}_predicted").state == "off"
    # Observation only: learning and forecasting never called a service.
    assert service_calls == []


async def test_incremental_learning_and_restart(hass: HomeAssistant, pilot: Pilot, freezer) -> None:
    now = local_noon()
    history = office_history(pilot, now, 6)
    entry, m, fake = await start(hass, pilot, freezer, history)
    await m.async_learn()
    assert len(fake.calls) == 7
    await m.async_learn()  # nothing new: only today is read again
    assert len(fake.calls) == 8
    # The next day: only the newly completed day and today.
    freezer.move_to(now + timedelta(days=1))
    await m.async_learn()
    assert len(fake.calls) == 10
    assert len(m.days) == 7
    learned = sorted(m.days)
    # Restart: the learned days come back from the store; the Recorder is not asked for them again.
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    m2 = entry.runtime_data.forecast
    assert sorted(m2.days) == learned
    fake2 = FakeRecorder(history)
    m2.fetch_history = fake2
    m2.oldest_history = fake2.oldest
    await m2.async_learn()
    assert len(fake2.calls) == 1


async def test_short_and_missing_history(hass: HomeAssistant, pilot: Pilot, freezer) -> None:
    now = local_noon()
    entry, m, fake = await start(hass, pilot, freezer, office_history(pilot, now, 1))
    await m.async_learn()
    await hass.async_block_till_done()
    sensor = fstate(hass, "sensor", f"{DOMAIN}_room_{pilot.area}_forecast")
    assert sensor.state == "unknown"  # no fabricated probability
    assert sensor.attributes["status"] == "insufficient_data"
    assert sensor.attributes["probability_15_min"] is None
    assert fstate(hass, "sensor", f"{DOMAIN}_room_{pilot.area}_forecast_confidence").state == "insufficient_data"
    assert fstate(hass, "binary_sensor", f"{DOMAIN}_room_{pilot.area}_predicted").state == "unknown"
    assert "Only 2 days of history (at least 3 needed)" in m.room(pilot.area).explanation


async def test_no_history_at_all(hass: HomeAssistant, pilot: Pilot, freezer) -> None:
    entry, m, fake = await start(hass, pilot, freezer, {})
    await m.async_learn()
    await hass.async_block_till_done()
    assert m.days == {} and fake.oldest() is None
    assert m.room(pilot.area).status == "insufficient_data"
    # Without a known start of the Recorder's history every day of the window is probed once, then never again.
    calls = len(fake.calls)
    assert calls == len(m.no_data) + 1 == m.settings.defaults.window_days + 1
    await m.async_learn()
    assert len(fake.calls) == calls + 1


async def test_unavailable_sensors(hass: HomeAssistant, pilot: Pilot, freezer) -> None:
    now = local_noon()
    entry, m, fake = await start(hass, pilot, freezer, office_history(pilot, now, 12))
    await m.async_learn()
    for e in (pilot.pir, pilot.radar, pilot.door, pilot.pc):
        hass.states.async_set(e, "unavailable")
    await hass.async_block_till_done()
    freezer.tick(timedelta(seconds=5))
    m._recompute()
    await hass.async_block_till_done()
    assert m.room(pilot.area).status == "unavailable"
    assert fstate(hass, "sensor", f"{DOMAIN}_room_{pilot.area}_forecast").state == "unavailable"
    log = entry.runtime_data.lighting.decisions.entries(subject(pilot.area))
    assert any(d["kind"] == "forecast_unavailable" and "unreliable" in d["explanation"] for d in log)


async def test_recorder_writes_bounded(hass: HomeAssistant, pilot: Pilot, freezer) -> None:
    now = local_noon()
    entry, m, fake = await start(hass, pilot, freezer, office_history(pilot, now, 12))
    await m.async_learn()
    await hass.async_block_till_done()
    ids = {e.entity_id for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
           if "forecast" in e.unique_id or "predicted" in e.unique_id}
    writes: list[str] = []
    hass.bus.async_listen("state_changed", lambda ev: writes.append(ev.data["entity_id"])
                          if ev.data["entity_id"] in ids else None)
    for _ in range(20):
        m._recompute()  # the same situation recalculated: nothing new to write
    await hass.async_block_till_done()
    assert writes == []
    # Unrecorded detail attributes.
    from custom_components.smart_occupancy.forecast_entities import UNRECORDED_FORECAST

    assert {"explanation", "factors", "window"} <= UNRECORDED_FORECAST


# ------------------------------------------------------------------ live
async def test_movement_next_room_and_confirmation(hass: HomeAssistant, pilot: Pilot, freezer) -> None:
    now = local_noon()
    entry, m, fake = await start(hass, pilot, freezer, office_history(pilot, now, 12))
    await m.async_learn()
    await hass.async_block_till_done()
    hass.states.async_set(pilot.hall_pir, "on")
    await hass.async_block_till_done()
    freezer.tick(timedelta(seconds=2))
    m._recompute()
    await hass.async_block_till_done()
    nxt = fstate(hass, "sensor", f"{DOMAIN}_predicted_next_room")
    assert nxt.state == "Kamar Norður"
    assert nxt.attributes["from_area_id"] == pilot.hall
    hass.states.async_set(pilot.pir, "on")
    await hass.async_block_till_done()
    house = entry.runtime_data.lighting.decisions.entries(HOUSE_SUBJECT)
    kinds = [d["kind"] for d in house]
    assert "movement_predicted" in kinds and "movement_outcome" in kinds
    outcome = next(d for d in house if d["kind"] == "movement_outcome")
    assert outcome["details"]["confirmed"] is True
    assert "subsequently confirmed" in outcome["explanation"]


async def test_prediction_outcome_logged_and_log_bounded(hass: HomeAssistant, pilot: Pilot, freezer) -> None:
    now = local_noon()
    entry, m, fake = await start(hass, pilot, freezer, office_history(pilot, now, 12))
    await m.async_learn()
    await m.async_set_settings(m.settings.with_room(pilot.area, m.settings.room(pilot.area).__class__(
        horizon_minutes=30)))
    await hass.async_block_till_done()
    log = entry.runtime_data.lighting.decisions
    assert any(d["kind"] == "predicted" for d in log.entries(subject(pilot.area)))
    # The same prediction recalculated is not logged again.
    before = log.entries(subject(pilot.area))
    for _ in range(10):
        m._recompute()
    assert log.entries(subject(pilot.area)) == before
    # Confirmed by actual occupancy.
    hass.states.async_set(pilot.pir, "on")
    hass.states.async_set(pilot.radar, "on")
    await hass.async_block_till_done()
    freezer.tick(timedelta(seconds=15))
    hass.states.async_set(pilot.pir, "off")
    await hass.async_block_till_done()
    assert any(d["kind"] == "prediction_confirmed" for d in log.entries(subject(pilot.area)))
    for i in range(30):
        m._decision(subject(pilot.area), "test", "x", f"e{i}", key=("t", i))
    assert len(log.entries(subject(pilot.area))) == DECISION_LOG_SIZE
    # Forecast records never push automatic lighting decisions out of the room's own log.
    assert all(d["module"] != "forecast" for d in log.entries(pilot.area))


async def test_observation_only_and_occupancy_unchanged(hass: HomeAssistant, pilot: Pilot, freezer,
                                                        service_calls) -> None:
    now = local_noon()
    entry, m, fake = await start(hass, pilot, freezer, office_history(pilot, now, 12))
    occupancy = entry.runtime_data.occupancy
    before = occupancy.diagnostics()
    await m.async_learn()
    for _ in range(5):
        m._recompute()
    await hass.async_block_till_done()
    assert occupancy.diagnostics() == before  # forecasting only reads the occupancy engines
    hass.states.async_set(pilot.pir, "on")
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.smart_occupancy_kamar_nordur_occupied").state == "on"
    freezer.tick(timedelta(minutes=5))
    m._recompute()
    await hass.async_block_till_done()
    assert service_calls == []


# ------------------------------------------------------------------ settings
async def test_settings_inheritance_and_restart(hass: HomeAssistant, pilot: Pilot, freezer, hass_ws_client,
                                                hass_admin_user, caplog) -> None:
    now = local_noon()
    entry, m, fake = await start(hass, pilot, freezer, office_history(pilot, now, 4))
    ws = await client(hass, hass_ws_client, hass_admin_user)
    area = pilot.area
    assert (await ws(type=SETTINGS_WS, values={"probability_threshold": 70}))["success"]
    assert m.settings.effective(area).probability_threshold == 70
    assert (await ws(type=SETTINGS_WS, area_id=area, values={"probability_threshold": 50}))["success"]
    assert (await ws(type=SETTINGS_WS, values={"probability_threshold": 80}))["success"]
    eff = m.settings.effective(area)
    assert eff.probability_threshold == 50 and "probability_threshold" in eff.overridden
    assert m.settings.effective(pilot.hall).probability_threshold == 80
    caplog.set_level(logging.CRITICAL, logger=WS_LOGGER)
    bad = await ws(type=SETTINGS_WS, values={"window_days": 2})
    assert not bad["success"]
    assert (await ws(type=SETTINGS_WS, values={"enabled": False}))["success"]
    await hass.async_block_till_done()
    assert m.room(area).status == "disabled"
    assert fstate(hass, "sensor", f"{DOMAIN}_room_{area}_forecast").state == "unavailable"
    assert (await ws(type=SETTINGS_WS, values={"enabled": True}))["success"]
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    m2 = entry.runtime_data.forecast
    assert m2.settings.effective(area).probability_threshold == 50
    assert m2.settings.defaults.probability_threshold == 80
    assert (await ws(type=SETTINGS_WS, area_id=area, values={"probability_threshold": None}))["success"]
    assert m2.settings.effective(area).probability_threshold == 80
    assert m2.settings.rooms == {}


async def test_connections(hass: HomeAssistant, pilot: Pilot, freezer, hass_ws_client, hass_admin_user) -> None:
    entry, m, fake = await start(hass, pilot, freezer, {})
    assert m.settings.connections is None
    ws = await client(hass, hass_ws_client, hass_admin_user)
    reply = await ws(type=CONNECTION_WS, action="add", a=pilot.area, b=pilot.hall, kind="open")
    assert reply["success"], reply
    assert [c.kind for c in m.connections()] == ["open"]
    assert frozenset((pilot.area, pilot.hall)) in m.replay_spec().overlaps
    reply = await ws(type=CONNECTION_WS, action="remove", a=pilot.area, b=pilot.hall)
    assert m.connections() == [] and m.settings.connections == ()
    reply = await ws(type=CONNECTION_WS, action="reset")
    assert m.settings.connections is None


async def test_room_detail_uses_real_learned_data(hass: HomeAssistant, pilot: Pilot, freezer, hass_ws_client,
                                                  hass_admin_user) -> None:
    now = local_noon()
    entry, m, fake = await start(hass, pilot, freezer, office_history(pilot, now, 12))
    await m.async_learn()
    ws = await client(hass, hass_ws_client, hass_admin_user)
    reply = await ws(type=ROOM_WS, area_id=pilot.area)
    assert reply["success"], reply
    d = reply["result"]
    assert d["observation_only"] is True
    weekly = d["patterns"]["weekly"]
    assert len(weekly) == 7 and len(weekly[0]) == 24
    assert all(row[13] is not None and row[13] > 0.5 for row in weekly)  # 12:10-14:10 every day
    assert all(row[3] is not None and row[3] == 0 for row in weekly)
    assert d["patterns"]["days"] == 12
    assert d["accuracy"]["by_horizon"]["15"]["forecasts"] > 0
    assert d["learning"]["room_days"] == 13  # 12 learned days + today so far
    assert any(x["area_id"] == pilot.hall for x in d["movement"]["previous"])
    hall = (await ws(type=ROOM_WS, area_id=pilot.hall))["result"]
    assert hall["movement"]["next"][0]["area_id"] == pilot.area
    assert d["settings"]["effective"]["horizon_minutes"] == 15
    assert any(c["entity_id"] == pilot.pc for c in d["settings"]["automatic_contexts"])
    editor = await ws(type="smart_occupancy/lighting/editor", area_id=pilot.area)
    assert editor["result"]["room"]["forecast"]["status"] == "ok"
