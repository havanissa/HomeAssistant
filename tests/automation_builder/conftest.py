"""Fixtures of the Automation Builder runtime suite: the compiler's output loaded into a real Home Assistant.

The generated configuration goes through Home Assistant's own automation parser and runs in its automation
engine; the tests then change states and time and look at the service calls that result.

Run in its own process: ``python scripts/run_tests.py tests/automation_builder``.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
import logging
from pathlib import Path
import sys
from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

APP = Path(__file__).resolve().parents[2] / "automation_builder"
for path in (APP / "backend", APP / "tests"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from ab import compiler  # noqa: E402
import builders as B  # noqa: E402


@pytest.fixture(autouse=True)
def no_errors_logged(caplog):
    yield
    errors = [r for r in caplog.get_records("call") + caplog.get_records("teardown") if r.levelno >= logging.ERROR]
    assert not errors, [r.getMessage() for r in errors]


class House:
    """A Home Assistant with the automations of the given devices, and recorded service calls.

    While ``apply`` is true a recorded call changes the entity's state, like a working device would
    (``apply = False`` simulates a command that is lost).
    """

    SERVICES = {"light": ("turn_on", "turn_off", "toggle"), "switch": ("turn_on", "turn_off", "toggle"),
                "cover": ("open_cover", "close_cover"), "climate": ("set_temperature", "set_hvac_mode"),
                "input_select": ("select_option",)}
    RESULT = {"turn_on": "on", "turn_off": "off", "open_cover": "open", "close_cover": "closed"}

    def __init__(self, hass: HomeAssistant, freezer) -> None:
        self.hass = hass
        self.freezer = freezer
        self.calls: list[ServiceCall] = []
        self.apply = True

    async def load(self, *devices: dict[str, Any], settings: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        automations = []
        for device in devices:
            automations += compiler.compile_device(device, settings or B.settings())["automations"]
        for domain, services in self.SERVICES.items():
            for service in services:
                self.hass.services.async_register(domain, service, self._record)
        assert await async_setup_component(self.hass, "automation", {
            "automation": [compiler.with_id(a) for a in automations]})
        await self.hass.async_block_till_done()
        for automation in automations:
            entity = self.entity_of(automation["id"])
            assert entity is not None, f"{automation['id']} was not loaded"
            assert self.hass.states.get(entity).state == "on", self.hass.states.get(entity)
        return automations

    async def _record(self, call: ServiceCall) -> None:
        self.calls.append(call)
        if not self.apply:
            return
        entity_ids = call.data.get("entity_id") or []
        for entity_id in [entity_ids] if isinstance(entity_ids, str) else entity_ids:
            current = self.hass.states.get(entity_id)
            if call.service == "toggle":
                new = "off" if current and current.state == "on" else "on"
            else:
                new = self.RESULT.get(call.service) or call.data.get("hvac_mode") or call.data.get("option")
            if new:
                self.hass.states.async_set(entity_id, new, current.attributes if current else None, context=call.context)

    def entity_of(self, automation_id: str) -> str | None:
        for state in self.hass.states.async_all("automation"):
            if state.attributes.get("id") == automation_id:
                return state.entity_id
        return None

    def set(self, entity_id: str, state: str, **attributes: Any) -> None:
        self.hass.states.async_set(entity_id, state, attributes)

    async def settle(self) -> None:
        """Let Home Assistant work off what is due. Not ``async_block_till_done``: that waits for a running
        automation, and a run inside a ``delay`` never ends while the clock is frozen."""
        for _ in range(60):
            await asyncio.sleep(0)

    async def advance(self, seconds: float, step: float = 1.0) -> None:
        """Move time forward in steps (timers of ``for:`` triggers and delays fire on the way)."""
        remaining = seconds
        while remaining > 0:
            delta = min(step, remaining)
            self.freezer.tick(timedelta(seconds=delta))
            async_fire_time_changed(self.hass, dt_util.utcnow())
            await self.settle()
            remaining -= delta

    def taken(self) -> list[tuple[str, tuple[str, ...], dict[str, Any]]]:
        """The calls so far as ``(action, entities, data)``, emptied."""
        out = []
        for call in self.calls:
            data = dict(call.data)
            entities = data.pop("entity_id", [])
            out.append((f"{call.domain}.{call.service}", tuple([entities] if isinstance(entities, str) else entities), data))
        self.calls.clear()
        return out


@pytest.fixture
async def house(hass: HomeAssistant, freezer):
    yield House(hass, freezer)
    entities = [s.entity_id for s in hass.states.async_all("automation")]
    if entities:  # detach every trigger, so no timer outlives the test
        await hass.services.async_call("automation", "turn_off", {"entity_id": entities}, blocking=True)
        await hass.async_block_till_done()
