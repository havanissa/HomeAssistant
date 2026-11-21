"""The workflow: devices and rules, the deployment plan, deployment, external changes, failures, recovery."""

from __future__ import annotations

import json

import pytest

from ab import compiler
from ab.model import ConfigError
import builders as B
from conftest import ADMIN, GUEST, make_core

LAMP = "light.bed_lamp"
MOTION = "binary_sensor.motion"
OCC = "binary_sensor.sod_kamar_occupied"
ON = [B.call("light.turn_on")]
OFF = [B.call("light.turn_off")]


def rule(name, triggers, actions, conditions=None, **extra):
    return {"name": name, "enabled": True, "triggers": triggers, "conditions": conditions or B.all_of(),
            "actions": actions, **extra}


def add_device(core, targets=(LAMP,), name="Bed lamp"):
    return core.create_device({"name": name, "targets": list(targets)}, core.store.revision, ADMIN)["device_id"]


def add_rule(core, device_id, *args, **kwargs):
    return core.save_rule(device_id, rule(*args, **kwargs), core.store.revision, ADMIN)["rule_id"]


async def deploy(core, device_id, overwrite=False):
    plan = await core.plan(device_id)
    return await core.deploy(device_id, core.store.revision, plan["plan_hash"], overwrite, ADMIN)


async def status(core, device_id):
    return (await core.device_view(device_id))["live"]["status"]


# ------------------------------------------------------- devices and rules
async def test_fresh_installation_is_empty(core):
    overview = await core.overview()
    assert overview["devices"] == [] and overview["orphans"] == []
    assert core.store.config["settings"]["occupancy"]["first_seconds"] == 2


async def test_device_and_entity_discovery(core):
    index = core.index()
    targets = {e["entity_id"] for e in index.search("", "target")}
    assert targets == {LAMP, "switch.relay_1", "switch.relay_2", "cover.blind", "climate.heater", "input_select.mood",
                       "input_boolean.manual"}
    assert [e["entity_id"] for e in index.search("relay stova", "target")] == ["switch.relay_1", "switch.relay_2"]
    assert index.search("", "occupancy")[0]["entity_id"] == OCC
    assert {e["entity_id"] for e in index.search("", "numeric")} == {"sensor.lux"}
    devices = index.physical_devices("")
    relay = next(d for d in devices if d["name"] == "Dual relay")
    # one physical device, two distinct outputs: the user chooses which
    assert [e["entity_id"] for e in relay["entities"]] == ["switch.relay_1", "switch.relay_2"] and relay["area"] == "Stova"
    detail = index.view("input_select.mood", detail=True)
    assert detail["suggested_states"] == ["Dagur", "Hygge", "Nátt"]


async def test_create_device_with_explicit_targets(core):
    device_id = core.create_device({"targets": ["switch.relay_2"], "ha_device_id": "dev-relay"}, core.store.revision, ADMIN)["device_id"]
    device = core.store.config["devices"][device_id]
    assert device["targets"] == ["switch.relay_2"] and device["name"] == "Dual relay"  # only the chosen output
    with pytest.raises(ConfigError, match="does not exist"):
        core.create_device({"name": "X", "targets": ["light.nope"]}, core.store.revision, ADMIN)
    with pytest.raises(ConfigError, match="cannot be a controlled entity"):
        core.create_device({"name": "X", "targets": ["automation.something"]}, core.store.revision, ADMIN)
    with pytest.raises(ConfigError) as err:
        core.create_device({"name": "X", "targets": [LAMP]}, "stale-revision", ADMIN)
    assert err.value.status == 409


