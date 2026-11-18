"""The integration's side of the curtain bridge, simulated for the App tests (Phase A5).

Answers the bridge commands with the integration's rules (ownership record with
epoch, lease token, increasing sequence numbers, the cover command allowlist of
``curtain_sync.check_cover_command`` for the configured covers) and records
everything the App sends. An accepted ``execute`` moves the fake cover: it
reports "opening" / "closing" and then the final position with the App's
context, like a real device. The real integration side is tested in the
integration's suite against real Home Assistant.
"""

from __future__ import annotations

from datetime import UTC, datetime
import secrets
from typing import Any

from occupancy_core import fingerprint as core_fingerprint
from smart_occupancy import curtain_sync as S
from smart_occupancy.curtains.config import CurtainConfig
from smart_occupancy.runtime_manifest import fingerprint

AREA = "kamar_nordur"
COVER = "cover.kamar_curtain"
WINDOW = "w_kamar"
LAYER = "l_kamar"
PRESETS = {"p_open": ("Fully open", 100), "p_half": ("Half", 50), "p_closed": ("Closed", 0)}


def curtain_doc(mode: str = "observing", **window: Any) -> dict[str, Any]:
    """The integration's curtain configuration: one window "Kamar" with one blackout cover and three presets."""
    config = CurtainConfig.from_dict({
        "centralized": True,
        "windows": [{
            "window_id": WINDOW, "name": "Kamar", "area_id": AREA, "mode": mode,
            "layers": [{"layer_id": LAYER, "entity_id": COVER, "kind": "blackout"}],
            "presets": [{"preset_id": pid, "name": name, "positions": {LAYER: position}}
                        for pid, (name, position) in PRESETS.items()],
            **window,
        }],
    })
    return config.as_dict()


def cover_state(value: str, position: int) -> dict[str, Any]:
    return {"entity_id": COVER, "state": value,
            "attributes": {"friendly_name": "Curtain Kamar", "supported_features": 15, "current_position": position},
            "last_changed": "2026-11-17T10:00:00+00:00", "last_updated": "2026-11-17T10:00:00+00:00",
            "context": {"id": "x", "parent_id": None, "user_id": None}}


class FakeCurtains:
    def __init__(self, fake: Any) -> None:
        self.fake = fake
        self.owner, self.epoch, self.token, self.seq = S.OWNER_INTEGRATION, 0, None, 0
        self.owner_instance: str | None = None
        self.owner_session: str | None = None
        self.sessions: dict[str, tuple[Any, int, str]] = {}  # session -> (ws, sub id, instance)
        self.config = curtain_doc()
        self.state: dict[str, Any] = {}
        self.moving: list[str] = []  # windows whose covers the integration is moving (a claim is refused)
        self.executed: list[dict[str, Any]] = []
        self.published: list[dict[str, Any]] = []
        self.released = 0
        self.refuse_execute: str | None = None
        self.fingerprint = fingerprint()
        self.core_fingerprint = core_fingerprint()
        self.history: list[dict[str, Any]] = []
        self.position = 100
        # The cover reports its position (a positionable blackout curtain, fully open).
        fake.states = [s for s in fake.states if s["entity_id"] != COVER] + [cover_state("open", self.position)]

    def status(self) -> dict[str, Any]:
        state = S.OWNER_APP if self.owner == S.OWNER_APP else S.OWNER_INTEGRATION
        return {"owner": self.owner, "state": state, "epoch": self.epoch, "fingerprint": self.fingerprint,
                "core_fingerprint": self.core_fingerprint, "config_revision": S.config_revision(self.config),
                "manager": "dormant" if self.owner == S.OWNER_APP else "running", "moving": list(self.moving),
                "windows": len(self.config.get("windows") or []), "executed": len(self.executed),
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

    async def set_config(self, config: dict[str, Any], fired: str | None = None) -> None:
        """The integration's configuration changed (an editor): every session gets the ``config`` event."""
        self.config = CurtainConfig.from_dict(config).as_dict()
        await self.broadcast({"type": "config", "config": self.config, "config_revision": S.config_revision(self.config),
                              "fired": fired or datetime.now(UTC).isoformat()})

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
            hello = {"type": "hello", "session_id": session, "ownership": self.status(), "config": self.config,
                     "config_revision": S.config_revision(self.config), "lease_token": lease}
            if lease is not None:
                hello["renames"] = []
            if self.owner == S.OWNER_INTEGRATION:
                hello |= {"state": self.state, "running": True}
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
            elif self.moving:
                await error(S.REJECT_BUSY, "a cover is still being moved (" + ", ".join(self.moving) + ")")
            else:
                self.token = secrets.token_hex(16)
                self._change(S.OWNER_APP, S.REASON_HANDOVER, session=msg["session_id"],
                             instance=self.sessions[msg["session_id"]][2])
                await result({"epoch": self.epoch, "lease_token": self.token, "lease_until": None,
                              "config": self.config, "config_revision": S.config_revision(self.config),
                              "state": self.state})
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
                ids = S.check_cover_command(msg["domain"], msg["service"], msg["data"], covers=S.covers_of(self.config))
            except S.ProtocolError as err:
                await error(err.code, str(err))
                return
            if not S.valid_context_id(msg.get("context_id")):
                await error(S.REJECT_INVALID, "invalid context id")
                return
            self.executed.append(msg)
            await result({"seq": self.seq})
            await self._move(ids[0], msg["service"], msg["data"].get("position"), msg["context_id"])
        elif kind in ("heartbeat", "publish"):
            if code := self._owner_check(msg):
                await error(code, code)
                return
            if kind == "publish":
                if msg.get("config") is not None:
                    self.config = CurtainConfig.from_dict(msg["config"]).as_dict()
                if msg.get("state") is not None:
                    try:
                        self.state = S.check_state(msg["state"])
                    except S.ProtocolError as err:
                        await error(err.code, str(err))
                        return
                self.published.append(msg)
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

    async def _move(self, entity_id: str, service: str, position: Any, context_id: str) -> None:
        """The cover travels and reports, with the command's context (a local device)."""
        target = {"open_cover": 100, "close_cover": 0}.get(service, position)
        target = self.position if target is None else int(target)
        attrs = {"friendly_name": "Curtain Kamar", "supported_features": 15}
        if target != self.position:
            await self.fake.emit_live_context(entity_id, "opening" if target > self.position else "closing",
                                              {**attrs, "current_position": self.position}, context_id)
        self.position = target
        await self.fake.emit_live_context(entity_id, "open" if target > 0 else "closed",
                                          {**attrs, "current_position": target}, context_id)

    async def take_back(self, reason: str = S.REASON_LEASE_EXPIRED) -> None:
        """The integration takes the curtains back on its own (lease expiry)."""
        self._change(S.OWNER_INTEGRATION, reason)
        await self.broadcast({"type": "ownership", "ownership": self.status()})
