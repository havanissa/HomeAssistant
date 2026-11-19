"""Phase A4 / A6: the App's engine modules and the host interface they run on.

* The engine modules are the App's own since Phase A6 (the companion integration ships none of them).
* Every Home Assistant name they import exists in the host interface (``backend/homeassistant``).
* The host behaves like Home Assistant where the runtime depends on it: listener order and nested events, eager
  tasks, timers (and their recheck), state objects from the websocket, contexts, the store format and migrations,
  registries, MQTT and trigger subscriptions through the host backend, and the references of automations.
"""

from __future__ import annotations

import ast
import asyncio
from datetime import timedelta
import importlib
import json
from pathlib import Path
import subprocess
import sys
from typing import Any

import pytest

from homeassistant.core import Context, Event, HomeAssistant, HostBackend, State, callback, ulid_now
from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er, label_registry as lr
from homeassistant.helpers import _registry
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from smart_lighting.lighting_scripts import AutomationEntity, ScriptRefs

APP = Path(__file__).resolve().parents[1]
REPO = APP.parent
VENDORED = APP / "backend/smart_occupancy"


def _manifest() -> list[str]:
    tree = ast.parse((VENDORED / "runtime_manifest.py").read_text())
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", None) == "MODULES":
            return list(ast.literal_eval(node.value))
    raise AssertionError("MODULES not found")


def test_the_engine_modules_are_the_apps_only_copy() -> None:
    """Phase A6: the companion integration ships none of the engine modules; the manifests list the App's."""
    from smart_occupancy.app_manifest import ALL_MODULES, APP_MODULES, FINGERPRINT_0_9_0  # noqa: PLC0415
    from smart_occupancy.runtime_manifest import MODULES, fingerprint  # noqa: PLC0415

    assert list(MODULES) == _manifest()
    # App 0.8.0 changed the 0.9.0 runtime on purpose (the migration wizard refuses those handovers from 0.9.0).
    assert fingerprint() != FINGERPRINT_0_9_0 and len(FINGERPRINT_0_9_0) == 64
    assert set(APP_MODULES).isdisjoint(MODULES) and all((VENDORED / n).is_file() for n in ALL_MODULES)
    present = {p.name for p in VENDORED.glob("*.py")} | {"curtains/" + p.name for p in (VENDORED / "curtains").glob("*.py")}
    assert present - {"__init__.py", "curtains/__init__.py"} <= set(ALL_MODULES) | {m for m in MODULES}, present - set(ALL_MODULES)
    companion = REPO / "custom_components/smart_occupancy"
    # The companion has a ``const.py`` of its own (its version, store keys and option names); no engine module.
    shared = {"const.py"}
    assert not [name for name in MODULES if name not in shared and (companion / name).exists()]
    assert "engine" not in (companion / "const.py").read_text(encoding="utf-8").lower().split("import", 1)[0]


