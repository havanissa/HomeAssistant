"""The integration's side of the occupancy bridge, simulated for the App tests (Phase A3.2).

Runs the shared engine (``occupancy_core.House``) on the fake Home Assistant's
states, like the integration does, and answers the bridge commands with the
integration's rules: ownership record with epoch, lease token, increasing
sequence numbers, configuration revision, fallback when a new App instance
connects. The real integration side is tested in the integration's suite
(``tests/test_engine_bridge.py``) against real Home Assistant.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import secrets
from typing import Any

from occupancy_core import CORE_VERSION, fingerprint
from occupancy_core import protocol as P
from occupancy_core.house import House, HouseSpec, Observation, RoomSpec
from occupancy_core.types import OccupancyTiming, SensorAssignment
from occupancy_core.wire import WireError, iso, result_from_wire, result_to_wire, spec_to_wire

FAST = {"correlation_window": 1, "exit_confirmation": 2, "inactivity_timeout": 30, "dropout_grace": 1,
        "trigger_stuck_after": 1800, "presence_stuck_after": 0, "startup_hold": 0}


def now() -> datetime:
    return datetime.now(UTC)


class FakeEngine:
    def __init__(self, fake: Any, timing: dict[str, int] | None = None) -> None:
        self.fake = fake
        self.timing = {**FAST, **(timing or {})}
        self.states: dict[str, Observation] = {}
        for s in fake.states:
            self.states[s["entity_id"]] = Observation(s["state"], datetime.fromisoformat(s["last_changed"]),
                                                      s["attributes"].get("friendly_name"))
        self.house = House(self.spec())
        self.house.load(None, now(), self.states.get, lambda e: (self.states.get(e) or Observation(None, None)).name)
        self.owner, self.epoch, self.token, self.seq = P.OWNER_INTEGRATION, 0, None, 0
        self.owner_instance: str | None = None
        self.owner_session: str | None = None
        self.sessions: dict[str, tuple[Any, int, str]] = {}  # session -> (ws, sub id, instance)
        self.accepted: dict[str, dict[str, Any]] = {}
        self.publishes: list[dict[str, Any]] = []
        self.heartbeats = 0
        self.fail_publish: str | None = None  # refuse the next publish with this code
        self.history: list[dict[str, Any]] = []
        self.timers: dict[str, asyncio.TimerHandle] = {}
        self.fingerprint = fingerprint()
        self.core_version = CORE_VERSION
        for area_id in self.house.spec.area_ids:
            self.house.evaluate(area_id, now())

    def spec(self) -> HouseSpec:
        rooms = []
        for area_id, room in self.fake.rooms_options.items():
            if not room.get("enabled"):
                continue
            sensors = tuple(SensorAssignment.from_dict(s) for s in room.get("sensors", []))
            timing = OccupancyTiming.from_dict({**self.timing, **(room.get("timing") or {})})
            rooms.append(RoomSpec(area_id, sensors, timing, tuple(room.get("adjacent_rooms") or [])))
        return HouseSpec(tuple(rooms))

    def status(self) -> dict[str, Any]:
        return {"protocol": P.PROTOCOL_VERSION, "integration_version": "0.7.0", "core_version": self.core_version,
                "fingerprint": self.fingerprint, "owner": self.owner, "state": self.owner, "epoch": self.epoch,
                "config_revision": self.house.spec.revision(), "lease_until": None,
                "history": list(reversed(self.history)), "sessions": len(self.sessions)}

    def wires(self) -> dict[str, Any]:
        return {a: result_to_wire(e.result, e.visit_confirmed) for a, e in self.house.engines.items() if e.result}

    async def broadcast(self, message: dict[str, Any]) -> None:
        for ws, sub_id, _ in list(self.sessions.values()):
            if not ws.closed:
                await ws.send_json({"id": sub_id, "type": "event", "event": message})

    def change_owner(self, owner: str, reason: str, instance: str | None = None, session: str | None = None) -> None:
        self.epoch += 1
        self.owner, self.seq = owner, 0
        self.owner_instance, self.owner_session = instance, session
        self.token = secrets.token_hex(16) if owner == P.OWNER_APP else None
        if owner == P.OWNER_INTEGRATION:
            self.accepted.clear()
        self.history.append({"epoch": self.epoch, "owner": owner, "reason": reason, "at": iso(now())})

    async def fence(self, reason: str = P.REASON_LEASE_EXPIRED) -> None:
        """The integration takes over (e.g. the lease expired) and tells every session."""
        self.change_owner(P.OWNER_INTEGRATION, reason)
        await self.broadcast({"type": "ownership", "ownership": self.status()})

    # ----------------------------------------------------------- events
    async def on_state(self, entity_id: str, value: str | None, changed: datetime) -> None:
        obs = Observation(value, changed, entity_id) if value is not None else None
        if obs:
            self.states[entity_id] = obs
        t = now()
        for area_id in self.house.observe(entity_id, obs, t):
            await self.evaluate(area_id, t)

    async def evaluate(self, area_id: str, t: datetime) -> None:
        engine = self.house.engines[area_id]
        before = result_to_wire(engine.result, engine.visit_confirmed) if engine.result else None
        self.house.evaluate(area_id, t)
        if handle := self.timers.pop(area_id, None):
            handle.cancel()
        deadline = self.house.next_deadline(area_id, t)
        if deadline is not None:
            loop = asyncio.get_running_loop()
            self.timers[area_id] = loop.call_later(
                max(0.0, (deadline - t).total_seconds()),
                lambda a=area_id: loop.create_task(self.evaluate(a, now())))
        wire = result_to_wire(engine.result, engine.visit_confirmed)
        if wire != before:
            await self.broadcast({"type": "result", "area_id": area_id, "at": iso(t), "result": wire})

    async def send_input(self, message: dict[str, Any]) -> None:
        await self.broadcast({"type": "input", "at": iso(now()), **message})

    def stop(self) -> None:
        for handle in self.timers.values():
            handle.cancel()

    # --------------------------------------------------------- commands
    async def answer(self, ws: Any, msg: dict[str, Any], result: Any, error: Any) -> None:
        kind = msg["type"].removeprefix("smart_occupancy/engine/")
        if kind == "status":
            await result(self.status())
        elif kind == "subscribe":
            if msg.get("protocol") != P.PROTOCOL_VERSION:
                await error(P.REJECT_VERSION, "protocol")
                return
            session = secrets.token_hex(16)
            lease = None
            if self.owner == P.OWNER_APP and msg["instance_id"] != self.owner_instance:
                self.change_owner(P.OWNER_INTEGRATION, P.REASON_APP_RESTARTED)
            elif self.owner == P.OWNER_APP:
                if msg.get("resume_token") == self.token:
                    self.change_owner(P.OWNER_APP, P.REASON_RESUMED, msg["instance_id"], session)
                    lease = self.token
                else:
                    self.change_owner(P.OWNER_INTEGRATION, P.REASON_REVOKED)
            self.sessions[session] = (ws, msg["id"], msg["instance_id"])
            await result(None)
            await ws.send_json({"id": msg["id"], "type": "event", "event": {
                "type": "hello", "session_id": session, "ownership": self.status(),
                "spec": spec_to_wire(self.house.spec), "state": self.house.export_state(), "results": self.wires(),
                "inputs": {"preliminary": {}, "holds": {}}, "lease_token": lease}})
        elif kind == "claim":
            if msg["session_id"] not in self.sessions:
                await error(P.REJECT_UNKNOWN_SESSION, "unknown session")
            elif self.owner != P.OWNER_INTEGRATION or msg["expected_epoch"] != self.epoch:
                await error(P.REJECT_BUSY, "busy")
            elif msg["config_revision"] != self.house.spec.revision():
                await error(P.REJECT_CONFIG, "config")
            elif not msg["readiness"].get("ready"):
                await error(P.REJECT_NOT_READY, "not ready")
            else:
                self.change_owner(P.OWNER_APP, P.REASON_HANDOVER, self.sessions[msg["session_id"]][2],
                                  msg["session_id"])
                self.history[-1]["readiness"] = msg["readiness"]
                await result({"epoch": self.epoch, "lease_token": self.token, "state": self.house.export_state(),
                              "results": self.wires(), "lease_until": iso(now() + timedelta(seconds=30)),
                              "config_revision": self.house.spec.revision()})
        elif kind in ("publish", "heartbeat"):
            code = self.check(msg)
            if code:
                await error(code, code)
                return
            if kind == "publish" and self.fail_publish:
                code, self.fail_publish = self.fail_publish, None
                await error(code, code)
                return
            self.seq = msg["seq"]
            if kind == "publish":
                try:
                    for area_id, wire in msg["results"].items():
                        result_from_wire(wire, self.house.spec.room(area_id))
                except (WireError, AttributeError):
                    await error(P.REJECT_INVALID, "invalid")
                    return
                self.accepted.update(msg["results"])
                self.publishes.append(msg)
            else:
                self.heartbeats += 1
            await result({"seq": self.seq, "lease_until": iso(now() + timedelta(seconds=30))})
        elif kind == "release":
            released = self.owner == P.OWNER_APP
            if released:
                self.change_owner(P.OWNER_INTEGRATION, P.REASON_RELEASE)
            await result({"released": released, "ownership": self.status()})
        else:
            await error("unknown_command", "unknown")

    def check(self, msg: dict[str, Any]) -> str | None:
        if msg["session_id"] not in self.sessions:
            return P.REJECT_UNKNOWN_SESSION
        if self.owner != P.OWNER_APP or msg["session_id"] != self.owner_session:
            return P.REJECT_NOT_OWNER
        if msg["epoch"] != self.epoch:
            return P.REJECT_STALE_EPOCH
        if msg["lease_token"] != self.token:
            return P.REJECT_LEASE
        if msg["seq"] <= self.seq:
            return P.REJECT_SEQUENCE
        if msg["config_revision"] != self.house.spec.revision():
            return P.REJECT_CONFIG
        return None
