"""Settings → Migration & System (Phase A6): the reviewed move from the monolithic integration to the App + companion.

Eight steps, none automatic, each reported with what it found or did:

1. **inspect** - versions, which integration is installed (monolithic 0.9.0 or the companion), who owns each
   subsystem, configuration revisions, entities, device availability, runtime health, backups, blockers.
2. **backup** - one complete file in ``/data/migration/backups``: the core configuration (or the staged copy),
   the lighting, curtain and prediction exports (configuration, state, learned data), the floor plan, and the
   companion's export of the retired data when available. No Home Assistant credentials.
3. **validate** - the handover readiness of every subsystem that the monolithic integration still owns, the
   configuration's integrity, the App's storage, the companion's compatibility, the device targets, that no two
   owners exist and that no cover is moving.
4. **transfer_config** - the remaining integration-owned configuration (rooms, sensors, lights, categories, mood
   source, timing) into the App: reviewed first (what changes, what is missing), then applied as a *staged* copy
   while the monolithic integration still runs, or as the authority once the companion is installed.
5. **transfer_control** - the reviewed handover of each subsystem (the existing A3.2 / A4 / A5 handovers on
   0.9.0) in a safe order: occupancy, lighting, curtains, forecasts.
6. **activate** - once the companion integration is installed: compare the staged configuration with the
   companion's copy of the legacy options, promote it to the authority and let the runtimes claim every domain.
7. **verify** - presence decisions, native entities, ownership, overrides, curtains, forecasts, configuration
   persistence, restart recovery; degraded functions are reported.
8. **retire** - the companion backs the legacy data up and removes it from the active installation (never the
   App's backups, never the history).

``abort`` undoes what can be undone: a staged configuration is dropped, control is given back to the
monolithic integration (its bridges' release), or the legacy data is restored for a rollback to 0.9.0.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import companion_protocol as P
from .companion_client import CompanionError
from .core_config import EXPORT_FORMAT, CoreConfigError

if TYPE_CHECKING:
    from .core import AppCore

_LOGGER = logging.getLogger(__name__)

STEPS = ("inspect", "backup", "validate", "transfer_config", "transfer_control", "activate", "verify", "retire")
DOMAINS = ("occupancy", "lighting", "curtains", "forecast")
BACKUPS = 20
MIGRATION_SCHEMA = 1


class MigrationError(Exception):
    def __init__(self, code: str, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class MigrationService:
    def __init__(self, core: AppCore, data_dir: Path | None) -> None:
        self.core = core
        self.dir = data_dir / "migration" if data_dir else None
        self.state: dict[str, Any] = {"schema": MIGRATION_SCHEMA, "started_at": None, "steps": {}, "completed_at": None,
                                      "aborted": None}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------- storage
    async def load(self) -> None:
        if self.dir is None:
            return
        try:
            data = json.loads((self.dir / "state.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as err:
            _LOGGER.warning("The migration state could not be read: %s", err)
            return
        if isinstance(data, dict) and data.get("schema") == MIGRATION_SCHEMA:
            self.state = data

    async def _save(self) -> None:
        if self.dir is None:
            return

        def write() -> None:
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp = self.dir / "state.tmp"
            tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self.dir / "state.json")

        await asyncio.to_thread(write)

    async def _record(self, step: str, status: str, result: dict[str, Any]) -> dict[str, Any]:
        if self.state.get("started_at") is None:
            self.state["started_at"] = utcnow()
        self.state["steps"][step] = {"status": status, "at": utcnow(), "result": result}
        if step == "retire" and status == "done":
            self.state["completed_at"] = utcnow()
        await self._save()
        return result

    # --------------------------------------------------------------- facts
    @property
    def mode(self) -> str:
        """``companion`` (the companion integration is installed), ``legacy`` (0.9.0) or ``none``."""
        companion = self.core.companion
        if companion is not None and companion.present:
            return "companion"
        report = self.core.ha.integration if self.core.ha else None
        if report is not None and report.detected:
            return "legacy" if (companion is None or companion.state == "absent") else "unknown"
        return "none"

    def _runtimes(self) -> dict[str, Any]:
        return {"occupancy": self.core.occupancy, "lighting": self.core.lighting, "curtains": self.core.curtains,
                "forecast": self.core.forecast}

    def owners(self) -> dict[str, dict[str, Any]]:
        """Who runs each subsystem now, as the integration or the companion acknowledges it."""
        out: dict[str, dict[str, Any]] = {}
        companion = self.core.companion
        for domain, rt in self._runtimes().items():
            if rt is None:
                out[domain] = {"owner": "unknown", "source": "none"}
                continue
            if companion is not None and companion.present:
                record = companion.ownership.get(domain) or {}
                out[domain] = {"owner": record.get("state") or "none", "epoch": record.get("epoch"),
                               "source": "companion", "app_mode": rt.mode, "claimed": bool(rt.cm and rt.cm.owner)}
            else:
                integ = getattr(rt, "integration", {}) or {}
                out[domain] = {"owner": integ.get("state") or integ.get("owner") or "unknown",
                               "epoch": integ.get("epoch"), "source": "legacy", "app_mode": rt.mode,
                               "bridge": rt.bridge}
        return out

    def legacy_options(self) -> dict[str, Any] | None:
        """The monolithic integration's options as the App knows them (its lighting bridge carries them)."""
        rt = self.core.lighting
        options = getattr(rt, "options", None) if rt is not None else None
        return dict(options) if isinstance(options, dict) and options.get("rooms") is not None else None

    # ---------------------------------------------------------------- steps
    async def inspect(self) -> dict[str, Any]:
        core = self.core
        ha = core.ha
        companion = core.companion
        mode = self.mode
        legacy_status = None
        if mode == "companion" and companion is not None:
            try:
                legacy_status = await companion.legacy_status()
            except CompanionError as err:
                legacy_status = {"error": f"{err.code}: {err}"}
        config = core.core_config.status()
        legacy_options = self.legacy_options()
        entities = ha.integration.as_dict() if ha is not None and ha.integration else None
        rooms = (core.core_config.options or legacy_options or {}).get("rooms") or {}
        configured: set[str] = set()
        for room in rooms.values():
            if isinstance(room, dict) and room.get("enabled"):
                configured.update(s.get("entity_id") for s in room.get("sensors") or [] if isinstance(s, dict))
                configured.update(room.get("lights") or [])
        states = ha.states if ha is not None else {}
        unavailable = sorted(e for e in configured if e and (states.get(e) or {}).get("state") in ("unavailable", None))
        blockers: list[str] = []
        if ha is None or not ha.connected:
            blockers.append("Home Assistant is not connected")
        if mode == "none":
            blockers.append("Neither the monolithic integration nor the companion is installed")
        if mode == "legacy" and legacy_options is None:
            blockers.append("The integration's options are not known yet (the lighting bridge has not connected)")
        if mode == "companion" and companion is not None and companion.state == "incompatible":
            blockers.append(f"The companion is not compatible: {companion.error}")
        runtimes = {d: {"mode": rt.mode, "bridge": rt.bridge, "error": getattr(rt, "bridge_error", None)}
                    for d, rt in self._runtimes().items() if rt is not None}
        result = {
            "at": utcnow(), "mode": mode,
            "versions": {"app": core.app_version, "integration": (ha.integration.version if ha and ha.integration else None),
                         "companion": companion.hello.get("companion_version") if companion else None,
                         "companion_protocol": companion.hello.get("protocol") if companion else None,
                         "app_protocol": P.PROTOCOL_VERSION, "home_assistant": ha.ha_version if ha else None},
            "compatible": mode != "companion" or (companion is not None and companion.state == "present"),
            "owners": self.owners(), "config": config,
            "legacy": {"options_revision": P.revision(legacy_options) if legacy_options else None,
                       "rooms": len(rooms), "status": legacy_status},
            "entities": entities, "unavailable_devices": unavailable, "runtimes": runtimes,
            "backups": {"migration": self.backups(), "core": core.core_config.backups()[:5],
                        "lighting": (core.lighting.backups() if core.lighting else [])[:5],
                        "curtains": (core.curtains.backups() if core.curtains else [])[:5],
                        "predictions": (core.forecast.backups() if core.forecast else [])[:5]},
            "blockers": blockers,
        }
        return await self._record("inspect", "done" if not blockers else "blocked", result)

    async def backup(self, user_id: str | None) -> dict[str, Any]:
        core = self.core
        payload: dict[str, Any] = {"format": "smart_lighting.migration_backup", "version": 1, "created_at": utcnow(),
                                   "by": user_id, "app_version": core.app_version, "mode": self.mode}
        payload["core_config"] = core.core_config.export("migration") if core.core_config.options else None
        payload["legacy_options"] = self.legacy_options()
        payload["lighting"] = core.lighting.export() if core.lighting else None
        payload["curtains"] = core.curtains.export() if core.curtains else None
        payload["predictions"] = core.forecast.export() if core.forecast else None
        payload["floorplan"] = core.floorplan.export() if core.floorplan is not None and hasattr(core.floorplan, "export") else None
        if self.mode == "companion" and core.companion is not None:
            try:
                payload["companion_legacy"] = await core.companion.legacy_export()
            except CompanionError as err:
                payload["companion_legacy"] = {"error": f"{err.code}: {err}"}
        if self.dir is None:
            raise MigrationError("no_storage", "The App has no data folder for backups", 503)
        folder = self.dir / "backups"
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        path = folder / f"migration-{stamp}.json"

        def write() -> int:
            folder.mkdir(parents=True, exist_ok=True)
            text = json.dumps(payload, ensure_ascii=False, indent=1, default=str)
            if "SUPERVISOR_TOKEN" in text or (core.settings.token and core.settings.token in text):
                raise MigrationError("credentials", "The backup would contain a credential; refused", 500)
            path.write_text(text, encoding="utf-8")
            for old in sorted(folder.glob("migration-*.json"))[:-BACKUPS]:
                old.unlink(missing_ok=True)
            return len(text)

        size = await asyncio.to_thread(write)
        result = {"id": path.stem, "size": size, "created_at": payload["created_at"],
                  "contents": {k: v is not None for k, v in payload.items() if k not in ("format", "version",
                                                                                      "created_at", "by",
                                                                                      "app_version", "mode")}}
        return await self._record("backup", "done", result)

    def backups(self) -> list[dict[str, Any]]:
        folder = self.dir / "backups" if self.dir else None
        if folder is None or not folder.is_dir():
            return []
        return [{"id": p.stem, "size": p.stat().st_size} for p in sorted(folder.glob("migration-*.json"), reverse=True)]

    def backup_file(self, backup_id: str) -> dict[str, Any]:
        folder = self.dir / "backups" if self.dir else None
        if folder is None or not backup_id.startswith("migration-") or "/" in backup_id or ".." in backup_id:
            raise MigrationError("unknown_backup", "Unknown backup", 404)
        try:
            return json.loads((folder / f"{backup_id}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as err:
            raise MigrationError("unknown_backup", "Unknown backup", 404) from err

    async def validate(self) -> dict[str, Any]:
        core = self.core
        checks: list[dict[str, Any]] = []

        def check(key: str, ok: bool, title: str, detail: str, warning: bool = False) -> None:
            checks.append({"key": key, "status": "ok" if ok else ("warning" if warning else "error"), "title": title,
                           "detail": detail})

        mode = self.mode
        check("connected", bool(core.ha and core.ha.connected), "Home Assistant connected",
              "Connected" if core.ha and core.ha.connected else "Not connected")
        owners = self.owners()
        for domain, rt in self._runtimes().items():
            if rt is None:
                continue
            owner = owners[domain]
            if mode == "legacy":
                if owner.get("owner") == "app":
                    check(f"{domain}_handover", True, f"{domain}: handed over", "The App already runs it")
                else:
                    ready = rt.readiness()
                    failed = [c["title"] for c in ready.get("checks", []) if c["status"] == "error"]
                    check(f"{domain}_handover", bool(ready.get("ready")), f"{domain}: handover readiness",
                          "Ready (the earlier phases' comparison evidence is valid)" if ready.get("ready")
                          else "Not ready: " + ", ".join(failed), warning=True)
            else:
                check(f"{domain}_owner", owner.get("owner") in ("app", "none", "app_away"), f"{domain}: one owner",
                      f"owner {owner.get('owner')}")
        options = core.core_config.options or self.legacy_options()
        try:
            from .core_config import validate_options  # noqa: PLC0415

            validate_options(options or {})
            check("config", options is not None, "Configuration integrity",
                  "The configuration parses with the shared model" if options is not None else "No configuration")
        except CoreConfigError as err:
            check("config", False, "Configuration integrity", str(err))
        writable = core.settings.data_dir is not None and os.access(core.settings.data_dir, os.W_OK)
        check("storage", writable, "App storage", "Writable" if writable else "The data folder is not writable")
        if mode == "companion":
            companion = core.companion
            check("companion", bool(companion and companion.state == "present"), "Companion compatible",
                  f"protocol {companion.hello.get('protocol')}" if companion and companion.present
                  else (companion.error if companion else "no companion"))
        states = core.ha.states if core.ha else {}
        missing = []
        for room in ((options or {}).get("rooms") or {}).values():
            if isinstance(room, dict) and room.get("enabled"):
                missing += [e for e in room.get("lights") or [] if e not in states]
                missing += [s.get("entity_id") for s in room.get("sensors") or [] if isinstance(s, dict)
                            and s.get("entity_id") not in states]
        check("targets", not missing, "Device targets exist", "Every configured light and sensor exists in Home Assistant"
              if not missing else f"Missing: {', '.join(sorted(set(missing))[:10])}", warning=True)
        conflicts = [d for d, o in owners.items() if o.get("owner") == "app" and o.get("source") == "legacy"
                     and self._runtimes()[d] is not None and self._runtimes()[d].mode != "owner"
                     and self._runtimes()[d].mode != "authoritative"]
        check("owners", not conflicts, "No conflicting owners",
              "Every subsystem has exactly one owner" if not conflicts else f"Mismatch: {', '.join(conflicts)}")
        moving = core.curtains.moving_windows() if core.curtains is not None else []
        check("covers", not moving, "No cover is moving", "Every cover is at rest" if not moving else
              f"Moving: {', '.join(moving)}")
        ok = all(c["status"] != "error" for c in checks)
        result = {"at": utcnow(), "ok": ok, "mode": mode, "checks": checks}
        return await self._record("validate", "done" if ok else "blocked", result)

    async def transfer_config(self, *, apply: bool, user_id: str | None) -> dict[str, Any]:
        core = self.core
        mode = self.mode
        if mode == "companion" and core.companion is not None:
            export = await core.companion.legacy_export()
            options = (export.get("entry") or {}).get("options")
            source = "companion legacy export"
            authority = "app"
        else:
            options = self.legacy_options()
            source = "the integration's options (lighting bridge)"
            authority = "staged"
        if not isinstance(options, dict):
            raise MigrationError("no_source", "The integration's configuration is not available to import; the "
                                              "monolithic integration must be connected or its data must still be "
                                              "present in the companion")
        document = {"format": EXPORT_FORMAT, "version": 1, "options": options}
        states = core.ha.states if core.ha else {}
        try:
            review = core.core_config.import_review(document, entity_exists=lambda e: e in states)
        except CoreConfigError as err:
            raise MigrationError(err.code, str(err), err.status) from err
        result = {"at": utcnow(), "source": source, "authority": authority, "review": review,
                  "source_revision": P.revision(options), "applied": False}
        if apply:
            try:
                await core.core_config.import_apply(document, expected_revision=core.core_config.revision,
                                                    source="legacy", reason="migration", authority=authority)
            except CoreConfigError as err:
                raise MigrationError(err.code, str(err), err.status) from err
            result["applied"] = True
            result["status"] = core.core_config.status()
            if core.core_host is not None:
                core.core_host.schedule_snapshot(0)
        return await self._record("transfer_config", "done" if apply else "reviewed", result)

    async def transfer_control(self, domain: str, user_id: str | None) -> dict[str, Any]:
        if domain not in DOMAINS:
            raise MigrationError("invalid", f"unknown domain {domain}")
        rt = self._runtimes()[domain]
        if rt is None:
            raise MigrationError("not_running", "The runtime is not running", 503)
        order = list(DOMAINS)
        before = order[:order.index(domain)]
        owners = self.owners()
        pending = [d for d in before if owners[d].get("owner") not in ("app", "app_away")]
        if pending and self.mode == "legacy":
            raise MigrationError("order", f"Hand over {', '.join(pending)} first (safe order: occupancy, lighting, "
                                          "curtains, forecasts)")
        try:
            outcome = await rt.handover(user_id)
        except Exception as err:  # noqa: BLE001 - the runtimes' own refusals (readiness, moving covers)
            raise MigrationError(getattr(err, "code", "refused"), str(err), getattr(err, "status", 409)) from err
        step = self.state["steps"].get("transfer_control") or {"result": {"domains": {}}}
        domains = dict(step["result"].get("domains") or {})
        domains[domain] = {"at": utcnow(), "outcome": outcome, "by": user_id}
        all_done = all(d in domains for d in DOMAINS)
        return await self._record("transfer_control", "done" if all_done else "partial",
                                  {"at": utcnow(), "domains": domains, "owners": self.owners()})

    async def activate(self, *, reimport: bool, user_id: str | None) -> dict[str, Any]:
        core = self.core
        companion = core.companion
        if self.mode != "companion" or companion is None or not companion.present:
            raise MigrationError("companion_missing", "Install the companion (Smart Occupancy & Lighting 1.0.0 or "
                                                      "newer) in Home Assistant, then come back to this step")
        if not companion.trusted:
            await companion.pair(user_id)
        legacy = await companion.legacy_status()
        staged_rev = core.core_config.revision
        legacy_rev = legacy.get("options_revision")
        matches = legacy_rev is None or staged_rev == legacy_rev
        # The legacy options are imported only when there is nothing yet, or when the administrator asked for a
        # re-import after seeing the review. An authoritative configuration edited in the App is never replaced
        # by a retry of this step.
        if core.core_config.options is None or (not matches and reimport):
            if legacy.get("present"):
                await self.transfer_config(apply=True, user_id=user_id)
                matches = True
            elif core.core_config.options is None:
                raise MigrationError("no_config", "There is no configuration to activate: import it or set the App "
                                                  "up as a new installation")
        elif not matches and core.core_config.authoritative:
            matches = True  # already activated and edited since: the App's configuration stands
        if not matches:
            review = await self.transfer_config(apply=False, user_id=user_id)
            result = {"at": utcnow(), "activated": False, "needs_reimport": True, "staged_revision": staged_rev,
                      "legacy_revision": legacy_rev, "review": review["review"]}
            return await self._record("activate", "blocked", result)
        status = await core.core_config.promote("activated")
        claims = {}
        for domain, rt in self._runtimes().items():
            if rt is not None and rt.cm is not None:
                try:
                    claims[domain] = await rt.cm.claim(user_id, manual=True)
                except CompanionError as err:
                    claims[domain] = {"claimed": False, "error": f"{err.code}: {err}"}
        if core.core_host is not None:
            core.core_host.schedule_snapshot(0)
        result = {"at": utcnow(), "activated": True, "config": status, "claims": claims, "owners": self.owners()}
        return await self._record("activate", "done", result)

    async def verify(self) -> dict[str, Any]:
        core = self.core
        companion = core.companion
        checks: list[dict[str, Any]] = []

        def check(key: str, ok: bool, title: str, detail: str, warning: bool = False) -> None:
            checks.append({"key": key, "status": "ok" if ok else ("warning" if warning else "error"), "title": title,
                           "detail": detail})

        present = bool(companion and companion.present)
        check("companion", present, "Companion connected", companion.state if companion else "none")
        owners = self.owners()
        for domain in DOMAINS:
            check(f"{domain}_owner", owners[domain].get("owner") == "app", f"{domain}: the App controls it",
                  f"owner {owners[domain].get('owner')}")
        occ = core.occupancy
        rooms = list((core.core_config.options or {}).get("rooms") or {})
        enabled = [a for a, r in ((core.core_config.options or {}).get("rooms") or {}).items()
                   if isinstance(r, dict) and r.get("enabled")]
        decided = [a for a in enabled if occ is not None and occ.result(a) is not None]
        check("presence", len(decided) == len(enabled), "Presence decisions",
              f"{len(decided)} of {len(enabled)} rooms decided")
        live = (await companion.status() if companion and companion.present else None) or {}
        entities = (live.get("entities") or companion.hello.get("entities") or {}) if companion else {}
        expected = {f"smart_occupancy_room_{a}_state" for a in enabled}
        check("entities", expected <= set(entities), "Native entities",
              f"{len(entities)} entities registered by the companion" if expected <= set(entities)
              else f"Missing room entities for {', '.join(sorted(a for a in enabled if f'smart_occupancy_room_{a}_state' not in entities))}")
        ctrl = core.lighting.controller if core.lighting else None
        check("lighting", ctrl is not None, "Lighting runtime", "Running" if ctrl is not None else "Not running")
        if ctrl is not None:
            switches = sum(len(r.switches) for r in ctrl.lighting.rooms.values())
            overrides = sum(len(rt.overrides) for rt in ctrl.runtime.values())
            check("switches", True, "Switch inputs", f"{switches} switch(es) configured")
            check("overrides", True, "Manual overrides", f"{overrides} active override(s) kept")
        mgr = core.curtains.manager if core.curtains else None
        check("curtains", mgr is not None or not (core.curtains and core.curtains.stored_config_doc()),
              "Curtain control", "Running" if mgr is not None else "Not running (no curtain configuration)",
              warning=True)
        fc = core.forecast.manager if core.forecast else None
        check("forecast", fc is not None, "Forecast publishing", f"status {fc.status.state}" if fc else "Not running",
              warning=True)
        cfg = core.core_config.status()
        check("config", bool(cfg["authoritative"]) and bool(cfg["path"]), "Configuration persisted",
              f"revision {cfg['revision']} in the App's storage" if cfg["authoritative"] else "Not authoritative")
        record = (companion.ownership.get("lighting") or {}) if companion else {}
        history = record.get("history") or []
        restarted = any(h.get("reason") == P.REASON_RESTART for h in history)
        check("restart", True, "Restart recovery", "Recovered after a Home Assistant restart already" if restarted
              else "Not exercised yet: restart Home Assistant and verify again", warning=True)
        degraded = [c["title"] for c in checks if c["status"] != "ok"]
        result = {"at": utcnow(), "ok": not any(c["status"] == "error" for c in checks), "checks": checks,
                  "degraded": degraded}
        return await self._record("verify", "done" if result["ok"] else "blocked", result)

    async def retire(self, user_id: str | None) -> dict[str, Any]:
        core = self.core
        companion = core.companion
        if self.mode != "companion" or companion is None or not companion.present:
            raise MigrationError("companion_missing", "The companion is not connected")
        verify = self.state["steps"].get("verify") or {}
        if verify.get("status") != "done":
            raise MigrationError("not_verified", "Verify the installation first (step 7)")
        legacy = await companion.legacy_status()
        if not legacy.get("present"):
            result = {"at": utcnow(), "already": True, "legacy": legacy}
            return await self._record("retire", "done", result)
        try:
            outcome = await companion.legacy_retire(legacy.get("options_revision"), user_id)
        except CompanionError as err:
            raise MigrationError(err.code, str(err), err.status) from err
        result = {"at": utcnow(), "outcome": outcome, "by": user_id}
        return await self._record("retire", "done", result)

    async def abort(self, user_id: str | None, *, restore_legacy: bool) -> dict[str, Any]:
        """Undo what can be undone: drop a staged copy, hand control back on 0.9.0, or restore the legacy data."""
        core = self.core
        actions: list[str] = []
        if core.core_config.staged:
            await core.core_config.demote("migration aborted")
            actions.append("configuration demoted to a staged copy")
        if self.mode == "legacy":
            for domain, rt in self._runtimes().items():
                if rt is not None and rt.mode in ("owner", "authoritative"):
                    try:
                        await rt.release(user_id)
                        actions.append(f"{domain} returned to the integration")
                    except Exception as err:  # noqa: BLE001
                        actions.append(f"{domain}: release refused ({err})")
        elif self.mode == "companion" and restore_legacy and core.companion is not None:
            try:
                outcome = await core.companion.legacy_restore(user_id)
                actions.append(f"legacy data restored ({len(outcome.get('stores_restored') or [])} stores)")
            except CompanionError as err:
                actions.append(f"restore refused: {err.code}: {err}")
        self.state["aborted"] = {"at": utcnow(), "by": user_id, "actions": actions}
        await self._save()
        return {"at": utcnow(), "actions": actions}

    def status(self) -> dict[str, Any]:
        steps = []
        for name in STEPS:
            item = self.state["steps"].get(name) or {}
            steps.append({"key": name, "status": item.get("status", "pending"), "at": item.get("at"),
                          "result": item.get("result")})
        return {"mode": self.mode, "started_at": self.state.get("started_at"), "completed_at": self.state.get("completed_at"),
                "aborted": self.state.get("aborted"), "steps": steps, "owners": self.owners(),
                "config": self.core.core_config.status(),
                "companion": self.core.companion.brief() if self.core.companion else None}
