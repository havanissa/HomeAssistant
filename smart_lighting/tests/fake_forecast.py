"""The integration's side of the forecast bridge, simulated for the App tests (Phase A5).

Answers the bridge commands with the integration's rules (ownership record with
epoch, lease token, increasing sequence numbers, the view and export validation
of ``forecast_sync``) and records everything the App sends. Its learned data are
three clearly synthetic days built with the forecast model's own replay code
(the same way the integration's forecast tests build theirs). No message of this
bridge can reach a device.
"""

from __future__ import annotations

import copy
from datetime import UTC, date, datetime, timedelta
import secrets
from typing import Any
from zoneinfo import ZoneInfo

from occupancy_core import fingerprint as core_fingerprint
from occupancy_core.types import OccupancyTiming, SensorAssignment, SensorKind, SensorRole
from smart_occupancy import forecast_sync as S
from smart_occupancy.forecast_history import (
    EvalSettings,
    ReplaySpec,
    RoomSpec,
    lookahead_seconds,
    replay,
    summarize,
    warmup_seconds,
)
from smart_occupancy.forecast_model import HouseDay, build_house_model
from smart_occupancy.forecast_settings import ForecastSettings
from smart_occupancy.runtime_manifest import fingerprint

AREA = "kamar_nordur"
RADAR = "binary_sensor.kamar_radar"
TZ = ZoneInfo("Atlantic/Faroe")  # the fake Home Assistant's time zone (conftest)
LEARNED_DAYS = 3


# -- synthetic histories (the integration's tests/forecast_helpers.py, on the vendored modules) -----------------
def day_start(d: date) -> float:
    return datetime(d.year, d.month, d.day, tzinfo=TZ).timestamp()


def daykey(ts: float) -> tuple[int, bool, int]:
    d = datetime.fromtimestamp(ts, TZ)
    return d.weekday(), d.weekday() < 5, d.hour


def radar(entity_id: str) -> SensorAssignment:
    return SensorAssignment(entity_id, SensorKind.RADAR, SensorRole.MAINTAIN)


def spec(*rooms: tuple[str, tuple[SensorAssignment, ...]], **kwargs: Any) -> ReplaySpec:
    return ReplaySpec(rooms={a: RoomSpec(a, s, OccupancyTiming()) for a, s in rooms}, **kwargs)


class Sim:
    """Builds a history: entity -> [(ts, state)]."""

    def __init__(self, entities: list[str], start: float) -> None:
        self.h: dict[str, list[tuple[float, str]]] = {e: [(start, "off")] for e in entities}

    def pulse(self, entity: str, at: float, hold: float = 60) -> None:
        self.h[entity] += [(at, "on"), (at + hold, "off")]

    def stay(self, entity: str, start: float, end: float, every: float = 240, hold: float = 60) -> None:
        t = start
        while t < end:
            self.pulse(entity, t, hold)
            t += every

    def history(self) -> dict[str, list[tuple[float, str]]]:
        return {e: sorted(v) for e, v in self.h.items()}


def learn(sp: ReplaySpec, history: dict[str, list[tuple[float, str]]], days: list[date]) -> list[HouseDay]:
    """Process the days like the runtime: each day scored with a model of the earlier days only."""
    out: list[HouseDay] = []
    for d in days:
        ds = day_start(d)
        rp = replay(sp, history, ds - warmup_seconds(), ds + 86400 + lookahead_seconds())
        model = build_house_model(out, sp.rooms, sp.context_keys, today=d.isoformat(), half_life_days=30,
                                 ordinal=lambda s: date.fromisoformat(s).toordinal()) if out else None
        out.append(summarize(sp, rp, date=d.isoformat(), dow=d.weekday(), workday=d.weekday() < 5, day_start=ds,
                             day_end=ds + 86400, daykey=daykey, model=model,
                             settings=EvalSettings(min_days=3, min_entries=5)))
    return out


def synthetic_days(today: date | None = None) -> list[HouseDay]:
    """The last three completed days before today: evening visits to Kamar Norður, nothing else."""
    today = today or datetime.now(TZ).date()
    dates = [today - timedelta(days=n) for n in range(LEARNED_DAYS, 0, -1)]
    sp = spec((AREA, (radar(RADAR),)))
    sim = Sim([RADAR], day_start(dates[0]) - 86400)
    for d in dates:
        ds = day_start(d)
        sim.stay(RADAR, ds + 19 * 3600, ds + 22 * 3600)
    return learn(sp, sim.history(), dates)


