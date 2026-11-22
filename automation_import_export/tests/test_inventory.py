"""Discovery and classification edge cases."""

from __future__ import annotations

from conftest import FakeHome, automation
import yaml

from aie.haconfig import HaConfigFolder


async def items(core):
    view = await core.inventory_view()
    return {i["key"]: i for i in view["items"]}, view


async def test_builder_namespace_is_case_insensitive_and_markers_are_ambiguous(core, home: FakeHome):
    home.add_ui(automation("Automation_Builder_X", "Odd case"))
    home.add_ui(automation("1700000000020", "Plain alias", description="Managed by the Automation Builder App: x"))
    home.add_ui({"id": "1700000000021", "alias": "From AB blueprint",
                 "use_blueprint": {"path": "automation_builder/dev.yaml", "input": {}}})
    by, _ = await items(core)
    assert by["Automation_Builder_X"]["owner"] == "builder" and not by["Automation_Builder_X"]["deletable"]
    assert by["1700000000020"]["owner"] == "review"
    assert by["1700000000021"]["owner"] == "review"
    assert any("blueprint" in p["message"] for p in by["1700000000021"]["problems"])


async def test_duplicate_and_non_string_ids_in_the_file_block_deletion(core, home: FakeHome, ha_folder):
    entries = yaml.safe_load((ha_folder / "automations.yaml").read_text())
    entries.append({**entries[0], "alias": "duplicate"})
    entries.append({"id": 1234, "alias": "numeric", "triggers": [], "actions": []})
    entries.append({"alias": "no id", "triggers": [], "actions": []})
    (ha_folder / "automations.yaml").write_text(yaml.safe_dump(entries))
    by, view = await items(core)
    assert view["file_scan"]["duplicate_ids"] == ["1700000000001"]
    assert view["file_scan"]["without_id"] == 1 and view["file_scan"]["non_string_ids"] == ["1234"]
    assert not by["1700000000001"]["deletable"]
    assert any(p["code"] == "duplicate_id" for p in by["1700000000001"]["problems"])


async def test_unknown_metadata_definitions_block_deletion(core, home: FakeHome):
    home.registry["automation.kitchen_motion_light"]["labels"].append("gone_label")
    home.registry["automation.hall_alarm"]["area_id"] = "gone_area"
    home.registry["automation.no_metadata"]["categories"] = {"automation": "GONE"}
    by, _ = await items(core)
    for key, code in (("1700000000001", "label_unknown"), ("1700000000002", "area_unknown"), ("1700000000003", "category_unknown")):
        assert not by[key]["deletable"] and any(p["code"] == code for p in by[key]["problems"]), key


async def test_integration_and_orphan_entries_are_protected(core, home: FakeHome):
    home.registry["automation.from_integration"] = {**home._entry("automation.from_integration", "x1", "Integration rule"),  # noqa: SLF001
                                                    "platform": "some_integration", "config_entry_id": "abc"}
    home.registry["automation.left_behind"] = home._entry("automation.left_behind", "gone_id", "Left behind")  # noqa: SLF001
    by, view = await items(core)
    assert by["entity:automation.from_integration"]["source"] == "integration"
    assert by["entity:automation.from_integration"]["protected"]
    assert by["gone_id"]["source"] == "orphan" and by["gone_id"]["protected"] and not by["gone_id"]["deletable"]
    assert view["summary"]["integration"] == 2


async def test_unaddressable_ids_and_unreadable_configs(core, home: FakeHome):
    home.add_external("automation.weird", automation("has/slash", "Weird id"))
    by, _ = await items(core)
    assert any(p["code"] == "config_unreadable" for p in by["has/slash"]["problems"])
    assert not by["has/slash"]["deletable"]


async def test_without_the_config_folder_the_app_says_what_it_cannot_know(core, tmp_path):
    core.folder = HaConfigFolder(tmp_path / "missing")
    _, view = await items(core)
    assert any(p["code"] == "config_folder_unavailable" for p in view["problems"])
    assert view["file_scan"]["available"] is False


async def test_dependencies_are_reported(core, home: FakeHome):
    home.add_ui(automation("1700000000030", "Refs", actions=[
        {"action": "script.missing_one"}, {"action": "light.turn_on", "target": {"entity_id": ["light.kitchen", "light.gone"],
                                                                                 "device_id": "dev-gone", "area_id": "nowhere"}},
        {"condition": "template", "value_template": "{{ is_state('sensor.ghost', 'on') }}"}]))
    by, _ = await items(core)
    codes = {p["code"]: p for p in by["1700000000030"]["problems"]}
    assert codes["missing_entities"]["items"] == ["light.gone", "script.missing_one"]
    assert codes["missing_devices"]["items"] == ["dev-gone"] and codes["missing_areas"]["items"] == ["nowhere"]
    assert codes["missing_template_entities"]["items"] == ["sensor.ghost"]
    assert by["1700000000030"]["deletable"]  # dependency warnings do not block
    refs = by["1700000000002"]["references"]
    assert "script.goodnight" in refs["scripts"] and "scene.evening" in refs["scenes"]
    assert "input_boolean.guest" in refs["helpers"]
