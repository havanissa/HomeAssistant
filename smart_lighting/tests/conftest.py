"""Test fixtures: a fake Home Assistant websocket API and Supervisor.

The fake records every command the App sends, so tests can prove that only
read-only commands are used.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import copy
import hashlib
import json
from pathlib import Path
import sys
from typing import Any

from aiohttp import WSMsgType, web
import pytest

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR / "backend"))

from smart_lighting.core import AppCore  # noqa: E402
from smart_lighting.settings import load_settings  # noqa: E402
from smart_lighting.web import build_app  # noqa: E402

TOKEN = "supervisor-token-abcdef0123456789"
HA_VERSION = "2026.11.1"
ADMIN_ID = "user-admin-1"
USER_ID = "user-plain-2"
ADMIN_HEADERS = {"X-Remote-User-Id": ADMIN_ID, "X-Smart-Lighting": "1"}


def revision_of(value: Any) -> str:
    """Same digest as the integration's rooms_api.revision_of."""
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def state(entity_id: str, value: str, name: str, unit: str | None = None) -> dict[str, Any]:
    attributes: dict[str, Any] = {"friendly_name": name}
    if unit:
        attributes["unit_of_measurement"] = unit
    return {
        "entity_id": entity_id,
        "state": value,
        "attributes": attributes,
        "last_changed": "2026-11-17T10:00:00+00:00",
        "last_updated": "2026-11-17T10:00:00+00:00",
        "context": {"id": "x", "parent_id": None, "user_id": None},
    }