async def test_rules_save_edit_disable_move_delete(core):
    device_id = add_device(core)
    first = add_rule(core, device_id, "Motion", [B.state(MOTION, to="on")], ON)
    second = add_rule(core, device_id, "", [B.state(MOTION, to="off", for_seconds=120)], OFF)
    device = core.store.config["devices"][device_id]
    assert [r["id"] for r in device["rules"]] == [first, second] and device["rules"][1]["name"] == "Rule 2"
    # editing keeps the rule's identity
    edited = {**device["rules"][0], "actions": [B.call("light.turn_on", brightness_pct=40)], "enabled": False}
    assert core.save_rule(device_id, edited, core.store.revision, ADMIN)["rule_id"] == first
    device = core.store.config["devices"][device_id]
    assert device["rules"][0]["actions"][0]["data"] == {"brightness_pct": 40} and not device["rules"][0]["enabled"]
    core.move_rule(device_id, second, "up", core.store.revision, ADMIN)
    assert [r["id"] for r in core.store.config["devices"][device_id]["rules"]] == [second, first]
    core.delete_rule(device_id, first, core.store.revision, ADMIN)
    assert [r["id"] for r in core.store.config["devices"][device_id]["rules"]] == [second]
    with pytest.raises(ConfigError, match="not valid"):
        add_rule(core, device_id, "Bad", [], ON)


async def test_preview_of_a_rule_being_edited(core):
    device_id = add_device(core)
    add_rule(core, device_id, "Off", [B.state(MOTION, to="on")], OFF)
    incomplete = core.preview_rule(device_id, rule("New", [], []))
    assert not incomplete["valid"] and {p["path"] for p in incomplete["problems"]} == {"triggers", "actions"}
    preview = core.preview_rule(device_id, rule("New", [B.state(MOTION, to="on")], ON))
    assert preview["valid"] and preview["summary"]["text"] == "IF Motion changes to “on” THEN turn Bed lamp on"
    assert preview["automation"]["rules"] == 2 and "Rule: New" in preview["yaml"]
    assert [c["code"] for c in preview["conflicts"]] == ["contradiction"]
    assert len(core.store.config["devices"][device_id]["rules"]) == 1  # a preview saves nothing
    occupancy = core.preview_rule(device_id, rule("Occ", [B.occupancy(OCC)], [B.call("light.toggle")]))
    assert occupancy["has_occupancy"] and not occupancy["recheck"]["active"] and "toggle" in occupancy["recheck"]["why_not"]


# --------------------------------------------------------------- deployment
async def test_plan_shows_what_would_be_deployed_and_changes_nothing(core, home):
    device_id = add_device(core)
    add_rule(core, device_id, "Motion", [B.state(MOTION, to="on")], ON)
    plan = await core.plan(device_id)
    item = plan["items"][0]
    assert plan["can_deploy"] and item["operation"] == "create" and item["external"] is None
    assert "alias: Bed lamp (Automation Builder)" in item["yaml"] and item["diff"].startswith("--- in Home Assistant now")
    assert home.file == {} and home.requests["POST"] == 0 and home.commands_sent["validate_config"] == 1
    assert await status(core, device_id) == "not_deployed"


async def test_deploy_confirms_and_records(core, home):
    device_id = add_device(core)
    add_rule(core, device_id, "Motion", [B.state(MOTION, to="on")], ON)
    result = await deploy(core, device_id)
    automation_id = f"automation_builder_{device_id}"
    assert result["deployed"] and result["items"] == [{"id": automation_id, "operation": "create",
                                                       "entity_id": "automation.bed_lamp_automation_builder"}]
    assert home.file[automation_id]["alias"] == "Bed lamp (Automation Builder)"
    view = await core.device_view(device_id)
    automation = view["live"]["automations"][0]
    assert view["live"]["status"] == "in_sync" and automation["entity_id"] == "automation.bed_lamp_automation_builder"
    assert automation["deployed_by"] == ADMIN and automation["traces"][0]["result"] == "finished"
    assert view["last_result"]["ok"]
    # nothing to deploy now; a second deployment is refused as "nothing changed"
    plan = await core.plan(device_id)
    assert plan["items"][0]["operation"] == "none" and not plan["can_deploy"] and not plan["changes"]
    # records survive a restart of the App
    again = make_core(core.settings.data_dir, home)
    assert await status(again, device_id) == "in_sync"


