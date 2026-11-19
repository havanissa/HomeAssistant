"""Fixtures of the companion suite (Phase A6): a real Home Assistant, the companion, and a simulated App session."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import logging
import secrets
from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar, entity_registry as er

from custom_components.smart_occupancy import protocol as P
from custom_components.smart_occupancy.const import DOMAIN

WS_LOGGER = "homeassistant.components.websocket_api.http.connection"
KAMAR = "kamar_nordur"
STOVA = "stova"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture(autouse=True)
def no_errors_logged(caplog):
    """Fail any test during which an ERROR (or worse) was logged."""
    yield
    errors = [r for r in caplog.get_records("call") + caplog.get_records("teardown")
              if r.levelno >= logging.ERROR]
    assert not errors, [r.getMessage() for r in errors]


@pytest.fixture(autouse=True)
def no_deprecated_usage_reported(caplog):
    yield
    reports = [
        r.getMessage() for r in caplog.get_records("call") + caplog.get_records("teardown")
        if "smart_occupancy" in r.getMessage()
        and ("Detected that custom integration" in r.getMessage() or "deprecated" in r.getMessage().lower())
    ]
    assert not reports, reports


def legacy_options(rooms: dict[str, Any] | None = None) -> dict[str, Any]:
    """Options as a 0.9.0 (entry 1.2) installation stores them."""
    return {
        "config_version": 2, "operating_mode": "observe",
        "mood": {"auto_mood_entity": None, "manual_mood_entity": None, "main_mood_entity": "select.smart_lighting_mood",
                 "auto_option": "Auto", "mismatch_grace_seconds": 30, "builtin": ["Dagur", "Kvøld"]},
        "categories": [{"key": "lamps", "name": "Lamps", "label_id": None, "kind": "source"}],
        "rooms": rooms if rooms is not None else {
            KAMAR: {"enabled": True, "sensors": [{"entity_id": "binary_sensor.kamar_pir", "kind": "pir",
                                                 "role": "trigger", "inverted": False}],
                    "lights": ["light.kamar_lamp"], "reference_occupancy_entity": "input_boolean.kamar_helper",
                    "mood_overrides": {}, "light_overrides": {}, "timing": {}, "adjacent_rooms": []},
        },
        "global_profiles": {},
        "occupancy": {"correlation_window": 10, "exit_confirmation": 300, "inactivity_timeout": 600,
                      "dropout_grace": 30, "trigger_stuck_after": 1800, "presence_stuck_after": 0, "startup_hold": 120},
    }


async def setup_companion(hass: HomeAssistant, options: dict[str, Any] | None = None, *, minor_version: int = 3,
                          entry: MockConfigEntry | None = None) -> MockConfigEntry:
    if entry is None:
        entry = MockConfigEntry(domain=DOMAIN, title="Smart Occupancy & Lighting", data={}, options=options or {},
                                version=1, minor_version=minor_version, unique_id=DOMAIN)
        entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


@pytest.fixture
async def areas(hass: HomeAssistant) -> dict[str, str]:
    reg = ar.async_get(hass)
    out = {}
    for area_id, name in ((KAMAR, "Kamar Norður"), (STOVA, "Stova")):
        created = reg.async_get_or_create(name)
        out[area_id] = created.id
    return out


def entity_view(value: Any, attributes: dict[str, Any] | None = None, available: bool = True) -> dict[str, Any]:
    return {"value": value, "attributes": attributes or {}, "available": available}


def system_view(rooms: dict[str, dict[str, Any]] | None = None, *, mood_select: bool = True,
                options: list[str] | None = None) -> dict[str, Any]:
    rooms = rooms if rooms is not None else {KAMAR: {"name": "Kamar Norður", "reference": "input_boolean.kamar_helper"}}
    return {
        "rooms": rooms,
        "sensors": {
            "status": entity_view("healthy", {"summary": "No problems.", "problems": [], "last_checked": "x"}),
            "operating_mode": entity_view("observe", {"configured_mode": "observe", "lighting_control": True,
                                                      "mood_lighting_rooms": list(rooms)}),
            "effective_mood": entity_view("Dagur", {"follows": "manual"}),
            "configured_rooms": entity_view(len(rooms), {"rooms": {r["name"]: "healthy" for r in rooms.values()}}),
            "light_categories": entity_view(1, {"categories": {}}),
            "discovered_sensors": entity_view(0, {"sensors": []}),
            "missing_entities": entity_view(0, {"items": [], "entities": []}),
            "unavailable_entities": entity_view(0, {"items": [], "entities": []}),
            "config_problems": entity_view(0, {"errors": 0, "warnings": 0, "problems": [], "notes": []}),
        },
        "room_observation": {a: entity_view("healthy", {"area_id": a, "usable": True, "reasons": []}) for a in rooms},
        "mood_mismatch": entity_view(False, {"main_mood": "Dagur", "expected_main_mood": "Dagur", "grace_seconds": 30}),
        "mood_select": {"enabled": mood_select, "options": options or ["Dagur", "Kvøld", "Manual"]},
    }


def occupancy_view(area_id: str = KAMAR, state: str = "confirmed_occupancy", occupied: bool = True,
                   last_activity: str = "2026-11-19T10:00:00+00:00") -> dict[str, Any]:
    attrs = {"room": "Kamar Norður", "occupied": occupied, "reason": "PIR detected motion", "reason_code": "trigger",
             "confidence": 80, "active_sensors": ["PIR"], "signals": [], "health": {"occupancy": {"status": "healthy"}},
             "observe_only": False, "lighting_control": True, "lighting_paused": False, "timing": {}, "adjacent_rooms": []}
    return {"rooms": {area_id: {
        "occupied": entity_view(occupied),
        "state": entity_view(state, attrs),
        "confidence": entity_view(80, {"score": 80, "basis": "pir", "meaning": "evidence quality"}),
        "last_activity": entity_view(last_activity, {"source": "binary_sensor.kamar_pir", "source_name": "PIR"}),
        "reason": entity_view("PIR detected motion", {"reason_code": "trigger"}),
        "helper_comparison": entity_view("agree", {"helper": "input_boolean.kamar_helper", "disagreements": 0}),
    }}}


def lighting_view(area_id: str = KAMAR, mode: str = "active") -> dict[str, Any]:
    return {"rooms": {area_id: {
        "mood_lighting": entity_view(mode, {"area_id": area_id, "control_mode": mode, "manual_overrides": {}}),
        "daylight": entity_view("dark", {"decision": "dark", "reason": "sun down", "activation": None}),
    }}, "manual_mode": False, "summary": {"active_rooms": [area_id], "overrides": 0}}


def forecast_view(area_id: str = KAMAR) -> dict[str, Any]:
    return {"rooms": {area_id: {
        "forecast": entity_view(62, {"status": "ok", "horizon_minutes": 15, "observation_only": True, "confidence": "high"}),
        "forecast_confidence": entity_view("high", {"status": "ok", "score": 90}),
        "predicted": entity_view(True, {"status": "ok", "probability": 62, "threshold": 50}),
    }}, "predicted_next_room": entity_view("Stova", {"area_id": STOVA, "probability": 40, "observation_only": True}),
        "forecast_learning": entity_view("ready", {"days_learned": 12, "observation_only": True})}


def curtains_view() -> dict[str, Any]:
    return {"windows": {"w1": {"name": "South window", "area_id": KAMAR, "mode": "automatic", "condition": "daytime",
                               "position": 100, "hold": None, "presets": [{"id": "open", "name": "Open"}]}},
            "summary": {"automatic": 1, "moving": 0}}


class FakeApp:
    """The App's side of the companion protocol over one real websocket connection."""

    def __init__(self, hass: HomeAssistant, client: Any, instance: str | None = None) -> None:
        self.hass = hass
        self.client = client
        self.instance = instance or secrets.token_hex(16)
        self._id = 100
        self.sub_id: int | None = None
        self.events: list[dict[str, Any]] = []
        self.session: str | None = None
        self.hello: dict[str, Any] | None = None
        self.pairing_token: str | None = None
        self.grants: dict[str, dict[str, Any]] = {}
        self.seq: dict[str, int] = {}

    async def send(self, **msg: Any) -> dict[str, Any]:
        self._id += 1
        await self.client.send_json({"id": self._id, **msg})
        while True:
            reply = await self.client.receive_json()
            if reply.get("type") == "event" and reply.get("id") == self.sub_id:
                self.events.append(reply["event"])
                continue
            if reply.get("id") == self._id:
                return reply

    async def drain(self) -> list[dict[str, Any]]:
        await self.hass.async_block_till_done()
        await self.send(type="ping")
        return self.events

    async def event(self, kind: str, timeout: float = 5.0) -> dict[str, Any]:
        """The next event of this kind on the session (without waiting for Home Assistant's tasks)."""
        for event in self.events:
            if event.get("type") == kind and not event.get("_seen"):
                event["_seen"] = True
                return event
        async with asyncio.timeout(timeout):
            while True:
                reply = await self.client.receive_json()
                if reply.get("type") == "event" and reply.get("id") == self.sub_id:
                    self.events.append(reply["event"])
                    if reply["event"].get("type") == kind:
                        reply["event"]["_seen"] = True
                        return reply["event"]

    async def subscribe(self, token: str | None = "keep") -> dict[str, Any]:
        self._id += 1
        self.sub_id = self._id
        params: dict[str, Any] = {"id": self._id, "type": "smart_occupancy/companion/subscribe",
                                  "instance_id": self.instance, "protocol": P.PROTOCOL_VERSION, "app_version": "0.7.0"}
        if token == "keep":
            token = self.pairing_token
        if token:
            params["pairing_token"] = token
        await self.client.send_json(params)
        reply = await self.client.receive_json()
        assert reply["success"], reply
        hello = await self.client.receive_json()
        assert hello["type"] == "event" and hello["event"]["type"] == "hello", hello
        self.hello = hello["event"]
        self.session = self.hello["session_id"]
        return self.hello

    async def pair(self) -> str:
        reply = await self.send(type="smart_occupancy/companion/pair", session_id=self.session, requested_by="admin")
        assert reply["success"], reply
        self.pairing_token = reply["result"]["pairing_token"]
        return self.pairing_token

    async def claim(self, domain: str, *, targets: dict[str, Any] | None = None, expected_epoch: int | None = None,
                    config_revision: str = "abc123") -> dict[str, Any]:
        if expected_epoch is None:
            ownership = (await self.send(type="smart_occupancy/companion/status"))["result"]["ownership"]
            expected_epoch = ownership[domain]["epoch"]
        if targets is None and domain in P.COMMAND_DOMAINS:
            targets = {"lights": ["light.kamar_lamp"], "relays": [], "actions": []} if domain == "lighting" else \
                {"covers": ["cover.kamar"]}
        reply = await self.send(type="smart_occupancy/companion/claim", session_id=self.session, domain=domain,
                                expected_epoch=expected_epoch, config_revision=config_revision, targets=targets,
                                requested_by="admin")
        if reply["success"]:
            self.grants[domain] = reply["result"]
            self.seq[domain] = 0
        return reply

    def owner_params(self, domain: str) -> dict[str, Any]:
        self.seq[domain] = self.seq.get(domain, 0) + 1
        grant = self.grants[domain]
        return {"session_id": self.session, "domain": domain, "epoch": grant["epoch"],
                "lease_token": grant["lease_token"], "seq": self.seq[domain]}

    async def owner(self, command: str, domain: str, **params: Any) -> dict[str, Any]:
        return await self.send(type=f"smart_occupancy/companion/{command}", **self.owner_params(domain), **params)

    async def publish(self, domain: str, view: dict[str, Any], **extra: Any) -> dict[str, Any]:
        if domain == "system":
            return await self.send(type="smart_occupancy/companion/publish", session_id=self.session, domain="system",
                                   view=view)
        return await self.owner("publish", domain, view=view, **extra)

    async def execute(self, domain: str, service_domain: str, service: str, data: dict[str, Any],
                      context_id: str | None = None, **extra: Any) -> dict[str, Any]:
        return await self.owner("execute", domain, service_domain=service_domain, service=service, data=data,
                                context_id=context_id or "01KV5B0000000000000000ABCD", **extra)

    async def heartbeat(self, domain: str) -> dict[str, Any]:
        return await self.owner("heartbeat", domain)

    async def start(self, *domains: str, pair: bool = True) -> dict[str, Any]:
        """Subscribe, pair and claim: the normal start of a paired App."""
        await self.subscribe()
        if pair and not self.pairing_token:
            await self.pair()
        for domain in domains:
            reply = await self.claim(domain)
            assert reply["success"], reply
        return self.hello


async def admin_ws(hass: HomeAssistant, hass_ws_client) -> Any:
    return await hass_ws_client(hass)


async def user_ws(hass: HomeAssistant, hass_ws_client, hass_read_only_user) -> Any:
    refresh = await hass.auth.async_create_refresh_token(hass_read_only_user, "https://example.com/app")
    return await hass_ws_client(hass, access_token=hass.auth.async_create_access_token(refresh))


async def tick(hass: HomeAssistant, freezer, seconds: float, step: float = 5.0) -> None:
    remaining = seconds
    while remaining > 0:
        delta = min(step, remaining)
        freezer.tick(timedelta(seconds=delta))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
        remaining -= delta


def entity_id(hass: HomeAssistant, platform: str, unique_id: str) -> str | None:
    return er.async_get(hass).async_get_entity_id(platform, DOMAIN, unique_id)


def observation(hass: HomeAssistant, area_id: str):
    """The room observation sensor (no suggested entity id, as in 0.9.0: found by its unique id)."""
    eid = entity_id(hass, "sensor", f"{DOMAIN}_room_{area_id}_observation")
    return hass.states.get(eid) if eid else None
