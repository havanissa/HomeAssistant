"""Restoration: collisions, mapping, acknowledgements, blueprints, failures, verification and rollback."""

from __future__ import annotations

import pytest
from conftest import FakeApi, FakeHome, automation, wait_op

from aie.core import AppCore, OperationError


async def export(core: AppCore, keys) -> str:
    await core.inventory_view()
    op = await wait_op(core, await core.start_export({"keys": keys}, "user-admin"))
    assert op["status"] == "completed", op["log"]
    return op["archive_id"]


async def remove(home: FakeHome, *ids: str, forget: bool = True) -> None:
    for config_id in ids:
        with home.scoped(deletions={config_id}):
            await FakeApi(home).delete(config_id)
    if forget:
        home.deleted.clear()


async def run(core: AppCore, archive_id: str, decisions=None):
    plan = await core.restore_plan({"archive_id": archive_id, "decisions": decisions or {}})
    op = await core.restore_execute({"archive_id": archive_id, "decisions": decisions or {},
                                     "plan_hash": plan["plan_hash"], "confirm": True}, "user-admin")
    return plan, await wait_op(core, op)


async def test_identical_existing_automations_only_get_their_metadata(core, home: FakeHome):
    archive = await export(core, ["1700000000001"])
    entry = home.registry["automation.kitchen_motion_light"]
    entry["labels"] = []
    entry["area_id"] = None
    plan, op = await run(core, archive)
    assert plan["items"][0]["action"] == "metadata"
    assert op["status"] == "completed" and not [w for w in home.writes if w[0] == "post"]
    assert entry["labels"] == ["lighting", "night"] and entry["area_id"] == "kitchen"
    assert op["pre_restore_archive"]  # the metadata it had before is backed up


async def test_a_different_existing_automation_is_skipped_replaced_or_copied(core, home: FakeHome):
    archive = await export(core, ["1700000000003"])
    home.file[2]["alias"] = "Edited meanwhile"
    plan = await core.restore_plan({"archive_id": archive})
    item = plan["items"][0]
    assert item["action"] == "skip" and item["collision"] == "different" and item["diff"]
    assert not plan["ready"]
    plan, op = await run(core, archive, {"collisions": {"1700000000003": "replace"}})
    assert op["status"] == "completed" and home.ui_config("1700000000003")["alias"] == "No metadata"
    backup = core.archive_view(op["pre_restore_archive"])
    assert backup["inventory"][0]["alias"] == "Edited meanwhile"
    plan, op2 = await run(core, archive, {"collisions": {"1700000000003": "new"}, "new_ids": {"1700000000003": "copy_1"}})
    assert op2["status"] == "completed"
    copy = home.ui_config("copy_1")
    assert copy["id"] == "copy_1" and copy["alias"] == "No metadata"
    assert home.entity_of("copy_1") == "automation.no_metadata_2"
    # rollback of the copy removes it; rollback of the replace puts the edited version back
    rb = await wait_op(core, await core.rollback({"operation_id": op2["id"], "confirm": True}, "user-admin"))
    assert rb["status"] == "completed" and home.ui_config("copy_1") is None
    rb = await wait_op(core, await core.rollback({"operation_id": op["id"], "confirm": True}, "user-admin"))
    assert rb["status"] == "completed", rb["log"]
    assert home.ui_config("1700000000003")["alias"] == "Edited meanwhile"


async def test_a_stale_plan_or_missing_acknowledgement_is_refused(core, home: FakeHome):
    archive = await export(core, ["1700000000002"])
    await remove(home, "1700000000002")
    plan = await core.restore_plan({"archive_id": archive})
    home.add_label("Something new")  # the live state changed since the preview
    with pytest.raises(OperationError) as err:
        await core.restore_execute({"archive_id": archive, "plan_hash": plan["plan_hash"], "confirm": True}, "u")
    assert err.value.code == "stale_plan" and "plan" in err.value.extra
    decisions = {"areas": {"hall": {"action": "drop"}}}
    plan = await core.restore_plan({"archive_id": archive, "decisions": decisions})
    with pytest.raises(OperationError) as err:
        await core.restore_execute({"archive_id": archive, "decisions": decisions, "plan_hash": plan["plan_hash"],
                                    "confirm": True}, "u")
    assert err.value.code == "not_acknowledged" and err.value.extra["missing"] == ["drop_area:hall"]
    with pytest.raises(OperationError) as err:
        await core.restore_execute({"archive_id": archive, "plan_hash": (await core.restore_plan(
            {"archive_id": archive}))["plan_hash"]}, "u")
    assert err.value.code == "not_confirmed"


