"""The companion integration's side of the companion protocol (Phase A6), simulated for the App tests.

Mirrors ``custom_components/smart_occupancy/companion.py`` closely enough for the App's runtimes, the migration
wizard and the setup to be exercised against it: sessions with a hello, pairing (a token the companion issues
and later checks), claims with an epoch and a lease, strictly increasing sequence numbers, the fenced executor
(the shared protocol module's allowlist, the published targets), published views, release and revoke, legacy
status / export / retire / restore, relayed card actions and renames. The real companion is tested against a real
Home Assistant in ``tests/companion``; the protocol module is byte-identical on both sides.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
import secrets
from typing import Any

from smart_lighting import companion_protocol as P

COMPANION_VERSION = "1.0.0"


def now() -> datetime:
    return datetime.now(UTC)


def iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value else None


class Record:
    def __init__(self, domain: str) -> None:
        self.domain = domain
        self.owner = P.OWNER_NONE
        self.epoch = 0
        self.session: str | None = None
        self.lease_token: str | None = None
        self.lease_until: datetime | None = None
        self.seq = 0
        self.config_revision: str | None = None
        self.targets: dict[str, list[str]] = {}
        self.executed = 0
        self.execute_errors = 0
        self.rejections: dict[str, int] = {}
        self.published_at: datetime | None = None
        self.history: list[dict[str, Any]] = []

    def state(self, sessions: dict[str, Any]) -> str:
        if self.owner != P.OWNER_APP:
            return P.STATE_NONE
        if self.session in sessions and self.lease_until and self.lease_until > now():
            return P.STATE_APP
        return P.STATE_APP_AWAY

    def as_dict(self, sessions: dict[str, Any]) -> dict[str, Any]:
        return {"owner": self.owner, "state": self.state(sessions), "epoch": self.epoch,
                "connected": self.session in sessions, "lease_valid": bool(self.lease_until and self.lease_until > now()),
                "lease_until": iso(self.lease_until), "seq": self.seq, "config_revision": self.config_revision,
                "targets": self.targets, "executed": self.executed, "execute_errors": self.execute_errors,
                "rejections": dict(self.rejections), "published_at": iso(self.published_at),
                "history": list(reversed(self.history[-10:]))}


class FakeCompanion:
    def __init__(self, fake: Any) -> None:
        self.fake = fake
        fake.integration_version = COMPANION_VERSION
        self.records = {d: Record(d) for d in P.DOMAINS}
        self.sessions: dict[str, dict[str, Any]] = {}  # session -> {"ws", "sub", "instance", "trusted"}
        self.pairing_hash: str | None = None
        self.boot_id = secrets.token_hex(8)
        self.views: dict[str, Any] = {}
        self.views_at: dict[str, str] = {}
        self.executed: list[dict[str, Any]] = []
        self.publishes: list[dict[str, Any]] = []
        self.actions: dict[str, Any] = {}
        self.action_results: list[dict[str, Any]] = []
        self.renames_applied = 0
        self.rechecks = 0
        self.fail_execute: str | None = None  # refuse the next execute with this code
        self.legacy_options: dict[str, Any] | None = copy.deepcopy(fake.legacy_options) if getattr(fake, "legacy_options", None) else None
        self.legacy_stores: dict[str, Any] = {"smart_occupancy.lighting": {"version": 3, "data": {"rooms": {}}}}
        self.retired: dict[str, Any] | None = None
        self.backup: dict[str, Any] | None = None
        self.renames: list[list[str]] = []

    # ---------------------------------------------------------------- helpers
    @property
    def paired(self) -> bool:
        return self.pairing_hash is not None

    def ownership(self) -> dict[str, Any]:
        return {d: r.as_dict(self.sessions) for d, r in self.records.items()}

    def legacy_summary(self) -> dict[str, Any]:
        options = self.legacy_options
        return {"present": options is not None, "retired": self.retired,
                "options_revision": P.revision(options) if options is not None else None,
                "rooms": len((options or {}).get("rooms") or {}), "entry_minor_version": 3}

    def entity_map(self) -> dict[str, str]:
        out = {"status": "sensor.smart_occupancy_lighting_status", "operating_mode": "sensor.smart_occupancy_lighting_operating_mode"}
        rooms = self.views.get("system", {}).get("rooms", {}) if self.views.get("system") else {}
        for area in rooms:
            for key in P.OCCUPANCY_ENTITY_KEYS:
                out[f"smart_occupancy_room_{area}_{key}"] = f"sensor.smart_occupancy_{area}_{key}"
        return out

    def hello(self, session: str) -> dict[str, Any]:
        return {"type": "hello", "session_id": session, "companion_version": COMPANION_VERSION,
                "protocol": P.PROTOCOL_VERSION, "ha_version": "2026.11.1", "boot_id": self.boot_id,
                "paired": self.paired, "trusted": self.sessions[session]["trusted"], "ownership": self.ownership(),
                "renames": list(self.renames), "entities": self.entity_map(),
                "rooms": (self.views.get("system") or {}).get("rooms") or {}, "views_at": dict(self.views_at),
                "recorder_oldest": None, "legacy": self.legacy_summary(), "entry_id": "abc"}

    async def broadcast(self, message: dict[str, Any]) -> None:
        for s in list(self.sessions.values()):
            if not s["ws"].closed:
                await s["ws"].send_json({"id": s["sub"], "type": "event", "event": message})

    async def send(self, session: str, message: dict[str, Any]) -> None:
        s = self.sessions.get(session)
        if s and not s["ws"].closed:
            await s["ws"].send_json({"id": s["sub"], "type": "event", "event": message})

    def close_ws(self, ws: Any) -> None:
        for session, s in list(self.sessions.items()):
            if s["ws"] is ws:
                self.sessions.pop(session)
                for r in self.records.values():
                    if r.session == session:
                        r.session = None  # the owner is away: the record keeps the owner
                        r.history.append({"epoch": r.epoch, "owner": r.owner, "reason": P.REASON_SESSION_CLOSED,
                                          "at": iso(now()), "by": None})

    def _session(self, msg: dict[str, Any], ws: Any) -> dict[str, Any] | None:
        s = self.sessions.get(str(msg.get("session_id")))
        return s if s is not None and s["ws"] is ws else None

    def _owner_check(self, msg: dict[str, Any], s: dict[str, Any], record: Record) -> str | None:
        if record.owner != P.OWNER_APP or record.session != msg["session_id"]:
            return P.REJECT_NOT_OWNER
        if msg["epoch"] != record.epoch:
            return P.REJECT_STALE_EPOCH
        if msg["lease_token"] != record.lease_token or not record.lease_until or record.lease_until < now():
            return P.REJECT_LEASE
        if not isinstance(msg["seq"], int) or msg["seq"] <= record.seq:
            return P.REJECT_SEQUENCE
        return None

    def _change(self, record: Record, owner: str, reason: str, by: str | None = None) -> None:
        record.owner = owner
        record.session = None
        record.lease_token = None
        record.lease_until = None
        record.history.append({"epoch": record.epoch, "owner": owner, "reason": reason, "at": iso(now()), "by": by})

    # --------------------------------------------------------- test controls
    async def revoke(self, domain: str | None = None, reason: str = P.REASON_REVOKED) -> None:
        for d, r in self.records.items():
            if domain in (None, d) and r.owner == P.OWNER_APP:
                r.epoch += 1
                self._change(r, P.OWNER_NONE, reason)
        await self.broadcast({"type": "ownership", "ownership": self.ownership()})

    async def expire_lease(self, domain: str) -> None:
        """The lease timer fired in the companion (no heartbeat): the App is away."""
        r = self.records[domain]
        r.lease_until = now() - timedelta(seconds=1)
        await self.broadcast({"type": "ownership", "ownership": self.ownership()})

    async def unpair(self) -> None:
        self.pairing_hash = None
        for s in self.sessions.values():
            s["trusted"] = False
        for r in self.records.values():
            if r.owner == P.OWNER_APP:
                r.epoch += 1
                self._change(r, P.OWNER_NONE, P.REASON_UNPAIRED)
        await self.broadcast({"type": "pairing", "paired": False})
        await self.broadcast({"type": "ownership", "ownership": self.ownership()})

    async def card_action(self, kind: str, params: dict[str, Any], user: str | None = "admin") -> str:
        """A dashboard card's action, relayed to the owner session of any domain."""
        action_id = secrets.token_hex(8)
        self.actions[action_id] = None
        owner = next((r.session for r in self.records.values() if r.owner == P.OWNER_APP and r.session), None)
        assert owner is not None, "no App session owns a domain"
        await self.send(owner, {"type": "action", "action_id": action_id, "kind": kind, "params": params, "user": user})
        return action_id

    async def rename(self, old: str, new: str) -> None:
        self.renames.append([old, new])
        await self.broadcast({"type": "renames", "renames": [[old, new]]})

    async def recheck(self) -> None:
        self.rechecks += 1
        await self.broadcast({"type": "recheck"})

    # ------------------------------------------------------------- protocol
    async def answer(self, ws: Any, msg: dict[str, Any], result: Any, error: Any) -> None:  # noqa: C901, PLR0912, PLR0915
        kind = msg["type"].split("smart_occupancy/companion/", 1)[1]
        if kind == "subscribe":
            if msg.get("protocol") != P.PROTOCOL_VERSION:
                await error(P.REJECT_VERSION, "protocol")
                return
            session = secrets.token_hex(16)
            token = msg.get("pairing_token")
            trusted = bool(self.pairing_hash and P.valid_id(token) and P.token_hash(token) == self.pairing_hash)
            self.sessions[session] = {"ws": ws, "sub": msg["id"], "instance": msg["instance_id"], "trusted": trusted}
            await result(None)
            await ws.send_json({"id": msg["id"], "type": "event", "event": self.hello(session)})
            return
        if kind == "status":
            await result({"companion_version": COMPANION_VERSION, "protocol": P.PROTOCOL_VERSION, "paired": self.paired,
                          "ownership": self.ownership(), "sessions": len(self.sessions), "legacy": self.legacy_summary(),
                          "entities": self.entity_map()})
            return
        if kind == "view":
            await result({"owners": {d: r.state(self.sessions) for d, r in self.records.items()}, "rooms": {},
                          "occupancy": {}, "lighting": {}, "curtains": {}, "forecast": {}, "system": self.views.get("system") or {}})
            return
        if kind == "legacy/status":
            await result(self.legacy_summary() | {"backup": self.backup})
            return
        if kind == "legacy/export":
            if self.legacy_options is None and not self.legacy_stores:
                await error(P.REJECT_NO_LEGACY, "no legacy data")
                return
            await result({"schema": 1, "exported_at": iso(now()), "companion_version": COMPANION_VERSION,
                          "entry": {"entry_id": "abc", "title": "Smart Occupancy & Lighting", "version": 1, "minor_version": 3,
                                    "options": copy.deepcopy(self.legacy_options)},
                          "options_revision": P.revision(self.legacy_options) if self.legacy_options is not None else None,
                          "legacy_owners": {}, "stores": copy.deepcopy(self.legacy_stores)})
            return
        if kind == "legacy/retire":
            if self.legacy_options is None:
                await error(P.REJECT_NO_LEGACY, "no legacy data")
                return
            if msg.get("expected_options_revision") not in (None, P.revision(self.legacy_options)):
                await error("stale_revision", "changed")
                return
            self.backup = {"options": self.legacy_options, "stores": self.legacy_stores, "backed_up_at": iso(now()),
                           "options_revision": P.revision(self.legacy_options)}
            removed = sorted(self.legacy_stores)
            self.retired = {"retired_at": iso(now()), "by": msg.get("requested_by")}
            self.legacy_options, self.legacy_stores = None, {}
            await result({"retired_at": self.retired["retired_at"], "stores_removed": removed,
                          "backup": "smart_occupancy.legacy_backup", "options_revision": self.backup["options_revision"]})
            return
        if kind == "legacy/restore":
            if self.backup is None:
                await error(P.REJECT_NOT_RETIRED, "nothing retired")
                return
            self.legacy_options, self.legacy_stores = self.backup["options"], self.backup["stores"]
            self.retired = None
            await result({"restored_at": iso(now()), "stores_restored": sorted(self.legacy_stores), "options": True,
                          "options_revision": self.backup["options_revision"]})
            return
        if kind == "revoke":
            await self.revoke(msg.get("domain"))
            await result({"revoked": [d for d in P.DOMAINS if msg.get("domain") in (None, d)]})
            return
        s = self._session(msg, ws)
        if s is None:
            await error(P.REJECT_UNKNOWN_SESSION, "unknown session")
            return
        if kind == "pair":
            token = secrets.token_hex(16)
            self.pairing_hash = P.token_hash(token)
            for other in self.sessions.values():
                other["trusted"] = False
            s["trusted"] = True
            await result({"pairing_token": token, "issued_at": iso(now())})
            await self.broadcast({"type": "pairing", "paired": True})
            return
        if kind == "action_result":
            self.action_results.append(msg)
            self.actions[msg["action_id"]] = msg
            await result({"accepted": True})
            return
        if kind == "renames_applied":
            self.renames_applied += 1
            self.renames.clear()
            await result({"ok": True})
            return
        if kind == "claim":
            domain = msg["domain"]
            record = self.records[domain]
            if not s["trusted"]:
                record.rejections[P.REJECT_NOT_PAIRED] = record.rejections.get(P.REJECT_NOT_PAIRED, 0) + 1
                await error(P.REJECT_NOT_PAIRED, "not paired")
                return
            if msg["expected_epoch"] != record.epoch:
                await error(P.REJECT_BUSY, f"epoch {record.epoch}")
                return
            other = self.sessions.get(record.session or "")
            if record.owner == P.OWNER_APP and other is not None and other is not s and record.lease_until and record.lease_until > now():
                await error(P.REJECT_BUSY, "another session holds the lease")
                return
            try:
                targets = P.check_targets(domain, msg.get("targets") or {}) if (domain in P.COMMAND_DOMAINS or msg.get("targets") is not None) else {}
            except P.ProtocolError as err:
                await error(err.code, str(err))
                return
            if domain in P.COMMAND_DOMAINS and msg.get("targets") is None:
                await error(P.REJECT_INVALID, "targets required")
                return
            record.owner = P.OWNER_APP
            record.epoch += 1
            record.session = msg["session_id"]
            record.lease_token = secrets.token_hex(16)
            record.lease_until = now() + timedelta(seconds=P.LEASE_SECONDS)
            record.seq = 0
            record.config_revision = msg.get("config_revision")
            record.targets = targets
            record.history.append({"epoch": record.epoch, "owner": P.OWNER_APP, "reason": P.REASON_CLAIM, "at": iso(now()),
                                   "by": msg.get("requested_by")})
            await result({"domain": domain, "epoch": record.epoch, "lease_token": record.lease_token,
                          "lease_until": iso(record.lease_until), "lease_seconds": P.LEASE_SECONDS,
                          "heartbeat_seconds": P.HEARTBEAT_SECONDS})
            await self.broadcast({"type": "ownership", "ownership": self.ownership()})
            return
        if kind == "publish" and msg["domain"] == "system":
            if not s["trusted"] or not any(r.owner == P.OWNER_APP and r.session == msg["session_id"] for r in self.records.values()):
                await error(P.REJECT_NOT_OWNER, "not an owner")
                return
            try:
                view = P.check_view("system", msg.get("view"))
            except P.ProtocolError as err:
                await error(err.code, str(err))
                return
            self.views["system"] = view
            self.views_at["system"] = iso(now()) or ""
            await result({"ok": True})
            return
        domain = msg.get("domain")
        if domain not in self.records:
            await error(P.REJECT_INVALID, "unknown domain")
            return
        record = self.records[domain]
        if kind in ("heartbeat", "publish", "execute", "release"):
            code = self._owner_check(msg, s, record)
            if code:
                record.rejections[code] = record.rejections.get(code, 0) + 1
                await error(code, code)
                return
            record.seq = msg["seq"]
            record.lease_until = now() + timedelta(seconds=P.LEASE_SECONDS)
        if kind == "heartbeat":
            await result({"seq": record.seq, "lease_until": iso(record.lease_until), "epoch": record.epoch})
        elif kind == "publish":
            try:
                view = P.check_view(domain, msg.get("view"))
                if msg.get("targets") is not None:
                    record.targets = P.check_targets(domain, msg["targets"])
            except P.ProtocolError as err:
                await error(err.code, str(err))
                return
            if P.valid_revision(msg.get("config_revision")):
                record.config_revision = msg["config_revision"]
            record.published_at = now()
            self.views[domain] = view
            self.views_at[domain] = iso(now()) or ""
            self.publishes.append({"domain": domain, "view": view, "seq": record.seq})
            await result({"seq": record.seq, "lease_until": iso(record.lease_until)})
        elif kind == "execute":
            if domain not in P.COMMAND_DOMAINS:
                await error(P.REJECT_NOT_ALLOWED, "commands nothing")
                return
            try:
                P.check_command(domain, msg["service_domain"], msg["service"], msg["data"], targets=record.targets)
            except P.ProtocolError as err:
                record.execute_errors += 1
                record.rejections[err.code] = record.rejections.get(err.code, 0) + 1
                await error(err.code, str(err))
                return
            if self.fail_execute:
                code, self.fail_execute = self.fail_execute, None
                record.execute_errors += 1
                await error(code, code)
                return
            record.executed += 1
            self.executed.append({"domain": domain, "service_domain": msg["service_domain"], "service": msg["service"],
                                  "data": msg["data"], "context_id": msg["context_id"], "purpose": msg.get("purpose"),
                                  "area_id": msg.get("area_id"), "seq": msg["seq"]})
            await self.fake.service_called(msg["service_domain"], msg["service"], msg["data"], msg["context_id"])
            await result({"seq": record.seq, "lease_until": iso(record.lease_until)})
        elif kind == "release":
            self._change(record, P.OWNER_NONE, P.REASON_RELEASE, msg.get("requested_by"))
            await result({"domain": domain, "owner": P.OWNER_NONE, "epoch": record.epoch})
            await self.broadcast({"type": "ownership", "ownership": self.ownership()})
        else:
            await error("unknown_command", f"Unknown command {msg['type']}")
