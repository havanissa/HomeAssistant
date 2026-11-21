"""Room-first selection, room groups, the room filter of the pickers and the optional Default Rule."""

from __future__ import annotations

import pytest

from ab import compiler, model
from ab.model import ConfigError

import builders as B
from conftest import ADMIN

LAMP = "light.bed_lamp"
MOTION = "binary_sensor.motion"
S = B.settings()


# ------------------------------------------------------------------- rooms
def test_rooms_list_areas_with_what_can_be_controlled(core, home):
    index = core.index()
    rooms = index.rooms()
    assert [(r["name"], r["devices"], r["entities"]) for r in rooms] == [("Kamar", 1, 1), ("Stova", 1, 2), ("Unassigned", 0, 4)]
    assert rooms[-1]["area_id"] is None and rooms[-1]["unassigned"] is True
    assert [r["name"] for r in index.rooms("sto")] == ["Stova"]
    assert [r["name"] for r in index.rooms("unass")] == ["Unassigned"]


def test_devices_of_a_room_and_their_entities(core, home):
    index = core.index()
    stova = index.room_devices("stova")
    assert [(d["name"], d["model"], [e["entity_id"] for e in d["entities"]]) for d in stova] == [
        ("Dual relay", "Acme R2", ["switch.relay_1", "switch.relay_2"])]
    # entities without a device: one entry, only in "Unassigned", only controllable ones
    loose = index.room_devices(None)
    assert [(d["device_id"], d["name"]) for d in loose] == [(None, "Entities without a device")]
    assert {e["entity_id"] for e in loose[0]["entities"]} == {"cover.blind", "climate.heater", "input_select.mood", "input_boolean.manual"}
    assert index.room_devices("kamar", "relay") == [] and len(index.room_devices("stova", "relay 2")) == 1
    # an entity moved to another room than its device: the device shows in both rooms, with all its entities
    next(e for e in home.entity_registry if e["entity_id"] == "switch.relay_2")["area_id"] = "kamar"
    index = core.index()
    kamar = index.room_devices("kamar")
    assert [d["name"] for d in kamar] == ["Bed lamp", "Dual relay"]
    assert [(e["entity_id"], e["area"]) for e in kamar[1]["entities"]] == [("switch.relay_1", "Stova"), ("switch.relay_2", "Kamar")]
    # an entity with an area of its own and no device
    next(e for e in home.entity_registry if e["entity_id"] == "cover.blind")["area_id"] = "stova"
    assert [d["name"] for d in core.index().room_devices("stova")] == ["Dual relay", "Entities without a device"]


async def test_overview_groups_devices_by_room_without_duplicating_them(core, home):
    for name, targets in (("Lamp", [LAMP]), ("Relays", ["switch.relay_1", "switch.relay_2"]), ("Blind", ["cover.blind"]),
                          ("Lamp and relay", [LAMP, "switch.relay_1"]), ("Lamp and blind", [LAMP, "cover.blind"])):
        core.create_device({"name": name, "targets": targets}, core.store.revision, ADMIN)
    overview = await core.overview()
    by_id = {d["id"]: d for d in overview["devices"]}
    groups = [(g["name"], g["kind"], sorted(by_id[i]["name"] for i in g["device_ids"])) for g in overview["rooms"]]
    assert groups == [("Kamar", "room", ["Lamp"]), ("Stova", "room", ["Relays"]),
                      ("Multiple rooms", "multiple", ["Lamp and blind", "Lamp and relay"]), ("Unassigned", "unassigned", ["Blind"])]
    assert sum(len(g["device_ids"]) for g in overview["rooms"]) == len(overview["devices"]) == 5  # each exactly once
    mixed = next(d for d in overview["devices"] if d["name"] == "Lamp and relay")
    assert [a["name"] for a in mixed["room"]["areas"]] == ["Kamar", "Stova"]
    partly = next(d for d in overview["devices"] if d["name"] == "Lamp and blind")
    assert partly["room"]["partly_unassigned"] is True
    # one automation per device whatever the group: rooms organise the page, nothing else
    device_id = mixed["id"]
    core.save_rule(device_id, {"name": "R", "triggers": [B.state(MOTION, to="on")], "actions": [B.call("homeassistant.turn_on")]},
                   core.store.revision, ADMIN)
    assert len((await core.device_view(device_id))["live"]["automations"]) == 1
    assert (await core.device_view(device_id))["room"]["kind"] == "multiple"