async def test_multiple_rules_one_automation_and_redeploy(core, home):
    device_id = add_device(core)
    add_rule(core, device_id, "Motion", [B.state(MOTION, to="on")], ON)
    await deploy(core, device_id)
    add_rule(core, device_id, "No motion", [B.state(MOTION, to="off", for_seconds=120)], OFF)
    add_rule(core, device_id, "Hygge", [B.state("input_select.mood", to="Hygge")], [B.call("light.turn_on", brightness_pct=30)])
    assert await status(core, device_id) == "changes_pending"
    plan = await core.plan(device_id)
    assert plan["items"][0]["operation"] == "update" and "+  - alias: 'Rule: Hygge'" in plan["items"][0]["diff"].replace('"', "'")
    await deploy(core, device_id)
    assert list(home.file) == [f"automation_builder_{device_id}"]  # still one automation
    assert len(home.file[f"automation_builder_{device_id}"]["actions"]) == 3
    assert await status(core, device_id) == "in_sync"


async def test_plan_must_be_the_one_the_user_saw(core, home):
    device_id = add_device(core)
    add_rule(core, device_id, "Motion", [B.state(MOTION, to="on")], ON)
    plan = await core.plan(device_id)
    add_rule(core, device_id, "Later", [B.state(MOTION, to="off")], OFF)
    with pytest.raises(ConfigError) as err:
        await core.deploy(device_id, core.store.revision, plan["plan_hash"], False, ADMIN)
    assert err.value.code == "plan_changed" and home.file == {}
    with pytest.raises(ConfigError) as err:
        await core.deploy(device_id, "old-revision", plan["plan_hash"], False, ADMIN)
    assert err.value.code == "stale"


async def test_conflicts_block_deployment_until_confirmed(core, home):
    device_id = add_device(core)
    add_rule(core, device_id, "On", [B.state(MOTION, to="on")], ON)
    add_rule(core, device_id, "Off", [B.state(MOTION, to="on")], OFF)
    plan = await core.plan(device_id)
    assert not plan["can_deploy"] and "conflict" in plan["reasons"][0]
    with pytest.raises(ConfigError) as err:
        await core.deploy(device_id, core.store.revision, plan["plan_hash"], False, ADMIN)
    assert err.value.code == "blocked" and home.file == {}
    key = plan["conflicts"][0]["key"]
    core.update_device(device_id, {"acknowledge": key}, core.store.revision, ADMIN)
    await deploy(core, device_id)
    assert await status(core, device_id) == "in_sync"
    # the confirmation ends when the conflict it belonged to is gone
    first = core.store.config["devices"][device_id]["rules"][0]
    core.delete_rule(device_id, first["id"], core.store.revision, ADMIN)
    assert core.store.config["devices"][device_id]["acknowledged"] == []


async def test_home_assistant_validation_failure_blocks(core, home):
    device_id = add_device(core)
    add_rule(core, device_id, "Motion", [B.state(MOTION, to="on")], ON)
    home.invalid = {"actions": "extra keys not allowed @ data[0]['bogus']"}
    plan = await core.plan(device_id)
    assert not plan["can_deploy"] and "extra keys not allowed" in plan["reasons"][0]
    assert plan["items"][0]["errors"] == ["actions: extra keys not allowed @ data[0]['bogus']"]


async def test_external_modification_is_detected_and_never_overwritten_silently(core, home):
    device_id = add_device(core)
    add_rule(core, device_id, "Motion", [B.state(MOTION, to="on")], ON)
    await deploy(core, device_id)
    automation_id = f"automation_builder_{device_id}"
    home.edit_outside(automation_id, alias="Edited by hand", mode="single")
    view = await core.device_view(device_id)
    assert view["live"]["status"] == "modified"
    assert (await core.overview())["devices"][0]["warnings"] == 1
    plan = await core.plan(device_id)
    assert plan["needs_overwrite"] and plan["items"][0]["external"] == "modified"
    assert "-  alias: Edited by hand" in plan["items"][0]["diff"]
    with pytest.raises(ConfigError) as err:
        await core.deploy(device_id, core.store.revision, plan["plan_hash"], False, ADMIN)
    assert err.value.code == "external_change" and home.file[automation_id]["alias"] == "Edited by hand"
    await core.deploy(device_id, core.store.revision, plan["plan_hash"], True, ADMIN)
    assert home.file[automation_id]["alias"] == "Bed lamp (Automation Builder)" and await status(core, device_id) == "in_sync"


