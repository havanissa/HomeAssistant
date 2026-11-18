"""Phase A5: forecast ownership, the published view, the learned-data mirror and the rollback (integration side).

A simulated App (``FakeApp``) speaks the forecast bridge protocol over a real
websocket connection; the Recorder is SIMULATED (``FakeRecorder`` fed with the
synthetic office history of ``test_forecast_runtime``). Forecasts are observation
only on both sides: no test here expects, and every test forbids, a service call.
The tests prove:

* the integration produces the forecasts by default (also after a restart); a
  session gets everything it learned (settings, daily summaries) and every
  recomputed view and settings change;
* a reviewed handover needs the same forecast code, the current settings,
  readiness and the expected epoch; it exports the learned days, stops the
  integration's manager (dormant: no learning, no recomputing) and the forecast
  entities keep their last values until the App publishes;
* the App's published view is shown on the forecast entities and in the read-only
  room view; its learned data is adopted by the dormant manager and kept in the
  mirror store; invalid publications are refused; the forecast editors are
  read-only meanwhile;
* release, the ``return`` command, the admin service, lease expiry, a new App
  instance and a restart nobody resumes give control back: the integration's
  manager starts again with the App's learned days (nothing is retrained: its
  first learning run reads only today), publishes its own view, and the old App's
  commands are refused;
* the export round-trips: a fresh manager that imports it forecasts exactly the
  same at the same moment, and a published room forecast reads back as the
  entities need it;
* non-administrators cannot use any bridge command or the service; the
  diagnostics download includes the ownership record.
"""

from __future__ import annotations

from collections import deque
import copy
from datetime import timedelta
import json
import secrets
from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import Unauthorized
from homeassistant.util import dt as dt_util

from custom_components.smart_occupancy import forecast_sync as S
from custom_components.smart_occupancy.const import DOMAIN, FORECAST_START_DELAY_SECONDS
from custom_components.smart_occupancy.diagnostics import async_get_config_entry_diagnostics
from custom_components.smart_occupancy.forecast import ForecastManager, RoomForecast
from custom_components.smart_occupancy.forecast_model import HORIZONS
from custom_components.smart_occupancy.forecast_settings import RoomForecastSettings
from custom_components.smart_occupancy.occupancy_core import fingerprint as core_fingerprint
from custom_components.smart_occupancy.runtime_manifest import RUNTIME_VERSION, fingerprint

from .conftest import Pilot
from .test_forecast_runtime import (
    CONNECTION_WS,
    ROOM_WS,
    SETTINGS_WS,
    FakeRecorder,
    client,
    fstate,
    local_noon,
    office_history,
    start,
)
from .test_lighting_runtime import WS_LOGGER

SUB = "smart_occupancy/forecast/bridge/subscribe"
MIRROR_KEY = "smart_occupancy.forecast_mirror"
DAYS = 12


def readiness(ready: bool = True) -> dict[str, Any]:
    return {"ready": ready, "checks": [{"key": "days", "status": "ok" if ready else "error"}],
            "summary": {"days": DAYS, "same": DAYS}}


def wire(value: Any) -> Any:
    """As the websocket delivers it (tuples become lists)."""
    return json.loads(json.dumps(value))