async def test_entity_id_taken_needs_an_acknowledgement(core, home: FakeHome):
    archive = await export(core, ["1700000000003"])
    await remove(home, "1700000000003")
    home.add_ui(automation("other", "No metadata"))  # takes automation.no_metadata
    plan = await core.restore_plan({"archive_id": archive})
    assert plan["acknowledgements_missing"] == ["entity_id:1700000000003"]
    plan, op = await run(core, archive, {"acknowledgements": ["entity_id:1700000000003"]})
    assert op["status"] == "completed" and home.entity_of("1700000000003") == "automation.no_metadata_2"


async def test_missing_blueprint_is_installed_from_the_archive(core, home: FakeHome):
    archive = await export(core, ["1700000000004"])
    await remove(home, "1700000000004")
    del home.blueprints["homeassistant/motion_light.yaml"]
    (core.folder.root / "blueprints/automation/homeassistant/motion_light.yaml").unlink()
    plan = await core.restore_plan({"archive_id": archive})
    assert plan["blueprints"][0]["action"] == "install"
    plan, op = await run(core, archive)
    assert op["status"] == "completed", op["log"]
    assert home.blueprint_files["homeassistant/motion_light.yaml"].startswith("blueprint:")


async def test_missing_blueprint_without_a_file_needs_an_acknowledgement(core, home: FakeHome, tmp_path):
    import shutil
    shutil.rmtree(core.folder.root / "blueprints")  # the archive cannot include it
    archive = await export(core, ["1700000000004"])
    await remove(home, "1700000000004")
    del home.blueprints["homeassistant/motion_light.yaml"]
    home.unavailable_on_load.add("1700000000004")
    plan = await core.restore_plan({"archive_id": archive})
    assert plan["acknowledgements_missing"] == ["blueprint_missing:homeassistant/motion_light.yaml"]
    plan, op = await run(core, archive, {"acknowledgements": plan["acknowledgements_missing"]})
    assert op["status"] == "completed"  # unavailable was acknowledged as expected


async def test_a_refused_write_is_a_partial_restoration_and_can_be_rolled_back(core, home: FakeHome):
    archive = await export(core, ["1700000000001", "1700000000003"])
    await remove(home, "1700000000001", "1700000000003")
    home.fail_post["1700000000001"] = "Message malformed: refused"
    plan, op = await run(core, archive)
    st = {k: v["status"] for k, v in op["items"].items()}
    assert st == {"1700000000001": "failed", "1700000000003": "restored"} and op["status"] == "incomplete"
    rb = await wait_op(core, await core.rollback({"operation_id": op["id"], "confirm": True}, "user-admin"))
    assert rb["status"] == "completed" and home.ui_config("1700000000003") is None


async def test_lost_labels_make_the_restoration_incomplete_not_a_success(core, home: FakeHome, monkeypatch):
    archive = await export(core, ["1700000000001"])
    await remove(home, "1700000000001")
    original = home.ws_config_entity_registry_update

    def drop_labels(entity_id, **changes):  # Home Assistant silently ignores a label (as for a deleted label)
        changes.pop("labels", None)
        return original(entity_id, **changes)
    monkeypatch.setattr(home, "ws_config_entity_registry_update", drop_labels)
    plan, op = await run(core, archive)
    assert op["status"] == "incomplete"
    assert any("labels" in d for d in op["result"]["results"]["1700000000001"]["discrepancies"])


async def test_external_and_builder_records_are_not_restored_to_their_own_ids(core, home: FakeHome):
    await core.inventory_view()
    op = await wait_op(core, await core.start_export({"mode": "all"}, "user-admin"))
    plan = await core.restore_plan({"archive_id": op["archive_id"], "decisions": {
        "selection": ["package_rule_1", "automation_builder_dev_1"]}})
    acts = {i["key"]: (i["action"], i.get("collision")) for i in plan["items"] if i["action"] != "not_selected"}
    assert acts == {"package_rule_1": ("skip", "protected"), "automation_builder_dev_1": ("skip", "protected")}
    plan = await core.restore_plan({"archive_id": op["archive_id"], "decisions": {
        "selection": ["automation_builder_dev_1"], "collisions": {"automation_builder_dev_1": "new"}}})
    item = next(i for i in plan["items"] if i["key"] == "automation_builder_dev_1")
    assert item["action"] == "new" and not item["target_id"].startswith("automation_builder")
    assert "copy:automation_builder_dev_1" in plan["acknowledgements_missing"]