async def test_external_deletion_and_foreign_automation(core, home):
    device_id = add_device(core)
    add_rule(core, device_id, "Motion", [B.state(MOTION, to="on")], ON)
    await deploy(core, device_id)
    automation_id = f"automation_builder_{device_id}"
    home.delete_outside(automation_id)
    assert await status(core, device_id) == "missing"
    plan = await core.plan(device_id)
    assert plan["items"][0]["external"] == "missing" and plan["items"][0]["operation"] == "create" and not plan["needs_overwrite"]
    await deploy(core, device_id)
    assert await status(core, device_id) == "in_sync"
    # the App lost its records: what is in Home Assistant is no longer provably its own
    (core.settings.data_dir / "deploy" / "state.json").write_text("{broken")
    blank = make_core(core.settings.data_dir, home)
    assert blank.deploys.problem and await status(blank, device_id) == "foreign"
    home.edit_outside(automation_id, description="mine now")
    plan = await blank.plan(device_id)
    assert plan["items"][0]["external"] == "foreign" and plan["needs_overwrite"]


async def test_failed_deployment_reports_and_restores(core, home):
    device_id = add_device(core)
    add_rule(core, device_id, "Motion", [B.state(MOTION, to="on")], ON)
    # 1. Home Assistant refuses the automation: nothing changed
    home.reject = "Message malformed: required key not provided"
    with pytest.raises(ConfigError) as err:
        await deploy(core, device_id)
    assert err.value.code == "deploy_failed" and "Nothing was changed in Home Assistant" in str(err.value)
    assert home.file == {} and await status(core, device_id) == "not_deployed"
    assert not (await core.device_view(device_id))["last_result"]["ok"]
    # 2. recovery: the same rules deploy once Home Assistant accepts them
    home.reject = None
    await deploy(core, device_id)
    automation_id = f"automation_builder_{device_id}"
    good = json.dumps(home.file[automation_id])
    # 3. an update is saved but Home Assistant does not load it: the previous automation is put back
    add_rule(core, device_id, "Off", [B.state(MOTION, to="off")], OFF)
    plan = await core.plan(device_id)
    original_put = home.put

    async def put_without_loading(automation_id, config):
        home.loads = False
        try:
            await original_put(automation_id, config)
        finally:
            home.loads = True

    home.put = put_without_loading
    with pytest.raises(ConfigError) as err:
        await core.deploy(device_id, core.store.revision, plan["plan_hash"], False, ADMIN)
    home.put = original_put
    assert "did not load the new version" in str(err.value) and "The previous automation was put back" in str(err.value)
    assert json.dumps(home.file[automation_id]) == good
    assert await status(core, device_id) == "changes_pending"  # the rules are kept; the record is the old deployment
    # 4. Home Assistant unreachable
    home.unreachable = True
    with pytest.raises(ConfigError) as err:
        await core.plan(device_id)
    assert err.value.status == 502
    home.unreachable = False
    await deploy(core, device_id)
    assert await status(core, device_id) == "in_sync"


async def test_a_new_automation_that_is_not_loaded_is_removed_again(core, home):
    device_id = add_device(core)
    add_rule(core, device_id, "Motion", [B.state(MOTION, to="on")], ON)
    home.loads = False
    with pytest.raises(ConfigError, match="automation: !include automations.yaml"):
        await deploy(core, device_id)
    assert home.file == {} and core.deploys.records == {}


async def test_not_connected(core, home):
    device_id = add_device(core)
    add_rule(core, device_id, "Motion", [B.state(MOTION, to="on")], ON)
    home.connected = False
    with pytest.raises(ConfigError) as err:
        await core.plan(device_id)
    assert err.value.status == 503
    view = await core.device_view(device_id)
    assert view["live"]["status"] == "unknown"
    add_rule(core, device_id, "Still editable", [B.state(MOTION, to="off")], OFF)  # rules do not need Home Assistant