def test_room_filter_of_the_entity_picker(core, home):
    home.entity_registry.append({"entity_id": "sensor.kamar_temp", "device_id": None, "area_id": "kamar"})
    home.states["sensor.kamar_temp"] = {"entity_id": "sensor.kamar_temp", "state": "21", "attributes": {"friendly_name": "Kamar temperature"}}
    index = core.index()
    everything = {e["entity_id"] for e in index.search("")}
    kamar = [e["entity_id"] for e in index.search("", area="kamar")]
    assert set(kamar) == {LAMP, "sensor.kamar_temp"}          # directly assigned, and inherited from the device
    assert "sensor.lux" in everything and "sensor.lux" not in kamar   # a helper without a room is not assumed to be in one
    # the entity that is chosen now stays visible, first, although it is in no room
    kept = index.search("", area="kamar", keep="sensor.lux")
    assert kept[0]["entity_id"] == "sensor.lux" and kept[0]["current"] is True and len(kept) == 3
    assert [e["entity_id"] for e in index.search("temp", area="kamar")] == ["sensor.kamar_temp"]
    assert index.search("", "numeric", area="stova") == []
    assert {e["entity_id"] for e in index.search("", "target", area="stova")} == {"switch.relay_1", "switch.relay_2"}


# ------------------------------------------------------------ default rule
def with_default(default, *rules):
    return B.device(1, "Lamp", [LAMP], list(rules) or [B.rule(1, "A", [B.state(MOTION, to="on")], [B.call("light.turn_on")],
                                                               B.all_of(B.c_numeric("sensor.lux", below=100)))], default_rule=default)


def problems(build) -> str:
    with pytest.raises(ConfigError) as err:
        build()
    return " | ".join(p["message"] for p in err.value.problems)


def test_default_rule_is_optional_and_absent_from_ordinary_documents():
    assert "default_rule" not in with_default(None) and "default_rule" not in with_default({})
    assert "default_rule" not in with_default({"enabled": False, "actions": []})
    kept = with_default({"enabled": False, "actions": [B.call("light.turn_off")]})
    assert kept["default_rule"] == {"enabled": False, "actions": [B.call("light.turn_off")], "override_entity": None}
    assert model.active_default(kept) is None
    assert "Add at least one action" in problems(lambda: with_default({"enabled": True, "actions": []}))
    assert "cannot wait" in problems(lambda: with_default({"enabled": True, "actions": [B.delay(5), B.call("light.turn_off")]}))
    assert "not one of this device's selected entities" in problems(
        lambda: with_default({"enabled": True, "actions": [B.call("light.turn_off", target=["light.other"])]}))


def test_default_rule_compiles_into_one_native_wrapper():
    plain = compiler.compile_device(with_default(None), S)["automations"][0]
    device = with_default({"enabled": True, "actions": [B.call("light.turn_off")], "override_entity": "input_boolean.manual"})
    auto = compiler.compile_device(device, S)["automations"][0]
    config = auto["config"]
    assert auto["mode"] == "queued" and config["triggers"] == plain["config"]["triggers"] and len(config["actions"]) == 1
    wrapper = config["actions"][0]
    rule_block = plain["config"]["actions"][0]
    assert wrapper["if"] == [{"condition": "and", "conditions": rule_block["if"]}]
    assert wrapper["then"] == [rule_block]                      # the rules exactly as without a default
    assert wrapper["else"] == [{"alias": "Default", "if": [
        {"condition": "trigger", "id": [t["id"] for t in config["triggers"]]},
        {"condition": "state", "entity_id": "input_boolean.manual", "state": "off"}],
        "then": [{"action": "light.turn_off", "target": {"entity_id": [LAMP]}}]}]
    assert "Default" in config["description"] and auto["blocks"] == [
        {"rule_id": "r_00000001", "kind": "rule", "index": 0, "path": "action/0/then/0"}]
    # no enabled rule: the default has nothing that could call for it, and says so
    alone = B.device(1, "Lamp", [LAMP], [], default_rule={"enabled": True, "actions": [B.call("light.turn_off")]})
    result = compiler.compile_device(alone, S)
    assert result["automations"] == [] and [n["code"] for n in result["notes"]] == ["default_without_rules"]


