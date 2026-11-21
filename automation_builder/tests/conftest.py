"""Fixtures of the App tests: a fake Home Assistant (websocket client and automation configuration endpoint)."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
import re
from typing import Any

import pytest

from ab import compiler
from ab.core import AppCore
from ab.ha import ALLOWED_COMMANDS, DeployError, HomeAssistantError, NotAllowed
from ab.settings import load_settings

ADMIN = "user-admin"
GUEST = "user-guest"


def state(entity_id: str, value: str, **attributes: Any) -> dict[str, Any]:
    return {"entity_id": entity_id, "state": value, "attributes": attributes,
            "last_changed": "2026-11-21T10:00:00+00:00", "last_updated": "2026-11-21T10:00:00+00:00"}


class FakeHome:
    """Home Assistant as the App sees it: states, registries, the websocket commands and automations.yaml."""

    def __init__(self) -> None:
        self.connected = True
        self.states: dict[str, dict[str, Any]] = {}
        self.areas = [{"area_id": "kamar", "name": "Kamar"}, {"area_id": "stova", "name": "Stova"}]
        self.devices = [
            {"id": "dev-lamp", "name": "Bed lamp", "area_id": "kamar", "manufacturer": "Acme", "model": "L1"},
            {"id": "dev-relay", "name": "Dual relay", "area_id": "stova", "manufacturer": "Acme", "model": "R2"},
        ]
        self.entity_registry: list[dict[str, Any]] = []
        self.commands_sent: Counter[str] = Counter()
        self.file: dict[str, dict[str, Any]] = {}    # automations.yaml, by id (each with its "id")
        self.loaded: dict[str, dict[str, Any]] = {}  # what the automation entities run
        self.requests: Counter[str] = Counter()
        self.reject: str | None = None      # a POST is refused with this message
        self.loads = True                   # a saved automation is loaded (its entity appears)
        self.unreachable = False
        self.invalid: dict[str, str] = {}   # validate_config errors by key
        self.related: dict[str, list[str]] = {}
        for entity_id, value, device, attributes in (
            ("light.bed_lamp", "off", "dev-lamp", {"friendly_name": "Bed lamp", "supported_color_modes": ["color_temp"]}),
            ("switch.relay_1", "off", "dev-relay", {"friendly_name": "Relay 1"}),
            ("switch.relay_2", "off", "dev-relay", {"friendly_name": "Relay 2"}),
            ("cover.blind", "open", None, {"friendly_name": "Blind"}),
            ("climate.heater", "heat", None, {"friendly_name": "Heater", "hvac_modes": ["off", "heat"]}),
            ("binary_sensor.motion", "off", None, {"friendly_name": "Motion", "device_class": "motion"}),
            ("binary_sensor.sod_kamar_occupied", "off", None, {"friendly_name": "Kamar occupied",
                                                                "device_class": "occupancy", "fresh": True}),
            ("sensor.lux", "250", None, {"friendly_name": "Lux", "unit_of_measurement": "lx"}),
            ("input_select.mood", "Dagur", None, {"friendly_name": "Mood", "options": ["Dagur", "Hygge", "Nátt"]}),
            ("input_boolean.manual", "off", None, {"friendly_name": "Manual"}),
        ):
            self.states[entity_id] = state(entity_id, value, **attributes)
            self.entity_registry.append({"entity_id": entity_id, "device_id": device, "area_id": None})

    # ------------------------------------------------------------ websocket
    def status(self) -> dict[str, Any]:
        return {"state": "connected" if self.connected else "disconnected", "connected": self.connected,
                "ha_version": "2026.11.1", "last_error": None}

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def command(self, kind: str, **params: Any) -> Any:
        if kind not in ALLOWED_COMMANDS:
            raise NotAllowed(kind)
        if not self.connected:
            raise ConnectionError("not connected")
        self.commands_sent[kind] += 1
        if kind == "config/auth/list":
            return [{"id": ADMIN, "is_active": True, "is_owner": True, "group_ids": ["system-admin"]},
                    {"id": GUEST, "is_active": True, "is_owner": False, "group_ids": ["system-users"]}]
        if kind == "validate_config":
            return {key: {"valid": key not in self.invalid, "error": self.invalid.get(key)}
                    for key in ("triggers", "conditions", "actions") if key in params}
        if kind == "automation/config":
            for automation_id, config in self.loaded.items():
                if self.entity_of(automation_id) == params["entity_id"]:
                    return {"config": config}
            raise HomeAssistantError("not_found", "Entity not found")
        if kind == "search/related":
            return {"automation": self.related.get(params["item_id"], [])}
        if kind == "trace/list":
            return [{"run_id": "1", "timestamp": {"start": "2026-11-21T10:05:00+00:00"}, "state": "stopped",
                     "script_execution": "finished", "trigger": "state of binary_sensor.motion"}]
        raise AssertionError(f"unexpected command {kind}")

    # ------------------------------------------------- automation endpoint
    def entity_of(self, automation_id: str) -> str | None:
        for entity_id, value in self.states.items():
            if entity_id.startswith("automation.") and value["attributes"].get("id") == automation_id:
                return entity_id
        return None

    def _load(self, automation_id: str, config: dict[str, Any] | None) -> None:
        old = self.entity_of(automation_id)
        if config is None:
            self.loaded.pop(automation_id, None)
            if old:
                del self.states[old]
            return
        if not self.loads:
            return
        self.loaded[automation_id] = config
        entity_id = old or "automation." + re.sub(r"[^a-z0-9]+", "_", config["alias"].lower()).strip("_")
        previous = self.states.get(entity_id, {}).get("state", "on")
        self.states[entity_id] = state(entity_id, previous, id=automation_id, friendly_name=config["alias"],
                                       last_triggered=None)

    def _check(self, automation_id: str, method: str) -> None:
        assert compiler.is_managed_id(automation_id), automation_id
        self.requests[method] += 1
        if self.unreachable:
            raise DeployError("Home Assistant could not be reached (ClientConnectorError)")

    async def get(self, automation_id: str) -> dict[str, Any] | None:
        self._check(automation_id, "GET")
        return self.file.get(automation_id)

    async def put(self, automation_id: str, config: dict[str, Any]) -> None:
        self._check(automation_id, "POST")
        if self.reject:
            raise DeployError(f"Home Assistant did not accept the automation: {self.reject}", 400)
        assert "id" not in config
        self.file[automation_id] = {"id": automation_id, **config}
        self._load(automation_id, self.file[automation_id])

    async def delete(self, automation_id: str) -> None:
        self._check(automation_id, "DELETE")
        self.file.pop(automation_id, None)
        self._load(automation_id, None)

    def edit_outside(self, automation_id: str, **changes: Any) -> None:
        """Somebody edits the automation in Home Assistant's own editor."""
        self.file[automation_id] = {**self.file[automation_id], **changes}
        self._load(automation_id, self.file[automation_id])

    def delete_outside(self, automation_id: str) -> None:
        del self.file[automation_id]
        self._load(automation_id, None)


@pytest.fixture
def home() -> FakeHome:
    return FakeHome()


def make_core(tmp_path: Path, home: FakeHome) -> AppCore:
    settings = load_settings({"AB_DATA_DIR": str(tmp_path), "AB_ALLOWED_CLIENTS": "*", "AB_OPTIONS": str(tmp_path / "o.json"),
                              "SUPERVISOR_TOKEN": "secret-token"})
    core = AppCore(settings)
    core.ha = home  # type: ignore[assignment]
    core.api = home  # type: ignore[assignment]
    core.CONFIRM_TIMEOUT = 0.3
    core.CONFIRM_INTERVAL = 0.02
    return core


@pytest.fixture
def core(tmp_path: Path, home: FakeHome) -> AppCore:
    return make_core(tmp_path, home)
