"""Export -> verify -> delete -> import -> verify exact restoration, against the fake Home Assistant."""

from __future__ import annotations

import copy

from conftest import FakeHome, wait_op

from aie.archive import Archive


def keys_of(core, **where):
    snap = core._cache[1]  # noqa: SLF001
    return [i.key for i in snap.items if all(getattr(i, k) == v for k, v in where.items())]


async def test_inventory_classifies_sources_and_owners(core, home: FakeHome):
    view = await core.inventory_view()
    by = {i["key"]: i for i in view["items"]}
    assert by["1700000000001"]["source"] == "ui" and by["1700000000001"]["owner"] == "ordinary"
    assert by["1700000000001"]["deletable"] is True
    assert [lab["id"] for lab in by["1700000000001"]["labels"]] == ["lighting", "night"]
    assert by["1700000000001"]["area"] == {"id": "kitchen", "name": "Kitchen"}
    assert by["1700000000001"]["categories"][0]["name"] == "Lights"
    assert by["automation_builder_dev_1"]["owner"] == "builder" and by["automation_builder_dev_1"]["protected"]
    assert by["automation_builder_dev_1"]["deletable"] is False
    assert by["1700000000007"]["owner"] == "review" and by["1700000000007"]["needs_review"]
    assert by["package_rule_1"]["source"] == "external" and not by["package_rule_1"]["deletable"]
    assert by["entity:automation.no_id_rule"]["source"] == "external"
    assert by["1700000000004"]["blueprint"] == "homeassistant/motion_light.yaml"
    assert by["1700000000005"]["registry_disabled"] == "user" and by["1700000000005"]["source"] == "ui"
    s = view["summary"]
    assert s["builder"] == 1 and s["review"] == 1 and s["external"] == 2


async def test_full_round_trip_restores_yaml_labels_categories_areas_identity_and_state(core, home: FakeHome):
    before_file = copy.deepcopy(home.file)
    before_registry = copy.deepcopy(home.registry)
    before_states = {e: s["state"] for e, s in home.states.items()}
    await core.inventory_view()
    ordinary = keys_of(core, source="ui", owner="ordinary")
    assert set(ordinary) == {"1700000000001", "1700000000002", "1700000000003", "1700000000004", "1700000000005"}

    op = await wait_op(core, await core.start_cleanup({"keys": ordinary}, "user-admin"))
    assert op["status"] == "awaiting_confirmation", op.get("error") or op["log"]
    assert op["verification"]["ok"], [c for c in op["verification"]["checks"] if not c["ok"]]
    assert sorted(d["key"] for d in op["preview"]["delete"]) == sorted(ordinary)
    op = await wait_op(core, await core.confirm_cleanup({"operation_id": op["id"], "plan_hash": op["plan_hash"],
                                                         "confirm_count": len(ordinary), "archive_ack": True}, "user-admin"))
    assert op["status"] == "completed", op["log"]
    assert op["report"]["protected_changed"] == []
    assert {c["id"] for c in home.file} == {"automation_builder_dev_1", "1700000000007"}
    builder_before = next(c for c in before_file if c["id"] == "automation_builder_dev_1")
    assert home.ui_config("automation_builder_dev_1") == builder_before

    # Restore to the same installation; the deleted-entity records are gone too (as after a long time).
    home.deleted.clear()
    archive_id = op["archive_id"]
    plan = await core.restore_plan({"archive_id": archive_id})
    assert plan["counts"]["create"] == 5 and not plan["acknowledgements_missing"], plan
    op2 = await wait_op(core, await core.restore_execute({"archive_id": archive_id, "plan_hash": plan["plan_hash"],
                                                          "confirm": True}, "user-admin"))
    assert op2["status"] == "completed", op2["log"]
    after = {c["id"]: c for c in home.file}
    for config in before_file:
        assert after[config["id"]] == config  # exact, including unknown fields and types
    for entity_id, entry in before_registry.items():
        if entry["unique_id"] in ("automation_builder_dev_1", "1700000000007") or entity_id.endswith("package_rule"):
            continue
        now = home.registry.get(entity_id)
        assert now is not None, f"{entity_id} was not restored with its entity id"
        for field in ("labels", "categories", "area_id", "name", "icon", "aliases", "disabled_by", "hidden_by"):
            assert sorted(now[field]) == sorted(entry[field]) if field == "labels" else now[field] == entry[field], field
        if entity_id in before_states:
            assert home.states[entity_id]["state"] == before_states[entity_id]
    assert "automation.disabled_entity" not in home.states  # still disabled


