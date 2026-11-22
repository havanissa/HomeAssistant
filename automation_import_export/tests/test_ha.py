"""The allowlist and the write scope: what the App can send to Home Assistant at all."""

from __future__ import annotations

import pytest

from aie.ha import ALLOWED_COMMANDS, AutomationConfigApi, HomeAssistantClient, NotAllowed, WriteScope, check_params


def scope(**kw) -> WriteScope:
    s = WriteScope()
    s.configs |= set(kw.get("configs", ()))
    s.deletions |= set(kw.get("deletions", ()))
    s.entities |= set(kw.get("entities", ()))
    s.metadata = kw.get("metadata", False)
    return s


def refused(command: str, s: WriteScope | None = None, **params) -> None:
    with pytest.raises(NotAllowed):
        check_params(command, params, s or WriteScope())


def test_no_command_can_control_a_device_or_delete_metadata():
    forbidden = {"execute_script", "fire_event", "config/label_registry/delete", "config/category_registry/delete",
                 "config/area_registry/delete", "config/entity_registry/remove", "config/device_registry/update",
                 "blueprint/delete", "render_template", "subscribe_trigger", "config/label_registry/update",
                 "config/category_registry/update", "config/area_registry/update", "conversation/process"}
    assert not forbidden & ALLOWED_COMMANDS
    for command in forbidden:
        refused(command)


def test_service_calls_only_for_automations_of_the_running_restore():
    s = scope(entities={"automation.a"}, configs={"1"})
    check_params("call_service", {"domain": "automation", "service": "turn_off", "target": {"entity_id": "automation.a"}}, s)
    check_params("call_service", {"domain": "automation", "service": "reload", "service_data": {"id": "1"}}, s)
    refused("call_service", s, domain="light", service="turn_off", target={"entity_id": "automation.a"})
    refused("call_service", s, domain="automation", service="trigger", target={"entity_id": "automation.a"})
    refused("call_service", s, domain="automation", service="turn_off", target={"entity_id": "automation.b"})
    refused("call_service", s, domain="automation", service="turn_off", target={"area_id": "kitchen"})
    refused("call_service", s, domain="automation", service="turn_off", target={"entity_id": "all"})
    refused("call_service", s, domain="automation", service="reload")                           # all automations
    refused("call_service", s, domain="automation", service="reload", service_data={"id": "2"})
    refused("call_service", s, domain="automation", service="turn_on", target={"entity_id": "automation.a"},
            return_response=True)
    refused("call_service", WriteScope(), domain="automation", service="turn_off", target={"entity_id": "automation.a"})


def test_registry_updates_only_for_scoped_automation_entities_and_fields():
    s = scope(entities={"automation.a"})
    check_params("config/entity_registry/update", {"entity_id": "automation.a", "labels": [], "area_id": None,
                                                   "categories": {"automation": None}, "new_entity_id": "automation.b"}, s)
    refused("config/entity_registry/update", s, entity_id="light.a", labels=[])
    refused("config/entity_registry/update", s, entity_id="automation.other", labels=[])
    refused("config/entity_registry/update", s, entity_id="automation.a", device_class="x")
    refused("config/entity_registry/update", s, entity_id="automation.a", new_entity_id="light.x")
    refused("config/entity_registry/update", s, entity_id="automation.a", disabled_by="integration")
    refused("config/entity_registry/update", s, entity_id="automation.a", options_domain="x", options={})


def test_metadata_creation_needs_a_confirmed_restore_and_stays_narrow():
    refused("config/label_registry/create", WriteScope(), name="x")
    s = scope(metadata=True)
    check_params("config/label_registry/create", {"name": "x", "color": "red", "icon": "mdi:x"}, s)
    check_params("config/category_registry/create", {"scope": "automation", "name": "x"}, s)
    refused("config/category_registry/create", s, scope="script", name="x")
    refused("config/area_registry/create", s, name="x", floor_id="f")
    check_params("blueprint/save", {"domain": "automation", "path": "homeassistant/motion_light.yaml", "yaml": "x"}, s)
    for bad in ("../x.yaml", "/abs.yaml", "a/../../b.yaml", "x.py", "automation_builder/dev.yaml"):
        refused("blueprint/save", s, domain="automation", path=bad, yaml="x")
    refused("blueprint/save", s, domain="automation", path="a/b.yaml", yaml="x", allow_override=True)
    refused("blueprint/save", s, domain="script", path="a/b.yaml", yaml="x")


def test_reads_are_narrow_too():
    check_params("automation/config", {"entity_id": "automation.x"}, WriteScope())
    refused("automation/config", entity_id="script.x")
    refused("config/entity_registry/get_entries", entity_ids=["light.x"])
    refused("blueprint/list", domain="script")


async def test_the_editor_endpoint_is_limited():
    s = WriteScope()
    api = AutomationConfigApi(None, "http://supervisor/core/api", "t", s)  # type: ignore[arg-type]
    for bad in ("../../config/core", "a/b", "x?y=1", "", "é", "a" * 300, "-lead"):
        assert not api.addressable(bad)
        with pytest.raises(NotAllowed):
            await api.get(bad)
    with pytest.raises(NotAllowed):
        await api.put("1700000000001", {})        # not in the scope
    with pytest.raises(NotAllowed):
        await api.delete("1700000000001")
    s.deletions.add("automation_builder_x")
    with pytest.raises(NotAllowed):
        await api.delete("automation_builder_x")  # never, whatever the scope
    assert api._url("1700000000001") == "http://supervisor/core/api/config/automation/config/1700000000001"  # noqa: SLF001


async def test_scope_is_cleared_after_the_operation():
    client = HomeAssistantClient(None, "ws://x", "t")  # type: ignore[arg-type]
    with client.scoped(configs={"a"}, entities={"automation.a"}, metadata=True):
        assert client.scope.configs == {"a"} and client.scope.metadata
    assert not client.scope.configs and not client.scope.entities and not client.scope.metadata
    with pytest.raises(NotAllowed):
        await client.command("call_service", domain="automation", service="turn_off", target={"entity_id": "automation.a"})