def test_every_imported_home_assistant_name_exists() -> None:
    from smart_occupancy.app_manifest import ALL_MODULES  # noqa: PLC0415

    missing = []
    for name in ALL_MODULES:
        tree = ast.parse((VENDORED / name).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("homeassistant"):
                module = importlib.import_module(node.module)
                for alias in node.names:
                    if not hasattr(module, alias.name):
                        try:
                            importlib.import_module(f"{node.module}.{alias.name}")
                        except ImportError:
                            missing.append(f"{name}: {node.module}.{alias.name}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("homeassistant"):
                        importlib.import_module(alias.name)
    assert not missing, missing


def test_the_runtime_imports_without_home_assistant() -> None:
    result = subprocess.run(
        [sys.executable, "-c", "import sys; sys.path.insert(0, 'backend'); import smart_occupancy.controller, "
         "smart_occupancy.lighting_websocket; import homeassistant; print(homeassistant.__file__)"],
        capture_output=True, text=True, cwd=APP, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().startswith(str(APP / "backend/homeassistant"))


# ------------------------------------------------------------------ semantics
async def test_listeners_in_order_and_nested_events_after_the_current_one() -> None:
    hass = HomeAssistant(HostBackend())
    seen: list[str] = []

    @callback
    def first(event: Event) -> None:
        seen.append(f"first:{event.event_type}")
        if event.event_type == "a":
            hass.bus.async_fire_internal(Event("b"))
            seen.append("first:after_fire")

    @callback
    def second(event: Event) -> None:
        seen.append(f"second:{event.event_type}")

    hass.bus.async_listen("*", first)
    hass.bus.async_listen("*", second)
    hass.bus.async_fire_internal(Event("a"))
    assert seen == ["first:a", "first:after_fire", "second:a", "first:b", "second:b"]


async def test_tasks_start_eagerly() -> None:
    hass = HomeAssistant(HostBackend())
    started = []

    async def job() -> None:
        started.append(True)
        await asyncio.sleep(0)

    hass.async_create_task(job())
    assert started == [True]  # ran up to its first suspension already
    await hass.async_cancel_tasks()


async def test_state_tracking_and_states_from_the_websocket() -> None:
    hass = HomeAssistant(HostBackend())
    events = []
    async_track_state_change_event(hass, ["light.lamp"], callback(lambda e: events.append(e)))
    wire = {"entity_id": "light.lamp", "state": "on", "attributes": {"brightness": 80},
            "last_changed": "2026-06-15T10:00:00+00:00", "last_updated": "2026-06-15T10:00:01.5+00:00",
            "context": {"id": "01KV5B0000000000000000ABCD", "parent_id": None, "user_id": "u1"}}
    hass.async_state_changed("light.lamp", State.from_wire(wire))
    hass.async_state_changed("light.other", State.from_wire({**wire, "entity_id": "light.other"}))
    assert len(events) == 1
    event = events[0]
    assert event.data["old_state"] is None and event.data["new_state"].attributes["brightness"] == 80
    assert event.context.id == "01KV5B0000000000000000ABCD" and event.context.user_id == "u1"
    assert event.data["new_state"].last_updated.microsecond == 500000
    assert hass.states.get("LIGHT.LAMP").state == "on" and hass.states.is_state("light.lamp", "on")
    assert [s.entity_id for s in hass.states.async_all("light")] == ["light.lamp", "light.other"]


async def test_timers_count_from_when_the_event_fired_in_home_assistant() -> None:
    """A debounce started by an event that reached the App late ends when it would have in Home Assistant."""
    hass = HomeAssistant(HostBackend())
    fired: list[Any] = []
    loop = asyncio.get_running_loop()
    async_track_state_change_event(hass, ["light.lamp"], callback(
        lambda e: async_call_later(hass, 0.5, callback(lambda now: fired.append(loop.time())))))
    for lag, entity in ((0.4, "light.lamp"), (30.0, "light.lamp")):
        start = loop.time()
        fired.clear()
        then = (dt_util.utcnow() - timedelta(seconds=lag)).isoformat()
        hass.async_state_changed(entity, State.from_wire({
            "entity_id": entity, "state": "on" if lag < 1 else "off", "attributes": {}, "last_changed": then,
            "last_updated": then, "context": {"id": ulid_now(), "parent_id": None, "user_id": None}}))
        assert hass.event_time is None  # only while the event is dispatched
        await asyncio.sleep(0.6)
        assert len(fired) == 1
        # 0.4 s late: fires after about 0.1 s; a clock that disagrees by 30 s: counts from now (0.5 s).
        assert (fired[0] - start < 0.3) if lag < 1 else (fired[0] - start >= 0.45)


def test_contexts_are_ulids() -> None:
    ids = {Context().id for _ in range(50)} | {ulid_now()}
    assert len(ids) == 51 and all(len(i) == 26 and i.isupper() for i in ids)


async def test_timers_fire_and_recheck_the_clock() -> None:
    hass = HomeAssistant(HostBackend())
    fired = []
    cancel = async_call_later(hass, 0.05, callback(lambda now: fired.append(now)))
    async_call_later(hass, 0.05, callback(lambda now: fired.append("second")))()
    await asyncio.sleep(0.12)
    assert len(fired) == 1 and fired[0] <= dt_util.utcnow()
    cancel()


async def test_store_format_delay_and_migration(tmp_path: Path) -> None:
    saved: list[str] = []

    class Backend(HostBackend):
        def store_saved(self, key: str, data: Any) -> None:
            saved.append(key)

    hass = HomeAssistant(Backend())
    hass.storage_dir = str(tmp_path)
    store: Store[dict[str, Any]] = Store(hass, 1, "smart_occupancy.lighting", minor_version=9)
    await store.async_save({"a": 1})
    raw = json.loads((tmp_path / "smart_occupancy.lighting").read_text())
    assert raw == {"version": 1, "minor_version": 9, "key": "smart_occupancy.lighting", "data": {"a": 1}}
    store.async_delay_save(lambda: {"a": 2}, 0.01)
    assert await store.async_load() == {"a": 2}  # unsaved data is the newest
    await asyncio.sleep(0.05)
    assert json.loads((tmp_path / "smart_occupancy.lighting").read_text())["data"] == {"a": 2}
    assert saved == ["smart_occupancy.lighting", "smart_occupancy.lighting"]

    class Migrating(Store):
        async def _async_migrate_func(self, major, minor, data):
            return {**data, "migrated_from": [major, minor]}

    newer = Migrating(hass, 1, "smart_occupancy.lighting", minor_version=10)
    assert await newer.async_load() == {"a": 2, "migrated_from": [1, 9]}
    memory = HomeAssistant(HostBackend())  # no storage folder: memory only (the shadow)
    mem_store: Store[dict[str, Any]] = Store(memory, 1, "k")
    await mem_store.async_save({"x": 1})
    assert await mem_store.async_load() == {"x": 1} and not list(tmp_path.glob("k"))


def test_registries_from_the_websocket_lists() -> None:
    hass = HomeAssistant.__new__(HomeAssistant)
    hass.data = {}
    _registry.install(hass, entities=[{"entity_id": "light.lamp", "device_id": "d1", "platform": "hue",
                                       "labels": ["lamps"], "area_id": None}],
                      devices=[{"id": "d1", "area_id": "kamar", "identifiers": [["hue", "x"]],
                                "config_entries": ["e1"], "name": "Lamp"}],
                      areas=[{"area_id": "kamar", "name": "Kamar Norður", "floor_id": None}],
                      labels=[{"label_id": "lamps", "name": "Lamps"}])
    entry = er.async_get(hass).async_get("light.lamp")
    assert entry.domain == "light" and entry.labels == {"lamps"} and entry.device_class is None
    device = dr.async_get(hass).async_get("d1")
    assert device.area_id == "kamar" and ("hue", "x") in device.identifiers
    assert [d.id for d in dr.async_entries_for_config_entry(dr.async_get(hass), "e1")] == ["d1"]
    assert er.async_entries_for_device(er.async_get(hass), "d1")[0].entity_id == "light.lamp"
    assert ar.async_get(hass).async_get_area_by_name("kamar  norður").area_id == "kamar"
    assert lr.async_get(hass).async_get_label("lamps").name == "Lamps"


async def test_mqtt_and_triggers_go_through_the_host_backend() -> None:
    from homeassistant.components import mqtt  # noqa: PLC0415
    from homeassistant.config_entries import ConfigEntry  # noqa: PLC0415
    from homeassistant.helpers.trigger import async_initialize_triggers, async_validate_trigger_config  # noqa: PLC0415

    calls: list[Any] = []

    class Backend(HostBackend):
        async def async_mqtt_subscribe(self, topic, handler):
            calls.append(("mqtt", topic))
            handler({"topic": "zigbee2mqtt/switch", "payload": '{"action": "on"}', "retain": False})
            return lambda: calls.append("unsub")

        async def async_validate_triggers(self, configs):
            return configs

        async def async_subscribe_triggers(self, configs, action):
            await action({"trigger": {"id": "on_left"}}, None)
            return lambda: None

    hass = HomeAssistant(Backend())
    received = []
    with pytest.raises(Exception, match="not set up"):
        await mqtt.async_subscribe(hass, "zigbee2mqtt/#", received.append)
    hass.config.components.add("mqtt")
    hass.config_entries.add(ConfigEntry("m1", "mqtt"))
    unsub = await mqtt.async_subscribe(hass, "zigbee2mqtt/#", callback(lambda m: received.append(m)), encoding=None)
    assert received[0].payload == b'{"action": "on"}' and received[0].topic == "zigbee2mqtt/switch"
    unsub()
    fired = []

    async def action(variables, context=None):
        fired.append(variables["trigger"]["id"])

    configs = await async_validate_trigger_config(hass, [{"platform": "device", "domain": "mqtt"}])
    await async_initialize_triggers(hass, configs, action, "smart_occupancy", "switch", lambda *a, **k: None)
    assert fired == ["on_left"] and calls == [("mqtt", "zigbee2mqtt/#"), "unsub"]


def test_script_references_like_home_assistant() -> None:
    raw = {"actions": [
        {"action": "light.turn_on", "target": {"entity_id": ["light.a", "light.b"], "area_id": "kamar",
                                               "device_id": "d1", "label_id": "lamps", "floor_id": "ground"}},
        {"service": "light.turn_off", "entity_id": "light.c"},
        {"action": "light.turn_on", "data": {"entity_id": "{{ states('x') }}"}},
        {"scene": "scene.evening"},
        {"choose": [{"conditions": [{"condition": "state", "entity_id": "input_boolean.x", "state": "on"}],
                     "sequence": [{"action": "switch.turn_on", "target": {"entity_id": "switch.relay"}}]}],
         "default": [{"action": "light.toggle", "entity_id": "light.d"}]},
        {"if": [{"condition": "and", "conditions": [{"condition": "state", "entity_id": "sensor.y", "state": "1"}]}],
         "then": [{"device_id": "d2", "domain": "light", "type": "turn_on"}]},
        {"parallel": [{"sequence": [{"action": "light.turn_on", "target": {"entity_id": "light.e"}}]},
                      {"action": "light.turn_on", "target": {"entity_id": "light.f"}}]},
    ]}
    refs = AutomationEntity("automation.x", raw).action_script
    assert refs.referenced_entities == {"light.a", "light.b", "light.c", "scene.evening", "input_boolean.x",
                                        "switch.relay", "light.d", "sensor.y", "light.e", "light.f"}
    assert refs.referenced_devices == {"d1", "d2"} and refs.referenced_areas == {"kamar"}
    assert refs.referenced_labels == {"lamps"} and refs.referenced_floors == {"ground"}
    assert ScriptRefs.of(None).referenced_entities == set()


def test_time_helpers() -> None:
    assert dt_util.parse_datetime("2026-06-15T10:00:00Z") == dt_util.parse_datetime("2026-06-15T10:00:00+00:00")
    assert dt_util.parse_datetime("not a time") is None
    assert dt_util.as_utc(dt_util.utcnow() + timedelta(hours=1)) > dt_util.utcnow()
