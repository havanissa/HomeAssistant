"""End-to-end checks of the real App image against a real, disposable Home Assistant 2026.11.1.

    python e2e_checks.py <tokens.json> <data dir> <stack dir> <ha config dir> [phase ...]

Phases (default: all, in this order): setup roundtrip crossinstall blueprint changes collisions corrupt yaml
duplicates interrupt_export interrupt_delete interrupt_restore failed_write restart security registry builder.

Every phase starts from the seeded fixtures (prepare.py seed) and leaves them as they were. Home Assistant is
read and changed for test purposes only with the test's own administrator token; the App acts through the fake
Supervisor with its own user, and everything it sends is recorded there.
"""

from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
import subprocess
import sys
import time
from typing import Any
import zipfile

import aiohttp
import yaml
from fixtures import AUTOMATIONS, BUILDER, EXTERNAL, ORDINARY, REVIEW
from hactl import HA, registry_fields
from prepare import seed

BASE = "http://127.0.0.1:18410"
INGRESS = BASE + "/api/hassio_ingress/aieIngressToken_Qw3rty"
RESULTS: list[dict[str, Any]] = []


def check(name: str, ok: Any, detail: Any = "") -> bool:
    RESULTS.append({"check": name, "ok": bool(ok), "detail": "" if ok else str(detail)[:1500]})
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  -- {str(detail)[:600]}"), flush=True)
    return bool(ok)


class App:
    def __init__(self, s: aiohttp.ClientSession) -> None:
        self.s = s

    async def get(self, path: str, **headers: str) -> tuple[int, Any]:
        async with self.s.get(f"{INGRESS}/{path}", headers=headers) as r:
            body = await r.read()
            try:
                return r.status, json.loads(body)
            except ValueError:
                return r.status, body

    async def post(self, path: str, body: Any, **headers: str) -> tuple[int, Any]:
        h = {"X-AIE": "1", "Content-Type": "application/json", **headers}
        async with self.s.post(f"{INGRESS}/{path}", data=json.dumps(body), headers=h) as r:
            return r.status, await r.json(content_type=None)

    async def upload(self, data: bytes, name: str = "test.zip") -> tuple[int, Any]:
        async with self.s.post(f"{INGRESS}/api/archives/upload", data=data, headers={
                "X-AIE": "1", "Content-Type": "application/zip", "X-Filename": name}) as r:
            return r.status, await r.json(content_type=None)

    async def op(self, op_id: str, until: tuple[str, ...] = (), timeout: float = 120) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            status, op = await self.get(f"api/operations/{op_id}")
            if status != 200:
                raise RuntimeError(f"operation {op_id}: {status} {op}")
            if (until and op["status"] in until) or (not until and op["status"] not in (
                    "running", "exporting", "deleting", "restoring", "rolling_back")):
                return op
            if time.monotonic() > deadline:
                raise RuntimeError(f"operation {op_id} still {op['status']}")
            await asyncio.sleep(0.5)

    async def health(self, timeout: float = 90) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                status, data = await self.get("api/overview")
                if status == 200 and data["connection"]["home_assistant"]["connected"]:
                    return True
            except (aiohttp.ClientError, ValueError):
                pass
            await asyncio.sleep(1)
        return False

    async def recorded(self) -> dict[str, Any]:
        async with self.s.get(BASE + "/_test/commands") as r:
            return await r.json()

    async def fail(self, mode: str | None, match: str | None = None) -> None:
        q = f"?mode={mode or ''}" + (f"&match={match}" if match else "")
        async with self.s.post(BASE + "/_test/fail_api" + q) as r:
            await r.read()

    # workflows
    async def cleanup(self, keys: list[str], include_review: list[str] | None = None) -> dict[str, Any]:
        status, op = await self.post("api/cleanup/start", {"keys": keys, "include_review": include_review or []})
        assert status == 200, op
        return await self.op(op["id"])

    async def confirm(self, op: dict[str, Any]) -> dict[str, Any]:
        status, data = await self.post("api/cleanup/confirm", {"operation_id": op["id"], "plan_hash": op["plan_hash"],
                                                               "confirm_count": len(op["preview"]["delete"]),
                                                               "archive_ack": True})
        assert status == 200, data
        return await self.op(op["id"])

    async def plan(self, archive_id: str, decisions: dict[str, Any] | None = None) -> dict[str, Any]:
        status, plan = await self.post("api/restore/plan", {"archive_id": archive_id, "decisions": decisions or {}})
        assert status == 200, plan
        return plan

    async def restore(self, archive_id: str, decisions: dict[str, Any] | None = None) -> dict[str, Any]:
        plan = await self.plan(archive_id, decisions)
        status, op = await self.post("api/restore/execute", {"archive_id": archive_id, "decisions": decisions or {},
                                                             "plan_hash": plan["plan_hash"], "confirm": True})
        assert status == 200, op
        return await self.op(op["id"])


async def baseline(ha: HA) -> dict[str, Any]:
    by_unique = await ha.by_unique()
    states = await ha.states()
    out = {}
    for config_id in [*AUTOMATIONS, *EXTERNAL]:
        entry = by_unique.get(config_id)
        out[config_id] = {"config": await ha.config(config_id), "registry": registry_fields(entry),
                          "state": (states.get(entry["entity_id"]) or {}).get("state") if entry else None}
    return out


def same(a: Any, b: Any) -> bool:
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


async def compare(ha: HA, before: dict[str, Any], keys: list[str], label: str, remap: dict[str, str] | None = None,
                  ignore: tuple[str, ...] = ()) -> None:
    now = await baseline(ha)
    remap = remap or {}
    for key in keys:
        b, n = before[key], now[key]
        check(f"{label}: {key} configuration identical", same(b["config"], n["config"]), (b["config"], n["config"]))
        rb, rn = dict(b["registry"] or {}), dict(n["registry"] or {})
        for field in ignore:
            rb.pop(field, None)
            rn.pop(field, None)
        if remap:
            rb["labels"] = sorted(remap.get(x, x) for x in rb.get("labels", []))
            rb["categories"] = {s: remap.get(c, c) for s, c in rb.get("categories", {}).items()}
            if rb.get("area_id"):
                rb["area_id"] = remap.get(rb["area_id"], rb["area_id"])
        check(f"{label}: {key} registry identical (entity id, labels, category, area, name, icon, aliases, flags, "
              "exposure)", same(rb, rn), (rb, rn))
        check(f"{label}: {key} state identical ({b['state']})", b["state"] == n["state"], (b["state"], n["state"]))


async def gone(ha: HA, keys: list[str], label: str) -> None:
    by_unique = await ha.by_unique()
    states = await ha.states()
    for key in keys:
        cfg = await ha.config(key)
        entity = [e for e, s in states.items() if (s.get("attributes") or {}).get("id") == key]
        check(f"{label}: {key} removed from automations.yaml, registry and states",
              cfg is None and key not in by_unique and not entity, (cfg is not None, key in by_unique, entity))


