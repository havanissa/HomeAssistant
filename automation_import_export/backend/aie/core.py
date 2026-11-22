"""The App's operations: discovery, export, Export & Delete, verification, import, restore, rollback.

Every operation that writes anything (an archive, an automation, a registry entry) is an *operation* with a
durable journal (``store.OperationJournal``) and runs in the background; the interface follows it. Only one
such operation runs at a time. Nothing in Home Assistant is changed without an explicit confirmation that
carries the hash of the exact plan the user saw.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import json
import logging
import re
import time
from typing import Any

import aiohttp

from . import deps, yamlio
from .archive import Archive, ArchiveError, build, verify
from .canonical import digest, same, sha256_bytes
from .const import APP_NAME, APP_VERSION
from .ha import AutomationConfigApi, ConfigApiError, HomeAssistantClient, HomeAssistantError, NotAllowed
from .haconfig import HaConfigFolder
from .inventory import Item, Snapshot, _owner, registry_digest, registry_view, summary, take_snapshot
from .logbuffer import BufferHandler
from .restore import ACTIONS_WRITING, PlanError, RestoreRunner, build_plan
from .settings import Settings
from .store import ArchiveStore, InstanceIdentity, OperationJournal, iso, new_id

_LOGGER = logging.getLogger(__name__)
SNAPSHOT_CACHE_SECONDS = 5.0
MAX_YAML_ITEMS = 2000
SETTLE_SECONDS = 1.0  # the entity removal after a DELETE is a task in Home Assistant


class OperationError(Exception):
    def __init__(self, message: str, code: str = "invalid", status: int = 400, **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.extra = extra


class AppCore:
    def __init__(self, settings: Settings, buffer: BufferHandler | None = None) -> None:
        self.settings = settings
        self.buffer = buffer
        self.session: aiohttp.ClientSession | None = None
        self.ha: HomeAssistantClient | None = None
        self.api: AutomationConfigApi | None = None
        self.folder = HaConfigFolder(settings.ha_config_dir)
        self.identity = InstanceIdentity(settings.data_dir)
        self.archives = ArchiveStore(settings.data_dir)
        self.journal = OperationJournal(settings.data_dir)
        self.lock = asyncio.Lock()
        self.tasks: set[asyncio.Task[Any]] = set()
        self._admins: tuple[float, dict[str, bool]] | None = None
        self._cache: tuple[float, Snapshot] | None = None
        self.interrupted: list[str] = []

    # --------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.identity.load()
        self.archives.load()
        self.interrupted = [op["id"] for op in self.journal.load()]
        if self.interrupted:
            _LOGGER.error("%d operation(s) were interrupted by a stop of the App: %s", len(self.interrupted),
                          ", ".join(self.interrupted))
        self.session = aiohttp.ClientSession()
        self.ha = HomeAssistantClient(self.session, self.settings.ha_ws_url, self.settings.token)
        self.api = AutomationConfigApi(self.session, self.settings.ha_api_url, self.settings.token, self.ha.scope)
        await self.ha.start()

    async def stop(self) -> None:
        for task in list(self.tasks):
            task.cancel()
        if self.ha is not None:
            await self.ha.stop()
        if self.session is not None:
            await self.session.close()

    def attach(self, ha: Any, api: Any) -> None:
        """Tests: use a fake Home Assistant."""
        self.ha, self.api = ha, api

    # ------------------------------------------------------------------ users
    async def is_admin(self, user_id: str | None) -> bool:
        if not user_id or self.ha is None:
            return False
        now = time.monotonic()
        if self._admins is None or now - self._admins[0] > 60 or user_id not in self._admins[1]:
            users = await self.ha.command("config/auth/list") or []
            self._admins = (now, {
                u.get("id"): bool(u.get("is_active", True)) and not u.get("system_generated")
                and (bool(u.get("is_owner")) or "system-admin" in (u.get("group_ids") or []))
                for u in users if isinstance(u, dict)})
        return bool(self._admins[1].get(user_id))

    # ---------------------------------------------------------------- reading
    def connection(self) -> dict[str, Any]:
        return {"home_assistant": self.ha.status() if self.ha else {"state": "not_started", "connected": False},
                "config_folder": self.folder.available}

    def _require_ha(self) -> tuple[HomeAssistantClient, AutomationConfigApi]:
        if self.ha is None or self.api is None or not self.ha.connected:
            raise OperationError("Home Assistant is not connected; try again in a moment.", "not_connected", 503)
        return self.ha, self.api

    async def snapshot(self, *, cached: bool = False, fetch: Any = ()) -> Snapshot:
        """``fetch``: automation ids whose configuration must come from Home Assistant's editor endpoint itself
        (what an operation verifies or decides on); everything else is listed from automations.yaml."""
        if cached and self._cache and time.monotonic() - self._cache[0] < SNAPSHOT_CACHE_SECONDS:
            return self._cache[1]
        ha, api = self._require_ha()
        try:
            snap = await take_snapshot(ha, api, self.folder, now=iso(), fetch=[f for f in fetch if f])
        except (HomeAssistantError, ConnectionError, TimeoutError) as err:
            raise OperationError(f"Home Assistant could not be read: {err}", "ha_error", 503) from err
        self._cache = (time.monotonic(), snap)
        return snap

    def installation(self, snap: Snapshot) -> dict[str, Any]:
        return {"app_instance_id": self.identity.instance_id, "location_name": snap.ha_config.get("location_name"),
                "ha_version": snap.ha_config.get("version"),
                "fingerprint": digest(sorted(snap.areas))[:16]}

    async def inventory_view(self) -> dict[str, Any]:
        snap = await self.snapshot(cached=True)
        return {
            "taken_at": snap.taken_at, "summary": summary(snap),
            "items": [i.public(snap) for i in snap.items],
            "labels": [{"id": k, "name": v.get("name"), "color": v.get("color"), "icon": v.get("icon")}
                       for k, v in sorted(snap.labels.items(), key=lambda kv: str(kv[1].get("name")).casefold())],
            "categories": [{"id": k, "name": v.get("name"), "scope": scope}
                           for scope, defs in snap.categories.items() for k, v in defs.items()],
            "areas": [{"id": k, "name": v.get("name")} for k, v in snap.areas.items()],
            "problems": snap.problems, "file_scan": snap.file_scan.public(),
        }

    async def overview(self) -> dict[str, Any]:
        out: dict[str, Any] = {"archives": len(self.archives.entries), "operations": self.journal.listing(8),
                               "interrupted": [o for o in self.journal.listing(200) if o["status"] == "interrupted"],
                               "awaiting": [o for o in self.journal.listing(200)
                                            if o["status"] == "awaiting_confirmation"]}
        try:
            snap = await self.snapshot(cached=True)
            out["summary"] = summary(snap)
            out["problems"] = snap.problems
        except OperationError as err:
            out["summary"] = None
            out["problems"] = [{"level": "error", "code": err.code, "message": str(err)}]
        return out

    # --------------------------------------------------------------- plumbing
    def _spawn(self, op: dict[str, Any], coro: Awaitable[Any]) -> None:
        async def runner() -> None:
            try:
                async with self.lock:
                    await coro
            except asyncio.CancelledError:
                raise
            except (OperationError, PlanError, ArchiveError, HomeAssistantError, ConfigApiError, NotAllowed,
                    ConnectionError, TimeoutError) as err:
                op["status"] = "failed"
                op["error"] = str(err)
                self.journal.log(op, "error", f"Failed: {err}")
            except Exception as err:  # noqa: BLE001 - recorded, never silent
                _LOGGER.exception("Operation %s failed", op["id"])
                op["status"] = "failed"
                op["error"] = f"{type(err).__name__}: {err}"
                self.journal.log(op, "error", f"Failed unexpectedly: {type(err).__name__}: {err}")
            finally:
                self._cache = None
                self.journal.save(op)

        task = asyncio.get_running_loop().create_task(runner(), name=f"operation-{op['id']}")
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    def _busy_check(self) -> None:
        if self.lock.locked():
            raise OperationError("Another operation is running; wait until it has finished.", "busy", 409)

    async def _hook(self, name: str, index: int) -> None:
        """Test hooks only (AIE_TEST_HOOKS): pause before a step so an end-to-end test can interrupt the App."""
        if not self.settings.test_hooks:
            return
        try:
            hooks = json.loads((self.settings.data_dir / "test_hooks.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(hooks, dict) and isinstance(hooks.get(name), int) and index >= hooks[name]:
            _LOGGER.warning("Test hook: pausing at %s #%d", name, index)
            await asyncio.sleep(3600)

    def operation(self, op_id: str) -> dict[str, Any]:
        op = self.journal.get(op_id)
        if op is None:
            raise OperationError("Unknown operation", "unknown_operation", 404)
        return op

    # ----------------------------------------------------------------- export
    async def _archive(self, op: dict[str, Any], snap: Snapshot, items: list[Item], *, purpose: str,
                       selection: dict[str, Any], deletion: list[str] | None = None,
                       live_compare: bool = True) -> tuple[str, dict[str, Any]]:
        files: dict[str, bytes] = {}
        for item in items:
            path = item.blueprint
            if path and path not in files:
                content = self.folder.read_blueprint(path)
                if content is not None:
                    files[path] = content
        archive_id = new_id("aie")
        created_at = iso()
        data, manifest = build(archive_id=archive_id, created_at=created_at, purpose=purpose, selection=selection,
                               items=items, snap=snap, installation=self.installation(snap), blueprint_files=files,
                               extra_warnings=[p for p in snap.problems if p["level"] != "info"])
        self.journal.log(op, "info", f"Archive {archive_id} built: {len(items)} automation(s), {len(data)} bytes")
        await self._hook("export_write", 0)
        entry = self.archives.add(archive_id, data, {
            "created_at": created_at, "purpose": purpose, "label": selection.get("label"),
            "count": len(items), "sha256": sha256_bytes(data),
            "ha_version": snap.ha_config.get("version"), "location_name": snap.ha_config.get("location_name"),
            "complete": manifest["complete"], "operation_id": op["id"],
            "warnings": len(manifest["warnings"]), "source_instance": self.identity.instance_id})
        op["archive_id"] = archive_id
        self.journal.log(op, "info", f"Archive {archive_id} stored in the App ({entry['size']} bytes)")
        live = None  # an imported file is not in Home Assistant yet
        if live_compare:
            ids = [i.config_id for i in items if i.source == "ui"]
            self.journal.log(op, "info", f"Comparing {len(items)} automation(s) with Home Assistant (each one read "
                                         "through its automation editor)")
            live = await self.snapshot(fetch=ids)
        report = verify(data, expected_keys=[i.key for i in items], live=live, deletion_keys=deletion,
                        stored_sha256=entry["sha256"], verified_at=iso())
        self.archives.update(archive_id, verification={"status": report["status"], "at": report["verified_at"],
                                                       "ok": report["ok"]})
        self.journal.log(op, "info" if report["ok"] else "warning",
                         f"Verification: {report['status']} ({sum(1 for c in report['checks'] if c['ok'])}/"
                         f"{len(report['checks'])} checks passed)")
        return archive_id, report

    def _select(self, snap: Snapshot, keys: Any, mode: Any) -> list[Item]:
        if mode == "all":
            return [i for i in snap.items if i.config is not None or i.registry is not None]
        if mode == "all_ordinary":
            return [i for i in snap.items if i.source == "ui" and i.owner == "ordinary"]
        if not isinstance(keys, list) or not keys or not all(isinstance(k, str) for k in keys):
            raise OperationError("Select at least one automation", "empty_selection")
        found, missing = [], []
        for key in dict.fromkeys(keys):
            item = snap.item(key)
            (found.append(item) if item else missing.append(key))
        if missing:
            raise OperationError(f"No longer in Home Assistant: {', '.join(missing[:5])}", "unknown_key", 409)
        return found

    async def start_export(self, data: dict[str, Any], by: str | None) -> dict[str, Any]:
        self._busy_check()
        self._require_ha()
        mode = data.get("mode")
        purpose = "backup" if mode == "all" else "export"
        op = self.journal.create("export", by, title="Export" if purpose == "export" else "Backup of all automations",
                                 status="exporting")

        async def run() -> None:
            snap = await self.snapshot()
            items = self._select(snap, data.get("keys"), mode)
            if not items:
                raise OperationError("There is nothing to export", "empty_selection")
            op["selection"] = {"mode": mode or "selected", "keys": [i.key for i in items]}
            archive_id, report = await self._archive(op, snap, items, purpose=purpose, selection={
                "mode": mode or "selected", "keys": [i.key for i in items], "label": str(data.get("label") or "")[:80],
                "filters": data.get("filters") if isinstance(data.get("filters"), dict) else None})
            op["verification"] = report
            op["status"] = "completed"
            op["summary"] = f"{len(items)} automation(s) exported; verification {report['status']}"
            self.journal.log(op, "info", op["summary"])

        self._spawn(op, run())
        return op

    # --------------------------------------------------------- export & delete
    async def start_cleanup(self, data: dict[str, Any], by: str | None) -> dict[str, Any]:
        self._busy_check()
        self._require_ha()
        keys = data.get("keys")
        include_review = set(data.get("include_review") or [])
        if not isinstance(keys, list) or not keys:
            raise OperationError("Select at least one automation to clean up", "empty_selection")
        op = self.journal.create("cleanup", by, title="Export & Delete Legacy Automations", status="exporting")

        async def run() -> None:
            snap = await self.snapshot()
            requested = self._select(snap, keys, None)
            eligible: list[Item] = []
            excluded: dict[str, str] = {}
            for item in requested:
                if item.owner == "builder":
                    excluded[item.key] = "Automation Builder automation: protected"
                elif item.source != "ui":
                    excluded[item.key] = {"external": "Loaded from another YAML file: not deleted (export it "
                                                      "read-only instead)",
                                          "integration": "Provided by an integration: protected",
                                          "orphan": "A registry entry without an automation: not touched"}.get(
                        item.source, "Not an ordinary UI automation")
                elif item.owner == "review" and item.key not in include_review:
                    excluded[item.key] = "Ownership is ambiguous: excluded unless you include it explicitly"
                elif item.blocking:
                    excluded[item.key] = "; ".join(p["message"] for p in item.blocking)
                else:
                    eligible.append(item)
            self.journal.log(op, "info", f"{len(requested)} automation(s) selected, {len(eligible)} eligible, "
                                         f"{len(excluded)} excluded")
            op["selection"] = {"requested": [i.key for i in requested], "eligible": [i.key for i in eligible],
                               "excluded": excluded, "include_review": sorted(include_review)}
            op["dependencies"] = {i.key: {"references": i.references,
                                          "problems": [p for p in i.problems if p["code"].startswith("missing_")]}
                                  for i in eligible}
            op["protected_before"] = {i.key: {"config": i.config_hash, "registry": i.registry_hash, "state": i.state,
                                              "alias": i.alias, "owner": i.owner, "source": i.source}
                                      for i in snap.items if i not in eligible}
            if not eligible:
                op["status"] = "blocked"
                op["summary"] = "Nothing can be deleted: every selected automation is excluded"
                self.journal.log(op, "warning", op["summary"])
                return
            archive_id, report = await self._archive(op, snap, eligible, purpose="export_delete", selection={
                "mode": "cleanup", "keys": [i.key for i in eligible], "label": str(data.get("label") or "")[:80]},
                deletion=[i.key for i in eligible])
            op["verification"] = report
            self._prepare_deletion(op, report)

        self._spawn(op, run())
        return op

    def _prepare_deletion(self, op: dict[str, Any], report: dict[str, Any]) -> None:
        archive = Archive(self.archives.read(op["archive_id"]))
        records = {r["key"]: r for r in archive.records}
        deletable = [k for k in report["deletable"] if k in records]
        blocked = dict(report["blocked"])
        op["preview"] = {
            "delete": [{"key": k, "alias": records[k]["alias"], "config_id": records[k]["config_id"],
                        "entity_id": records[k]["entity_id"],
                        "config_hash": digest(records[k]["config"]),
                        "registry_hash": registry_digest(records[k]["registry"])} for k in deletable],
            "blocked": blocked}
        op["plan_hash"] = digest({"archive": self.archives.entries[op["archive_id"]]["sha256"],
                                  "delete": [[d["key"], d["config_hash"], d["registry_hash"]]
                                             for d in op["preview"]["delete"]]})[:24]
        for key in deletable:
            op["items"][key] = {"status": "pending", "alias": records[key]["alias"]}
        if deletable:
            op["status"] = "awaiting_confirmation"
            op["summary"] = (f"Exported and verified: {len(deletable)} automation(s) can be deleted"
                             + (f", {len(blocked)} blocked" if blocked else ""))
        else:
            op["status"] = "blocked"
            op["summary"] = "The archive verification blocks every deletion"
        self.journal.log(op, "info" if deletable else "warning", op["summary"])

    async def confirm_cleanup(self, data: dict[str, Any], by: str | None) -> dict[str, Any]:
        self._busy_check()
        op = self.operation(str(data.get("operation_id")))
        if op.get("kind") != "cleanup" or op.get("status") != "awaiting_confirmation":
            raise OperationError("This operation is not waiting for a confirmation", "not_awaiting", 409)
        if data.get("plan_hash") != op.get("plan_hash"):
            raise OperationError("The deletion preview changed; review it again", "stale_plan", 409)
        count = len(op["preview"]["delete"])
        if data.get("confirm_count") != count:
            raise OperationError(f"Type the number of automations to delete ({count}) to confirm", "not_confirmed")
        if data.get("archive_ack") is not True:
            raise OperationError("Confirm that the archive is kept (downloaded or stored in the App)", "not_confirmed")
        self._require_ha()
        op["status"] = "deleting"
        op["confirmed_by"] = by
        op["confirmed_at"] = iso()
        self.journal.log(op, "info", f"Deletion of {count} automation(s) confirmed")

        async def run() -> None:
            await self._delete(op)

        self._spawn(op, run())
        return op

    async def _delete(self, op: dict[str, Any]) -> None:
        ha, api = self._require_ha()
        archive = Archive(self.archives.read(op["archive_id"]))
        records = {r["key"]: r for r in archive.records}
        for index, entry in enumerate(op["preview"]["delete"]):
            key = entry["key"]
            if op["items"].get(key, {}).get("status") in ("deleted", "gone"):
                continue
            record = records[key]
            config_id = record["config_id"]
            await self._hook("delete", index)
            try:
                live = await api.get(config_id)
                reg = None
                if record["entity_id"]:
                    reg = (await ha.command("config/entity_registry/get_entries", entity_ids=[record["entity_id"]])
                           or {}).get(record["entity_id"])
            except (ConfigApiError, HomeAssistantError, ConnectionError, TimeoutError) as err:
                self.journal.item(op, key, status="failed", message=f"Could not re-read it: {err}")
                continue
            if live is None:
                self.journal.item(op, key, status="gone", message="It was already gone")
                continue
            if not same(live, record["config"]) or registry_digest(registry_view(reg)) != registry_digest(
                    record["registry"]):
                self.journal.item(op, key, status="skipped_changed",
                                  message="It changed after the export: not deleted (export it again)")
                self.journal.log(op, "warning", f"{record['alias']}: changed after the export, not deleted")
                continue
            self.journal.item(op, key, status="deleting")
            try:
                with ha.scoped(deletions={config_id}):
                    removed = await api.delete(config_id)
            except (ConfigApiError, NotAllowed) as err:
                self.journal.item(op, key, status="failed", message=str(err))
                self.journal.log(op, "error", f"{record['alias']}: not deleted: {err}")
                continue
            self.journal.item(op, key, status="deleted" if removed else "gone")
        await self._finish_cleanup(op, records)

    async def _finish_cleanup(self, op: dict[str, Any], records: dict[str, dict[str, Any]]) -> None:
        await asyncio.sleep(SETTLE_SECONDS)
        snap = await self.snapshot()
        remaining = []
        for key, entry in op["items"].items():
            if entry.get("status") not in ("deleted", "gone"):
                continue
            item = snap.item(key)
            if item is not None and (item.config is not None or item.state is not None):
                for _ in range(10):
                    await asyncio.sleep(0.5)
                    snap = await self.snapshot()
                    item = snap.item(key)
                    if item is None or (item.config is None and item.state is None):
                        break
            if item is not None and (item.config is not None or item.state is not None):
                remaining.append(key)
                self.journal.item(op, key, status="still_present", message="It is still in Home Assistant")
            else:
                self.journal.item(op, key, removal_verified=True)
        changed = []
        for key, before in (op.get("protected_before") or {}).items():
            item = snap.item(key)
            now = None if item is None else {"config": item.config_hash, "registry": item.registry_hash,
                                              "state": item.state}
            if now is None or now["config"] != before["config"] or now["registry"] != before["registry"]:
                changed.append({"key": key, "alias": before.get("alias"),
                                "change": "gone" if now is None else "changed"})
        counts = {s: sum(1 for e in op["items"].values() if e.get("status") == s)
                  for s in ("deleted", "gone", "skipped_changed", "failed", "still_present", "pending")}
        externals = [i for i in snap.items if i.source == "external"]
        op["report"] = {
            "counts": counts, "protected_checked": len(op.get("protected_before") or {}),
            "protected_changed": changed, "archive_id": op["archive_id"],
            "remaining_active": [{"key": i.key, "alias": i.alias, "source": i.source, "state": i.state}
                                 for i in snap.items if i.state in ("on", "off")],
            "external_remaining": len(externals),
        }
        done = counts["deleted"] + counts["gone"]
        if done == len(op["items"]) and not changed:
            op["status"] = "completed"
        elif done == 0:
            op["status"] = "failed"
        else:
            op["status"] = "partial"
        op["summary"] = (f"{counts['deleted']} deleted, {counts['skipped_changed']} skipped (changed), "
                         f"{counts['failed'] + counts['still_present']} failed; {len(changed)} protected automation(s) "
                         f"changed meanwhile; {len(op['report']['remaining_active'])} automation(s) remain active")
        self.journal.log(op, "info" if op["status"] == "completed" else "warning", op["summary"])
        if changed:
            self.journal.log(op, "warning", "Protected or excluded automations changed during the operation "
                                            "(not by this App): " + ", ".join(c["alias"] or c["key"] for c in changed))

    async def cancel(self, data: dict[str, Any], by: str | None) -> dict[str, Any]:
        op = self.operation(str(data.get("operation_id")))
        if op.get("status") not in ("awaiting_confirmation", "blocked", "interrupted", "partial"):
            raise OperationError("Only a waiting, partial or interrupted operation can be closed", "not_cancellable",
                                 409)
        op["resumable"] = False
        op["status"] = "cancelled"
        self.journal.log(op, "info", f"Closed without (further) changes by {by or 'the user'}")
        return op

    # ----------------------------------------------------------- interruptions
    async def reconcile(self, data: dict[str, Any], by: str | None) -> dict[str, Any]:
        """Look at Home Assistant after an interruption and record what really happened."""
        self._busy_check()
        op = self.operation(str(data.get("operation_id")))
        if op.get("status") != "interrupted":
            raise OperationError("This operation was not interrupted", "not_interrupted", 409)
        async with self.lock:
            snap = await self.snapshot(fetch=list(op.get("items") or {}))
            kind = op.get("kind")
            if kind == "cleanup" and op.get("archive_id") and op.get("preview"):
                archive = Archive(self.archives.read(op["archive_id"]))
                records = {r["key"]: r for r in archive.records}
                for key, entry in op["items"].items():
                    item = snap.item(key)
                    record = records.get(key) or {}
                    if item is None or item.config is None:
                        if entry.get("status") != "deleted":
                            self.journal.item(op, key, status="deleted", message="Confirmed deleted after the "
                                                                                  "interruption")
                    elif same(item.config, record.get("config")):
                        if entry.get("status") != "pending":
                            self.journal.item(op, key, status="pending", message="Not deleted (the interruption "
                                                                                 "came first)")
                    else:
                        self.journal.item(op, key, status="skipped_changed", message="Changed meanwhile")
                op["status"] = "partial"
                await self._finish_cleanup(op, records)
                pending = [k for k, e in op["items"].items() if e.get("status") == "pending"]
                op["resumable"] = bool(pending)
                if op["status"] == "completed":
                    self.journal.log(op, "info", "Reconciled: every automation was deleted before the interruption")
                else:
                    op["status"] = "partial"
                    self.journal.log(op, "warning", f"Reconciled: {len(pending)} automation(s) were not deleted. "
                                                    "Continue the deletion, or restore from the archive.")
            elif kind == "restore" and op.get("plan"):
                archive = Archive(self.archives.read(op["archive_id"]))
                runner = self._runner(op, archive, op["plan"])
                result = await runner.verify(op.get("mapping"))
                op["result"] = result
                op["status"] = "incomplete" if result["status"] != "completed" else "completed"
                self.journal.log(op, "warning" if op["status"] != "completed" else "info",
                                 "Reconciled after the interruption: " + json.dumps(result["counts"]) +
                                 ". Run the restore again to finish it (identical automations only get their "
                                 "metadata).")
            else:
                op["status"] = "failed"
                self.journal.log(op, "warning", "Reconciled: the interrupted operation left no archive; nothing "
                                                "in Home Assistant was changed by it.")
            self.journal.save(op)
        self._cache = None
        return op

    async def resume_cleanup(self, data: dict[str, Any], by: str | None) -> dict[str, Any]:
        """Continue an interrupted deletion: the remaining automations are verified again first."""
        self._busy_check()
        op = self.operation(str(data.get("operation_id")))
        if op.get("kind") != "cleanup" or not op.get("resumable"):
            raise OperationError("This deletion cannot be continued", "not_resumable", 409)
        pending = [k for k, e in op["items"].items() if e.get("status") == "pending"]
        archive_bytes = self.archives.read(op["archive_id"])
        snap = await self.snapshot(fetch=pending)
        report = verify(archive_bytes, live=snap, deletion_keys=pending,
                        stored_sha256=self.archives.entries[op["archive_id"]]["sha256"], verified_at=iso())
        op["verification_resume"] = report
        op["preview"]["delete"] = [d for d in op["preview"]["delete"] if d["key"] in report["deletable"]]
        op["preview"]["blocked"].update(report["blocked"])
        op["plan_hash"] = digest({"archive": self.archives.entries[op["archive_id"]]["sha256"], "resume": True,
                                  "delete": [[d["key"], d["config_hash"], d["registry_hash"]]
                                             for d in op["preview"]["delete"]]})[:24]
        op["resumable"] = False
        op["status"] = "awaiting_confirmation" if op["preview"]["delete"] else "partial"
        self.journal.log(op, "info", f"Verified again: {len(op['preview']['delete'])} automation(s) can still be "
                                     "deleted; confirm to continue")
        return op

    # ---------------------------------------------------------------- archives
    def archive_list(self) -> list[dict[str, Any]]:
        return self.archives.listing()

    def _load(self, archive_id: Any) -> tuple[bytes, Archive]:
        if not isinstance(archive_id, str) or archive_id not in self.archives.entries:
            raise OperationError("Unknown archive", "unknown_archive", 404)
        try:
            data = self.archives.read(archive_id)
        except KeyError:
            raise OperationError("The archive file is missing", "missing_archive", 404) from None
        try:
            return data, Archive(data)
        except ArchiveError as err:
            raise OperationError(f"The stored archive cannot be read: {err}", err.code, 422) from err

    def archive_view(self, archive_id: str) -> dict[str, Any]:
        entry = self.archives.entries.get(archive_id)
        if entry is None:
            raise OperationError("Unknown archive", "unknown_archive", 404)
        try:
            data, archive = self._load(archive_id)
        except OperationError as err:
            return {"entry": entry, "error": str(err)}
        m = archive.manifest
        return {"entry": entry, "summary": archive.summary(), "inventory": m.get("inventory"),
                "associations": m.get("associations"), "warnings": m.get("warnings"),
                "compatibility": m.get("compatibility"), "files": m.get("files"),
                "labels": archive.definitions("labels"), "categories": archive.definitions("categories"),
                "areas": archive.definitions("areas"), "blueprints": archive.definitions("blueprints"),
                "same_installation": (m.get("source_installation") or {}).get("app_instance_id")
                == self.identity.instance_id}

    async def verify_archive(self, data: dict[str, Any], by: str | None) -> dict[str, Any]:
        archive_id = data.get("archive_id")
        raw, archive = self._load(archive_id)
        live = None
        if data.get("live") and self.ha is not None and self.ha.connected:
            live = await self.snapshot(fetch=[i.get("config_id") for i in archive.manifest.get("inventory") or []
                                              if isinstance(i, dict) and i.get("source") == "ui"])
        entry = self.archives.entries[archive_id]
        report = verify(raw, live=live, stored_sha256=entry.get("sha256"), verified_at=iso(),
                        expected_keys=(archive.manifest.get("selection") or {}).get("keys"))
        self.archives.update(archive_id, verification={"status": report["status"], "at": report["verified_at"],
                                                       "ok": report["ok"], "live": live is not None})
        return report

    def archive_file(self, archive_id: str) -> tuple[bytes, str]:
        raw, archive = self._load(archive_id)
        created = re.sub(r"[^0-9T]", "", str(archive.manifest.get("created_at") or "")[:19])
        count = (archive.manifest.get("counts") or {}).get("automations")
        return raw, f"automations-{created or archive_id}-{count if isinstance(count, int) else 'x'}.zip"

    async def delete_archive(self, data: dict[str, Any], by: str | None) -> dict[str, Any]:
        archive_id = data.get("archive_id")
        if data.get("confirm") is not True:
            raise OperationError("Deleting an archive needs a confirmation", "not_confirmed")
        if not isinstance(archive_id, str) or archive_id not in self.archives.entries:
            raise OperationError("Unknown archive", "unknown_archive", 404)
        pinned = [o for o in self.journal.operations.values() if o.get("archive_id") == archive_id
                  and o.get("status") in ("awaiting_confirmation", "interrupted", "deleting", "restoring", "partial")]
        if pinned:
            raise OperationError("This archive belongs to an operation that is not finished (waiting for a "
                                 "confirmation, interrupted or partial): finish or close it first", "pinned", 409)
        self.archives.delete(archive_id)
        op = self.journal.create("archive_delete", by, title="Archive deleted", status="completed",
                                 archive_id=archive_id, summary=f"Archive {archive_id} deleted")
        return {"deleted": archive_id, "operation": op["id"]}

    async def upload(self, raw: bytes, filename: str, by: str | None) -> dict[str, Any]:
        try:
            archive = Archive(raw)
        except ArchiveError as err:
            raise OperationError(str(err), err.code, 422) from err
        report = verify(raw, verified_at=iso())
        if report["status"] == "failed":
            failed = [c for c in report["checks"] if not c["ok"]]
            raise OperationError("The archive failed verification: " + "; ".join(
                d for c in failed for d in c["details"][:3]), "verification_failed", 422, report=report)
        sha = report["summary"]["sha256"]
        existing = next((e for e in self.archives.entries.values() if e.get("sha256") == sha), None)
        if existing:
            return {"archive": existing, "verification": report, "duplicate": True}
        m = archive.manifest
        archive_id = new_id("aie")
        entry = self.archives.add(archive_id, raw, {
            "created_at": m.get("created_at"), "uploaded_at": iso(), "purpose": "uploaded",
            "original_purpose": m.get("purpose"), "source_archive_id": m.get("archive_id"),
            "label": str(filename or "")[:120], "count": (m.get("counts") or {}).get("automations"), "sha256": sha,
            "ha_version": (m.get("home_assistant") or {}).get("version"),
            "location_name": (m.get("home_assistant") or {}).get("location_name"), "complete": m.get("complete"),
            "warnings": len(m.get("warnings") or []),
            "source_instance": (m.get("source_installation") or {}).get("app_instance_id"),
            "verification": {"status": report["status"], "at": report["verified_at"], "ok": report["ok"]}})
        self.journal.create("upload", by, title="Archive uploaded", status="completed", archive_id=archive_id,
                            summary=f"Uploaded {filename or 'an archive'}: verification {report['status']}")
        return {"archive": entry, "verification": report, "duplicate": False}

    async def import_yaml(self, data: dict[str, Any], by: str | None) -> dict[str, Any]:
        """An ordinary Home Assistant automation YAML file becomes an archive (configuration only)."""
        text = data.get("text")
        filename = str(data.get("filename") or "automations.yaml")[:120]
        if not isinstance(text, str) or not text.strip():
            raise OperationError("The file is empty", "empty_file")
        try:
            parsed = yamlio.load_tolerant(text)
        except Exception as err:  # noqa: BLE001 - YAML errors are reported
            raise OperationError(f"The file is not valid YAML: {err}", "invalid_yaml", 422) from err
        if isinstance(parsed, dict) and "automation" in parsed and isinstance(parsed["automation"], list):
            parsed = parsed["automation"]
        if isinstance(parsed, dict):
            parsed = [parsed]
        if not isinstance(parsed, list) or not parsed:
            raise OperationError("The file contains no automations (a list of automations was expected)",
                                 "no_automations", 422)
        if len(parsed) > MAX_YAML_ITEMS:
            raise OperationError(f"The file has more than {MAX_YAML_ITEMS} automations", "too_many", 422)
        snap = await self.snapshot()
        items: list[Item] = []
        refused: list[dict[str, Any]] = []
        seen: set[str] = set()
        stamp = int(time.time() * 1000)
        for index, entry in enumerate(parsed, start=1):
            if not isinstance(entry, dict):
                refused.append({"index": index, "reason": "not an automation (a mapping was expected)"})
                continue
            tags = yamlio.find_tags(entry)
            if tags:
                refused.append({"index": index, "alias": str(entry.get("alias") or ""),
                                "reason": "uses tags that cannot be resolved here: " + ", ".join(tags[:5])})
                continue
            if not yamlio.json_compatible(entry):
                refused.append({"index": index, "alias": str(entry.get("alias") or ""),
                                "reason": "contains values Home Assistant's editor cannot store (dates, binary...)"})
                continue
            if not any(k in entry for k in ("triggers", "trigger", "use_blueprint")):
                refused.append({"index": index, "alias": str(entry.get("alias") or ""),
                                "reason": "has no triggers and no blueprint"})
                continue
            config = dict(entry)
            problems: list[dict[str, Any]] = []
            if "id" not in config:
                while str(stamp) in seen or str(stamp) in snap.configs:
                    stamp += 1
                config = {"id": str(stamp), **config}
                stamp += 1
                problems.append({"level": "info", "code": "id_assigned",
                                 "message": f"It had no id; the id {config['id']} was assigned"})
            config_id = str(config["id"]) if not isinstance(config["id"], str) else config["id"]
            if not isinstance(config["id"], str):
                config["id"] = config_id
                problems.append({"level": "info", "code": "id_text", "message": "Its id was converted to text"})
            if config_id in seen:
                refused.append({"index": index, "alias": str(entry.get("alias") or ""),
                                "reason": f"the id {config_id} occurs more than once in the file"})
                continue
            seen.add(config_id)
            owner, reasons = _owner(config_id, config, None)
            if owner == "builder":
                problems.append({"level": "info", "code": "builder", "message": reasons[0] + ": protected."})
            refs = deps.references(config)
            known = {"entities": snap.entity_ids, "devices": snap.devices, "areas": set(snap.areas),
                     "floors": set(snap.floors), "labels": set(snap.labels),
                     "blueprints": {p for p, v in snap.blueprints.items() if "metadata" in v}}
            problems.extend(deps.check(refs, **known))
            items.append(Item(key=config_id, config_id=config_id, entity_id=None,
                              alias=str(config.get("alias") or config_id), source="yaml_file", owner=owner,
                              state=None, config=config, config_origin=filename, registry=None, problems=problems,
                              references=refs, attributes={}))
        if not items:
            raise OperationError("No automation in the file can be imported", "nothing_importable", 422,
                                 refused=refused)
        op = self.journal.create("yaml_import", by, title=f"YAML file {filename}", status="running")
        warnings = [{"level": "warning", "code": "refused", "message": f"Entry {r['index']} not imported: {r['reason']}"}
                    for r in refused]
        async with self.lock:
            archive_id, report = await self._archive(op, snap, items, purpose="yaml_import", selection={
                "mode": "yaml_file", "keys": [i.key for i in items], "label": filename}, live_compare=False)
        self.archives.update(archive_id, label=filename, refused=refused)
        op["status"] = "completed"
        op["summary"] = f"{len(items)} automation(s) read from {filename}" + (
            f", {len(refused)} refused" if refused else "")
        op["refused"] = refused
        self.journal.log(op, "info", op["summary"])
        for w in warnings:
            self.journal.log(op, "warning", w["message"])
        return {"archive_id": archive_id, "imported": len(items), "refused": refused, "operation": op["id"],
                "verification": {"status": report["status"]}}

    # ----------------------------------------------------------------- restore
    async def restore_plan(self, data: dict[str, Any]) -> dict[str, Any]:
        _, archive = self._load(data.get("archive_id"))
        snap = await self.snapshot()
        ha, _api = self._require_ha()
        plan = await build_plan(archive, snap, data.get("decisions") or {}, instance_id=self.identity.instance_id,
                                folder=self.folder, ha=ha)
        plan["live_choices"] = {
            "labels": [{"id": k, "name": v.get("name")} for k, v in snap.labels.items()],
            "categories": [{"id": k, "name": v.get("name"), "scope": s} for s, d in snap.categories.items()
                           for k, v in d.items()],
            "areas": [{"id": k, "name": v.get("name")} for k, v in snap.areas.items()]}
        return plan

    def _runner(self, op: dict[str, Any], archive: Archive, plan: dict[str, Any]) -> RestoreRunner:
        ha, api = self._require_ha()
        return RestoreRunner(
            ha=ha, api=api, archive=archive, plan=plan,
            log=lambda level, message: self.journal.log(op, level, message),
            item=lambda key, **changes: self.journal.item(op, key, **changes),
            snapshot=self.snapshot, hook=self._hook, items_view=op["items"])

    async def restore_execute(self, data: dict[str, Any], by: str | None) -> dict[str, Any]:
        self._busy_check()
        raw, archive = self._load(data.get("archive_id"))
        snap = await self.snapshot()
        ha, _api = self._require_ha()
        decisions = data.get("decisions") or {}
        plan = await build_plan(archive, snap, decisions, instance_id=self.identity.instance_id, folder=self.folder,
                                ha=ha)
        if data.get("plan_hash") != plan["plan_hash"]:
            raise OperationError("Home Assistant or the choices changed since the preview: review it again",
                                 "stale_plan", 409, plan=plan)
        if plan["acknowledgements_missing"]:
            raise OperationError("Some deviations from an exact restoration are not acknowledged yet",
                                 "not_acknowledged", 400, missing=plan["acknowledgements_missing"])
        if not plan["ready"]:
            raise OperationError("There is nothing to restore with these choices", "nothing_to_restore")
        if data.get("confirm") is not True:
            raise OperationError("A restore needs a confirmation", "not_confirmed")
        op = self.journal.create("restore", by, title="Restore", status="restoring", archive_id=data["archive_id"],
                                 plan=plan, decisions=decisions)
        for entry in plan["items"]:
            if entry.get("action") in (*ACTIONS_WRITING, "metadata"):
                op["items"][entry["key"]] = {"status": "pending", "alias": entry.get("alias"),
                                             "action": entry["action"], "target_id": entry.get("target_id")}
        self.journal.save(op)

        async def run() -> None:
            touched = [i["target_id"] for i in plan["items"] if i.get("action") in ("replace", "metadata")]
            if touched:
                items = [snap.item(t) for t in touched if snap.item(t) is not None]
                if items:
                    backup_id, report = await self._archive(op, snap, items, purpose="pre_restore", selection={
                        "mode": "pre_restore", "keys": [i.key for i in items],
                        "label": f"Before restore {op['id']}"})
                    if report["status"] == "failed":
                        raise OperationError("The backup before replacing could not be verified; nothing was "
                                             "replaced", "backup_failed")
                    op["pre_restore_archive"] = backup_id
                    op["archive_id"] = data["archive_id"]
                    self.journal.log(op, "info", f"Backup of {len(items)} automation(s) that will be replaced or "
                                                 f"re-labelled: {backup_id}")
            runner = self._runner(op, archive, plan)
            result = await runner.run()
            op["mapping"] = getattr(runner, "mapping", None)
            op["result"] = result
            op["status"] = result["status"] if result["status"] != "nothing" else "failed"
            op["summary"] = (f"{result['counts']['restored']} restored exactly, {result['counts']['incomplete']} "
                             f"incomplete, {result['counts']['failed']} failed")
            self.journal.log(op, "info" if op["status"] == "completed" else "warning", op["summary"])
            for key, res in result["results"].items():
                for discrepancy in res["discrepancies"]:
                    self.journal.log(op, "warning", f"{key}: {discrepancy}")

        self._spawn(op, run())
        return op

    async def rollback(self, data: dict[str, Any], by: str | None) -> dict[str, Any]:
        """Undo a restore: remove what it created, put back what it replaced (from its own backup)."""
        self._busy_check()
        op = self.operation(str(data.get("operation_id")))
        if op.get("kind") != "restore" or op.get("status") not in ("completed", "incomplete", "failed", "partial"):
            raise OperationError("Only a finished restore can be rolled back", "not_rollbackable", 409)
        if data.get("confirm") is not True:
            raise OperationError("A rollback needs a confirmation", "not_confirmed")
        self._require_ha()
        rb = self.journal.create("rollback", by, title=f"Rollback of {op['id']}", status="rolling_back",
                                 archive_id=op.get("pre_restore_archive"), restore_operation=op["id"])

        async def run() -> None:
            ha, api = self._require_ha()
            for entry in op["plan"]["items"]:
                key = entry["key"]
                state = (op["items"].get(key) or {}).get("status")
                if entry.get("action") not in ("create", "new") or state in (None, "pending", "failed"):
                    continue
                target = entry["target_id"]
                live = await api.get(target)
                if live is None:
                    self.journal.item(rb, key, status="gone")
                    continue
                if not same(live, entry["config"]):
                    self.journal.item(rb, key, status="kept_changed", message="Changed after the restore: kept")
                    continue
                with ha.scoped(deletions={target}):
                    await api.delete(target)
                self.journal.item(rb, key, status="removed", target_id=target)
            backup = op.get("pre_restore_archive")
            if backup:
                _, archive = self._load(backup)
                snap = await self.snapshot()
                keys = [r["key"] for r in archive.records]
                decisions = {"selection": keys, "collisions": {k: "replace" for k in keys}}
                plan = await build_plan(archive, snap, decisions, instance_id=self.identity.instance_id,
                                        folder=self.folder, ha=ha)
                plan_acks = plan["acknowledgements_missing"]
                if plan_acks:
                    decisions["acknowledgements"] = plan_acks
                    plan = await build_plan(archive, snap, decisions, instance_id=self.identity.instance_id,
                                            folder=self.folder, ha=ha)
                for entry in plan["items"]:
                    if entry.get("action") in (*ACTIONS_WRITING, "metadata"):
                        rb["items"][entry["key"]] = {"status": "pending", "action": entry["action"]}
                runner = self._runner(rb, archive, plan)
                result = await runner.run()
                rb["result"] = result
            counts = {s: sum(1 for e in rb["items"].values() if e.get("status") == s)
                      for s in ("removed", "restored", "incomplete", "failed", "kept_changed", "gone")}
            rb["status"] = "completed" if not (counts["incomplete"] or counts["failed"] or counts["kept_changed"]) \
                else "incomplete"
            rb["summary"] = ", ".join(f"{v} {k}" for k, v in counts.items() if v) or "nothing to undo"
            self.journal.log(rb, "info" if rb["status"] == "completed" else "warning", "Rollback: " + rb["summary"])
            op["rolled_back_by"] = rb["id"]
            self.journal.save(op)

        self._spawn(rb, run())
        return rb

    # ------------------------------------------------------------- diagnostics
    def diagnostics(self) -> dict[str, Any]:
        return {
            "app": {"name": APP_NAME, "version": APP_VERSION, "instance_id": self.identity.instance_id},
            "settings": self.settings.public(), "connection": self.connection(),
            "commands_sent": dict(self.ha.commands_sent) if self.ha else {},
            "requests": dict(self.api.requests) if self.api else {},
            "archives": len(self.archives.entries), "operations": self.journal.listing(30),
            "log": self.buffer.entries() if self.buffer else [],
        }