class FakeApp:
    """The App's side of the forecast bridge on one websocket connection."""

    def __init__(self, hass: HomeAssistant, client: Any, instance: str | None = None) -> None:
        self.hass = hass
        self.client = client
        self.instance = instance or secrets.token_hex(16)
        self._id = 100
        self.sub_id: int | None = None
        self.events: list[dict[str, Any]] = []
        self.session: str | None = None
        self.hello: dict[str, Any] | None = None
        self.epoch: int | None = None
        self.token: str | None = None
        self.seq = 0

    async def send(self, **msg: Any) -> dict[str, Any]:
        self._id += 1
        await self.client.send_json({"id": self._id, **msg})
        while True:
            reply = await self.client.receive_json()
            if reply.get("type") == "event" and reply.get("id") == self.sub_id:
                self.events.append(reply["event"])
                continue
            if reply.get("id") == self._id:
                return reply

    async def drain(self) -> list[dict[str, Any]]:
        await self.hass.async_block_till_done()
        await self.send(type="ping")
        return self.events

    async def connect(self, resume_token: str | None = None, *, fp: str | None = None,
                      expect_ok: bool = True) -> dict[str, Any]:
        self._id += 1
        self.sub_id = self._id
        await self.client.send_json({
            "id": self._id, "type": SUB, "instance_id": self.instance, "protocol": S.PROTOCOL_VERSION,
            "app_version": "0.6.0", "runtime_version": RUNTIME_VERSION, "fingerprint": fp or fingerprint(),
            "core_fingerprint": core_fingerprint(), "resume_token": resume_token})
        reply = await self.client.receive_json()
        if not expect_ok:
            return reply
        assert reply["success"], reply
        hello = (await self.client.receive_json())["event"]
        assert hello["type"] == "hello"
        self.hello, self.session = hello, hello["session_id"]
        if hello.get("lease_token"):
            self.token, self.epoch, self.seq = hello["lease_token"], hello["ownership"]["epoch"], 0
        return hello

    async def claim(self, *, ready: bool = True, epoch: int | None = None, revision: str | None = None) -> dict:
        reply = await self.send(type="smart_occupancy/forecast/bridge/claim", session_id=self.session,
                                expected_epoch=self.hello["ownership"]["epoch"] if epoch is None else epoch,
                                settings_revision=revision or self.hello["settings_revision"],
                                readiness=readiness(ready))
        if reply["success"]:
            self.epoch, self.token, self.seq = reply["result"]["epoch"], reply["result"]["lease_token"], 0
        return reply

    def owner(self, **over: Any) -> dict[str, Any]:
        self.seq += 1
        return {"session_id": self.session, "epoch": self.epoch, "lease_token": self.token, "seq": self.seq, **over}

    async def heartbeat(self, **over: Any) -> dict[str, Any]:
        return await self.send(type="smart_occupancy/forecast/bridge/heartbeat", **self.owner(**over))

    async def publish(self, **params: Any) -> dict[str, Any]:
        return await self.send(type="smart_occupancy/forecast/bridge/publish", **self.owner(), **params)

    async def release(self, **params: Any) -> dict[str, Any]:
        return await self.send(type="smart_occupancy/forecast/bridge/release", **self.owner(), **params)


async def admin_client(hass: HomeAssistant, hass_ws_client, hass_admin_user):
    """A raw websocket client with a token issued after the clock moved."""
    return (await client(hass, hass_ws_client, hass_admin_user)).client


def bridge_of(entry):
    return entry.runtime_data.forecast_bridge


def mgr_of(entry) -> ForecastManager:
    return entry.runtime_data.forecast


async def tick(hass: HomeAssistant, freezer, seconds: float, step: float = 1.0) -> None:
    left = seconds
    while left > 0:
        freezer.tick(timedelta(seconds=min(step, left)))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
        left -= step


async def alive(hass: HomeAssistant, freezer, app: FakeApp, seconds: float) -> None:
    """Time passes while the App keeps its lease."""
    left = seconds
    while left > 0:
        step = min(S.HEARTBEAT_SECONDS, left)
        await tick(hass, freezer, step, step=step)
        assert (await app.heartbeat())["success"]
        left -= step


async def learned(hass: HomeAssistant, pilot: Pilot, freezer, days: int = DAYS):
    """The integration has learned ``days`` synthetic office days and forecasts Kamar Norður."""
    now = local_noon()
    history = office_history(pilot, now, days)
    entry, m, fake = await start(hass, pilot, freezer, history)
    await m.async_learn()
    await hass.async_block_till_done()
    assert m.status.state == "ready" and len(m.days) == days and m.room(pilot.area).status == "ok"
    return entry, m, fake, history


def app_view(pilot: Pilot, settings: dict[str, Any], *, probability: int = 73) -> dict[str, Any]:
    """A view as the App's forecast runtime would publish it (hand-built; clearly not a learned forecast)."""
    now = dt_util.utcnow()
    iso = now.isoformat(timespec="seconds")
    room = {
        "area_id": pilot.area, "status": "ok", "horizon": 15, "threshold": 60, "confidence_threshold": "moderate",
        "occupied": False, "state": "vacant", "probability": probability,
        "probabilities": {"5": 60, "15": probability, "30": 81, "60": 88}, "entry": None,
        "schedule": {"5": 30, "15": 40, "30": 45, "60": 50}, "predicted": True,
        "confidence": {"level": "high", "score": 90, "reasons": ["From the App"], "components": {"days": 1.0}},
        "explanation": "From the App", "factors": [{"kind": "schedule", "text": "Usually here at noon",
                                                    "impact": 0.4, "details": {}}],
        "window": {"kind": "occupied", "source": "forecast", "start": iso, "end": None,
                   "text": "Likely occupied within 5 min"},
        "next_rooms": [], "updated": iso,
    }
    hall = {**room, "area_id": pilot.hall, "probability": 10, "probabilities": {"5": 5, "15": 10, "30": 12, "60": 15},
            "predicted": False, "confidence": {"level": "moderate", "score": 50, "reasons": [], "components": {}},
            "window": None}
    return {"rooms": {pilot.area: room, pilot.hall: hall},
            "next_room": {"area_id": pilot.area, "probability": 0.64, "source": pilot.hall, "since": now.timestamp(),
                          "candidates": [[pilot.area, 0.64], [pilot.hall, 0.2]]},
            "status": {"state": "ready", "detail": "12 days learned (App)", "last_update": iso, "last_error": None},
            "days_learned": DAYS, "settings": settings}