def zip_with(files: dict[str, bytes], tweak: Any = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            info = zipfile.ZipInfo(name)
            if tweak:
                tweak(info)
            zf.writestr(info, data)
    return buf.getvalue()


# ----------------------------------------------------------------------------------------------------- phases
async def phase_setup(app: App, ha: HA, ctx: dict[str, Any]) -> None:
    status, data = await app.get("api/inventory")
    check("inventory readable", status == 200, data)
    by = {i["key"]: i for i in data["items"]}
    for key in ORDINARY:
        check(f"inventory: {key} is an ordinary, deletable UI automation",
              by.get(key, {}).get("source") == "ui" and by[key]["owner"] == "ordinary" and by[key]["deletable"], by.get(key))
    for key in BUILDER:
        check(f"inventory: {key} is Automation Builder's and protected",
              by[key]["owner"] == "builder" and by[key]["protected"] and not by[key]["deletable"], by[key])
    for key in REVIEW:
        check(f"inventory: {key} has ambiguous ownership (review)", by[key]["owner"] == "review", by[key])
    for key in EXTERNAL:
        check(f"inventory: {key} is external YAML, not deletable", by[key]["source"] == "external" and not by[key]["deletable"], by[key])
    no_id = [i for i in data["items"] if i["source"] == "external" and not i["config_id"]]
    check("inventory: the automation without an id is external", len(no_id) == 1, no_id)
    k = by["aie_kitchen_motion"]
    check("inventory: multiple labels shown", sorted(x["name"] for x in k["labels"]) == ["AIE Lighting", "AIE Night"], k["labels"])
    check("inventory: category and area shown", k["categories"][0]["name"] == "AIE Lights" and k["area"]["name"] == "AIE Kitchen", k)
    check("inventory: custom entity id", k["entity_id"] == "automation.aie_kitchen_motion_custom", k["entity_id"])
    check("inventory: blueprint association", by["aie_blueprint_motion"]["blueprint"] == "homeassistant/motion_light.yaml")
    check("inventory: disabled entity", by["aie_disabled"]["registry_disabled"] == "user", by["aie_disabled"])
    check("inventory: turned off", by["aie_hall_alarm"]["state"] == "off")
    check("inventory: four labels", len(by["aie_hidden"]["labels"]) == 4 and by["aie_hidden"]["hidden"] == "user")
    check("inventory: dependency warning for a missing entity",
          any(p["code"] == "missing_entities" for p in by["aie_missing_dep"]["problems"]), by["aie_missing_dep"]["problems"])
    check("inventory: the configuration folder is readable", data["file_scan"]["available"], data["file_scan"])


async def phase_roundtrip(app: App, ha: HA, ctx: dict[str, Any]) -> None:
    before = await baseline(ha)
    ctx["baseline"] = before
    op = await app.cleanup(ORDINARY)
    check("cleanup: exported and verified, waiting for confirmation", op["status"] == "awaiting_confirmation", op.get("error") or op["log"][-3:])
    check("cleanup: verification passed every check", op["verification"]["ok"],
          [c for c in op["verification"]["checks"] if not c["ok"]])
    check("cleanup: preview lists exactly the selected ordinary automations",
          sorted(d["key"] for d in op["preview"]["delete"]) == sorted(ORDINARY), op["preview"])
    check("cleanup: nothing deleted before the confirmation", all([await ha.config(k) is not None for k in ORDINARY]))
    status, raw = await app.get(f"api/archives/{op['archive_id']}/download")
    check("cleanup: archive downloadable", status == 200 and raw[:2] == b"PK", status)
    zf = zipfile.ZipFile(io.BytesIO(raw))
    manifest = json.loads(zf.read("manifest.json"))
    check("archive: manifest format and version", manifest["format"] == "automation-import-export-archive"
          and manifest["format_version"] == 1 and manifest["home_assistant"]["version"] == "2026.11.1", manifest["format"])
    check("archive: inventory = selection", sorted(i["key"] for i in manifest["inventory"]) == sorted(ORDINARY))
    labels_def = json.loads(zf.read("data/labels.json"))
    cats_def = json.loads(zf.read("data/categories.json"))
    areas_def = json.loads(zf.read("data/areas.json"))
    check("archive: label definitions with icon and colour", any(d["name"] == "AIE Lighting" and d["icon"] == "mdi:lightbulb"
                                                             and d["color"] == "amber" for d in labels_def.values()), labels_def)
    check("archive: category definitions with scope and icon", any(d["name"] == "AIE Lights" and d["icon"] == "mdi:lightbulb-group"
                                                               for d in cats_def.get("automation", {}).values()), cats_def)
    check("archive: area definitions", {d["name"] for d in areas_def.values()} >= {"AIE Kitchen", "AIE Hall", "AIE Bedroom"}, areas_def)
    assoc = manifest["associations"]["aie_hidden"]
    check("archive: all four labels of one automation kept", len(assoc["labels"]) == 4, assoc)
    for entry in manifest["inventory"]:
        parsed = yaml.safe_load(zf.read(entry["file"]))
        check(f"archive: {entry['key']} YAML file equals Home Assistant's configuration", same(parsed, before[entry["key"]]["config"]))
    check("archive: blueprint files included", "blueprints/automation/homeassistant/motion_light.yaml" in zf.namelist()
          and "blueprints/automation/aie_test/toggle_on_change.yaml" in zf.namelist(), zf.namelist())
    check("archive: README warns about private data", b"private" in zf.read("README.txt"))
    status, err = await app.post("api/cleanup/confirm", {"operation_id": op["id"], "plan_hash": op["plan_hash"],
                                                         "confirm_count": 1, "archive_ack": True})
    check("cleanup: a wrong confirmation count is refused", status == 400, (status, err))
    status, err = await app.post("api/cleanup/confirm", {"operation_id": op["id"], "plan_hash": "x" * 24,
                                                         "confirm_count": len(ORDINARY), "archive_ack": True})
    check("cleanup: a stale plan hash is refused", status == 409, (status, err))
    status, err = await app.post("api/cleanup/confirm", {"operation_id": op["id"], "plan_hash": op["plan_hash"],
                                                         "confirm_count": len(ORDINARY)})
    check("cleanup: confirmation without the archive acknowledgement is refused", status == 400, (status, err))
    op = await app.confirm(op)
    check("cleanup: completed", op["status"] == "completed", op.get("summary") or op["log"][-5:])
    check("cleanup: report says protected automations unchanged", op["report"]["protected_changed"] == [], op["report"])
    check("cleanup: report lists remaining active automations", {a["key"] for a in op["report"]["remaining_active"]}
          >= set(BUILDER) | set(REVIEW) | set(EXTERNAL), op["report"]["remaining_active"])
    await gone(ha, ORDINARY, "cleanup")
    after = await baseline(ha)
    for key in [*BUILDER, *REVIEW, *EXTERNAL]:
        check(f"cleanup: {key} untouched", same(before[key], after[key]), (before[key], after[key]))
    ctx["roundtrip_archive"] = op["archive_id"]

    plan = await app.plan(op["archive_id"])
    check("restore plan: every automation created, nothing to acknowledge",
          plan["counts"]["create"] == len(ORDINARY) and not plan["acknowledgements_missing"], (plan["counts"], plan["acknowledgements_missing"]))
    check("restore plan: labels, categories, areas resolved to the same ids",
          all(e["action"] == "use" and e["match"] == "id" for e in plan["labels"] + plan["categories"] + plan["areas"]),
          [(e["name"], e["action"], e.get("match")) for e in plan["labels"] + plan["categories"] + plan["areas"]])
    rop = await app.restore(op["archive_id"])
    check("restore: completed exactly", rop["status"] == "completed", (rop.get("summary"), rop["log"][-8:]))
    await compare(ha, before, ORDINARY, "round trip")
    states = await ha.states()
    for key in ORDINARY:
        entry = (await ha.by_unique()).get(key)
        if entry and not entry.get("disabled_by"):
            check(f"round trip: {key} loads in Home Assistant (not unavailable)", states[entry["entity_id"]]["state"] != "unavailable")
    rec = await app.recorded()
    bad = [h for h in rec["http"] if h["method"] in ("POST", "DELETE") and ("automation_builder" in h["path"]
                                                                            or any(x in h["path"] for x in EXTERNAL + REVIEW))]
    check("round trip: no write to Automation Builder's, external or review automations", not bad, bad)
    services = [c for c in rec["commands"] if c.get("type") == "call_service"]
    check("round trip: only automation services were called", all(c.get("domain") == "automation" for c in services), services)


async def phase_crossinstall(app: App, ha: HA, ctx: dict[str, Any]) -> None:
    """Metadata definitions lost (as on another installation): labels, a category and an area are recreated."""
    before = await baseline(ha)
    keys = ["aie_unicode", "aie_kitchen_motion", "aie_disabled"]
    op = await app.confirm(await app.cleanup(keys))
    check("cross: deleted", op["status"] == "completed", op.get("summary"))
    labels, cats, areas = await ha.names()
    await ha.cmd("config/label_registry/delete", label_id=labels["AIE Night"])
    await ha.cmd("config/category_registry/delete", scope="automation", category_id=cats["AIE Comfort"])
    await ha.cmd("config/area_registry/delete", area_id=areas["AIE Bedroom"])
    plan = await app.plan(op["archive_id"])
    acts = {(e["kind"], e["name"]): e["action"] for e in plan["labels"] + plan["categories"] + plan["areas"]}
    check("cross: missing label, category and area are recreated", acts.get(("label", "AIE Night")) == "create"
          and acts.get(("category", "AIE Comfort")) == "create" and acts.get(("area", "AIE Bedroom")) == "create", acts)
    check("cross: existing ones are reused by id", acts.get(("label", "AIE Lighting")) == "use", acts)
    rop = await app.restore(op["archive_id"])
    check("cross: restore completed", rop["status"] == "completed", (rop.get("summary"), rop["log"][-6:]))
    labels2, cats2, areas2 = await ha.names()
    remap = {labels["AIE Night"]: labels2["AIE Night"], cats["AIE Comfort"]: cats2["AIE Comfort"],
             areas["AIE Bedroom"]: areas2["AIE Bedroom"]}
    check("cross: one label per name, no duplicates", sum(1 for n in labels2 if n == "AIE Night") == 1)
    check("cross: the recreated category got a new id (mapped)", cats2["AIE Comfort"] != cats["AIE Comfort"])
    created = rop["result"]["created"]
    check("cross: the report lists what was created", len(created["labels"]) == 1 and len(created["categories"]) == 1
          and len(created["areas"]) == 1, created)
    await compare(ha, before, keys, "cross", remap=remap)
    await seed(ha)  # the other fixtures lost the deleted label/category/area: seed them again


async def phase_blueprint(app: App, ha: HA, ctx: dict[str, Any]) -> None:
    before = await baseline(ha)
    op = await app.confirm(await app.cleanup(["aie_custom_blueprint"]))
    check("blueprint: deleted", op["status"] == "completed")
    await ha.cmd("blueprint/delete", domain="automation", path="aie_test/toggle_on_change.yaml")
    listed = await ha.cmd("blueprint/list", domain="automation")
    check("blueprint: the blueprint file is gone", "aie_test/toggle_on_change.yaml" not in listed)
    plan = await app.plan(op["archive_id"])
    check("blueprint: plan installs it from the archive", plan["blueprints"] and plan["blueprints"][0]["action"] == "install", plan["blueprints"])
    rop = await app.restore(op["archive_id"])
    check("blueprint: restore completed", rop["status"] == "completed", (rop.get("summary"), rop["log"][-6:]))
    listed = await ha.cmd("blueprint/list", domain="automation")
    check("blueprint: installed again", "aie_test/toggle_on_change.yaml" in listed)
    await compare(ha, before, ["aie_custom_blueprint"], "blueprint")


async def phase_changes(app: App, ha: HA, ctx: dict[str, Any]) -> None:
    """A change between export and deletion is detected: that automation is not deleted."""
    before = await baseline(ha)
    op = await app.cleanup(["aie_plain", "aie_unicode"])
    changed = {**before["aie_plain"]["config"], "description": "changed after the export"}
    await ha.post("aie_plain", changed)
    await asyncio.sleep(1)
    op = await app.confirm(op)
    items = op["items"]
    check("changes: the changed automation was skipped", items["aie_plain"]["status"] == "skipped_changed", items)
    check("changes: the unchanged one was deleted", items["aie_unicode"]["status"] == "deleted", items)
    check("changes: status partial", op["status"] == "partial", op["status"])
    check("changes: the changed automation still runs with its change", (await ha.config("aie_plain")) == changed)
    ctx["changes_archive"] = op["archive_id"]
    rop = await app.restore(op["archive_id"], {"selection": ["aie_unicode"]})
    check("changes: the deleted one restored", rop["status"] == "completed", rop.get("summary"))
    await ha.post("aie_plain", before["aie_plain"]["config"])
    await asyncio.sleep(1)
    await compare(ha, before, ["aie_unicode", "aie_plain"], "changes")


async def phase_collisions(app: App, ha: HA, ctx: dict[str, Any]) -> None:
    before = await baseline(ha)
    status, op = await app.post("api/export", {"keys": ["aie_plain", "aie_hall_alarm"]})
    op = await app.op(op["id"])
    archive = op["archive_id"]
    check("collisions: export only", op["status"] == "completed" and op["verification"]["ok"], op.get("summary"))
    plan = await app.plan(archive)
    acts = {i["key"]: i["action"] for i in plan["items"]}
    check("collisions: identical automations only get their metadata", acts == {"aie_plain": "metadata", "aie_hall_alarm": "metadata"}, acts)
    await ha.post("aie_plain", {**before["aie_plain"]["config"], "alias": "AIE Plain rule (edited)"})
    await asyncio.sleep(1)
    plan = await app.plan(archive)
    item = next(i for i in plan["items"] if i["key"] == "aie_plain")
    check("collisions: a different existing automation is skipped by default", item["action"] == "skip"
          and item["collision"] == "different" and item["diff"], item)
    rop = await app.restore(archive, {"collisions": {"aie_plain": "replace"}})
    check("collisions: replace completed", rop["status"] == "completed", (rop.get("summary"), rop["log"][-5:]))
    check("collisions: a backup was taken before replacing", bool(rop.get("pre_restore_archive")))
    _, bview = await app.get(f"api/archives/{rop['pre_restore_archive']}")
    check("collisions: the backup holds the replaced version", any(i["alias"] == "AIE Plain rule (edited)" for i in bview["inventory"]), bview.get("inventory"))
    await compare(ha, before, ["aie_plain", "aie_hall_alarm"], "replace")
    rop2 = await app.restore(archive, {"selection": ["aie_plain"], "collisions": {"aie_plain": "new"}})
    check("collisions: restore as new completed", rop2["status"] == "completed", (rop2.get("summary"), rop2["log"][-5:]))
    copy_id = next(i["target_id"] for i in rop2["plan"]["items"] if i["key"] == "aie_plain")
    copy = await ha.config(copy_id)
    check("collisions: the copy has the new id and the original configuration",
          copy is not None and same({k: v for k, v in copy.items() if k != "id"},
                                    {k: v for k, v in before["aie_plain"]["config"].items() if k != "id"}), copy)
    entry = (await ha.by_unique()).get(copy_id)
    check("collisions: the copy got its own entity id", entry and entry["entity_id"] != before["aie_plain"]["registry"]["entity_id"], entry)
    status, rb = await app.post("api/restore/rollback", {"operation_id": rop2["id"], "confirm": True})
    rb = await app.op(rb["id"])
    check("collisions: rollback completed", rb["status"] == "completed", (rb.get("summary"), rb["log"][-4:]))
    check("collisions: rollback removed the copy", await ha.config(copy_id) is None)
    await compare(ha, before, ["aie_plain"], "after rollback")


def malicious() -> dict[str, tuple[bytes, str]]:
    good_manifest = json.dumps({"format": "automation-import-export-archive", "format_version": 1, "files": {}}).encode()

    def symlink(info: zipfile.ZipInfo) -> None:
        info.external_attr = (0o120777 << 16)

    def encrypted(data: bytes) -> bytes:
        """Set the 'encrypted' flag in every local and central header (zipfile clears it when writing)."""
        out = bytearray(data)
        for sig, offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
            start = 0
            while (i := out.find(sig, start)) >= 0:
                out[i + offset] |= 0x1
                start = i + 4
        return bytes(out)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", good_manifest)
        zf.writestr("big.bin", b"\0" * (20 * 1024 * 1024))
    big = buf.getvalue()
    dup = io.BytesIO()
    with zipfile.ZipFile(dup, "w") as zf:
        zf.writestr("manifest.json", good_manifest)
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            zf.writestr("manifest.json", good_manifest)
    return {
        "not a zip": (b"this is not a zip file", "not_zip"),
        "path traversal ../": (zip_with({"manifest.json": good_manifest, "../evil.yaml": b"x"}), "unsafe_path"),
        "absolute path": (zip_with({"manifest.json": good_manifest, "/etc/passwd": b"x"}), "unsafe_path"),
        "backslash path": (zip_with({"manifest.json": good_manifest, "a\\..\\b": b"x"}), "unsafe_path"),
        "symlink member": (zip_with({"manifest.json": good_manifest, "link": b"/etc/passwd"}, symlink), "unsafe_member"),
        "encrypted member": (encrypted(zip_with({"manifest.json": good_manifest})), "encrypted"),
        "oversized member": (big, "too_large"),
        "duplicate member": (dup.getvalue(), "duplicate_member"),
        "no manifest": (zip_with({"automations.yaml": b"[]"}), "no_manifest"),
        "unknown version": (zip_with({"manifest.json": json.dumps({"format": "automation-import-export-archive",
                                                                   "format_version": 99}).encode()}), "unknown_version"),
        "wrong format": (zip_with({"manifest.json": b'{"format": "something-else"}'}), "wrong_format"),
    }


async def phase_corrupt(app: App, ha: HA, ctx: dict[str, Any]) -> None:
    _, before = await app.get("api/archives")
    for name, (data, code) in malicious().items():
        status, err = await app.upload(data, name)
        check(f"corrupt: {name} refused ({code})", status in (413, 422) and err.get("error") == code, (status, err))
    _, raw = await app.get(f"api/archives/{ctx.get('roundtrip_archive') or (await first_archive(app))}/download")
    zf = zipfile.ZipFile(io.BytesIO(raw))
    files = {n: zf.read(n) for n in zf.namelist()}
    tampered = dict(files)
    victim = next(n for n in files if n.startswith("automations/"))
    tampered[victim] = files[victim].replace(b"alias:", b"alias: tampered", 1)
    status, err = await app.upload(zip_with(tampered), "tampered.zip")
    check("corrupt: a changed file (checksum mismatch) is refused", status == 422 and err.get("error") == "verification_failed", (status, err))
    truncated = raw[: len(raw) // 2]
    status, err = await app.upload(truncated, "truncated.zip")
    check("corrupt: a truncated archive is refused", status == 422, (status, err))
    status, err = await app.upload(raw, "copy.zip")
    check("corrupt: a valid archive is accepted (as a duplicate of the stored one)", status == 200 and err.get("duplicate"), (status, err))
    _, after = await app.get("api/archives")
    check("corrupt: no refused archive was stored", len(after["archives"]) == len(before["archives"]), (len(before["archives"]), len(after["archives"])))
    status, _ = await app.get("api/archives/..%2F..%2Fetc%2Fpasswd/download")
    check("corrupt: path traversal in an archive id is refused", status == 404, status)


async def first_archive(app: App) -> str:
    _, data = await app.get("api/archives")
    return data["archives"][0]["archive_id"]


async def phase_yaml(app: App, ha: HA, ctx: dict[str, Any]) -> None:
    text = """
- id: aie_yaml_one
  alias: AIE YAML import one
  triggers:
    - trigger: event
      event_type: aie_yaml_one
  actions:
    - action: input_boolean.toggle
      target:
        entity_id: input_boolean.aie_target
- alias: AIE YAML import without id
  triggers:
    - trigger: event
      event_type: aie_yaml_two
  actions: []
- alias: AIE YAML with a secret
  triggers:
    - trigger: event
      event_type: x
  actions:
    - action: notify.notify
      data:
        message: !secret some_secret
"""
    status, res = await app.post("api/import/yaml", {"filename": "my_automations.yaml", "text": text})
    check("yaml: file read", status == 200 and res["imported"] == 2 and len(res["refused"]) == 1, res)
    check("yaml: the !secret entry is refused with its reason", "!secret" in res["refused"][0]["reason"], res["refused"])
    check("yaml: the import archive verifies", res["verification"]["status"] == "verified", res["verification"])
    plan = await app.plan(res["archive_id"])
    acts = sorted(i["action"] for i in plan["items"])
    check("yaml: both automations will be created", acts == ["create", "new"] or acts == ["create", "create"], acts)
    rop = await app.restore(res["archive_id"])
    check("yaml: restore completed", rop["status"] == "completed", (rop.get("summary"), rop["log"][-5:]))
    one = await ha.config("aie_yaml_one")
    check("yaml: imported with its id and configuration", one and one["alias"] == "AIE YAML import one", one)
    created = [i["target_id"] for i in rop["plan"]["items"] if i.get("target_id")]
    for config_id in created:
        await ha.delete(config_id)
    status, err = await app.post("api/import/yaml", {"filename": "bad.yaml", "text": "- : : not yaml ["})
    check("yaml: invalid YAML refused", status == 422, (status, err))


async def phase_duplicates(app: App, ha: HA, ctx: dict[str, Any]) -> None:
    path = Path(ctx["ha_config"]) / "automations.yaml"
    original = path.read_text()
    data = yaml.safe_load(original)
    plain = next(a for a in data if a["id"] == "aie_plain")
    path.write_text(original + yaml.safe_dump([{**plain, "alias": "AIE duplicate of plain"}], allow_unicode=True))
    try:
        status, inv = await app.get("api/inventory")
        item = next(i for i in inv["items"] if i["key"] == "aie_plain")
        check("duplicates: the duplicate id is detected", "aie_plain" in inv["file_scan"]["duplicate_ids"], inv["file_scan"])
        check("duplicates: the automation is blocked from deletion", not item["deletable"]
              and any(p["code"] == "duplicate_id" for p in item["problems"]), item["problems"])
        op = await app.cleanup(["aie_plain", "aie_hall_alarm"])
        check("duplicates: cleanup excludes it", "aie_plain" in op["selection"]["excluded"]
              and [d["key"] for d in op["preview"]["delete"]] == ["aie_hall_alarm"], (op["selection"], op.get("preview")))
        await app.post("api/operations/cancel", {"operation_id": op["id"]})
    finally:
        path.write_text(original)
    check("duplicates: nothing was deleted", await ha.config("aie_hall_alarm") is not None and await ha.config("aie_plain") is not None)


def stack(ctx: dict[str, Any], *args: str) -> None:
    subprocess.run([str(Path(ctx["stack_dir"]) / "stack.sh"), *args], check=True, capture_output=True)


def hooks(ctx: dict[str, Any], value: dict[str, int] | None) -> None:
    path = Path(ctx["data_dir"]) / "test_hooks.json"
    if value is None:
        path.unlink(missing_ok=True)
    else:
        path.write_text(json.dumps(value))


async def wait_log(app: App, op_id: str, text: str, timeout: float = 60) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _, op = await app.get(f"api/operations/{op_id}")
        if any(text in e["message"] for e in op.get("log", [])):
            return op
        await asyncio.sleep(0.3)
    raise RuntimeError(f"{text!r} not logged")


async def restart_app(app: App, ctx: dict[str, Any]) -> None:
    stack(ctx, "kill-app")
    hooks(ctx, None)
    stack(ctx, "start-app")
    check("App back after the kill", await app.health())


async def phase_interrupt_export(app: App, ha: HA, ctx: dict[str, Any]) -> None:
    _, before = await app.get("api/archives")
    hooks(ctx, {"export_write": 0})
    status, op = await app.post("api/export", {"keys": ["aie_plain"]})
    await wait_log(app, op["id"], "built")
    await asyncio.sleep(1)
    await restart_app(app, ctx)
    _, op2 = await app.get(f"api/operations/{op['id']}")
    check("interrupted export: marked interrupted", op2["status"] == "interrupted", op2["status"])
    _, after = await app.get("api/archives")
    check("interrupted export: no partial archive stored", len(after["archives"]) == len(before["archives"]))
    status, op3 = await app.post("api/operations/reconcile", {"operation_id": op["id"]})
    check("interrupted export: reconciled (nothing changed)", status == 200 and op3["status"] == "failed", op3)


async def phase_interrupt_delete(app: App, ha: HA, ctx: dict[str, Any]) -> None:
    before = await baseline(ha)
    keys = ["aie_plain", "aie_hall_alarm", "aie_unicode", "aie_hidden", "1700000000123"]
    op = await app.cleanup(keys)
    hooks(ctx, {"delete": 2})
    status, _ = await app.post("api/cleanup/confirm", {"operation_id": op["id"], "plan_hash": op["plan_hash"],
                                                       "confirm_count": 5, "archive_ack": True})
    await wait_log(app, op["id"], "confirmed")
    for _ in range(60):
        _, cur = await app.get(f"api/operations/{op['id']}")
        if sum(1 for e in cur["items"].values() if e["status"] == "deleted") >= 2:
            break
        await asyncio.sleep(0.3)
    await asyncio.sleep(1)
    await restart_app(app, ctx)
    _, cur = await app.get(f"api/operations/{op['id']}")
    check("interrupted deletion: marked interrupted", cur["status"] == "interrupted", cur["status"])
    status, cur = await app.post("api/operations/reconcile", {"operation_id": op["id"]})
    st = {k: v["status"] for k, v in cur["items"].items()}
    check("interrupted deletion: reconciled - 2 deleted, 3 not", sorted(st.values()).count("deleted") == 2
          and sorted(st.values()).count("pending") == 3 and cur["resumable"], st)
    check("interrupted deletion: the 3 still exist in Home Assistant", all([await ha.config(k) is not None
                                                                           for k, s in st.items() if s == "pending"]))
    status, cur = await app.post("api/cleanup/resume", {"operation_id": op["id"]})
    check("interrupted deletion: verified again, waiting for a confirmation", cur["status"] == "awaiting_confirmation"
          and len(cur["preview"]["delete"]) == 3, (cur["status"], cur.get("preview")))
    cur = await app.confirm(cur)
    check("interrupted deletion: finished", cur["status"] == "completed", (cur["status"], cur.get("summary")))
    await gone(ha, keys, "interrupted deletion")
    rop = await app.restore(op["archive_id"])
    check("interrupted deletion: restored", rop["status"] == "completed", (rop.get("summary"), rop["log"][-5:]))
    await compare(ha, before, keys, "interrupted deletion")


async def phase_interrupt_restore(app: App, ha: HA, ctx: dict[str, Any]) -> None:
    before = await baseline(ha)
    keys = ["aie_plain", "aie_hall_alarm", "aie_unicode", "aie_kitchen_motion"]
    op = await app.confirm(await app.cleanup(keys))
    check("interrupted restore: deleted first", op["status"] == "completed")
    hooks(ctx, {"restore_write": 2})
    plan = await app.plan(op["archive_id"])
    status, rop = await app.post("api/restore/execute", {"archive_id": op["archive_id"], "decisions": {},
                                                         "plan_hash": plan["plan_hash"], "confirm": True})
    for _ in range(60):
        _, cur = await app.get(f"api/operations/{rop['id']}")
        if sum(1 for e in cur["items"].values() if e["status"] == "written") >= 2:
            break
        await asyncio.sleep(0.3)
    await asyncio.sleep(1)
    await restart_app(app, ctx)
    _, cur = await app.get(f"api/operations/{rop['id']}")
    check("interrupted restore: marked interrupted", cur["status"] == "interrupted", cur["status"])
    status, cur = await app.post("api/operations/reconcile", {"operation_id": rop["id"]})
    check("interrupted restore: reconciled as incomplete", cur["status"] == "incomplete", (cur["status"], cur.get("result", {}).get("counts")))
    plan = await app.plan(op["archive_id"])
    acts = sorted(i["action"] for i in plan["items"])
    check("interrupted restore: the written ones only need their metadata now", acts.count("metadata") == 2 and acts.count("create") == 2, acts)
    rop2 = await app.restore(op["archive_id"])
    check("interrupted restore: finished by running it again", rop2["status"] == "completed", (rop2.get("summary"), rop2["log"][-5:]))
    await compare(ha, before, keys, "interrupted restore")


async def phase_failed_write(app: App, ha: HA, ctx: dict[str, Any]) -> None:
    before = await baseline(ha)
    keys = ["aie_plain", "aie_hall_alarm"]
    op = await app.confirm(await app.cleanup(keys))
    await app.fail("refuse", "aie_hall_alarm")
    try:
        rop = await app.restore(op["archive_id"])
    finally:
        await app.fail(None)
    st = {k: v["status"] for k, v in rop["items"].items()}
    check("failed write: partial restoration reported (not a success)", rop["status"] in ("incomplete", "partial")
          and st.get("aie_hall_alarm") == "failed" and st.get("aie_plain") == "restored", (rop["status"], st))
    status, rb = await app.post("api/restore/rollback", {"operation_id": rop["id"], "confirm": True})
    rb = await app.op(rb["id"])
    check("failed write: rollback removed the restored one", rb["status"] == "completed" and await ha.config("aie_plain") is None,
          (rb["status"], rb.get("summary")))
    rop = await app.restore(op["archive_id"])
    check("failed write: the full restore then succeeds", rop["status"] == "completed", rop.get("summary"))
    await compare(ha, before, keys, "failed write")


async def phase_restart(app: App, ha: HA, ctx: dict[str, Any]) -> None:
    _, before = await app.get("api/archives")
    _, ops = await app.get("api/operations")
    stack(ctx, "stop-app")
    stack(ctx, "start-app")
    check("restart: App back", await app.health())
    _, after = await app.get("api/archives")
    _, ops2 = await app.get("api/operations")
    check("restart: archives kept", [a["archive_id"] for a in after["archives"]] == [a["archive_id"] for a in before["archives"]])
    check("restart: operation journal kept", len(ops2["operations"]) == len(ops["operations"]))
    status, report = await app.post("api/archives/verify", {"archive_id": after["archives"][-1]["archive_id"]})
    check("restart: a stored archive still verifies", status == 200 and report["status"] in ("verified", "incomplete"), report.get("status"))


async def phase_security(app: App, ha: HA, ctx: dict[str, Any]) -> None:
    status, _ = await app.get("api/inventory", **{"X-Test-User": "not-an-admin"})
    check("security: a non-administrator cannot read the inventory", status == 403, status)
    status, _ = await app.get(f"api/archives/{await first_archive(app)}/download", **{"X-Test-User": "not-an-admin"})
    check("security: a non-administrator cannot download an archive", status == 403, status)
    async with app.s.post(f"{INGRESS}/api/export", data=json.dumps({"mode": "all"}), headers={"Content-Type": "application/json"}) as r:
        check("security: a request without the App's header is refused", r.status == 403, r.status)
    status, _ = await app.post("api/export", {"mode": "all"}, Origin="http://evil.example")
    check("security: a cross-site request is refused", status == 403, status)
    status, _ = await app.post("api/archives/delete", {"archive_id": await first_archive(app)})
    check("security: deleting an archive needs a confirmation", status == 400, status)
    async with app.s.get(INGRESS + "/") as r:
        csp = r.headers.get("Content-Security-Policy", "")
        check("security: strict Content-Security-Policy", "default-src 'self'" in csp and "script-src 'self'" in csp, csp)


# ------------------------------------------------------------------ helpers, labels & categories (0.2)
REG_HELPERS = {  # domain -> create parameters (the name gives the id)
    "input_number": {"name": "AIE Target temp", "min": 15, "max": 25, "step": 0.5, "mode": "slider",
                     "unit_of_measurement": "°C", "icon": "mdi:thermometer"},
    "input_select": {"name": "AIE House mode", "options": ["Home", "Away", "Night"]},
    "input_text": {"name": "AIE Note", "max": 120},
    "counter": {"name": "AIE Visits", "step": 2, "initial": 0},
    "timer": {"name": "AIE Old timer", "duration": "0:05:00"},
}


async def reg_items(ha: HA, domain: str) -> dict[str, Any]:
    return {i["id"]: i for i in await ha.cmd(f"{domain}/list")}


async def reg_entry(ha: HA, entity_id: str) -> dict[str, Any] | None:
    return (await ha.cmd("config/entity_registry/get_entries", entity_ids=[entity_id])).get(entity_id)


async def reg_seed(ha: HA, ctx: dict[str, Any]) -> dict[str, Any]:
    """Helpers with different uses, a label and a category on entities, a device and an area."""
    for domain, params in REG_HELPERS.items():
        if params["name"].lower().replace(" ", "_") not in await reg_items(ha, domain):
            await ha.cmd(f"{domain}/create", **params)
    labels = {x["name"]: x["label_id"] for x in await ha.cmd("config/label_registry/list")}
    for name, extra in (("AIE Reg Night", {"color": "indigo", "icon": "mdi:weather-night", "description": "night"}),
                        ("AIE Unused label", {})):
        if name not in labels:
            labels[name] = (await ha.cmd("config/label_registry/create", name=name, **extra))["label_id"]
    cats = {x["name"]: x["category_id"] for x in await ha.cmd("config/category_registry/list", scope="helpers")}
    if "AIE Climate" not in cats:
        cats["AIE Climate"] = (await ha.cmd("config/category_registry/create", scope="helpers", name="AIE Climate",
                                            icon="mdi:sun-thermometer"))["category_id"]
    autocats = {x["name"]: x["category_id"] for x in await ha.cmd("config/category_registry/list", scope="automation")}
    if "AIE Empty" not in autocats:
        await ha.cmd("config/category_registry/create", scope="automation", name="AIE Empty")
    areas = {x["name"]: x["area_id"] for x in await ha.cmd("config/area_registry/list")}
    night = labels["AIE Reg Night"]
    for _ in range(40):
        if await reg_entry(ha, "input_number.aie_target_temp"):
            break
        await asyncio.sleep(0.25)
    await ha.cmd("config/entity_registry/update", entity_id="input_number.aie_target_temp", labels=[night],
                 categories={"helpers": cats["AIE Climate"]}, area_id=areas["AIE Kitchen"],
                 name="AIE Target temperature", aliases=["warmth"])
    await ha.cmd("homeassistant/expose_entity", assistants=["conversation"], entity_ids=["input_number.aie_target_temp"],
                 should_expose=True)
    await ha.cmd("config/entity_registry/update", entity_id="light.aie_lamp", labels=[night])
    devices = await ha.cmd("config/device_registry/list")
    device = sorted(devices, key=lambda d: d["id"])[0]
    await ha.cmd("config/device_registry/update", device_id=device["id"], labels=[night])
    await ha.cmd("config/area_registry/update", area_id=areas["AIE Kitchen"], labels=[night])
    # uses: a UI script (template), a dashboard, a template helper, a YAML file
    await ha.rest("POST", "config/script/config/aie_heat", {"alias": "AIE heat", "sequence": [
        {"action": "climate.set_temperature", "data": {"temperature": "{{ states('input_number.aie_target_temp') }}"},
         "target": {"area_id": areas["AIE Kitchen"]}}]})
    dashboards = {d["url_path"] for d in await ha.cmd("lovelace/dashboards/list")}
    if "aie-dash" not in dashboards:
        await ha.cmd("lovelace/dashboards/create", url_path="aie-dash", title="AIE Dash", mode="storage",
                     show_in_sidebar=False, require_admin=False)
    await ha.cmd("lovelace/config/save", url_path="aie-dash", config={"views": [{"title": "Home", "cards": [
        {"type": "entities", "entities": ["input_select.aie_house_mode"]}]}]})
    entries = [e for e in await ha.cmd("config_entries/get", type_filter=["helper"]) if e["title"] == "AIE template"]
    if not entries:
        status, flow = await ha.rest("POST", "config/config_entries/flow", {"handler": "template"})
        status, flow = await ha.rest("POST", f"config/config_entries/flow/{flow['flow_id']}", {"next_step_id": "sensor"})
        status, flow = await ha.rest("POST", f"config/config_entries/flow/{flow['flow_id']}", {
            "name": "AIE template", "state": "{{ states('input_text.aie_note') | length }}"})
        check("registry: template helper created for the test", status == 200 and flow.get("type") == "create_entry",
              flow)
    folder = Path(ctx["ha_config"])
    (folder / "aie_notes").mkdir(exist_ok=True)
    (folder / "aie_notes" / "visits.yaml").write_text("# counts visits\nvisits: counter.aie_visits\n")
    (folder / "secrets.yaml").write_text("never_read: timer.aie_old_timer\n")
    await asyncio.sleep(3)  # Home Assistant writes new config entries and scripts to its files shortly after
    return {"night": night, "unused_label": labels["AIE Unused label"], "climate": cats["AIE Climate"],
            "kitchen": areas["AIE Kitchen"], "device": device["id"]}


async def phase_registry(app: App, ha: HA, ctx: dict[str, Any]) -> None:
    ids = await reg_seed(ha, ctx)
    night, climate = ids["night"], ids["climate"]
    status, view = await app.get("api/registry")
    check("registry: the overview is read", status == 200, view)
    if status != 200:
        return
    items = {x["key"]: x for x in [*view["helpers"], *view["labels"], *view["categories"]]}
    usage = lambda key: items.get(key, {}).get("usage") or {}  # noqa: E731
    check("registry: a helper used in a script template is found",
          [x["id"] for x in usage("helper:input_number:aie_target_temp").get("scripts", [])] == ["script.aie_heat"],
          usage("helper:input_number:aie_target_temp"))
    check("registry: a helper on a dashboard is found",
          [x["name"] for x in usage("helper:input_select:aie_house_mode").get("dashboards", [])] == ["AIE Dash"],
          usage("helper:input_select:aie_house_mode"))
    check("registry: a helper used by a template helper is found",
          [x["name"] for x in usage("helper:input_text:aie_note").get("helpers", [])] == ["AIE template"],
          usage("helper:input_text:aie_note"))
    check("registry: a helper in a YAML file is found",
          [x["id"] for x in usage("helper:counter:aie_visits").get("yaml", [])] == ["aie_notes/visits.yaml"],
          usage("helper:counter:aie_visits"))
    check("registry: an unused helper is not used (secrets.yaml is never read)",
          items.get("helper:timer:aie_old_timer", {}).get("count") == 0, items.get("helper:timer:aie_old_timer"))
    lamp = items.get("yaml:input_boolean.aie_light") or {}
    check("registry: a YAML helper is listed read-only with its uses (scene, script, template light)",
          lamp.get("kind") == "yaml" and not lamp.get("deletable") and {"scenes", "scripts"} <= set(lamp.get("usage", {})),
          lamp)
    n = usage(f"label:{night}")
    check("registry: a label's assignments (entities, device, area) are counted",
          {x["id"] for x in n.get("entities", [])} == {"input_number.aie_target_temp", "light.aie_lamp"}
          and [x["id"] for x in n.get("devices", [])] == [ids["device"]] and [x["id"] for x in n.get("areas", [])] ==
          [ids["kitchen"]], n)
    check("registry: unused label and category", items.get(f"label:{ids['unused_label']}", {}).get("count") == 0
          and any(x["name"] == "AIE Empty" and x["count"] == 0 for x in view["categories"]), view["summary"])
    check("registry: the helper category is counted", items.get(f"category:helpers:{climate}", {}).get("count") == 1,
          items.get(f"category:helpers:{climate}"))
    template = next((h for h in view["helpers"] if h["kind"] == "entry" and h["name"] == "AIE template"), None)
    check("registry: integration helpers are listed read-only", template is not None and not template["deletable"],
          template)

    # backup of everything
    status, op = await app.post("api/registry/backup", {"mode": "all"})
    op = await app.op(op["id"]) if status == 200 else op
    check("registry: backup of everything verified", op.get("status") == "completed"
          and op["verification"]["status"] == "verified", op.get("verification") or op)
    status, raw = await app.get(f"api/archives/{op['archive_id']}/download")
    status2, up = await app.upload(raw, "registry.zip")
    check("registry: the downloaded archive is recognised on upload", status2 == 200 and up.get("duplicate")
          and up["archive"]["kind"] == "registry", up)

    # back up & delete
    before = await reg_entry(ha, "input_number.aie_target_temp")
    config_before = (await reg_items(ha, "input_number"))["aie_target_temp"]
    keys = [f"label:{night}", f"label:{ids['unused_label']}", f"category:helpers:{climate}",
            "helper:input_number:aie_target_temp", "helper:timer:aie_old_timer"]
    status, op = await app.post("api/registry/cleanup/start", {"keys": keys})
    op = await app.op(op["id"])
    check("registry: back up & delete waits for the confirmation", op["status"] == "awaiting_confirmation"
          and sorted(op["in_use"]) == sorted([f"label:{night}", f"category:helpers:{climate}",
                                              "helper:input_number:aie_target_temp"]), op.get("summary"))
    body = {"operation_id": op["id"], "plan_hash": op["plan_hash"], "confirm_count": 5, "archive_ack": True}
    status, refused = await app.post("api/registry/cleanup/confirm", body)
    check("registry: items in use need the explicit acknowledgement", status == 400 and refused.get("error") ==
          "not_confirmed", refused)
    check("registry: nothing deleted before the confirmation", "aie_target_temp" in await reg_items(ha, "input_number"))
    status, _ = await app.post("api/registry/cleanup/confirm", {**body, "in_use_ack": True})
    op = await app.op(op["id"])
    check("registry: deleted after the confirmation", op["status"] == "completed", op.get("summary"))
    labels_now = {x["label_id"] for x in await ha.cmd("config/label_registry/list")}
    lamp_entry = await reg_entry(ha, "light.aie_lamp")
    check("registry: gone from Home Assistant (helpers, label, category; the label is off every entity)",
          "aie_target_temp" not in await reg_items(ha, "input_number")
          and "aie_old_timer" not in await reg_items(ha, "timer") and night not in labels_now
          and climate not in {x["category_id"] for x in await ha.cmd("config/category_registry/list", scope="helpers")}
          and night not in lamp_entry["labels"], lamp_entry)

    # restore from the deletion archive
    archive_id = op["archive_id"]
    status, plan = await app.post("api/registry/restore/plan", {"archive_id": archive_id, "decisions": {}})
    check("registry: restore plan creates every item", status == 200 and {i["action"] for i in plan["items"]} ==
          {"create"} and plan["assignments_total"] == 3, (plan.get("assignments_total"), [(i["key"], i["action"]) for i in plan.get("items", [])]))
    status, rop = await app.post("api/registry/restore/execute", {"archive_id": archive_id, "decisions": {},
                                                                  "plan_hash": plan["plan_hash"], "confirm": True})
    rop = await app.op(rop["id"]) if status == 200 else rop
    check("registry: restore completed and verified", rop.get("status") == "completed",
          [m["message"] for m in rop.get("log", [])][-8:] if isinstance(rop, dict) else rop)
    restored = (await reg_items(ha, "input_number")).get("aie_target_temp")
    check("registry: the helper is back with its id and exact configuration", restored == config_before,
          (restored, config_before))
    after = await reg_entry(ha, "input_number.aie_target_temp") or {}
    new_climate = {x["name"]: x["category_id"] for x in await ha.cmd("config/category_registry/list",
                                                                     scope="helpers")}.get("AIE Climate")
    check("registry: its entity id, name, area, aliases, label, category and exposure are back",
          (after.get("name"), after.get("area_id"), after.get("aliases"), after.get("labels"),
           after.get("categories"), (after.get("options", {}).get("conversation") or {}).get("should_expose")) ==
          (before["name"], before["area_id"], before["aliases"], [night], {"helpers": new_climate}, True), after)
    lamp_entry = await reg_entry(ha, "light.aie_lamp")
    device = next(d for d in await ha.cmd("config/device_registry/list") if d["id"] == ids["device"])
    area = next(a for a in await ha.cmd("config/area_registry/list") if a["area_id"] == ids["kitchen"])
    check("registry: the label is back on the other entity, the device and the area",
          night in lamp_entry["labels"] and night in device["labels"] and night in area["labels"],
          (lamp_entry["labels"], device["labels"], area["labels"]))
    label = next(x for x in await ha.cmd("config/label_registry/list") if x["label_id"] == night)
    check("registry: the label definition is back", (label["name"], label["color"], label["icon"],
                                                      label["description"]) ==
          ("AIE Reg Night", "indigo", "mdi:weather-night", "night"), label)
    timer = (await reg_items(ha, "timer")).get("aie_old_timer")
    check("registry: the unused helper is back too", timer is not None and timer["duration"] == "0:05:00", timer)
    status, plan2 = await app.post("api/registry/restore/plan", {"archive_id": archive_id, "decisions": {}})
    check("registry: a second restore has nothing to do", status == 200 and not plan2["ready"],
          [(i["key"], i["action"], i.get("diff")) for i in plan2.get("items", [])])

    # collision: changed meanwhile -> skip by default, update on request (after a backup)
    await ha.cmd("config/label_registry/update", label_id=night, color="red")
    status, plan3 = await app.post("api/registry/restore/plan", {"archive_id": archive_id, "decisions": {}})
    item = next(i for i in plan3["items"] if i["key"] == f"label:{night}")
    check("registry: a changed label is a collision, skipped by default", item["action"] == "skip"
          and item["choices"] == ["skip", "update"], item)
    decisions = {"selection": [f"label:{night}"], "collisions": {f"label:{night}": "update"}, "assignments": False}
    status, plan4 = await app.post("api/registry/restore/plan", {"archive_id": archive_id, "decisions": decisions})
    status, rop = await app.post("api/registry/restore/execute", {"archive_id": archive_id, "decisions": decisions,
                                                                  "plan_hash": plan4["plan_hash"], "confirm": True})
    rop = await app.op(rop["id"])
    label = next(x for x in await ha.cmd("config/label_registry/list") if x["label_id"] == night)
    check("registry: update on request, after a backup", rop["status"] == "completed" and label["color"] == "indigo"
          and rop.get("pre_restore_archive"), (rop.get("summary"), label))
    rec = await app.recorded()
    kinds = [c["type"] for c in rec["commands"]]
    check("registry: deletes only for the confirmed items", (kinds.count("config/label_registry/delete"),
                                                             kinds.count("config/category_registry/delete"),
                                                             kinds.count("input_number/delete"),
                                                             kinds.count("timer/delete"),
                                                             kinds.count("input_select/delete") +
                                                             kinds.count("counter/delete") +
                                                             kinds.count("input_text/delete")) == (2, 1, 1, 1, 0),
          sorted({k for k in kinds if k.endswith("delete")}))


async def phase_builder(app: App, ha: HA, ctx: dict[str, Any]) -> None:
    initial = ctx.get("baseline")
    now = await baseline(ha)
    if initial:
        for key in BUILDER:
            check(f"builder: {key} identical to the start of the run", same(initial[key], now[key]), (initial[key], now[key]))
    rec = await app.recorded()
    writes = [h for h in rec["http"] if h["method"] != "GET" and "automation_builder" in h["path"]]
    check("builder: the App never wrote or deleted an Automation Builder automation", not writes, writes)
    entity = (now[BUILDER[0]]["registry"] or {}).get("entity_id")
    check("builder: the App never sent a registry update or service call for it", not [
        c for c in rec["commands"] if c.get("type") in ("config/entity_registry/update", "call_service")
        and entity and entity in json.dumps(c)], entity)
    kinds = sorted({c["type"] for c in rec["commands"]})
    check("allowlist: only allowlisted websocket commands were sent", set(kinds) <= {
        "ping", "get_config", "get_states", "config/auth/list", "config/entity_registry/list",
        "config/entity_registry/get_entries", "config/label_registry/list", "config/category_registry/list",
        "config/area_registry/list", "config/floor_registry/list", "config/device_registry/list", "automation/config",
        "blueprint/list", "validate_config", "config/entity_registry/update", "config/label_registry/create",
        "config/category_registry/create", "config/area_registry/create", "blueprint/save", "call_service",
        "homeassistant/expose_entity",
        # 0.2: helpers, labels and categories
        "script/config", "lovelace/dashboards/list", "lovelace/config", "config_entries/get",
        "config/label_registry/update", "config/label_registry/delete", "config/category_registry/update",
        "config/category_registry/delete", "config/device_registry/update", "config/area_registry/update",
        *(f"{d}/{v}" for d in ("input_boolean", "input_number", "input_text", "input_select", "input_datetime",
                               "input_button", "counter", "timer", "schedule")
          for v in ("list", "create", "update", "delete"))}, kinds)


PHASES = ["setup", "roundtrip", "crossinstall", "blueprint", "changes", "collisions", "corrupt", "yaml", "duplicates",
          "interrupt_export", "interrupt_delete", "interrupt_restore", "failed_write", "restart", "security", "registry",
          "builder"]


async def main() -> None:
    tokens = json.loads(Path(sys.argv[1]).read_text())
    ctx = {"data_dir": sys.argv[2], "stack_dir": sys.argv[3], "ha_config": sys.argv[4]}
    phases = sys.argv[5:] or PHASES
    async with aiohttp.ClientSession() as s, HA(s, tokens["llat"]) as ha:
        app = App(s)
        check("App healthy", await app.health())
        for phase in phases:
            print(f"--- {phase}", flush=True)
            try:
                await globals()[f"phase_{phase}"](app, ha, ctx)
            except Exception as err:  # noqa: BLE001 - a phase failure is a result
                check(f"{phase}: ran without an exception", False, f"{type(err).__name__}: {err}")
                hooks(ctx, None)
                await app.fail(None)
                await seed(ha)
    failed = [r for r in RESULTS if not r["ok"]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    Path(ctx["data_dir"]).with_suffix(".results.json").write_text(json.dumps(RESULTS, indent=1))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
