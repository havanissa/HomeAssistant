"""Operations on helpers, labels and categories (0.2): the usage overview, backups, Back up & Delete, restore.

They follow the automation operations: every change is a journalled operation that runs in the background;
nothing is deleted before a verified archive exists and the user confirmed the exact list (by its plan hash and
its number); a restore runs only the plan the user saw. Mixed into :class:`aie.core.AppCore`.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from .archive import ArchiveError
from .canonical import digest, sha256_bytes
from .ha import HELPER_DOMAINS, HomeAssistantError, NotAllowed, registry_key
from .regarchive import RegistryArchive, build_registry_archive, verify_registry
from .registry import RegistrySnapshot, collect_users, item_fingerprint, overview, take_registry_snapshot
from .regrestore import WRITING, RegistryRestoreRunner, build_registry_plan
from .store import iso, new_id

REGISTRY_CACHE_SECONDS = 5.0
DELETE_ORDER = {"helper": 0, "category": 1, "label": 2}


class RegistryOps:
    """Mixin for AppCore (uses its journal, archives, lock, folder, identity, snapshot() and _spawn())."""

    _reg_cache: tuple[float, RegistrySnapshot] | None = None

    async def registry_snapshot(self, *, usage: bool = True, cached: bool = False) -> RegistrySnapshot:
        from .core import OperationError
        if cached and usage and self._reg_cache and time.monotonic() - self._reg_cache[0] < REGISTRY_CACHE_SECONDS:
            return self._reg_cache[1]
        ha, _api = self._require_ha()
        try:
            snap = await take_registry_snapshot(ha, now=iso())
            if usage:
                autos = await self.snapshot(cached=True)
                automations = [(i.entity_id or i.key, i.alias, i.config) for i in autos.items if i.config is not None]
                entry_ids = {str(e["entry_id"]) for e in snap.entry_helpers if e.get("entry_id")}
                options = await asyncio.to_thread(self.folder.helper_entry_options, entry_ids)
                files = await asyncio.to_thread(self.folder.yaml_texts)
                await collect_users(ha, snap, automations=automations, entry_options=options, yaml_files=files)
                if not self.folder.available:
                    snap.problems.append({
                        "level": "warning", "code": "config_folder_unavailable",
                        "message": "Home Assistant's configuration folder is not readable by the App: YAML files "
                                   "and integration helpers (templates, groups) are not searched for references."})
        except (HomeAssistantError, ConnectionError, TimeoutError) as err:
            raise OperationError(f"Home Assistant could not be read: {err}", "ha_error", 503) from err
        if usage:
            self._reg_cache = (time.monotonic(), snap)
        return snap

    async def registry_view(self) -> dict[str, Any]:
        snap = await self.registry_snapshot(cached=True)
        return {**overview(snap), "config_folder": self.folder.available}

    # ---------------------------------------------------------------- archives
    def _keys(self, snap: RegistrySnapshot, keys: Any) -> list[str]:
        from .core import OperationError
        if not isinstance(keys, list) or not keys or not all(isinstance(k, str) for k in keys):
            raise OperationError("Select at least one helper, label or category", "empty_selection")
        out, missing = [], []
        for key in dict.fromkeys(keys):
            (out.append(key) if item_fingerprint(snap, key) is not None else missing.append(key))
        if missing:
            raise OperationError(f"Not in Home Assistant (any more), or not something this App can back up: "
                                 f"{', '.join(missing[:5])}", "unknown_key", 409)
        return out

    @staticmethod
    def _all_keys(snap: RegistrySnapshot) -> list[str]:
        return [*(registry_key("label", i) for i in sorted(snap.labels)),
                *(registry_key("category", s, i) for s, d in sorted(snap.categories.items()) for i in sorted(d)),
                *sorted(snap.helpers)]

    async def _registry_archive(self, op: dict[str, Any], snap: RegistrySnapshot, keys: list[str], *, purpose: str,
                                selection: dict[str, Any], deletion: list[str] | None = None) -> tuple[str, dict]:
        autos = await self.snapshot(cached=True)
        archive_id = new_id("aie")
        created_at = iso()
        data, manifest = build_registry_archive(
            archive_id=archive_id, created_at=created_at, purpose=purpose, selection=selection, keys=keys, snap=snap,
            installation=self.installation(autos), ha_config=autos.ha_config)
        counts = manifest["counts"]
        self.journal.log(op, "info", f"Archive {archive_id} built: {counts['helpers']} helper(s), {counts['labels']} "
                                     f"label(s), {counts['categories']} categor(ies), {len(data)} bytes")
        entry = self.archives.add(archive_id, data, {
            "kind": "registry", "created_at": created_at, "purpose": purpose, "label": selection.get("label"),
            "count": counts["items"], "counts": counts, "sha256": sha256_bytes(data), "ha_version": autos.ha_config.get("version"),
            "location_name": autos.ha_config.get("location_name"), "complete": True, "operation_id": op["id"],
            "source_instance": self.identity.instance_id})
        op["archive_id"] = archive_id
        live = await self.registry_snapshot(usage=False)
        report = verify_registry(data, live=live, expected_keys=keys, deletion_keys=deletion,
                                 stored_sha256=entry["sha256"], verified_at=iso())
        self.archives.update(archive_id, verification={"status": report["status"], "at": report["verified_at"],
                                                       "ok": report["ok"]})
        self.journal.log(op, "info" if report["ok"] else "warning",
                         f"Verification: {report['status']} ({sum(1 for c in report['checks'] if c['ok'])}/"
                         f"{len(report['checks'])} checks passed)")
        return archive_id, report

    async def start_registry_backup(self, data: dict[str, Any], by: str | None) -> dict[str, Any]:
        from .core import OperationError
        self._busy_check()
        self._require_ha()
        everything = data.get("mode") == "all"
        op = self.journal.create("registry_backup", by, status="exporting",
                                 title="Backup of all helpers, labels and categories" if everything
                                 else "Backup of helpers, labels and categories")

        async def run() -> None:
            snap = await self.registry_snapshot(usage=False)
            keys = self._all_keys(snap) if everything else self._keys(snap, data.get("keys"))
            if not keys:
                raise OperationError("There is nothing to back up", "empty_selection")
            op["selection"] = {"mode": "all" if everything else "selected", "keys": keys}
            archive_id, report = await self._registry_archive(op, snap, keys, purpose="registry_backup", selection={
                "mode": "all" if everything else "selected", "keys": keys,
                "label": str(data.get("label") or "")[:80]})
            op["verification"] = report
            op["status"] = "completed"
            op["summary"] = f"{len(keys)} item(s) backed up; verification {report['status']}"
            self.journal.log(op, "info", op["summary"])

        self._spawn(op, run())
        return op

    # ---------------------------------------------------------------- back up & delete
    async def start_registry_cleanup(self, data: dict[str, Any], by: str | None) -> dict[str, Any]:
        from .core import OperationError
        self._busy_check()
        self._require_ha()
        keys = data.get("keys")
        if not isinstance(keys, list) or not keys:
            raise OperationError("Select at least one helper, label or category to delete", "empty_selection")
        op = self.journal.create("registry_cleanup", by, status="exporting",
                                 title="Back up & delete helpers, labels and categories")

        async def run() -> None:
            snap = await self.registry_snapshot()
            selected = self._keys(snap, keys)
            listing = overview(snap)
            by_key = {x["key"]: x for x in [*listing["helpers"], *listing["labels"], *listing["categories"]]}
            op["selection"] = {"requested": selected}
            archive_id, report = await self._registry_archive(op, snap, selected, purpose="registry_cleanup",
                                                              selection={"mode": "cleanup", "keys": selected,
                                                                         "label": str(data.get("label") or "")[:80]},
                                                              deletion=selected)
            op["verification"] = report
            archive = RegistryArchive(self.archives.read(archive_id))
            fingerprints = {i["key"]: i["fingerprint"] for i in archive.manifest["items"]}
            deletable = sorted(report["deletable"], key=lambda k: (DELETE_ORDER[k.split(":")[0]], k))
            op["preview"] = {
                "delete": [{"key": k, "kind": k.split(":")[0], "name": by_key[k]["name"],
                            "detail": by_key[k].get("type") or by_key[k].get("scope_name") or "",
                            "count": by_key[k]["count"], "usage": by_key[k]["usage"],
                            "fingerprint": fingerprints[k]} for k in deletable],
                "blocked": report["blocked"]}
            op["in_use"] = [d["key"] for d in op["preview"]["delete"] if d["count"]]
            op["plan_hash"] = digest({"archive": self.archives.entries[archive_id]["sha256"],
                                      "delete": [[d["key"], d["fingerprint"]] for d in op["preview"]["delete"]]})[:24]
            for d in op["preview"]["delete"]:
                op["items"][d["key"]] = {"status": "pending", "alias": d["name"], "kind": d["kind"]}
            if deletable:
                op["status"] = "awaiting_confirmation"
                op["summary"] = (f"Backed up and verified: {len(deletable)} item(s) can be deleted"
                                 + (f", {len(op['in_use'])} of them still in use" if op["in_use"] else ""))
            else:
                op["status"] = "blocked"
                op["summary"] = "The archive verification blocks every deletion"
            self.journal.log(op, "info" if deletable else "warning", op["summary"])

        self._spawn(op, run())
        return op

    async def confirm_registry_cleanup(self, data: dict[str, Any], by: str | None) -> dict[str, Any]:
        from .core import OperationError
        self._busy_check()
        op = self.operation(str(data.get("operation_id")))
        if op.get("kind") != "registry_cleanup" or op.get("status") != "awaiting_confirmation":
            raise OperationError("This operation is not waiting for a confirmation", "not_awaiting", 409)
        if data.get("plan_hash") != op.get("plan_hash"):
            raise OperationError("The deletion preview changed; review it again", "stale_plan", 409)
        count = len(op["preview"]["delete"])
        if data.get("confirm_count") != count:
            raise OperationError(f"Type the number of items to delete ({count}) to confirm", "not_confirmed")
        if data.get("archive_ack") is not True:
            raise OperationError("Confirm that the archive is kept (downloaded or stored in the App)", "not_confirmed")
        if op.get("in_use") and data.get("in_use_ack") is not True:
            raise OperationError("Some selected items are still in use: confirm that you want to delete them anyway",
                                 "not_confirmed")
        self._require_ha()
        op["status"] = "deleting"
        op["confirmed_by"] = by
        op["confirmed_at"] = iso()
        self.journal.log(op, "info", f"Deletion of {count} item(s) confirmed")

        async def run() -> None:
            await self._registry_delete(op)

        self._spawn(op, run())
        return op

    async def _registry_delete(self, op: dict[str, Any]) -> None:
        ha, _api = self._require_ha()
        for index, entry in enumerate(op["preview"]["delete"]):
            key = entry["key"]
            if op["items"].get(key, {}).get("status") in ("deleted", "gone"):
                continue
            await self._hook("registry_delete", index)
            live = await self.registry_snapshot(usage=False)
            now = item_fingerprint(live, key)
            if now is None:
                self.journal.item(op, key, status="gone", message="It was already gone")
                continue
            if now != entry["fingerprint"]:
                self.journal.item(op, key, status="skipped_changed",
                                  message="It changed after the backup: not deleted (back it up again)")
                self.journal.log(op, "warning", f"{entry['name']}: changed after the backup, not deleted")
                continue
            self.journal.item(op, key, status="deleting")
            kind, _, rest = key.partition(":")
            try:
                with ha.scoped(registry_deletions={key}):
                    if kind == "label":
                        await ha.command("config/label_registry/delete", label_id=rest)
                    elif kind == "category":
                        scope, _, category_id = rest.partition(":")
                        await ha.command("config/category_registry/delete", scope=scope, category_id=category_id)
                    else:
                        domain, _, item_id = rest.partition(":")
                        if domain not in HELPER_DOMAINS:
                            raise NotAllowed(f"{domain} helpers are not deleted by this App")
                        await ha.command(f"{domain}/delete", **{f"{domain}_id": item_id})
            except (HomeAssistantError, NotAllowed) as err:
                self.journal.item(op, key, status="failed", message=str(err))
                self.journal.log(op, "error", f"{entry['name']}: not deleted: {err}")
                continue
            self.journal.item(op, key, status="deleted")
        await self._finish_registry_cleanup(op)

    async def _finish_registry_cleanup(self, op: dict[str, Any]) -> None:
        from .core import SETTLE_SECONDS
        await asyncio.sleep(SETTLE_SECONDS)
        live = await self.registry_snapshot(usage=False)
        for key, entry in op["items"].items():
            if entry.get("status") in ("deleted", "gone"):
                if item_fingerprint(live, key) is not None:
                    self.journal.item(op, key, status="still_present", message="It is still in Home Assistant")
                else:
                    self.journal.item(op, key, removal_verified=True)
        counts = {s: sum(1 for e in op["items"].values() if e.get("status") == s)
                  for s in ("deleted", "gone", "skipped_changed", "failed", "still_present", "pending")}
        op["report"] = {"counts": counts, "archive_id": op["archive_id"]}
        done = counts["deleted"] + counts["gone"]
        op["status"] = "completed" if done == len(op["items"]) else ("failed" if done == 0 else "partial")
        op["summary"] = (f"{counts['deleted']} deleted, {counts['skipped_changed']} skipped (changed), "
                         f"{counts['failed'] + counts['still_present']} failed")
        self.journal.log(op, "info" if op["status"] == "completed" else "warning", op["summary"])

    async def reconcile_registry(self, op: dict[str, Any]) -> None:
        """After an interruption: record which items are really gone."""
        live = await self.registry_snapshot(usage=False)
        if op.get("kind") == "registry_cleanup" and op.get("preview"):
            for entry in op["preview"]["delete"]:
                key = entry["key"]
                if item_fingerprint(live, key) is None:
                    self.journal.item(op, key, status="deleted", message="Confirmed deleted after the interruption")
                elif op["items"].get(key, {}).get("status") not in ("pending", "skipped_changed"):
                    self.journal.item(op, key, status="pending", message="Not deleted (the interruption came first)")
            pending = [k for k, e in op["items"].items() if e.get("status") == "pending"]
            op["status"] = "partial" if pending else "completed"
            op["summary"] = (f"Reconciled: {len(pending)} item(s) were not deleted; restore from the archive or "
                             "start a new deletion" if pending else "Reconciled: every item was deleted")
        elif op.get("kind") == "registry_restore" and op.get("plan"):
            _raw, archive = self._load_registry(op["archive_id"])
            runner = RegistryRestoreRunner(ha=self._require_ha()[0], archive=archive, plan=op["plan"],
                                           log=lambda level, message: self.journal.log(op, level, message),
                                           item=lambda key, **c: self.journal.item(op, key, **c), now=iso)
            runner.labels = dict((op.get("mapping") or {}).get("labels") or {})
            runner.categories = dict((op.get("mapping") or {}).get("categories") or {})
            result = await runner.verify()
            op["result"] = result
            op["status"] = "completed" if result["status"] == "completed" else "incomplete"
            op["summary"] = "Reconciled after the interruption: run the restore again to finish it"
        else:
            op["status"] = "failed"
            op["summary"] = "Reconciled: the interrupted operation left no archive; nothing was changed by it"
        self.journal.log(op, "info", op["summary"])

    # ---------------------------------------------------------------- restore
    def _load_registry(self, archive_id: Any) -> tuple[bytes, RegistryArchive]:
        from .core import OperationError
        if not isinstance(archive_id, str) or archive_id not in self.archives.entries:
            raise OperationError("Unknown archive", "unknown_archive", 404)
        try:
            raw = self.archives.read(archive_id)
        except KeyError:
            raise OperationError("The archive file is missing", "missing_archive", 404) from None
        try:
            return raw, RegistryArchive(raw)
        except ArchiveError as err:
            raise OperationError(f"The stored archive cannot be read: {err}", err.code, 422) from err

    async def registry_restore_plan(self, data: dict[str, Any]) -> dict[str, Any]:
        _, archive = self._load_registry(data.get("archive_id"))
        snap = await self.registry_snapshot(usage=False)
        return build_registry_plan(archive, snap, data.get("decisions") or {}, instance_id=self.identity.instance_id)

    async def registry_restore_execute(self, data: dict[str, Any], by: str | None) -> dict[str, Any]:
        from .core import OperationError
        self._busy_check()
        _, archive = self._load_registry(data.get("archive_id"))
        snap = await self.registry_snapshot(usage=False)
        decisions = data.get("decisions") or {}
        plan = build_registry_plan(archive, snap, decisions, instance_id=self.identity.instance_id)
        if data.get("plan_hash") != plan["plan_hash"]:
            raise OperationError("Home Assistant or the choices changed since the preview: review it again",
                                 "stale_plan", 409, plan=plan)
        if not plan["ready"]:
            raise OperationError("There is nothing to restore with these choices", "nothing_to_restore")
        if data.get("confirm") is not True:
            raise OperationError("A restore needs a confirmation", "not_confirmed")
        op = self.journal.create("registry_restore", by, title="Restore of helpers, labels and categories",
                                 status="restoring", archive_id=data["archive_id"], plan=plan, decisions=decisions)
        for entry in plan["items"]:
            if entry["action"] in (*WRITING, "exists", "use_existing"):
                op["items"][entry["key"]] = {"status": "pending", "alias": entry.get("name"), "action": entry["action"],
                                             "kind": entry["kind"]}
        self.journal.save(op)

        async def run() -> None:
            touched = [i["key"] for i in plan["items"] if i["action"] in ("update", "replace", "metadata")]
            if touched:
                backup_id, report = await self._registry_archive(op, snap, touched, purpose="pre_restore", selection={
                    "mode": "pre_restore", "keys": touched, "label": f"Before restore {op['id']}"})
                if report["status"] == "failed":
                    raise OperationError("The backup before updating could not be verified; nothing was changed",
                                         "backup_failed")
                op["pre_restore_archive"] = backup_id
                op["archive_id"] = data["archive_id"]
                self.journal.log(op, "info", f"Backup of {len(touched)} item(s) that will be updated: {backup_id}")
            ha, _api = self._require_ha()
            runner = RegistryRestoreRunner(ha=ha, archive=archive, plan=plan,
                                           log=lambda level, message: self.journal.log(op, level, message),
                                           item=lambda key, **c: self.journal.item(op, key, **c), now=iso,
                                           hook=self._hook)
            try:
                result = await runner.run()
            finally:
                op["mapping"] = {"labels": runner.labels, "categories": runner.categories}
            op["result"] = result
            op["status"] = result["status"]
            op["summary"] = (f"{result['counts']['restored']} restored exactly, {result['counts']['incomplete']} "
                             f"incomplete, {result['counts']['failed']} failed")
            self.journal.log(op, "info" if op["status"] == "completed" else "warning", op["summary"])
            for key, res in result["results"].items():
                for discrepancy in res["discrepancies"]:
                    self.journal.log(op, "warning", f"{key}: {discrepancy}")

        self._spawn(op, run())
        return op

    # ---------------------------------------------------------------- archive views
    def registry_archive_view(self, archive_id: str, entry: dict[str, Any]) -> dict[str, Any]:
        from .core import OperationError
        try:
            _, archive = self._load_registry(archive_id)
            labels, categories, helpers = archive.labels, archive.categories, archive.helpers
        except (OperationError, ArchiveError) as err:
            return {"entry": entry, "kind": "registry", "error": str(err)}
        return {"entry": entry, "kind": "registry", "summary": archive.summary(), "items": archive.manifest.get("items"),
                "labels": labels, "categories": categories,
                "helpers": {k: {kk: v.get(kk) for kk in ("domain", "id", "entity_id", "config", "registry")}
                            for k, v in helpers.items()},
                "files": archive.manifest.get("files"),
                "same_installation": (archive.manifest.get("source_installation") or {}).get("app_instance_id")
                == self.identity.instance_id}