class FakeForecast:
    def __init__(self, fake: Any) -> None:
        self.fake = fake
        self.owner, self.epoch, self.token, self.seq = S.OWNER_INTEGRATION, 0, None, 0
        self.owner_instance: str | None = None
        self.owner_session: str | None = None
        self.sessions: dict[str, tuple[Any, int, str]] = {}  # session -> (ws, sub id, instance)
        self.settings = ForecastSettings().as_dict()
        self.days = {d.date: d.as_dict() for d in synthetic_days()}
        self.recorder_oldest = day_start(date.fromisoformat(min(self.days)))
        self.view: dict[str, Any] = {
            "rooms": {AREA: {"area_id": AREA, "name": "Kamar Norður", "status": "ok", "probability": 40,
                             "predicted": False, "horizon": 15, "threshold": 60,
                             "confidence": {"level": "moderate", "score": 55}}},
            "next_room": None, "status": {"state": "ready", "detail": f"{len(self.days)} days learned"},
            "days_learned": len(self.days), "settings": copy.deepcopy(self.settings)}
        self.published: list[dict[str, Any]] = []
        self.released = 0
        self.refuse_publish: str | None = None
        self.fingerprint = fingerprint()
        self.core_fingerprint = core_fingerprint()
        self.history: list[dict[str, Any]] = []

    # -- what the integration shows ---------------------------------------------------------------------------
    def export(self) -> dict[str, Any]:
        return {"schema": S.EXPORT_SCHEMA, "settings": copy.deepcopy(self.settings),
                "days": copy.deepcopy(self.days), "no_data": [], "recorder_oldest": self.recorder_oldest,
                "last_update": "2026-11-17T10:00:00+00:00", "connections": [],
                "exported_at": datetime.now(UTC).isoformat(timespec="seconds")}

    def learned_revision(self) -> str:
        return S.learned_revision({"days": self.days, "no_data": []})

    def options(self) -> dict[str, Any]:
        return {"config_version": 2, "operating_mode": "observe", "mood": {}, "categories": [],
                "rooms": copy.deepcopy(self.fake.rooms_options), "global_profiles": {},
                "occupancy": dict(self.fake.global_timing)}

    def status(self) -> dict[str, Any]:
        state = S.OWNER_APP if self.owner == S.OWNER_APP else S.OWNER_INTEGRATION
        return {"owner": self.owner, "state": state, "epoch": self.epoch, "fingerprint": self.fingerprint,
                "core_fingerprint": self.core_fingerprint, "settings_revision": S.revision(self.settings),
                "days_learned": len(self.days), "learned_revision": self.learned_revision(),
                "recorder_available": True, "recorder_oldest": self.recorder_oldest,
                "learning": {"state": "ready", "detail": f"{len(self.days)} days learned"},
                "manager": "dormant" if self.owner == S.OWNER_APP else "running", "published": len(self.published),
                "history": list(self.history)}

    def _change(self, owner: str, reason: str, *, session: str | None = None, instance: str | None = None) -> None:
        self.epoch += 1
        self.owner, self.seq = owner, 0
        self.owner_session, self.owner_instance = (session, instance) if owner == S.OWNER_APP else (None, None)
        self.history.insert(0, {"epoch": self.epoch, "owner": owner, "reason": reason})

    async def broadcast(self, message: dict[str, Any]) -> None:
        for ws, sub, _instance in list(self.sessions.values()):
            if not ws.closed:
                await ws.send_json({"id": sub, "type": "event", "event": message})

    async def send_view(self, view: dict[str, Any] | None = None) -> None:
        """The integration recomputed its forecasts: every session gets the ``view`` event."""
        self.view = S.check_view(view or self.view)
        await self.broadcast({"type": "view", "view": self.view, "fired": datetime.now(UTC).isoformat()})

    async def set_settings(self, settings: dict[str, Any]) -> None:
        """A person changed the forecast settings in the integration: every session gets the ``settings`` event."""
        self.settings = ForecastSettings.from_dict(settings).as_dict()
        await self.broadcast({"type": "settings", "settings": self.settings,
                              "settings_revision": S.revision(self.settings)})

    def _owner_check(self, msg: dict[str, Any]) -> str | None:
        if self.owner != S.OWNER_APP or msg.get("session_id") != self.owner_session:
            return S.REJECT_NOT_OWNER
        if msg.get("epoch") != self.epoch:
            return S.REJECT_STALE_EPOCH
        if msg.get("lease_token") != self.token:
            return S.REJECT_LEASE
        if msg.get("seq", 0) <= self.seq:
            return S.REJECT_SEQUENCE
        self.seq = msg["seq"]
        return None

    def _take(self, msg: dict[str, Any]) -> None:
        """Validate and keep a published view / export (the integration's mirror)."""
        if msg.get("view") is not None:
            self.view = S.check_view(msg["view"])
        if msg.get("export") is not None:
            export = S.check_export(msg["export"])
            self.settings = copy.deepcopy(export["settings"])
            self.days = copy.deepcopy(export["days"])

    async def answer(self, ws: Any, msg: dict[str, Any], result: Any, error: Any) -> None:
        kind = msg["type"].rsplit("/", 1)[-1]
        if kind == "subscribe":
            session = secrets.token_hex(16)
            lease = None
            if self.owner == S.OWNER_APP and msg["instance_id"] != self.owner_instance:
                self._change(S.OWNER_INTEGRATION, S.REASON_APP_RESTARTED)
            elif self.owner == S.OWNER_APP and msg.get("resume_token") == self.token:
                lease = self.token = secrets.token_hex(16)
                self._change(S.OWNER_APP, S.REASON_RESUMED, session=session, instance=msg["instance_id"])
            elif self.owner == S.OWNER_APP:
                self._change(S.OWNER_INTEGRATION, S.REASON_REVOKED)
            self.sessions[session] = (ws, msg["id"], msg["instance_id"])
            await result(None)
            hello = {"type": "hello", "session_id": session, "ownership": self.status(), "options": self.options(),
                     "lease_token": lease, "settings": copy.deepcopy(self.settings),
                     "settings_revision": S.revision(self.settings), "recorder_oldest": self.recorder_oldest}
            if self.owner == S.OWNER_INTEGRATION:
                hello |= {"export": self.export(), "view": copy.deepcopy(self.view)}
            await ws.send_json({"id": msg["id"], "type": "event", "event": hello})
        elif kind == "status":
            await result(self.status())
        elif kind == "claim":
            if self.owner != S.OWNER_INTEGRATION or msg["expected_epoch"] != self.epoch:
                await error(S.REJECT_BUSY, "busy")
            elif msg["settings_revision"] != S.revision(self.settings):
                await error(S.REJECT_CONFIG, "config")
            elif not msg["readiness"].get("ready"):
                await error(S.REJECT_NOT_READY, "not ready")
            else:
                self.token = secrets.token_hex(16)
                self._change(S.OWNER_APP, S.REASON_HANDOVER, session=msg["session_id"],
                             instance=self.sessions[msg["session_id"]][2])
                export = self.export()
                await result({"epoch": self.epoch, "lease_token": self.token, "lease_until": None, "export": export,
                              "learned_revision": S.learned_revision(export), "view": copy.deepcopy(self.view)})
                await self.broadcast({"type": "ownership", "ownership": self.status()})
        elif kind in ("heartbeat", "publish"):
            if code := self._owner_check(msg):
                await error(code, code)
                return
            if kind == "publish":
                if self.refuse_publish:
                    code, self.refuse_publish = self.refuse_publish, None
                    await error(code, code)
                    return
                try:
                    self._take(msg)
                except S.ProtocolError as err:
                    await error(err.code, str(err))
                    return
                self.published.append(msg)
            await result({"seq": self.seq, "lease_until": datetime.now(UTC).isoformat()})
        elif kind == "release":
            if code := self._owner_check(msg):
                await error(code, code)
                return
            try:
                self._take(msg)
            except S.ProtocolError as err:
                await error(err.code, str(err))
                return
            self.released += 1
            self._change(S.OWNER_INTEGRATION, S.REASON_RELEASE)
            await result({"ownership": self.status()})
            await self.broadcast({"type": "ownership", "ownership": self.status()})
        elif kind == "return":
            returned = self.owner == S.OWNER_APP
            if returned:
                self._change(S.OWNER_INTEGRATION, S.REASON_SERVICE)
                await self.broadcast({"type": "ownership", "ownership": self.status()})
            await result({"returned": returned, "ownership": self.status()})
        else:
            await error("unknown_command", msg["type"])

    async def take_back(self, reason: str = S.REASON_LEASE_EXPIRED) -> None:
        """The integration produces the forecasts again on its own (lease expiry)."""
        self._change(S.OWNER_INTEGRATION, reason)
        await self.broadcast({"type": "ownership", "ownership": self.status()})
