"""The integration's side of the lighting bridge, simulated for the App tests (Phase A4).

Answers the bridge commands with the integration's rules (ownership record with
epoch, hashed lease token, increasing sequence numbers, the command allowlist of
``lighting_sync.check_command``) and records everything the App sends. An
accepted ``execute`` changes the fake light state with the App's context, like a
local device. The real integration side is tested in the integration's suite
(``tests/test_lighting_bridge.py``) against real Home Assistant.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime
import secrets
from typing import Any

from occupancy_core import fingerprint as core_fingerprint
from smart_occupancy import lighting_sync as S
from smart_occupancy.lighting_config import LightingConfig
from smart_occupancy.runtime_manifest import fingerprint

AREA = "kamar_nordur"
LAMP = "light.kamar_lamp"
MAIN = "input_select.main_mood"
MOODS = ["Dagur", "Hygge"]


def lighting_doc() -> dict[str, Any]:
    """The integration's lighting configuration: Kamar Norður automatic, the lamp per mood."""
    config = LightingConfig.from_dict({
        "rooms": {AREA: {"control_enabled": True, "daylight": {"auto_on": "always"},
                         "light_overrides": {"Dagur": {LAMP: {"action": "on", "brightness_pct": 60}},
                                             "Hygge": {LAMP: {"action": "on", "brightness_pct": 25}}}}},
    })
    return config.as_dict()


class FakeLighting:
    def __init__(self, fake: Any) -> None:
        self.fake = fake
        self.owner, self.epoch, self.token, self.seq = S.OWNER_INTEGRATION, 0, None, 0
        self.owner_instance: str | None = None
        self.owner_session: str | None = None
        self.sessions: dict[str, tuple[Any, int, str]] = {}  # session -> (ws, sub id, instance)
        self.config = lighting_doc()
        self.state: dict[str, Any] = {}
        self.executed: list[dict[str, Any]] = []
        self.published: list[dict[str, Any]] = []
        self.inputs: list[dict[str, Any]] = []
        self.options_changes: list[dict[str, Any]] = []
        self.released = 0
        self.refuse_execute: str | None = None
        self.fingerprint = fingerprint()
        self.core_fingerprint = core_fingerprint()
        self.history: list[dict[str, Any]] = []
        fake.states += [
            {"entity_id": MAIN, "state": "Dagur", "attributes": {"friendly_name": "Main Mood", "options": MOODS},
             "last_changed": "2026-11-17T10:00:00+00:00", "last_updated": "2026-11-17T10:00:00+00:00",
             "context": {"id": "x", "parent_id": None, "user_id": None}},
        ]
        room = fake.rooms_options["kamar_nordur"]
        room["lights"] = [*room.get("lights", []), LAMP]

    def options(self) -> dict[str, Any]:
        return {"config_version": 2, "operating_mode": "observe", "mood": {"main_mood_entity": MAIN},
                "categories": [], "rooms": copy.deepcopy(self.fake.rooms_options), "global_profiles": {},
                "occupancy": dict(self.fake.global_timing)}

    def status(self) -> dict[str, Any]:
        state = S.OWNER_APP if self.owner == S.OWNER_APP else S.OWNER_INTEGRATION
        return {"owner": self.owner, "state": state, "epoch": self.epoch, "fingerprint": self.fingerprint,
                "core_fingerprint": self.core_fingerprint, "config_revision": S.config_revision(self.config),
                "controller": "dormant" if self.owner == S.OWNER_APP else "running", "history": list(self.history)}

    def _change(self, owner: str, reason: str, *, session: str | None = None, instance: str | None = None) -> None:
        self.epoch += 1
        self.owner, self.seq = owner, 0
        self.owner_session, self.owner_instance = (session, instance) if owner == S.OWNER_APP else (None, None)
        self.history.insert(0, {"epoch": self.epoch, "owner": owner, "reason": reason})

    async def broadcast(self, message: dict[str, Any]) -> None:
        for ws, sub, _instance in list(self.sessions.values()):
            if not ws.closed:
                await ws.send_json({"id": sub, "type": "event", "event": message})

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
                     "config": self.config, "config_revision": S.config_revision(self.config),
                     "lease_token": lease}
            if self.owner == S.OWNER_INTEGRATION:
                hello |= {"state": self.state, "recent": {"contexts": []}, "running": True}
            await ws.send_json({"id": msg["id"], "type": "event", "event": hello})
        elif kind == "status":
            await result(self.status())
        elif kind == "claim":
            if self.owner != S.OWNER_INTEGRATION or msg["expected_epoch"] != self.epoch:
                await error(S.REJECT_BUSY, "busy")
            elif msg["config_revision"] != S.config_revision(self.config):
                await error(S.REJECT_CONFIG, "config")
            elif not msg["readiness"].get("ready"):
                await error(S.REJECT_NOT_READY, "not ready")
            else:
                self.token = secrets.token_hex(16)
                self._change(S.OWNER_APP, S.REASON_HANDOVER, session=msg["session_id"],
                             instance=self.sessions[msg["session_id"]][2])
                await result({"epoch": self.epoch, "lease_token": self.token, "lease_until": None,
                              "config": self.config, "config_revision": S.config_revision(self.config),
                              "state": self.state, "recent": {"contexts": []}})
                await self.broadcast({"type": "ownership", "ownership": self.status()})
        elif kind == "execute":
            if code := self._owner_check(msg):
                await error(code, code)
                return
            if self.refuse_execute:
                code, self.refuse_execute = self.refuse_execute, None
                await error(code, code)
                return
            try:
                ids = S.check_command(msg["domain"], msg["service"], msg["data"], lights={LAMP}, doc=self.config)
            except S.ProtocolError as err:
                await error(err.code, str(err))
                return
            self.executed.append(msg)
            await result({"seq": self.seq})
            for entity_id in ids:
                attrs = {"friendly_name": "Lamp Kamar", "supported_color_modes": ["color_temp"]}
                on = msg["service"] != "turn_off"
                if on and "brightness_pct" in msg["data"]:
                    attrs["brightness"] = round(msg["data"]["brightness_pct"] * 255 / 100)
                await self.fake.emit_live_context(entity_id, "on" if on else "off", attrs, msg["context_id"])
        elif kind in ("heartbeat", "publish", "inputs", "options"):
            if code := self._owner_check(msg):
                await error(code, code)
                return
            {"publish": self.published, "inputs": self.inputs, "options": self.options_changes}.get(
                kind, []).append(msg)
            if kind == "publish" and msg.get("config"):
                self.config = msg["config"]
            if kind == "publish" and msg.get("state"):
                self.state = msg["state"]
            await result({"seq": self.seq, "lease_until": datetime.now(UTC).isoformat()})
        elif kind == "release":
            if code := self._owner_check(msg):
                await error(code, code)
                return
            self.config, self.state = msg.get("config") or self.config, msg.get("state") or self.state
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
        """The integration takes lighting back on its own (lease expiry)."""
        self._change(S.OWNER_INTEGRATION, reason)
        await self.broadcast({"type": "ownership", "ownership": self.status()})