async def test_separate_automation_lifecycle(core, home):
    device_id = add_device(core)
    add_rule(core, device_id, "Hygge", [B.state("input_select.mood", to="Hygge")], ON)
    stairs = add_rule(core, device_id, "Stairs", [B.state(MOTION, to="on")],
                      [B.call("light.turn_on"), B.delay(60), B.call("light.turn_off")])
    await deploy(core, device_id)
    own = f"automation_builder_{device_id}_{stairs}"
    assert set(home.file) == {f"automation_builder_{device_id}", own} and home.file[own]["mode"] == "restart"
    view = await core.device_view(device_id)
    assert [a["role"] for a in view["live"]["automations"]] == ["shared", "separate"]
    # the rule no longer waits: its own automation is removed, the rule joins the shared one
    stored = next(r for r in core.store.config["devices"][device_id]["rules"] if r["id"] == stairs)
    core.save_rule(device_id, {**stored, "actions": ON, "conditions": B.all_of(B.c_state("input_select.mood", "Dagur"))},
                   core.store.revision, ADMIN)
    plan = await core.plan(device_id)
    assert {i["id"]: i["operation"] for i in plan["items"]} == {f"automation_builder_{device_id}": "update", own: "delete"}
    await deploy(core, device_id)
    assert set(home.file) == {f"automation_builder_{device_id}"} and own not in core.deploys.records


async def test_disabling_everything_removes_the_automation(core, home):
    device_id = add_device(core)
    add_rule(core, device_id, "Motion", [B.state(MOTION, to="on")], ON)
    await deploy(core, device_id)
    core.update_device(device_id, {"enabled": False}, core.store.revision, ADMIN)
    plan = await core.plan(device_id)
    assert plan["items"][0]["operation"] == "delete" and plan["can_deploy"]
    await deploy(core, device_id)
    assert home.file == {} and await status(core, device_id) == "nothing"
    assert len(core.store.config["devices"][device_id]["rules"]) == 1  # the rules are kept


async def test_restore_rules_of_an_earlier_deployment(core, home):
    device_id = add_device(core)
    add_rule(core, device_id, "Motion", [B.state(MOTION, to="on")], ON)
    with pytest.raises(ConfigError, match="not been deployed"):
        core.restore_device(device_id, "deployed", core.store.revision, ADMIN)
    await deploy(core, device_id)
    first = json.dumps(home.file)
    add_rule(core, device_id, "Off", [B.state(MOTION, to="off")], OFF)
    await deploy(core, device_id)
    add_rule(core, device_id, "Third", [B.at("23:00")], OFF)
    core.restore_device(device_id, "deployed", core.store.revision, ADMIN)  # discard the undeployed change
    assert [r["name"] for r in core.store.config["devices"][device_id]["rules"]] == ["Motion", "Off"]
    assert await status(core, device_id) == "in_sync"
    core.restore_device(device_id, "previous", core.store.revision, ADMIN)  # roll back
    assert [r["name"] for r in core.store.config["devices"][device_id]["rules"]] == ["Motion"]
    assert await status(core, device_id) == "changes_pending"  # Home Assistant changes only on an explicit deploy
    await deploy(core, device_id)
    assert json.dumps(home.file) == first


async def test_delete_device_removes_its_automation(core, home):
    device_id = add_device(core)
    add_rule(core, device_id, "Motion", [B.state(MOTION, to="on")], ON)
    await deploy(core, device_id)
    home.edit_outside(f"automation_builder_{device_id}", alias="Edited")
    with pytest.raises(ConfigError) as err:
        await core.delete_device(device_id, core.store.revision, False, ADMIN)
    assert err.value.code == "external_change" and device_id in core.store.config["devices"]
    result = await core.delete_device(device_id, core.store.revision, True, ADMIN)
    assert result["removed_automations"] == 1 and home.file == {} and core.store.config["devices"] == {}
    assert core.deploys.records == {}


async def test_unmanaged_automations_are_reported_and_untouched(core, home):
    home.states["automation.old_lamp"] = {"entity_id": "automation.old_lamp", "state": "on",
                                          "attributes": {"id": "1699999999999", "friendly_name": "Old lamp automation"}}
    home.related[LAMP] = ["automation.old_lamp"]
    device_id = add_device(core)
    add_rule(core, device_id, "Motion", [B.state(MOTION, to="on")], ON)
    await deploy(core, device_id)
    view = await core.device_view(device_id)
    assert [r["entity_id"] for r in view["related"]] == ["automation.old_lamp"]  # its own automation is not listed
    assert "left untouched" in view["related"][0]["message"]
    assert set(home.file) == {f"automation_builder_{device_id}"} and home.requests["DELETE"] == 0
    assert "automation.old_lamp" not in {e["entity_id"] for e in core.index().search("", "any") if e["entity_id"].startswith("automation.bed")}


