"""Phase A3.2: the App's occupancy runtime, shadow comparison, handover, fencing and rollback (App side).

The fake Home Assistant runs the integration's side of the bridge with the
shared engine (``fake_engine.py``); the integration's real side is tested in
the integration's suite against real Home Assistant.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
from typing import Any

from aiohttp import web
import pytest

from occupancy_core import CORE_VERSION, fingerprint, golden
from occupancy_core import protocol as P
from occupancy_core.wire import spec_to_wire
from smart_lighting.core import AppCore
from smart_lighting.ha import ALLOWED_COMMANDS, ENGINE_COMMANDS
from smart_lighting.occupancy_compare import TOLERANCE_SECONDS, Comparator
from smart_lighting.web import build_app

from .conftest import ADMIN_HEADERS, USER_ID, FakeHomeAssistant, make_settings, wait_for
from .fake_engine import FakeEngine

REPO = Path(__file__).resolve().parents[2]
PIR, RADAR = "binary_sensor.kamar_pir", "binary_sensor.kamar_radar"
AREA = "kamar_nordur"


def two_sensor_room(fake: FakeHomeAssistant) -> None:
    fake.rooms_options[AREA]["sensors"] = [
        {"entity_id": PIR, "kind": "pir", "role": "trigger", "inverted": False},
        # Confirm vacancy: a cleared radar is exit evidence, so a visit ends after exit_confirmation (2 s).
        {"entity_id": RADAR, "kind": "radar", "role": "confirm_vacancy", "inverted": False},
    ]
    fake.rooms_options[AREA]["timing"] = {}
    for s in fake.states:
        if s["entity_id"] in (PIR, RADAR):
            s["state"] = "off"


async def start_app(fake: FakeHomeAssistant, aiohttp_client, **env: str) -> Any:
    settings = make_settings(fake, **env)
    core = AppCore(settings)
    app = build_app(settings, core)

    async def start(_: web.Application) -> None:
        await core.start()

    async def stop(_: web.Application) -> None:
        await core.stop()

    app.on_startup.insert(0, start)
    app.on_cleanup.append(stop)
    client = await aiohttp_client(app)
    await wait_for(lambda: core.ha is not None and core.ha.connected)
    client.core = core
    return client


@pytest.fixture
async def engine_app(fake_ha: FakeHomeAssistant, aiohttp_client) -> Any:
    two_sensor_room(fake_ha)
    fake_ha.engine = FakeEngine(fake_ha)
    client = await start_app(fake_ha, aiohttp_client, SL_ENGINE_MIN_SHADOW_SECONDS="0",
                             SL_ENGINE_MIN_TRANSITIONS="2")
    await wait_for(lambda: client.core.occupancy.mode == "shadow")
    yield client
    fake_ha.engine.stop()


def runtime(client: Any):
    return client.core.occupancy


async def visit(fake: FakeHomeAssistant, rt) -> None:
    """Someone enters (PIR + radar), stays, leaves: both engines see the same events."""
    await fake.emit_live(PIR, "on")
    await fake.emit_live(RADAR, "on")
    await wait_for(lambda: (rt.results.get(AREA) or {}).get("state") == "confirmed_occupancy")
    await fake.emit_live(PIR, "off")
    await fake.emit_live(RADAR, "off")
    await wait_for(lambda: (rt.results.get(AREA) or {}).get("state") == "vacant", timeout=10)
    await wait_for(lambda: (rt.integration_results.get(AREA) or {}).get("state") == "vacant", timeout=10)


# ----------------------------------------------------------------- package
def test_the_engine_lives_only_in_the_app() -> None:
    """Phase A6: the companion integration ships no engine; the App's occupancy_core is the only copy."""
    assert not (REPO / "custom_components/smart_occupancy/occupancy_core").exists()
    copy = REPO / "smart_lighting/backend/occupancy_core"
    files = sorted(p.name for p in copy.glob("*.py"))
    assert files == ["__init__.py", "engine.py", "golden.py", "house.py", "protocol.py", "types.py", "wire.py"]