class FakeHomeAssistant:
    """Speaks enough of the Home Assistant websocket protocol for the App."""

    def __init__(self) -> None:
        self.token = TOKEN
        self.commands: list[dict[str, Any]] = []
        self.connections: list[web.WebSocketResponse] = []
        self.subscriptions: dict[web.WebSocketResponse, list[int]] = {}
        self.auth_attempts = 0
        self.integration_installed = True
        self.states = [
            state("binary_sensor.kamar_nordur_occupancy", "off", "Kamar Norður occupancy"),
            state("sensor.kamar_nordur_status", "vacant", "Kamar Norður status"),
            state("light.kamar_nordur_loft", "on", "Loft ljós"),
            state("sensor.outdoor_lux", "1200", "Outdoor lux", "lx"),
        ]
        self.states += [
            {**state("binary_sensor.kamar_pir", "off", "PIR Kamar"), "attributes": {"friendly_name": "PIR Kamar",
                                                                                   "device_class": "motion"}},
            {**state("binary_sensor.kamar_radar", "on", "Radar Kamar"),
             "attributes": {"friendly_name": "Radar Kamar", "device_class": "occupancy"}},
            {**state("binary_sensor.stova_door", "unavailable", "Door Stova"),
             "attributes": {"friendly_name": "Door Stova", "device_class": "door"}},
            {**state("light.kamar_lamp", "off", "Lamp Kamar"),
             "attributes": {"friendly_name": "Lamp Kamar", "supported_color_modes": ["color_temp"]}},
            {**state("cover.kamar_curtain", "open", "Curtain Kamar"),
             "attributes": {"friendly_name": "Curtain Kamar", "supported_features": 15}},
        ]
        self.supervisor_requests: list[str] = []
        # The integration's room API (0.6.9), simplified: options, revisions, confirmation, restore.
        self.rooms_api = True
        self.loaded = True
        self.require_confirmation: list[str] = []
        self.drop_on: str | None = None  # close the connection instead of answering this command
        self.rooms_options: dict[str, Any] = {
            "kamar_nordur": {"enabled": True, "sensors": [
                {"entity_id": "binary_sensor.kamar_radar", "kind": "radar", "role": "maintain", "inverted": False}],
                "lights": ["light.kamar_nordur_loft"], "reference_occupancy_entity": None, "mood_overrides": {},
                "light_overrides": {}, "timing": {"inactivity_timeout": 600}, "adjacent_rooms": []},
        }
        self.users = [
            {"id": ADMIN_ID, "name": "Admin", "is_owner": True, "is_active": True, "system_generated": False,
             "group_ids": ["system-admin"]},
            {"id": USER_ID, "name": "Guest", "is_owner": False, "is_active": True, "system_generated": False,
             "group_ids": ["system-users"]},
        ]
        self.entry_subscriptions: dict[web.WebSocketResponse, list[int]] = {}
        # The integration's presence API (0.6.10), simplified; the Recorder history.
        self.presence_api = True
        self.global_timing: dict[str, int] = {"correlation_window": 10, "exit_confirmation": 300,
                                              "inactivity_timeout": 600, "dropout_grace": 30,
                                              "trigger_stuck_after": 1800, "presence_stuck_after": 0,
                                              "startup_hold": 120}
        self.history_components = True
        self.history_calls: list[dict[str, Any]] = []
        self.history: dict[str, list[dict[str, Any]]] = {
            "sensor.kamar_nordur_status": [{"s": "vacant", "lu": 1794900000.0},
                                           {"s": "possible_occupancy", "lu": 1794900060.0},
                                           {"s": "confirmed_occupancy", "lu": 1794900070.0}],
            "binary_sensor.kamar_radar": [{"s": "off", "lu": 1794900000.0}, {"s": "on", "lu": 1794900061.5},
                                          {"s": "on", "lu": 1794900062.0}],
        }
        self.floors = [{"floor_id": "ground", "name": "Ground floor", "level": 0, "icon": None}]
        # Phase A3.2: the integration's occupancy bridge (tests/fake_engine.py); None = an older integration.
        self.engine: Any = None
        # Phase A4: the integration's lighting bridge (tests/fake_lighting.py); None = an older integration.
        self.lighting: Any = None
        self.lighting_editor_calls: list[dict[str, Any]] = []

    # -- data ---------------------------------------------------------------
    def config(self) -> dict[str, Any]:
        return {
            "location_name": "Heima",
            "version": HA_VERSION,
            "state": "RUNNING",
            "time_zone": "Atlantic/Faroe",
            "country": "FO",
            "language": "en",
            "currency": "DKK",
            "unit_system": {"length": "km", "temperature": "°C"},
            "components": ["http", "websocket_api", "smart_occupancy", "light"]
            + (["recorder", "history"] if self.history_components else []),
            "safe_mode": False,
            "recovery_mode": False,
        }

    def entries(self, domain: str | None) -> list[dict[str, Any]]:
        if not self.integration_installed:
            return []
        entry = {"entry_id": "abc", "domain": "smart_occupancy", "title": "Smart Occupancy & Lighting", "state": "loaded",
                 "disabled_by": None, "reason": None, "source": "user"}
        return [entry] if domain in (None, "smart_occupancy") else []

    def registry(self) -> list[dict[str, Any]]:
        items = [
            {"entity_id": "light.kamar_nordur_loft", "platform": "hue", "disabled_by": None,
             "device_id": "dev-loft", "area_id": None, "name": None, "original_name": "Loft"},
            {"entity_id": "sensor.outdoor_lux", "platform": "zha", "disabled_by": None,
             "device_id": "dev-lux", "area_id": "uti", "name": None, "original_name": "Illuminance"},
            {"entity_id": "switch.hidden_registry_only", "platform": "zha", "disabled_by": None,
             "device_id": None, "area_id": None, "name": "Registry only", "original_name": None},
            {"entity_id": "switch.disabled_switch", "platform": "zha", "disabled_by": "user",
             "device_id": "dev-lux", "area_id": None, "name": None, "original_name": "Off"},
            {"entity_id": "binary_sensor.kamar_pir", "id": "reg-pir", "platform": "zha", "disabled_by": None,
             "device_id": "dev-pir", "area_id": None, "name": None, "original_name": "PIR"},
            {"entity_id": "binary_sensor.kamar_radar", "platform": "mqtt", "disabled_by": None,
             "device_id": "dev-radar", "area_id": None, "name": None, "original_name": "Radar"},
            {"entity_id": "binary_sensor.stova_door", "platform": "zha", "disabled_by": None,
             "device_id": "dev-door", "area_id": None, "name": None, "original_name": "Door"},
            {"entity_id": "light.kamar_lamp", "id": "reg-lamp", "platform": "hue", "disabled_by": None,
             "device_id": None, "area_id": "kamar_nordur", "name": None, "original_name": "Lamp"},
            {"entity_id": "cover.kamar_curtain", "platform": "zha", "disabled_by": None,
             "device_id": None, "area_id": "kamar_nordur", "name": None, "original_name": "Curtain"},
        ]
        if self.integration_installed:
            items += [
                {"entity_id": "binary_sensor.kamar_nordur_occupancy", "platform": "smart_occupancy", "disabled_by": None,
                 "unique_id": "smart_occupancy_room_kamar_nordur_occupied"},
                {"entity_id": "sensor.kamar_nordur_status", "platform": "smart_occupancy", "disabled_by": None,
                 "unique_id": "smart_occupancy_room_kamar_nordur_state"},
                {"entity_id": "sensor.disabled_one", "platform": "smart_occupancy", "disabled_by": "user"},
            ]
        return items

    def devices(self) -> list[dict[str, Any]]:
        return [
            {"id": "dev-loft", "name": "Hue loft", "name_by_user": "Loft ljós", "manufacturer": "Signify",
             "model": "LCT015", "area_id": "kamar_nordur", "disabled_by": None},
            {"id": "dev-lux", "name": "Outdoor sensor", "name_by_user": None, "manufacturer": "Aqara",
             "model": "GZCGQ01LM", "area_id": None, "disabled_by": None},
            {"id": "dev-off", "name": "Disabled device", "name_by_user": None, "manufacturer": None,
             "model": None, "area_id": None, "disabled_by": "user"},
            {"id": "dev-pir", "name": "Aqara motion", "name_by_user": None, "manufacturer": "Aqara",
             "model": "RTCGQ11LM", "area_id": "kamar_nordur", "disabled_by": None,
             "connections": [["zigbee", "00:15:8d:00:01:02:03:04"]], "identifiers": []},
            {"id": "dev-radar", "name": "FP1 radar", "name_by_user": None, "manufacturer": "Aqara",
             "model": "FP1 presence", "area_id": "kamar_nordur", "disabled_by": None},
            {"id": "dev-door", "name": "Door contact", "name_by_user": None, "manufacturer": "Aqara",
             "model": "MCCGQ11LM", "area_id": "stova", "disabled_by": None},
        ]

    # -- the integration's rooms API (simplified stand-in) ---------------------
    def rooms_model(self) -> dict[str, Any]:
        return {
            "api_version": 1, "integration_version": "0.6.9", "entry_id": "abc",
            "state": "loaded" if self.loaded else "not_loaded", "loaded": self.loaded, "writable": self.loaded,
            "operating_mode": "observe", "revision": revision_of(self.rooms_options),
            "rooms": {a: {"config": copy.deepcopy(r), "revision": revision_of(r), "area_exists": True,
                          "status": "healthy", "problems": [],
                          "occupancy": {"state": "confirmed_occupancy", "occupied": True, "reason": "Radar active"}
                          if r.get("enabled") else None,
                          "lighting": {"automatic": False, "control_enabled": False, "managed": list(r["lights"]),
                                       "relays": [], "zones": [], "switches": [], "priority_inputs": []},
                          "curtains": []}
                      for a, r in self.rooms_options.items()},
            "other_areas": {}, "timing": {"inactivity_timeout": 300},
            "schema": {"kinds": [{"value": "pir", "label": "PIR (motion)", "domains": ["binary_sensor"],
                                  "default_role": "trigger"},
                                 {"value": "radar", "label": "Radar (presence)", "domains": ["binary_sensor"],
                                  "default_role": "maintain"}],
                       "roles": [{"value": "trigger", "label": "Trigger"}, {"value": "maintain", "label": "Maintain"}]},
            "backup_format": "smart_occupancy.rooms_backup",
        }

    def room_plan(self, area_id: str, room: dict[str, Any] | None, expected: str | None) -> dict[str, Any]:
        old = self.rooms_options.get(area_id)
        changes = []
        if room is not None:
            before = {s["entity_id"] for s in (old or {}).get("sensors", [])}
            changes += [{"type": "sensor_added", "entity_id": s["entity_id"]} for s in room.get("sensors", [])
                        if s["entity_id"] not in before]
            changes += [{"type": "light_added", "entity_id": e} for e in room.get("lights", [])
                        if e not in (old or {}).get("lights", [])]
            changes += [{"type": "light_removed", "entity_id": e} for e in (old or {}).get("lights", [])
                        if e not in room.get("lights", [])]
        errors = [{"code": "not_a_light", "message": f"{e} is not a light entity", "entity_id": e}
                  for e in (room or {}).get("lights", []) if not e.startswith("light.")]
        return {"area_id": area_id, "room_revision": revision_of(old), "revision": revision_of(self.rooms_options),
                "stale": expected is not None and expected != revision_of(old), "errors": errors,
                "changes": changes, "problems_after": [], "problems_new": [], "problems_resolved": [],
                "impacts": [{"code": c, "level": "confirm", "title": c, "detail": c}
                            for c in self.require_confirmation],
                "requires_confirmation": list(self.require_confirmation), "apply": "reload" if changes else "none",
                "writable": self.loaded}

    def presence_model(self, area_id: str | None) -> dict[str, Any]:
        rooms = {}
        for a, r in self.rooms_options.items():
            if area_id and a != area_id:
                continue
            timing = r.get("timing") or {}
            signals = [{"entity_id": x["entity_id"], "name": x["entity_id"], "kind": x["kind"], "kind_name": x["kind"],
                        "role": x["role"], "inverted": x.get("inverted", False), "enabled": x.get("enabled", True),
                        "state": "on", "label": "Detected", "value": "active", "status": "active",
                        "condition": "active", "health": "available", "available": True,
                        "deadlines": {}, "effect": "presence"} for x in r.get("sensors", [])]
            rooms[a] = {
                "area_id": a, "name": a, "configured": True, "enabled": r.get("enabled", False),
                "revision": revision_of(r), "sensors": copy.deepcopy(r.get("sensors", [])),
                "timing": [{"key": k, "value": timing.get(k, v), "global": v, "overridden": k in timing}
                           for k, v in self.global_timing.items()],
                "timing_overrides": dict(timing),
                "entities": {"state": "sensor.kamar_nordur_status", "reason": None,
                             "occupied": "binary_sensor.kamar_nordur_occupancy"} if a == "kamar_nordur" else {},
                "problems": [], "automation": {"lighting_automatic": False, "curtains_automatic": []}, "zones": [],
                "diagnostics": [{"code": "single_signal", "level": "info", "basis": "heuristic", "title": "One signal",
                                 "detail": "", "action": "", "target": "sensors", "entity_id": None,
                                 "setting": None}],
                "occupancy": {"state": "confirmed_occupancy", "occupied": True, "reason": "Radar active",
                              "confidence": {"score": 80}} if r.get("enabled") else None,
                "signals": signals if r.get("enabled") else [],
                "explain": {"next": []} if r.get("enabled") else None,
            }
        return {"api_version": 1, "integration_version": "0.6.10", "now": "2026-11-17T10:00:00+00:00",
                "loaded": self.loaded, "writable": self.loaded, "state": "loaded", "operating_mode": "observe",
                "global": {"timing": dict(self.global_timing), "revision": revision_of(self.global_timing)},
                "timing_schema": [{"key": k, "default": v, "min": 0, "max": 86400} for k, v in self.global_timing.items()],
                "kinds": [], "roles": [], "states": [], "transitions": [],
                "state_values": {"active": ["on"], "inactive": ["off"]}, "rooms": rooms,
                "log": [{"seq": 1, "time": "2099-01-01T00:00:00.000+00:00", "kind": "start", "area_id": None}],
                "log_seq": 1}

    def presence_plan(self, area_id: str | None, change: dict[str, Any], expected: str | None) -> dict[str, Any]:
        current = revision_of(self.global_timing) if area_id is None else revision_of(self.rooms_options.get(area_id))
        timing = change.get("timing") or {}
        errors = [{"code": "timing_range", "setting": k, "message": f"{k} out of range"}
                  for k, v in timing.items() if v is not None and v < 0]
        changes = [{"type": "timing", "key": k, "new": v} for k, v in timing.items()]
        changes += [{"type": "sensor_role", "entity_id": x["entity_id"]} for x in change.get("sensors") or []]
        return {"scope": "global" if area_id is None else "room", "area_id": area_id, "revision": current,
                "stale": expected is not None and expected != current, "errors": errors,
                "changes": changes if not errors else [], "affected_rooms": [],
                "impacts": [{"code": c, "level": "confirm", "title": c, "detail": c}
                            for c in self.require_confirmation],
                "requires_confirmation": list(self.require_confirmation),
                "apply": "live" if changes and not errors else "none", "writable": self.loaded}

    async def notify_entry_changed(self) -> None:
        for ws, ids in list(self.entry_subscriptions.items()):
            for sub_id in ids:
                await ws.send_json({"id": sub_id, "type": "event", "event": [
                    {"type": "updated", "entry": {"entry_id": "abc", "domain": "smart_occupancy",
                                                  "state": "loaded"}}]})

    # -- protocol -----------------------------------------------------------
    async def websocket(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({"type": "auth_required", "ha_version": HA_VERSION})
        auth = await ws.receive_json()
        self.auth_attempts += 1
        if auth.get("type") != "auth" or auth.get("access_token") != self.token:
            await ws.send_json({"type": "auth_invalid", "message": "Invalid access token or password"})
            await ws.close()
            return ws
        await ws.send_json({"type": "auth_ok", "ha_version": HA_VERSION})
        self.connections.append(ws)
        self.subscriptions[ws] = []
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                data = msg.json()
                self.commands.append(data)
                await self._answer(ws, data)
        finally:
            self.connections.remove(ws)
            self.subscriptions.pop(ws, None)
            self.entry_subscriptions.pop(ws, None)
            if self.engine is not None:
                for session, item in list(self.engine.sessions.items()):
                    if item[0] is ws:
                        self.engine.sessions.pop(session)
                        if session == self.engine.owner_session:
                            self.engine.owner_session = None
        return ws

    async def _answer(self, ws: web.WebSocketResponse, msg: dict[str, Any]) -> None:
        mid, kind = msg["id"], msg["type"]

        async def result(value: Any) -> None:
            await ws.send_json({"id": mid, "type": "result", "success": True, "result": value})

        async def error(code: str, message: str) -> None:
            await ws.send_json({"id": mid, "type": "result", "success": False, "error": {"code": code, "message": message}})

        if self.drop_on and kind == self.drop_on:
            self.drop_on = None
            await ws.close()
            return
        if kind == "ping":
            await ws.send_json({"id": mid, "type": "pong"})
        elif kind == "subscribe_events":
            self.subscriptions[ws].append(mid)
            await result(None)
        elif kind == "get_config":
            await result(self.config())
        elif kind == "get_states":
            await result(self.states)
        elif kind == "config_entries/get":
            await result(self.entries(msg.get("domain")))
        elif kind == "manifest/get":
            if self.integration_installed and msg["integration"] == "smart_occupancy":
                await result({"domain": "smart_occupancy", "name": "Smart Occupancy & Lighting", "version": "0.6.8"})
            else:
                await error("not_found", "Integration not found")
        elif kind == "config/entity_registry/list":
            await result(self.registry())
        elif kind == "config/area_registry/list":
            await result([{"area_id": "kamar_nordur", "name": "Kamar Norður"}, {"area_id": "stova", "name": "Stova"},
                          {"area_id": "uti", "name": "Úti"}])
        elif kind == "config/device_registry/list":
            await result(self.devices())
        elif kind == "config/floor_registry/list":
            await result(self.floors)
        elif kind == "config/label_registry/list":
            await result([])
        elif kind == "config/auth/list":
            await result(self.users)
        elif kind == "config_entries/subscribe":
            self.entry_subscriptions.setdefault(ws, []).append(mid)
            await result(None)
        elif kind.startswith("smart_occupancy/rooms/") and not self.rooms_api:
            await error("unknown_command", "Unknown command.")
        elif kind == "smart_occupancy/rooms/get":
            await result(self.rooms_model())
        elif kind == "smart_occupancy/rooms/review":
            await result(self.room_plan(msg["area_id"], msg["room"], msg.get("expected_revision")))
        elif kind == "smart_occupancy/rooms/set":
            if not self.loaded:
                await error("not_loaded", "Smart Occupancy & Lighting is not running")
                return
            plan = self.room_plan(msg["area_id"], msg["room"], msg["expected_revision"])
            if plan["stale"]:
                await error("stale_revision", "The room configuration was changed elsewhere")
            elif plan["errors"]:
                await error("invalid_room", plan["errors"][0]["message"])
            elif [c for c in plan["requires_confirmation"] if c not in msg.get("confirmed", [])]:
                await error("confirmation_required", "These consequences must be confirmed")
            else:
                if msg["room"] is None:
                    self.rooms_options.pop(msg["area_id"], None)
                else:
                    self.rooms_options[msg["area_id"]] = {**self.rooms_options.get(msg["area_id"], {}), **msg["room"]}
                await result({"applied": plan["apply"], "changes": plan["changes"],
                              "room_revision": revision_of(self.rooms_options.get(msg["area_id"])),
                              "revision": revision_of(self.rooms_options)})
                await self.notify_entry_changed()
        elif kind == "smart_occupancy/rooms/restore/review":
            await result({"revision": revision_of(self.rooms_options), "stale": False, "errors": [],
                          "rooms": [{"area_id": a, "changes": [{"type": "normalized"}]} for a in msg["rooms"]],
                          "impacts": [{"code": "restore", "level": "confirm", "title": "Replace", "detail": ""}],
                          "requires_confirmation": ["restore"], "writable": self.loaded})
        elif kind == "smart_occupancy/rooms/restore":
            if msg["expected_revision"] != revision_of(self.rooms_options):
                await error("stale_revision", "Changed elsewhere")
            elif "restore" not in msg.get("confirmed", []):
                await error("confirmation_required", "These consequences must be confirmed")
            else:
                self.rooms_options = copy.deepcopy(msg["rooms"])
                await result({"applied": "reload", "rooms": list(msg["rooms"]),
                              "revision": revision_of(self.rooms_options)})
                await self.notify_entry_changed()
        elif kind.startswith("smart_occupancy/presence/") and not self.presence_api:
            await error("unknown_command", "Unknown command.")
        elif kind == "smart_occupancy/presence/get":
            await result(self.presence_model(msg.get("area_id")))
        elif kind == "smart_occupancy/presence/review":
            await result(self.presence_plan(msg.get("area_id"), msg["change"], msg.get("expected_revision")))
        elif kind == "smart_occupancy/presence/set":
            plan = self.presence_plan(msg.get("area_id"), msg["change"], msg["expected_revision"])
            if plan["stale"]:
                await error("stale_revision", "Changed elsewhere")
            elif plan["errors"]:
                await error("invalid_room", plan["errors"][0]["message"])
            elif [c for c in plan["requires_confirmation"] if c not in msg.get("confirmed", [])]:
                await error("confirmation_required", "These consequences must be confirmed")
            else:
                timing = msg["change"].get("timing") or {}
                if msg.get("area_id") is None:
                    self.global_timing.update({k: v for k, v in timing.items() if v is not None})
                    revision = revision_of(self.global_timing)
                else:
                    room = self.rooms_options[msg["area_id"]]
                    room["timing"] = {**room.get("timing", {}), **{k: v for k, v in timing.items()
                                                                     if v is not None}}
                    revision = revision_of(room)
                await result({"applied": plan["apply"], "changes": plan["changes"], "revision": revision,
                              "rooms_revision": revision_of(self.rooms_options)})
                await self.notify_entry_changed()
        elif kind.startswith("smart_occupancy/lighting/bridge/"):
            if self.lighting is None:
                await error("unknown_command", f"Unknown command {kind}")
            else:
                await self.lighting.answer(ws, msg, result, error)
        elif kind.startswith("smart_occupancy/lighting/") and self.lighting is not None:
            # The integration's lighting editor commands (forwarded while it controls lighting).
            self.lighting_editor_calls.append(msg)
            await result({"handled_by": "integration", "type": kind})
        elif kind in ("automation/config", "script/config"):
            await error("not_found", "Entity not found")
        elif kind.startswith("smart_occupancy/engine/"):
            if self.engine is None:
                await error("unknown_command", f"Unknown command {kind}")
            else:
                await self.engine.answer(ws, msg, result, error)
        elif kind == "history/history_during_period":
            self.history_calls.append(msg)
            await result({e: self.history[e] for e in msg["entity_ids"] if e in self.history})
        else:
            await error("unknown_command", f"Unknown command {kind}")

    async def emit_state(self, entity_id: str, value: str, name: str) -> None:
        new = state(entity_id, value, name)
        for ws, ids in list(self.subscriptions.items()):
            for sub_id in ids:
                await ws.send_json(
                    {"id": sub_id, "type": "event", "event": {"event_type": "state_changed",
                     "data": {"entity_id": entity_id, "old_state": None, "new_state": new}}}
                )

    async def emit_live(self, entity_id: str, value: str | None, name: str | None = None) -> None:
        """A state change happening now (last_changed = now), seen by the App and by the integration's engine."""
        from datetime import UTC, datetime

        changed = datetime.now(UTC)
        new = None
        if value is not None:
            new = {**state(entity_id, value, name or entity_id), "last_changed": changed.isoformat(),
                   "last_updated": changed.isoformat()}
        self.states = [s for s in self.states if s["entity_id"] != entity_id] + ([new] if new else [])
        if self.engine is not None:
            await self.engine.on_state(entity_id, value, changed)
        for ws, ids in list(self.subscriptions.items()):
            for sub_id in ids:
                await ws.send_json({"id": sub_id, "type": "event", "event": {"event_type": "state_changed",
                                    "data": {"entity_id": entity_id, "old_state": None, "new_state": new}}})

    async def emit_live_context(self, entity_id: str, value: str, attributes: dict[str, Any], context_id: str) -> None:
        """A device reporting a command: the state with the command's context (a local device)."""
        from datetime import UTC, datetime

        changed = datetime.now(UTC).isoformat()
        new = {"entity_id": entity_id, "state": value, "attributes": attributes, "last_changed": changed,
               "last_updated": changed, "context": {"id": context_id, "parent_id": None, "user_id": None}}
        self.states = [s for s in self.states if s["entity_id"] != entity_id] + [new]
        for ws, ids in list(self.subscriptions.items()):
            for sub_id in ids:
                await ws.send_json({"id": sub_id, "type": "event", "event": {"event_type": "state_changed",
                                    "data": {"entity_id": entity_id, "old_state": None, "new_state": new}}})

    async def drop_connections(self) -> None:
        for ws in list(self.connections):
            await ws.close()

    # -- supervisor ---------------------------------------------------------
    async def supervisor(self, request: web.Request) -> web.Response:
        self.supervisor_requests.append(request.path)
        if request.headers.get("Authorization") != f"Bearer {self.token}":
            return web.json_response({"result": "error", "message": "Unauthorized"}, status=401)
        if request.path == "/addons/self/info":
            data = {"name": "Smart Occupancy & Lighting", "slug": "smart_lighting", "version": "0.3.0",
                    "version_latest": "0.3.0", "update_available": False, "state": "started", "arch": "amd64",
                    "options": {"log_level": "info"}, "ingress_url": "/api/hassio_ingress/xyz/"}
        else:
            data = {"supervisor": "2026.11.2", "homeassistant": HA_VERSION, "hassos": "17.1",
                    "operating_system": "Home Assistant OS 17.1", "machine": "generic-x86-64", "arch": "amd64",
                    "channel": "stable", "supported": True, "healthy": True}
        return web.json_response({"result": "ok", "data": data})


@pytest.fixture
async def fake_ha(aiohttp_server: Callable[..., Awaitable[Any]], tmp_path: Path) -> Any:
    fake = FakeHomeAssistant()
    fake.data_dir = tmp_path / "data"
    app = web.Application()
    app.router.add_get("/core/websocket", fake.websocket)
    app.router.add_get("/addons/self/info", fake.supervisor)
    app.router.add_get("/info", fake.supervisor)
    server = await aiohttp_server(app)
    fake.base_url = str(server.make_url("")).rstrip("/")
    fake.ws_url = fake.base_url.replace("http://", "ws://") + "/core/websocket"
    return fake


def make_settings(fake: Any, **env: str) -> Any:
    values = {
        "SL_DATA_DIR": env.pop("SL_DATA_DIR", "") or str(Path(fake.data_dir)),
        "SUPERVISOR_TOKEN": TOKEN,
        "SL_HA_WS_URL": fake.ws_url,
        "SL_SUPERVISOR_URL": fake.base_url,
        "SL_ALLOWED_CLIENTS": "127.0.0.1,::1",
        "SL_OPTIONS": "/nonexistent/options.json",
    }
    values.update(env)
    return load_settings(values)


async def wait_for(condition: Callable[[], bool], timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.02)


@pytest.fixture
async def app_client(fake_ha: FakeHomeAssistant, aiohttp_client: Callable[..., Awaitable[Any]]) -> Any:
    """The App, started against the fake Home Assistant and connected."""
    settings = make_settings(fake_ha)
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
    await wait_for(lambda: core.supervisor is not None and core.supervisor.checked_at is not None)
    await wait_for(lambda: core.rooms is not None and core.rooms.api_state == "ok")
    client.core = core
    return client