async def test_missing_and_unavailable_entities_and_mismatch(core, home):
    device_id = add_device(core)
    add_rule(core, device_id, "Occupied", [B.occupancy(OCC)], ON, B.all_of(B.c_numeric("sensor.gone", below=5)))
    home.states[LAMP]["state"] = "unavailable"
    view = await core.device_view(device_id)
    assert {(p["code"], p["entity_id"]) for p in view["entity_problems"]} == {("missing_entity", "sensor.gone"),
                                                                             ("unavailable_entity", LAMP)}
    assert view["mismatches"] == []
    home.states[OCC]["state"] = "on"
    home.states[LAMP].update(state="off", last_changed="2026-11-21T10:30:00+00:00")
    mismatch = (await core.device_view(device_id))["mismatches"]
    assert len(mismatch) == 1 and "may have been a person" in mismatch[0]["message"]


# ------------------------------------------------------- backup and restore
async def test_backup_export_import_restore(core, home):
    device_id = add_device(core)
    add_rule(core, device_id, "Motion", [B.state(MOTION, to="on")], ON)
    await deploy(core, device_id)
    exported = json.loads(json.dumps(core.store.export()))
    other = add_device(core, ["cover.blind"], "Blind")
    add_rule(core, other, "Dusk", [B.numeric("sensor.lux", below=20)], [B.call("cover.close_cover")])
    await deploy(core, other)
    assert len(core.store.backups()) >= 3  # one before every change
    review = core.review_import(exported)
    assert review["changes"] == [{"type": "removed", "name": "Blind", "rules": 1, "deployed": True}]
    core.apply_import(exported, core.store.revision, ADMIN)
    assert list(core.store.config["devices"]) == [device_id]
    # nothing in Home Assistant changed; the automation of the vanished device is reported as a leftover
    assert f"automation_builder_{other}" in home.file
    orphans = (await core.overview())["orphans"]
    assert [o["id"] for o in orphans] == [f"automation_builder_{other}"]
    await core.remove_orphan(orphans[0]["id"], False)
    assert f"automation_builder_{other}" not in home.file and (await core.overview())["orphans"] == []
    # restore the newest backup: the Blind device is back, not deployed
    name = core.store.backups()[0]["name"]
    assert core.review_backup(name)["changes"][0]["type"] == "added"
    core.restore_backup(name, core.store.revision, ADMIN)
    assert other in core.store.config["devices"] and await status(core, other) == "not_deployed"
    with pytest.raises(ConfigError, match="not an Automation Builder"):
        core.review_import({"format": "smart-occupancy-detection-config", "config": {}})
    with pytest.raises(ConfigError):
        core.review_import({"format": "automation-builder-config", "version": 1, "config": {"devices": {"x": {}}}})


async def test_damaged_configuration_recovers_from_the_last_good_copy(core, home, tmp_path):
    device_id = add_device(core)
    add_rule(core, device_id, "Motion", [B.state(MOTION, to="on")], ON)
    (tmp_path / "config" / "config.json").write_text('{"schema": 1, "config": {"devices": ')
    recovered = make_core(tmp_path, home)
    assert recovered.store.recovery["used"] == "config.json.bak" and device_id in recovered.store.config["devices"]
    assert list((tmp_path / "config").glob("config.damaged-*.json"))


async def test_admin_check(core):
    assert await core.is_admin(ADMIN) and not await core.is_admin(GUEST) and not await core.is_admin(None)


async def test_settings_change_recompiles(core, home):
    device_id = add_device(core)
    add_rule(core, device_id, "Occ", [B.occupancy(OCC)], ON)
    await deploy(core, device_id)
    settings = {"occupancy": {**core.store.config["settings"]["occupancy"], "second_seconds": 30, "periodic_minutes": 0}}
    core.save_settings(settings, core.store.revision, ADMIN)
    assert await status(core, device_id) == "changes_pending"
    await deploy(core, device_id)
    triggers = home.file[f"automation_builder_{device_id}"]["triggers"]
    assert [t.get("for", {}).get("seconds") for t in triggers] == [None, 2, 30]
    assert compiler.is_managed_id(f"automation_builder_{device_id}")