def app_export(grant_export: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """The App's learned data: the integration's export less its oldest day (marked as a day without data)."""
    export = copy.deepcopy(grant_export)
    dropped = sorted(export["days"])[0]
    del export["days"][dropped]
    export["no_data"] = sorted({*export["no_data"], dropped})
    return export, dropped


def sensors(hass: HomeAssistant, pilot: Pilot) -> dict[str, Any]:
    return {"forecast": fstate(hass, "sensor", f"{DOMAIN}_room_{pilot.area}_forecast"),
            "predicted": fstate(hass, "binary_sensor", f"{DOMAIN}_room_{pilot.area}_predicted"),
            "confidence": fstate(hass, "sensor", f"{DOMAIN}_room_{pilot.area}_forecast_confidence"),
            "next": fstate(hass, "sensor", f"{DOMAIN}_predicted_next_room"),
            "learning": fstate(hass, "sensor", f"{DOMAIN}_forecast_learning")}


# ------------------------------------------------------------------ default and shadow
async def test_default_owner_and_shadow_copy(hass: HomeAssistant, pilot: Pilot, freezer, hass_ws_client,
                                             hass_admin_user, service_calls) -> None:
    entry, m, fake, _history = await learned(hass, pilot, freezer)
    bridge = bridge_of(entry)
    assert bridge.owner == S.OWNER_INTEGRATION and bridge.state() == S.OWNER_INTEGRATION and not m.dormant
    status = bridge.status()
    assert status["manager"] == "running" and status["days_learned"] == DAYS and status["recorder_oldest"] == fake.oldest()
    assert status["learned_revision"] == S.learned_revision(m.export_learning())
    app = FakeApp(hass, await admin_client(hass, hass_ws_client, hass_admin_user))
    hello = await app.connect()
    export = hello["export"]
    assert export == wire(m.export_learning())
    assert export["schema"] == S.EXPORT_SCHEMA == 1 and export["settings"] == m.settings.as_dict()
    assert set(export["days"]) == set(m.days) and len(export["days"]) == DAYS
    assert export["no_data"] == sorted(m.no_data) and export["recorder_oldest"] == m.status.recorder_oldest
    assert export["last_update"] == m.status.last_update and export["exported_at"]
    assert S.check_export(export) is export and set(export) <= S.EXPORT_SECTIONS
    view = hello["view"]
    assert view == wire(m.view()) and set(view) == S.VIEW_SECTIONS
    assert view["rooms"][pilot.area]["status"] == "ok" and view["days_learned"] == DAYS
    assert view["settings"] == m.settings.as_dict() and view["status"]["state"] == "ready"
    assert hello["settings_revision"] == S.revision(m.settings.as_dict()) == bridge.settings_revision()
    assert hello["settings"] == m.settings.as_dict() and hello["options"] == dict(entry.options)
    assert hello["ownership"]["owner"] == S.OWNER_INTEGRATION and hello["lease_token"] is None
    assert hello["recorder_oldest"] == fake.oldest()
    # Every recomputed view is streamed.
    hass.states.async_set(pilot.hall_pir, "on")
    await tick(hass, freezer, 3)
    events = await app.drain()
    views = [e["view"] for e in events if e["type"] == "view"]
    assert views and views[-1] == wire(m.view()) and views[-1]["next_room"]["area_id"] == pilot.area
    # And every settings change, with its revision.
    app.events.clear()
    await m.async_set_settings(m.settings.with_room(pilot.area, RoomForecastSettings(horizon_minutes=30)))
    events = await app.drain()
    settings = [e for e in events if e["type"] == "settings"][-1]
    assert settings["settings"] == m.settings.as_dict() and settings["settings"]["rooms"][pilot.area]["horizon_minutes"] == 30
    assert settings["settings_revision"] == S.revision(m.settings.as_dict()) == bridge.settings_revision()
    assert service_calls == []


async def test_restart_without_a_record_keeps_the_integration_in_control(hass: HomeAssistant, pilot: Pilot, freezer,
                                                                        service_calls) -> None:
    entry, m, fake, _history = await learned(hass, pilot, freezer)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    bridge, m2 = bridge_of(entry), mgr_of(entry)
    assert bridge.owner == S.OWNER_INTEGRATION and not bridge.resume_pending and bridge.epoch == 0
    assert not m2.dormant and m2._running and len(m2.days) == DAYS  # noqa: SLF001
    assert service_calls == []


# ------------------------------------------------------------------ handover
async def test_claim_refusals_and_a_handover_stops_learning(hass: HomeAssistant, pilot: Pilot, freezer,
                                                            hass_ws_client, hass_admin_user, service_calls) -> None:
    entry, m, fake, _history = await learned(hass, pilot, freezer)
    bridge = bridge_of(entry)
    other = FakeApp(hass, await admin_client(hass, hass_ws_client, hass_admin_user))
    await other.connect(fp="0" * 64)
    assert (await other.claim())["error"]["code"] == S.REJECT_VERSION
    app = FakeApp(hass, await admin_client(hass, hass_ws_client, hass_admin_user))
    await app.connect()
    assert (await app.claim(revision="0" * 16))["error"]["code"] == S.REJECT_CONFIG
    assert (await app.claim(ready=False))["error"]["code"] == S.REJECT_NOT_READY
    assert (await app.claim(epoch=99))["error"]["code"] == S.REJECT_BUSY
    assert bridge.owner == S.OWNER_INTEGRATION and not m.dormant and m._running  # noqa: SLF001
    before = {k: (s.state, dict(s.attributes)) for k, s in sensors(hass, pilot).items()}
    assert before["forecast"][0] not in ("unknown", "unavailable")
    calls = len(fake.calls)
    reply = await app.claim()
    assert reply["success"], reply
    grant = reply["result"]
    assert set(grant["export"]["days"]) == set(m.days) and len(grant["export"]["days"]) == DAYS
    assert grant["learned_revision"] == S.learned_revision(grant["export"]) == bridge.status()["learned_revision"]
    assert grant["view"]["rooms"][pilot.area]["status"] == "ok" and grant["lease_token"] == app.token
    assert m.dormant and not m._running  # noqa: SLF001
    status = bridge.status()
    assert status["owner"] == S.OWNER_APP and status["manager"] == "dormant" and status["epoch"] == 1
    assert status["history"][0]["reason"] == S.REASON_HANDOVER and status["owner_connected"]
    assert status["learned_at"] and bridge.mirror["export"] == grant["export"]
    # Dormant: the start-delay and refresh timers are gone, nothing is learned, nothing is recomputed.
    hass.states.async_set(pilot.hall_pir, "on")
    await alive(hass, freezer, app, FORECAST_START_DELAY_SECONDS + 60)
    assert len(fake.calls) == calls and len(m.days) == DAYS
    assert m.next_room is None  # the arrival was not evaluated by the dormant manager
    # The forecast entities keep their last values until the App publishes.
    after = {k: (s.state, dict(s.attributes)) for k, s in sensors(hass, pilot).items()}
    assert after == before
    assert service_calls == []


# ------------------------------------------------------------------ the owner publishes
async def test_published_view_and_learned_data_reach_the_integration(hass: HomeAssistant, pilot: Pilot, freezer,
                                                                    hass_ws_client, hass_admin_user, hass_storage,
                                                                    service_calls, caplog) -> None:
    entry, m, fake, _history = await learned(hass, pilot, freezer)
    bridge = bridge_of(entry)
    app = FakeApp(hass, await admin_client(hass, hass_ws_client, hass_admin_user))
    await app.connect()
    grant = (await app.claim())["result"]
    view = app_view(pilot, grant["export"]["settings"])
    reply = await app.publish(view=view)
    assert reply["success"], reply
    await hass.async_block_till_done()
    s = sensors(hass, pilot)
    assert s["forecast"].state == "73" and s["forecast"].attributes["explanation"] == "From the App"
    assert s["forecast"].attributes["confidence"] == "high" and s["forecast"].attributes["probability_30_min"] == 81
    assert s["forecast"].attributes["factors"] == ["Usually here at noon"]
    assert s["predicted"].state == "on" and s["confidence"].state == "high"
    assert s["next"].state == "Kamar Norður" and s["next"].attributes["from_area_id"] == pilot.hall
    assert s["next"].attributes["probability"] == 64 and s["next"].attributes["observation_only"] is True
    assert s["learning"].state == "ready" and m.status.detail == "12 days learned (App)"
    assert m.room(pilot.area).probability == 0.73 and m.room(pilot.hall).predicted is False
    assert bridge.published == 1 and bridge.mirror["view"] == view and bridge.status()["mirror_at"]
    # The read-only room view answers while dormant and shows what the App published.
    ws = await client(hass, hass_ws_client, hass_admin_user)
    reply = await ws(type=ROOM_WS, area_id=pilot.area)
    assert reply["success"], reply
    detail = reply["result"]
    assert detail["now"]["probability"] == 73 and detail["now"]["explanation"] == "From the App"
    assert detail["now"]["confidence"]["level"] == "high" and detail["patterns"]["days"] == DAYS
    # The App's learned data: adopted by the dormant manager (its read-only patterns follow) and kept in the mirror.
    export, dropped = app_export(grant["export"])
    assert (await app.publish(export=export))["success"]
    await hass.async_block_till_done()
    assert len(m.days) == DAYS - 1 and dropped not in m.days and dropped in m.no_data
    assert bridge.mirror["export"] == export and bridge.status()["days_learned"] == DAYS - 1
    assert bridge.status()["learned_revision"] == S.learned_revision(export)
    await bridge.async_flush()
    assert set(hass_storage[MIRROR_KEY]["data"]["export"]["days"]) == set(export["days"])
    assert hass_storage[MIRROR_KEY]["data"]["view"] == view
    assert set(hass_storage["smart_occupancy.forecast_days"]["data"]["days"]) == set(export["days"])
    assert (await ws(type=ROOM_WS, area_id=pilot.area))["result"]["patterns"]["days"] == DAYS - 1
    # Invalid publications change nothing.
    refused = {
        "view sections": (await app.publish(view={"bogus": 1}), S.REJECT_INVALID),
        "view rooms": (await app.publish(view={"rooms": []}), S.REJECT_INVALID),
        "view room": (await app.publish(view={"rooms": {pilot.area: 7}}), S.REJECT_INVALID),
        "view next_room": (await app.publish(view={"next_room": "x"}), S.REJECT_INVALID),
        "export schema": (await app.publish(export={**export, "schema": 2}), S.REJECT_VERSION),
        "export sections": (await app.publish(export={**export, "extra": 1}), S.REJECT_INVALID),
        "export days": (await app.publish(export={**export, "days": []}), S.REJECT_INVALID),
        "export settings": (await app.publish(export={**export, "settings": None}), S.REJECT_INVALID),
        "export no_data": (await app.publish(export={**export, "no_data": [1]}), S.REJECT_INVALID),
        "export oldest": (await app.publish(export={**export, "recorder_oldest": True}), S.REJECT_INVALID),
    }
    for what, (reply, code) in refused.items():
        assert not reply["success"] and reply["error"]["code"] == code, (what, reply)
    await hass.async_block_till_done()
    assert bridge.mirror["view"] == view and bridge.mirror["export"] == export and len(m.days) == DAYS - 1
    assert bridge.rejections[S.REJECT_INVALID] == 9 and bridge.rejections[S.REJECT_VERSION] == 1
    assert sensors(hass, pilot)["forecast"].state == "73"
    # The integration's forecast editors are read-only meanwhile; reading keeps working.
    settings_before = m.settings
    for msg in ({"type": SETTINGS_WS, "values": {"probability_threshold": 70}},
                {"type": SETTINGS_WS, "area_id": pilot.area, "values": {"horizon_minutes": 30}},
                {"type": CONNECTION_WS, "action": "add", "a": pilot.area, "b": pilot.hall, "kind": "open"},
                {"type": CONNECTION_WS, "action": "reset"},
                {"type": "smart_occupancy/forecast/learn"},
                {"type": "smart_occupancy/forecast/rebuild"}):
        reply = await ws(**msg)
        assert not reply["success"] and reply["error"]["code"] == "managed_by_app", (msg, reply)
        assert "Smart Lighting App" in reply["error"]["message"]
    assert m.settings == settings_before and len(m.days) == DAYS - 1 and len(fake.calls) == DAYS + 1
    assert (await ws(type=ROOM_WS, area_id=pilot.hall))["success"]
    assert service_calls == []


# ------------------------------------------------------------------ giving control back
@pytest.mark.parametrize("how", ["release", "return", "service", "lease", "new_instance", "restart"])
async def test_control_returns_with_the_apps_learned_days(hass: HomeAssistant, pilot: Pilot, freezer, hass_ws_client,
                                                          hass_admin_user, service_calls, how: str) -> None:
    entry, m, fake, _history = await learned(hass, pilot, freezer)
    app = FakeApp(hass, await admin_client(hass, hass_ws_client, hass_admin_user))
    await app.connect()
    grant = (await app.claim())["result"]
    export, dropped = app_export(grant["export"])
    view = app_view(pilot, export["settings"])
    assert (await app.publish(view=view, export=export))["success"]
    await hass.async_block_till_done()
    assert len(m.days) == DAYS - 1 and sensors(hass, pilot)["forecast"].state == "73"
    await alive(hass, freezer, app, 20)
    calls = len(fake.calls)
    if how == "release":
        reply = await app.release(view=view, export=export, detail="test")
        assert reply["success"] and reply["result"]["ownership"]["owner"] == S.OWNER_INTEGRATION
    elif how == "return":
        reply = await app.send(type="smart_occupancy/forecast/bridge/return", reason="test")
        assert reply["success"] and reply["result"]["returned"]
    elif how == "service":
        await hass.services.async_call(DOMAIN, "forecast_return_to_integration", {}, blocking=True,
                                       context=Context(user_id=hass_admin_user.id))
    elif how == "lease":
        await tick(hass, freezer, S.LEASE_SECONDS + 2)
    elif how == "new_instance":
        newcomer = FakeApp(hass, await admin_client(hass, hass_ws_client, hass_admin_user))
        hello = await newcomer.connect()
        assert hello["ownership"]["owner"] == S.OWNER_INTEGRATION and hello["lease_token"] is None
        assert set(hello["export"]["days"]) == set(export["days"])  # its copy is the restarted manager's
        assert hello["ownership"]["history"][0]["reason"] == S.REASON_APP_RESTARTED
    else:
        await bridge_of(entry).async_flush()
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        assert bridge_of(entry).resume_pending and mgr_of(entry).dormant
        assert sensors(hass, pilot)["forecast"].state == "73"  # the App's last view, until someone takes over
        mgr_of(entry).fetch_history, mgr_of(entry).oldest_history = fake, fake.oldest
        await tick(hass, freezer, S.RESUME_GRACE_SECONDS + 3, step=3)
        assert bridge_of(entry).history[-1]["reason"] == S.REASON_NOT_RESUMED
    await tick(hass, freezer, 2)
    bridge, m = bridge_of(entry), mgr_of(entry)
    assert bridge.owner == S.OWNER_INTEGRATION and bridge.history[-1]["owner"] == S.OWNER_INTEGRATION
    assert not m.dormant and m._running and bridge.status()["manager"] == "running"  # noqa: SLF001
    assert bridge.mirror.get("returned_at")
    assert len(m.days) == DAYS - 1 and dropped in m.no_data and dropped not in m.days  # the App's learned days
    assert m.settings.as_dict() == export["settings"]
    # It publishes its own view again (the App's hand-built one is gone).
    s = sensors(hass, pilot)
    assert s["forecast"].attributes["explanation"] != "From the App" and s["forecast"].state != "73"
    assert s["forecast"].state not in ("unknown", "unavailable") and s["confidence"].state != "high"
    assert m.room(pilot.area).explanation != "From the App"
    # The old App is fenced out (after a restart its socket is gone too: it comes back with its old credentials).
    if how == "restart":
        back = FakeApp(hass, await admin_client(hass, hass_ws_client, hass_admin_user), instance=app.instance)
        back.session, back.epoch, back.token, back.seq = app.session, app.epoch, app.token, app.seq
        app = back
    assert (await app.heartbeat())["error"]["code"] in S.LOST_CODES
    assert (await app.publish(view=view))["error"]["code"] in S.LOST_CODES
    assert sensors(hass, pilot)["forecast"].state != "73"
    # Nothing is retrained: the first learning run reads only today.
    assert len(fake.calls) == calls
    await tick(hass, freezer, FORECAST_START_DELAY_SECONDS + 5, step=5)
    assert len(fake.calls) == calls + 1 and fake.calls[-1][1].date() == dt_util.now().date()
    assert len(m.days) == DAYS - 1 and m.status.state == "ready"
    # No device was commanded anywhere (the admin service itself is the only call, on that path).
    assert [c.data["domain"] for c in service_calls] == ([DOMAIN] if how == "service" else [])


async def test_restart_keeps_the_manager_dormant_and_only_the_same_app_resumes(
        hass: HomeAssistant, pilot: Pilot, freezer, hass_ws_client, hass_admin_user, service_calls) -> None:
    entry, m, fake, _history = await learned(hass, pilot, freezer)
    app = FakeApp(hass, await admin_client(hass, hass_ws_client, hass_admin_user))
    await app.connect()
    grant = (await app.claim())["result"]
    view = app_view(pilot, grant["export"]["settings"])
    assert (await app.publish(view=view))["success"]
    token = app.token
    await bridge_of(entry).async_flush()
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    bridge, m2 = bridge_of(entry), mgr_of(entry)
    assert bridge.owner == S.OWNER_APP and bridge.resume_pending and bridge.state() == "resume_pending"
    assert m2.dormant and not m2._running and len(m2.days) == DAYS  # noqa: SLF001
    assert sensors(hass, pilot)["forecast"].state == "73"  # the App's published view is shown after the restart
    await tick(hass, freezer, 10)
    assert bridge.owner == S.OWNER_APP
    # A wrong token: revoked; the integration takes over.
    wrong = FakeApp(hass, await admin_client(hass, hass_ws_client, hass_admin_user), instance=app.instance)
    hello = await wrong.connect(resume_token="a" * 32)
    assert hello["lease_token"] is None and hello["ownership"]["owner"] == S.OWNER_INTEGRATION
    assert bridge.history[-1]["reason"] == S.REASON_REVOKED and not mgr_of(entry).dormant
    await hass.async_block_till_done()
    assert sensors(hass, pilot)["forecast"].state != "73"
    # The App takes over again, Home Assistant restarts, the same process resumes with its token.
    await wrong.client.close()
    await hass.async_block_till_done()
    app = FakeApp(hass, await admin_client(hass, hass_ws_client, hass_admin_user))
    await app.connect()
    assert (await app.claim())["success"]
    token = app.token
    await bridge_of(entry).async_flush()
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert bridge_of(entry).resume_pending
    again = FakeApp(hass, await admin_client(hass, hass_ws_client, hass_admin_user), instance=app.instance)
    hello = await again.connect(resume_token=token)
    assert hello["lease_token"] and hello["ownership"]["owner"] == S.OWNER_APP and "export" not in hello
    assert hello["ownership"]["history"][0]["reason"] == S.REASON_RESUMED
    assert mgr_of(entry).dormant and not bridge_of(entry).resume_pending
    assert (await again.publish(view=view))["success"]
    await hass.async_block_till_done()
    assert sensors(hass, pilot)["forecast"].state == "73"
    # A restart after which a different App instance connects: the integration takes over (app_restarted).
    await bridge_of(entry).async_flush()
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    stranger = FakeApp(hass, await admin_client(hass, hass_ws_client, hass_admin_user))
    hello = await stranger.connect(resume_token=token)
    assert hello["ownership"]["owner"] == S.OWNER_INTEGRATION
    assert bridge_of(entry).history[-1]["reason"] == S.REASON_APP_RESTARTED and not mgr_of(entry).dormant
    assert service_calls == []


# ------------------------------------------------------------------ parity of the export
async def test_export_round_trips_into_the_same_forecasts(hass: HomeAssistant, pilot: Pilot, freezer,
                                                          service_calls) -> None:
    entry, m, fake, history = await learned(hass, pilot, freezer)
    export = wire(m.export_learning())
    assert S.check_export(export) is export and S.learned_revision(export) == S.learned_revision(m.export_learning())
    # A fresh manager (the App's) imports the export and reads only today; with the same live occupancy at the
    # same moment it forecasts exactly the same.
    fresh = ForecastManager(hass, entry.runtime_data)
    fresh.decision_sink = lambda *args, **kwargs: None
    fake2 = FakeRecorder(history)
    fresh.fetch_history, fresh.oldest_history = fake2, fake2.oldest
    assert await fresh.async_import_learning(export, save=False) == DAYS
    assert sorted(fresh.days) == sorted(m.days) and fresh.settings == m.settings and fresh.no_data == m.no_data
    assert all(fresh.days[k].as_dict() == m.days[k].as_dict() for k in m.days)
    fresh.live = copy.deepcopy(m.live)
    fresh.arrivals = deque(m.arrivals, maxlen=200)
    await fresh.async_learn()
    assert len(fake2.calls) == 1 and fresh.status.state == "ready" and len(fresh.days) == DAYS
    m._recompute()  # noqa: SLF001 - the same moment
    assert fresh.view()["rooms"] == m.view()["rooms"]
    mine, theirs = m.room(pilot.area), fresh.room(pilot.area)
    assert mine.status == theirs.status == "ok"
    assert mine.probabilities == theirs.probabilities and mine.predicted == theirs.predicted
    assert mine.confidence == theirs.confidence and mine.explanation == theirs.explanation
    assert fresh.view()["next_room"] == m.view()["next_room"]
    assert S.revision(fresh.view()["rooms"]) == S.revision(m.view()["rooms"])
    # A published room forecast reads back as the entities need it.
    for area_id, rf in m.rooms.items():
        back = RoomForecast.from_dict(area_id, wire(rf.as_dict(m.name)))
        assert back is not None
        assert (back.area_id, back.status, back.predicted, back.horizon, back.threshold, back.confidence_threshold) == (
            area_id, rf.status, rf.predicted, rf.horizon, rf.threshold, rf.confidence_threshold)
        assert (back.explanation, back.window, back.occupied, back.state, back.updated) == (
            rf.explanation, rf.window, rf.occupied, rf.state, rf.updated)
        assert (back.confidence.level, back.confidence.score) == (rf.confidence.level, rf.confidence.score)
        assert back.confidence.reasons == rf.confidence.reasons
        assert round(back.probability * 100) == round(rf.probability * 100)
        for h in HORIZONS:
            assert round(back.probabilities[h] * 100) == round(rf.probabilities[h] * 100)
            assert round(back.schedule[h] * 100) == round(rf.schedule[h] * 100)
        assert [f.text for f in back.factors] == [f.text for f in rf.factors]
        assert back.fingerprint()[:4] == rf.fingerprint()[:4]
        assert back.as_dict(m.name) == wire(rf.as_dict(m.name))
    assert RoomForecast.from_dict(pilot.area, {"probability": 1}) is None  # no status: not a forecast
    assert RoomForecast.from_dict(pilot.area, "nope") is None
    assert service_calls == []


# ------------------------------------------------------------------ access and diagnostics
async def test_non_admins_cannot_use_the_bridge(hass: HomeAssistant, pilot: Pilot, freezer, hass_ws_client,
                                                hass_read_only_user, service_calls, caplog) -> None:
    caplog.set_level("CRITICAL", logger=WS_LOGGER)
    entry, m, fake, _history = await learned(hass, pilot, freezer)
    ws_client = await hass_ws_client(hass, access_token=hass.auth.async_create_access_token(
        await hass.auth.async_create_refresh_token(hass_read_only_user, "https://example.com/")))
    app = FakeApp(hass, ws_client)
    reply = await app.connect(expect_ok=False)
    assert not reply["success"] and reply["error"]["code"] == "unauthorized"
    for kind in ("status", "return"):
        reply = await app.send(type=f"smart_occupancy/forecast/bridge/{kind}")
        assert reply["error"]["code"] == "unauthorized", kind
    app.session, app.epoch, app.token = "0" * 32, 0, "0" * 32
    for kind in ("heartbeat", "publish", "release"):
        reply = await app.send(type=f"smart_occupancy/forecast/bridge/{kind}", **app.owner())
        assert reply["error"]["code"] == "unauthorized", kind
    reply = await app.send(type="smart_occupancy/forecast/bridge/claim", session_id="0" * 32, expected_epoch=0,
                           settings_revision="x", readiness=readiness())
    assert reply["error"]["code"] == "unauthorized"
    with pytest.raises(Unauthorized):
        await hass.services.async_call(DOMAIN, "forecast_return_to_integration", {}, blocking=True,
                                       context=Context(user_id=hass_read_only_user.id))
    assert bridge_of(entry).owner == S.OWNER_INTEGRATION and bridge_of(entry).sessions == {} and not m.dormant
    assert all(c.data["domain"] == DOMAIN for c in service_calls)  # at most the refused call itself; no device


async def test_diagnostics_include_the_ownership(hass: HomeAssistant, pilot: Pilot, freezer, hass_ws_client,
                                                 hass_admin_user, service_calls) -> None:
    entry, m, fake, _history = await learned(hass, pilot, freezer)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    own = diag["forecast_ownership"]
    assert own["owner"] == S.OWNER_INTEGRATION and own["manager"] == "running" and own["days_learned"] == DAYS
    assert diag["forecast"]["dormant"] is False
    app = FakeApp(hass, await admin_client(hass, hass_ws_client, hass_admin_user))
    await app.connect()
    assert (await app.claim())["success"]
    diag = await async_get_config_entry_diagnostics(hass, entry)
    own = diag["forecast_ownership"]
    assert own["owner"] == S.OWNER_APP and own["manager"] == "dormant" and own["epoch"] == 1
    assert own["owner_instance"] == app.instance[:8] and own["sessions"][0]["owner"] is True
    assert own["history"][0]["reason"] == S.REASON_HANDOVER and own["learned_at"]
    assert diag["forecast"]["dormant"] is True
    assert service_calls == []