async def test_default_rule_workflow(core, home):
    core.create_device({"name": "Lamp", "targets": [LAMP]}, core.store.revision, ADMIN)
    device_id = next(iter(core.store.config["devices"]))
    core.save_rule(device_id, {"name": "Motion", "triggers": [B.state(MOTION, to="on")], "actions": [B.call("light.turn_on")],
                               "conditions": B.all_of(B.c_numeric("sensor.lux", below=100))}, core.store.revision, ADMIN)
    plan = await core.plan(device_id)
    await core.deploy(device_id, core.store.revision, plan["plan_hash"], False, ADMIN)
    before = dict(home.file)
    view = await core.device_view(device_id)
    assert view["default_summary"] is None and view["room"]["name"] == "Kamar"
    preview = core.preview_default(device_id, {"enabled": True, "actions": []})
    assert preview["valid"] is False and "Add at least one action" in preview["problems"][0]["message"]
    default = {"enabled": True, "actions": [B.call("light.turn_off")]}
    preview = core.preview_default(device_id, default)
    assert preview["valid"] and preview["active"] and preview["summary"]["text"] == "DEFAULT (no rule matched) THEN turn Bed lamp off"
    assert "alias: Default" in preview["yaml"] and core.store.config["devices"][device_id].get("default_rule") is None
    core.update_device(device_id, {"default_rule": default}, core.store.revision, ADMIN)
    assert home.file == before                                   # saving is not deploying
    view = await core.device_view(device_id)
    assert view["live"]["status"] == "changes_pending" and view["default_summary"]["then"] == "turn Bed lamp off"
    plan = await core.plan(device_id)
    assert plan["can_deploy"] and plan["items"][0]["operation"] == "update"
    await core.deploy(device_id, core.store.revision, plan["plan_hash"], False, ADMIN)
    assert list(home.file) == [f"automation_builder_{device_id}"]  # still one automation
    assert home.file[f"automation_builder_{device_id}"]["actions"][0]["else"][0]["alias"] == "Default"
    assert (await core.overview())["devices"][0]["default"] is True
    # turned off again: the automation goes back to exactly what it was
    core.update_device(device_id, {"default_rule": {**default, "enabled": False}}, core.store.revision, ADMIN)
    plan = await core.plan(device_id)
    await core.deploy(device_id, core.store.revision, plan["plan_hash"], False, ADMIN)
    assert home.file == before
    # removed completely
    core.update_device(device_id, {"default_rule": None}, core.store.revision, ADMIN)
    assert "default_rule" not in core.store.config["devices"][device_id]


def test_documents_of_earlier_versions_load_unchanged(core):
    exported = core.store.export()
    exported["config"]["devices"]["d_0000000a"] = {"name": "Old", "targets": [LAMP], "enabled": True, "ha_device_id": None, "rules": [
        {"id": "r_0000000a", "name": "R", "enabled": True, "triggers": [{"type": "state", "entity_id": MOTION, "to": "on", "from": None, "for_seconds": 0}],
         "conditions": {"type": "and", "items": []}, "actions": [{"type": "service", "action": "light.turn_on", "target": "all", "data": {}}],
         "retrigger": "restart", "recheck": {"enabled": True, "first_seconds": None, "second_seconds": None, "periodic_minutes": None, "override_entity": None}}],
        "acknowledged": []}
    core.apply_import(exported, core.store.revision, ADMIN)
    device = core.store.config["devices"]["d_0000000a"]
    assert "default_rule" not in device and "blueprint" not in device
    # the hash of the automation Automation Builder 0.1.0 generates for this document (git 31cad38)
    assert compiler.compile_device(device, S)["automations"][0]["hash"] == "4a221eebc34470a1e359"