async def test_cross_installation_archive_maps_by_name_and_reports_it(core, home: FakeHome):
    archive = await export(core, ["1700000000001"])
    core.identity.instance_id = "00000000-0000-0000-0000-000000000000"
    await remove(home, "1700000000001")
    cat = next(iter(home.categories["automation"]))
    home.categories["automation"]["NEWID"] = {**home.categories["automation"].pop(cat), "category_id": "NEWID"}
    plan = await core.restore_plan({"archive_id": archive})
    assert plan["same_installation"] is False
    assert plan["categories"][0]["action"] == "use" and plan["categories"][0]["target"] == "NEWID"
    plan, op = await run(core, archive)
    assert op["status"] == "completed"
    assert home.registry["automation.kitchen_motion_light"]["categories"] == {"automation": "NEWID"}


async def test_several_labels_with_the_same_name_must_be_chosen(core, home: FakeHome):
    archive = await export(core, ["1700000000002"])
    await remove(home, "1700000000002")
    del home.labels["security"]
    home.add_label("Security")
    home.labels["security_x"] = {**home.labels["security"], "label_id": "security_x"}
    del home.labels["security"]
    home.labels["security_y"] = {**home.labels["security_x"], "label_id": "security_y"}
    plan = await core.restore_plan({"archive_id": archive})
    assert plan["labels"][0]["action"] == "unresolved" and plan["items"][0]["action"] == "blocked"
    plan, op = await run(core, archive, {"labels": {"security": {"action": "use", "target": "security_y"}}})
    assert op["status"] == "completed" and home.registry["automation.hall_alarm"]["labels"] == ["security_y"]


async def test_yaml_files_are_imported_without_metadata(core, home: FakeHome):
    text = """
automation:
  - alias: From file without id
    triggers: [{trigger: event, event_type: x}]
    actions: []
  - id: file_one
    alias: From file
    triggers: [{trigger: event, event_type: y}]
    actions: []
  - id: file_one
    alias: Duplicate id
    triggers: []
    actions: []
  - alias: Secret
    triggers: []
    actions: [{action: notify.x, data: {message: !secret s}}]
  - alias: Date value
    triggers: [{trigger: time, at: 2026-01-01}]
    actions: []
  - just a string
  - alias: No triggers
    actions: []
"""
    res = await core.import_yaml({"filename": "a.yaml", "text": text}, "user-admin")
    reasons = sorted(r["reason"].split(" ")[0] for r in res["refused"])
    assert res["imported"] == 2 and len(res["refused"]) == 5, res["refused"]
    plan, op = await run(core, res["archive_id"])
    assert op["status"] == "completed", op["log"]
    assert home.ui_config("file_one")["alias"] == "From file"
    assert any(c.get("alias") == "From file without id" and c["id"].isdigit() for c in home.file)
    assert reasons
    with pytest.raises(OperationError):
        await core.import_yaml({"filename": "b.yaml", "text": "- [unclosed"}, "user-admin")
    with pytest.raises(OperationError):
        await core.import_yaml({"filename": "c.yaml", "text": "{}"}, "user-admin")


async def test_restore_as_new_without_an_explicit_id_is_stable_between_preview_and_execution(core, home: FakeHome):
    archive = await export(core, ["1700000000003"])
    decisions = {"collisions": {"1700000000003": "new"}}
    first = await core.restore_plan({"archive_id": archive, "decisions": decisions})
    second = await core.restore_plan({"archive_id": archive, "decisions": decisions})
    assert first["plan_hash"] == second["plan_hash"]
    new_id = next(i["target_id"] for i in first["items"] if i["key"] == "1700000000003")
    assert new_id.isdigit() and len(new_id) == 13 and new_id != "1700000000003"
    op = await wait_op(core, await core.restore_execute({"archive_id": archive, "decisions": decisions,
                                                         "plan_hash": first["plan_hash"], "confirm": True}, "u"))
    assert op["status"] == "completed" and home.ui_config(new_id)["alias"] == "No metadata"


async def test_an_imported_yaml_file_verifies(core, home: FakeHome):
    res = await core.import_yaml({"filename": "x.yaml", "text": "- id: y1\n  alias: Y\n  triggers: []\n  actions: []\n"}, "u")
    assert res["verification"]["status"] == "verified"