async def test_restore_recreates_missing_label_category_and_area_and_maps_by_name(core, home: FakeHome):
    await core.inventory_view()
    op = await wait_op(core, await core.start_export({"keys": ["1700000000001"]}, "user-admin"))
    assert op["status"] == "completed"
    archive_id = op["archive_id"]
    cat = next(iter(home.categories["automation"]))
    # delete the automation the way Home Assistant does, then lose the metadata definitions
    with home.scoped(deletions={"1700000000001"}):
        from conftest import FakeApi
        await FakeApi(home).delete("1700000000001")
    home.deleted.clear()
    del home.labels["night"]
    home.labels["lighting"]["name"] = "Lighting"
    del home.categories["automation"][cat]
    home.areas.pop("kitchen")
    home.areas["kitchen_2"] = {"area_id": "kitchen_2", "name": "kitchen ", "icon": None, "floor_id": None,
                               "aliases": [], "labels": [], "picture": None}  # same name -> mapped, not duplicated
    plan = await core.restore_plan({"archive_id": archive_id})
    labels = {p["source_id"]: p for p in plan["labels"]}
    assert labels["lighting"]["action"] == "use" and labels["night"]["action"] == "create"
    assert plan["categories"][0]["action"] == "create"
    assert plan["areas"][0]["action"] == "use" and plan["areas"][0]["match"] == "name"
    op = await wait_op(core, await core.restore_execute({"archive_id": archive_id, "plan_hash": plan["plan_hash"],
                                                         "confirm": True}, "user-admin"))
    assert op["status"] == "completed", op["log"]
    entity = home.registry["automation.kitchen_motion_light"]
    assert set(entity["labels"]) == {"lighting", "night"}
    assert home.categories["automation"][entity["categories"]["automation"]]["name"] == "Lights"
    assert entity["area_id"] == "kitchen_2"
    assert not [w for w in home.writes if w[0] == "area_create"]
    assert sum(1 for lab in home.labels.values() if lab["name"] == "Night") == 1


async def test_dropping_a_label_needs_an_acknowledgement_and_is_reported(core, home: FakeHome):
    await core.inventory_view()
    op = await wait_op(core, await core.start_export({"keys": ["1700000000002"]}, "user-admin"))
    archive_id = op["archive_id"]
    home.file = [c for c in home.file if c["id"] != "1700000000002"]
    home.registry.pop("automation.hall_alarm")
    home.states.pop("automation.hall_alarm")
    decisions = {"labels": {"security": {"action": "drop"}}}
    plan = await core.restore_plan({"archive_id": archive_id, "decisions": decisions})
    assert plan["acknowledgements_missing"] == ["drop_label:security"] and not plan["ready"]
    decisions["acknowledgements"] = ["drop_label:security"]
    plan = await core.restore_plan({"archive_id": archive_id, "decisions": decisions})
    assert plan["ready"]
    op = await wait_op(core, await core.restore_execute({"archive_id": archive_id, "decisions": decisions,
                                                         "plan_hash": plan["plan_hash"], "confirm": True}, "user-admin"))
    assert op["status"] == "completed"
    assert home.registry["automation.hall_alarm"]["labels"] == []
    assert home.states["automation.hall_alarm"]["state"] == "off"


async def test_archive_contains_exact_yaml_and_metadata(core, home: FakeHome):
    await core.inventory_view()
    op = await wait_op(core, await core.start_export({"mode": "all"}, "user-admin"))
    archive = Archive(core.archives.read(op["archive_id"]))
    m = archive.manifest
    keys = {i["key"] for i in m["inventory"]}
    assert "automation_builder_dev_1" in keys and "package_rule_1" in keys  # backup-only mode includes protected
    assoc = m["associations"]["1700000000001"]
    assert assoc["labels"] == ["lighting", "night"] and assoc["area_id"] == "kitchen"
    assert archive.definitions("labels")["night"]["name"] == "Night"
    assert archive.blueprint_file("homeassistant/motion_light.yaml") is not None
    record = next(r for r in archive.records if r["key"] == "1700000000001")
    assert record["config"]["custom_field"] == {"kept": [1, 2.5, True, None, "on"]}
    assert op["verification"]["checks"] and all(c["ok"] for c in op["verification"]["checks"])
