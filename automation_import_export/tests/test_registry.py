"""Helpers, labels and categories: the usage overview, backups, Back up & Delete, restore (0.2)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import ADMIN, FakeHome, automation, wait_op

from aie.core import AppCore, OperationError
from aie.regarchive import RegistryArchive


@pytest.fixture
def reg(home: FakeHome, ha_folder: Path) -> dict:
    """Helpers with different kinds of use, labels and categories with assignments."""
    ids: dict = {}
    home.add_helper("input_boolean", "Vacation")                                   # used by a script
    home.add_helper("input_number", "Target temp", min=15, max=25, step=0.5, mode="slider", icon="mdi:thermometer")
    home.add_helper("input_select", "House mode", options=["Home", "Away"])       # used by a template helper
    home.add_helper("counter", "Visits", step=1, initial=0)                        # not used anywhere
    home.add_helper("timer", "Old timer", duration="0:05:00")                     # mentioned only in secrets.yaml
    home.add_helper("input_text", "Device trigger text")                           # used by its registry id
    home.add_helper("input_button", "Doorbell test")                               # used in a YAML package
    ids["climate"] = home.add_category("Climate", "mdi:sun-thermometer", scope="helpers")
    ids["unused_cat"] = home.add_category("Empty category")
    ids["unused_label"] = home.add_label("Unused label")
    entry = home.extra["input_number.target_temp"]
    entry.update(labels=["night"], categories={"helpers": ids["climate"]}, area_id="kitchen", name="Target temperature",
                 aliases=["warmth"])
    entry["options"]["conversation"] = {"should_expose": True}
    home.extra_entry("light.kitchen")["labels"] = ["night"]
    home.extra_entry("light.hall")["categories"] = {"helpers": ids["climate"]}
    home.devices["dev-1"]["labels"] = ["night"]
    home.areas["kitchen"]["labels"] = ["night"]
    home.scripts["script.goodnight"] = {"alias": "Good night", "sequence": [
        {"action": "input_boolean.turn_on", "target": {"entity_id": "input_boolean.vacation"}},
        {"action": "light.turn_off", "target": {"label_id": "security"}}]}
    home.dashboards[None] = {"views": [{"cards": [{"type": "entities", "entities": ["input_number.target_temp"]}]}]}
    home.dashboards["energy"] = {"strategy": {"type": "energy"}}
    home.dashboard_titles["energy"] = "Energy"
    registry_id = home.extra["input_text.device_trigger_text"]["id"]
    home.add_ui(automation("1700000000010", "Uses a registry id", triggers=[
        {"trigger": "state", "entity_id": registry_id, "to": "x"}]))
    home.config_entries.append({"entry_id": "tmpl1", "domain": "template", "title": "House template"})
    home.extra_entry("sensor.house_template").update(platform="template", config_entry_id="tmpl1")
    home.other_entities.add("sensor.house_template")
    storage = ha_folder / ".storage"
    storage.mkdir()
    (storage / "core.config_entries").write_text(json.dumps({"data": {"entries": [
        {"entry_id": "tmpl1", "domain": "template", "options": {"state": "{{ states('input_select.house_mode') }}"}},
        {"entry_id": "other", "domain": "mqtt", "data": {"password": "input_button.doorbell_test"}},
    ]}}))
    (ha_folder / "packages").mkdir()
    (ha_folder / "packages" / "doorbell.yaml").write_text(
        "script:\n  ring:\n    sequence:\n      - action: input_button.press\n        target:\n"
        "          entity_id: input_button.doorbell_test\n")
    (ha_folder / "secrets.yaml").write_text("x: timer.old_timer\n")
    home.writes.clear()
    return ids


def usage_of(view: dict, key: str) -> dict:
    return next(x for x in [*view["helpers"], *view["labels"], *view["categories"]] if x["key"] == key)


async def test_usage_overview_finds_every_kind_of_reference(core: AppCore, home: FakeHome, reg):
    view = await core.registry_view()
    assert usage_of(view, "helper:input_boolean:vacation")["usage"]["scripts"][0]["id"] == "script.goodnight"
    number = usage_of(view, "helper:input_number:target_temp")
    assert number["usage"]["dashboards"][0]["name"] == "Overview" and number["name"] == "Target temperature"
    assert usage_of(view, "helper:input_select:house_mode")["usage"]["helpers"][0]["name"] == "House template"
    assert usage_of(view, "helper:input_text:device_trigger_text")["usage"]["automations"][0]["name"] == \
        "Uses a registry id"
    assert usage_of(view, "helper:input_button:doorbell_test")["usage"] == {
        "yaml": [{"id": "packages/doorbell.yaml", "name": "packages/doorbell.yaml"}]}  # not the mqtt entry
    assert usage_of(view, "helper:counter:visits")["count"] == 0
    assert usage_of(view, "helper:timer:old_timer")["count"] == 0  # secrets.yaml is never read
    night = usage_of(view, "label:night")["usage"]
    assert {x["id"] for x in night["entities"]} >= {"light.kitchen", "input_number.target_temp",
                                                     "automation.kitchen_motion_light"}
    assert night["devices"] == [{"id": "dev-1", "name": "Lamp"}] and night["areas"][0]["id"] == "kitchen"
    assert usage_of(view, "label:security")["usage"]["scripts"][0]["id"] == "script.goodnight"  # label_id target
    assert usage_of(view, f"label:{reg['unused_label']}")["count"] == 0
    assert usage_of(view, f"category:helpers:{reg['climate']}")["count"] == 2
    assert usage_of(view, f"category:automation:{reg['unused_cat']}")["count"] == 0
    guest = next(h for h in view["helpers"] if h["key"] == "yaml:input_boolean.guest")
    assert guest["kind"] == "yaml" and not guest["deletable"] and guest["count"] == 1  # the hall alarm automation
    template = next(h for h in view["helpers"] if h["kind"] == "entry")
    assert template["name"] == "House template" and not template["deletable"]
    assert view["summary"]["labels_unused"] == 1 and view["searched"]["dashboards"] == 1
    assert not home.writes


async def test_backup_selected_and_everything(core: AppCore, home: FakeHome, reg):
    op = await wait_op(core, await core.start_registry_backup(
        {"keys": ["label:night", "helper:input_number:target_temp", f"category:helpers:{reg['climate']}"]}, ADMIN))
    assert op["status"] == "completed", op.get("error")
    assert op["verification"]["status"] == "verified"
    archive = RegistryArchive(core.archives.read(op["archive_id"]))
    night = archive.labels["night"]
    assert night["definition"]["name"] == "Night"
    assert {e["id"] for e in night["assignments"]["entities"]} >= {"light.kitchen", "input_number.target_temp"}
    helper = archive.helpers["helper:input_number:target_temp"]
    assert helper["config"]["min"] == 15 and helper["registry"]["labels"] == ["night"]
    assert helper["registry"]["exposure"] == {"conversation": True}
    assert core.archives.entries[op["archive_id"]]["kind"] == "registry"
    view = core.archive_view(op["archive_id"])
    assert view["kind"] == "registry" and len(view["items"]) == 3
    raw, name = core.archive_file(op["archive_id"])
    assert name.startswith("helpers-labels-categories-") and name.endswith("-3.zip")
    assert (await core.verify_archive({"archive_id": op["archive_id"], "live": True}, ADMIN))["status"] == "verified"
    everything = await wait_op(core, await core.start_registry_backup({"mode": "all"}, ADMIN))
    counts = core.archives.entries[everything["archive_id"]]["counts"]
    assert counts["helpers"] == 7 and counts["labels"] == 4 and counts["categories"] == 4
    assert not home.writes


async def start_cleanup(core: AppCore, keys):
    return await wait_op(core, await core.start_registry_cleanup({"keys": keys}, ADMIN))


async def confirm_cleanup(core: AppCore, op, **override):
    return await wait_op(core, await core.confirm_registry_cleanup({
        "operation_id": op["id"], "plan_hash": op["plan_hash"], "confirm_count": len(op["preview"]["delete"]),
        "archive_ack": True, "in_use_ack": True, **override}, ADMIN))


async def test_back_up_and_delete_needs_every_confirmation(core: AppCore, home: FakeHome, reg):
    op = await start_cleanup(core, ["helper:counter:visits", "label:night"])
    assert op["status"] == "awaiting_confirmation", op.get("error")
    assert op["in_use"] == ["label:night"]
    assert [d["key"] for d in op["preview"]["delete"]] == ["helper:counter:visits", "label:night"]
    for override, code in (({"confirm_count": 1}, "not_confirmed"), ({"archive_ack": False}, "not_confirmed"),
                           ({"in_use_ack": False}, "not_confirmed"), ({"plan_hash": "0" * 24}, "stale_plan")):
        with pytest.raises(OperationError) as err:
            await core.confirm_registry_cleanup({"operation_id": op["id"], "plan_hash": op["plan_hash"],
                                                 "confirm_count": 2, "archive_ack": True, "in_use_ack": True,
                                                 **override}, ADMIN)
        assert err.value.code == code
    assert not [w for w in home.writes if w[0].endswith("delete")]
    op = await confirm_cleanup(core, op)
    assert op["status"] == "completed", op["log"][-3:]
    assert "visits" not in home.collections["counter"] and "night" not in home.labels
    assert "input_number.target_temp" in home.extra and "night" not in home.extra["light.kitchen"]["labels"]
    assert home.devices["dev-1"]["labels"] == [] and home.areas["kitchen"]["labels"] == []
    assert all(e["removal_verified"] for e in op["items"].values())


async def test_an_item_changed_after_the_backup_is_not_deleted(core: AppCore, home: FakeHome, reg):
    op = await start_cleanup(core, ["helper:counter:visits", f"label:{reg['unused_label']}"])
    home.collections["counter"]["visits"]["step"] = 5
    op = await confirm_cleanup(core, op)
    assert op["items"]["helper:counter:visits"]["status"] == "skipped_changed"
    assert op["status"] == "partial" and "visits" in home.collections["counter"]
    assert reg["unused_label"] not in home.labels


async def restore(core: AppCore, archive_id: str, decisions: dict | None = None):
    plan = await core.registry_restore_plan({"archive_id": archive_id, "decisions": decisions or {}})
    op = await wait_op(core, await core.registry_restore_execute({
        "archive_id": archive_id, "decisions": decisions or {}, "plan_hash": plan["plan_hash"], "confirm": True}, ADMIN))
    return plan, op


async def test_delete_and_restore_round_trip(core: AppCore, home: FakeHome, reg):
    keys = ["label:night", f"category:helpers:{reg['climate']}", "helper:input_number:target_temp",
            "helper:counter:visits"]
    before_entry = json.loads(json.dumps(home.extra["input_number.target_temp"]))
    op = await confirm_cleanup(core, await start_cleanup(core, keys))
    assert op["status"] == "completed"
    assert "input_number.target_temp" not in home.extra and not home.categories["helpers"]
    home.extra_deleted.clear()  # another installation would not remember the deleted entity either
    plan, rop = await restore(core, op["archive_id"])
    actions = {i["key"]: i["action"] for i in plan["items"]}
    assert actions == {k: "create" for k in keys}
    assert plan["assignments_total"] >= 4
    assert rop["status"] == "completed", rop["log"][-6:]
    assert "night" in home.labels and home.labels["night"]["name"] == "Night"
    climate = next(iter(home.categories["helpers"]))
    assert home.categories["helpers"][climate]["name"] == "Climate"
    assert home.collections["input_number"]["target_temp"]["min"] == 15
    assert home.collections["input_number"]["target_temp"]["name"] == "Target temp"
    entry = home.extra["input_number.target_temp"]
    assert entry["labels"] == ["night"] and entry["categories"] == {"helpers": climate}
    assert (entry["area_id"], entry["name"], entry["aliases"]) == (before_entry["area_id"], before_entry["name"],
                                                                   before_entry["aliases"])
    assert entry["options"]["conversation"] == {"should_expose": True}
    assert "night" in home.extra["light.kitchen"]["labels"] and home.extra["light.hall"]["categories"] == {
        "helpers": climate}
    assert home.devices["dev-1"]["labels"] == ["night"] and home.areas["kitchen"]["labels"] == ["night"]
    assert "visits" in home.collections["counter"]
    # a second restore of the same archive finds everything there: nothing to do
    plan2 = await core.registry_restore_plan({"archive_id": op["archive_id"], "decisions": {}})
    assert {i["action"] for i in plan2["items"]} <= {"exists", "use_existing"}, [
        (i["key"], i.get("diff")) for i in plan2["items"]]
    assert not plan2["ready"] and plan2["assignments_total"] == 0
    with pytest.raises(OperationError):
        await core.registry_restore_execute({"archive_id": op["archive_id"], "decisions": {},
                                             "plan_hash": plan2["plan_hash"], "confirm": True}, ADMIN)


async def test_restore_collisions_default_to_skip_and_can_update(core: AppCore, home: FakeHome, reg):
    backup = await wait_op(core, await core.start_registry_backup(
        {"keys": ["label:night", "helper:input_number:target_temp"]}, ADMIN))
    home.labels["night"]["color"] = "red"
    home.collections["input_number"]["target_temp"]["max"] = 30
    plan = await core.registry_restore_plan({"archive_id": backup["archive_id"], "decisions": {}})
    items = {i["key"]: i for i in plan["items"]}
    assert items["label:night"]["action"] == "skip" and items["label:night"]["choices"] == ["skip", "update"]
    assert items["helper:input_number:target_temp"]["action"] == "skip"
    assert not plan["ready"]
    decisions = {"collisions": {"label:night": "update", "helper:input_number:target_temp": "replace"}}
    plan, rop = await restore(core, backup["archive_id"], decisions)
    assert rop["status"] == "completed", rop["log"][-4:]
    assert home.labels["night"]["color"] is None and home.collections["input_number"]["target_temp"]["max"] == 25
    assert rop["pre_restore_archive"]
    before = RegistryArchive(core.archives.read(rop["pre_restore_archive"]))
    assert before.labels["night"]["definition"]["color"] == "red"


async def test_restore_on_another_installation_maps_by_name(core: AppCore, home: FakeHome, reg):
    backup = await wait_op(core, await core.start_registry_backup(
        {"keys": [f"category:helpers:{reg['climate']}", "label:night"]}, ADMIN))
    # the "other installation": same names, other ids, nothing assigned
    del home.categories["helpers"][reg["climate"]]
    new_climate = home.add_category("Climate", scope="helpers")
    home.labels["nights"] = {**home.labels.pop("night"), "label_id": "nights"}
    plan = await core.registry_restore_plan({"archive_id": backup["archive_id"], "decisions": {"assignments": False}})
    items = {i["key"]: i for i in plan["items"]}
    assert items[f"category:helpers:{reg['climate']}"]["action"] == "use_existing"
    assert items[f"category:helpers:{reg['climate']}"]["target"] == new_climate
    assert items["label:night"]["target"] == "nights" and not plan["ready"]


async def test_registry_archive_upload_round_trip(core: AppCore, home: FakeHome, reg):
    op = await wait_op(core, await core.start_registry_backup({"keys": ["helper:counter:visits"]}, ADMIN))
    raw, _ = core.archive_file(op["archive_id"])
    again = await core.upload(raw, "copy.zip", ADMIN)
    assert again["duplicate"]
    core.archives.delete(op["archive_id"])
    uploaded = await core.upload(raw, "copy.zip", ADMIN)
    assert not uploaded["duplicate"] and uploaded["archive"]["kind"] == "registry"
    assert uploaded["verification"]["status"] == "verified"


async def test_writes_outside_the_operation_are_refused(core: AppCore, home: FakeHome, reg):
    from aie.ha import NotAllowed
    for command, params in (("config/label_registry/delete", {"label_id": "night"}),
                            ("counter/delete", {"counter_id": "visits"}),
                            ("config/entity_registry/update", {"entity_id": "light.kitchen", "labels": []})):
        with pytest.raises(NotAllowed):
            await home.command(command, **params)
    assert "night" in home.labels
