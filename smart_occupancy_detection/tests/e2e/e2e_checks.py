"""End-to-end checks: the real App image, a disposable Home Assistant 2026.11, and Smart Comfort running beside it.

    python e2e_checks.py --tokens tokens.json --ha-dir <dir with start_ha.sh/stop_ha.sh> --data-dir <App /data>

The App runs through ``stack.sh`` (fake Supervisor at 172.30.33.250, Ingress on 127.0.0.1:18300). Smart Comfort's
own e2e stack (its App on 172.30.32.2, its companion ``smart_occupancy``) may run at the same time. Sensors are
driven like people would: the test drivers (input_booleans the template sensors follow) are written through the
REST states API of the *test* Home Assistant. Never run this against a production instance.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Awaitable, Callable
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any

import aiohttp

HA = "http://127.0.0.1:18123"
SUP = "http://127.0.0.1:18300"
INGRESS = f"{SUP}/api/hassio_ingress/sodIngressToken_XyZ789"
HERE = Path(__file__).resolve().parent
KAMAR, STOVA = "kamar_nordur", "stova"
SENSORS = {
    KAMAR: [("binary_sensor.pir_kamar", "pir", "trigger"), ("binary_sensor.radar_kamar", "radar", "maintain"),
            ("binary_sensor.door_kamar", "door", "trigger")],
    STOVA: [("binary_sensor.pir_stova", "pir", "trigger")],
}
FAST_TIMING = {"correlation_window": 5, "exit_confirmation": 20, "inactivity_timeout": 40, "dropout_grace": 5,
               "trigger_stuck_after": 1800, "presence_stuck_after": 0, "startup_hold": 15}
ALLOWED_TYPES = {"auth", "ping", "get_config", "get_states", "subscribe_events", "unsubscribe_events",
                 "config/area_registry/list", "config/device_registry/list", "config/entity_registry/list",
                 "config/auth/list", "manifest/get", "smart_occupancy_detection/status",
                 "smart_occupancy_detection/session", "smart_occupancy_detection/pair",
                 "smart_occupancy_detection/unpair", "smart_occupancy_detection/claim",
                 "smart_occupancy_detection/heartbeat", "smart_occupancy_detection/publish",
                 "smart_occupancy_detection/release"}


class Run:
    def __init__(self, tokens: dict[str, Any], ha_dir: Path, data_dir: Path) -> None:
        self.tokens = tokens
        self.ha_dir = ha_dir
        self.data_dir = data_dir
        self.results: list[tuple[str, bool, str]] = []
        self.services: list[dict[str, Any]] = []
        self.session: aiohttp.ClientSession | None = None
        self.revision: str | None = None

    # ------------------------------------------------------------ helpers
    def check(self, name: str, ok: bool, detail: Any = "") -> bool:
        self.results.append((name, bool(ok), str(detail)[:400]))
        print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  -> {str(detail)[:400]}"), flush=True)
        return bool(ok)

    @property
    def admin(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.tokens['llat']}"}

    async def app(self, path: str, body: Any = None, user: str | None = None) -> tuple[int, Any]:
        headers = {"X-SOD": "1"} if body is not None else {}
        if user:
            headers["X-Test-User"] = user
        method = self.session.post if body is not None else self.session.get
        async with method(f"{INGRESS}/{path}", json=body, headers=headers) as r:
            try:
                return r.status, await r.json()
            except Exception:  # noqa: BLE001
                return r.status, await r.text()

    async def state(self, entity_id: str) -> dict[str, Any] | None:
        async with self.session.get(f"{HA}/api/states/{entity_id}", headers=self.admin) as r:
            return await r.json() if r.status == 200 else None

    async def drive(self, entity_id: str, state: str) -> None:
        """A test driver or a sensor state, written into the *test* Home Assistant (never a service call)."""
        current = await self.state(entity_id) or {}
        async with self.session.post(f"{HA}/api/states/{entity_id}", headers=self.admin,
                                     json={"state": state, "attributes": current.get("attributes", {})}) as r:
            assert r.status in (200, 201), await r.text()

    async def wait(self, predicate: Callable[[], Awaitable[bool]], timeout: float = 15.0) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                if await predicate():
                    return True
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(0.5)
        return False

    async def is_state(self, entity_id: str, value: str, **attrs: Any) -> bool:
        s = await self.state(entity_id)
        return bool(s) and s["state"] == value and all(s["attributes"].get(k) == v for k, v in attrs.items())

    async def ws(self, token: str, commands: list[dict[str, Any]]) -> list[dict[str, Any]]:
        async with self.session.ws_connect(f"{HA}/api/websocket") as ws:
            await ws.receive_json()
            await ws.send_json({"type": "auth", "access_token": token})
            await ws.receive_json()
            out = []
            for i, command in enumerate(commands, start=1):
                await ws.send_json({"id": i, **command})
                while (msg := await ws.receive_json()).get("id") != i:
                    pass
                out.append(msg)
            return out

    async def listen_services(self) -> None:
        """Every service call in Home Assistant during the run, with the user that caused it."""
        while True:
            try:
                async with self.session.ws_connect(f"{HA}/api/websocket") as ws:
                    await ws.receive_json()
                    await ws.send_json({"type": "auth", "access_token": self.tokens["llat"]})
                    await ws.receive_json()
                    await ws.send_json({"id": 1, "type": "subscribe_events", "event_type": "call_service"})
                    async for msg in ws:
                        data = msg.json()
                        if data.get("type") == "event":
                            event = data["event"]
                            self.services.append({"domain": event["data"].get("domain"),
                                                  "service": event["data"].get("service"),
                                                  "user_id": (event.get("context") or {}).get("user_id")})
            except (aiohttp.ClientError, asyncio.CancelledError, ConnectionError):
                if asyncio.current_task().cancelled():
                    return
            await asyncio.sleep(1)

    async def registry(self) -> list[dict[str, Any]]:
        return (await self.ws(self.tokens["llat"], [{"type": "config/entity_registry/list"}]))[0]["result"]

    async def save_room(self, area_id: str, room: dict[str, Any]) -> tuple[int, Any]:
        status, review = await self.app("api/rooms/review", {"area_id": area_id, "room": room})
        if status != 200 or not review.get("valid"):
            return status, review
        return await self.app("api/rooms/save", {"area_id": area_id, "room": room,
                                                 "expected_revision": review["revision"]})

    def room(self, area_id: str, **extra: Any) -> dict[str, Any]:
        return {"enabled": True, "sensors": [{"entity_id": e, "kind": k, "role": r, "inverted": False, "enabled": True}
                                             for e, k, r in SENSORS[area_id]],
                "activity": [], "timing": {}, "adjacent": [], **extra}

    def ha_script(self, name: str) -> None:
        subprocess.run([str(self.ha_dir / name)], check=True, timeout=200)

    def stack(self, command: str) -> None:
        subprocess.run([str(HERE / "stack.sh"), command], check=True, timeout=200)

    # ------------------------------------------------------------- phases
    async def fresh(self) -> None:
        status, health = await self.app("api/health")
        expected = os.environ.get("SOD_EXPECT_VERSION", "0.2.0")
        self.check(f"App {expected} answers through Ingress", status == 200 and health.get("version") == expected, health)
        status, overview = await self.app("api/overview")
        self.check("A fresh installation has no rooms", status == 200 and overview["rooms"] == [], overview)
        link = overview["connection"]["companion"]
        self.check("Nothing is published before pairing", link["state"] == "not_paired", link)
        sc_before = {e["entity_id"]: e["unique_id"] for e in await self.registry() if e["platform"] == "smart_occupancy"}
        self.sc_before = sc_before
        status, body = await self.app("api/companion/pair", {}, user="0123456789abcdef0123456789abcdef")
        self.check("Pairing needs a Home Assistant administrator", status == 403, (status, body))
        status, body = await self.app("api/companion/pair", {})
        self.check("An administrator pairs the App", status == 200 and body.get("paired"), body)
        self.check("App connection entity on", await self.wait(lambda: self.is_state("binary_sensor.sod_app_connected", "on")))
        status, areas = await self.app("api/areas")
        names = {a["area_id"]: a["name"] for a in areas["areas"]}
        self.check("Home Assistant areas are offered", {KAMAR, STOVA} <= set(names), names)
        status, found = await self.app(f"api/discover/{KAMAR}")
        kinds = {c["entity_id"]: c["suggested_kind"] for c in found["candidates"]}
        self.check("Discovery suggests the area's sensors", kinds.get("binary_sensor.pir_kamar") == "pir"
                   and kinds.get("binary_sensor.radar_kamar") == "radar" and kinds.get("binary_sensor.door_kamar") == "door",
                   kinds)
        self.check("Discovery never offers occupancy outputs",
                   not any(e.startswith(("binary_sensor.sod_", "binary_sensor.smart_occupancy")) for e in kinds), kinds)
        status, settings = await self.app("api/settings")
        status, review = await self.app("api/timing/review", {"timing": FAST_TIMING})
        status, saved = await self.app("api/timing/save", {"timing": FAST_TIMING, "expected_revision": review["revision"]})
        self.check("Global timing saved (reviewed)", status == 200, saved)
        for driver in ("input_boolean.kamar_motion", "input_boolean.kamar_door_open", "input_boolean.nobara_pc",
                       "input_boolean.stova_motion"):
            await self.drive(driver, "off")
        status, saved = await self.save_room(KAMAR, self.room(KAMAR))
        self.check("Room Kamar Norður saved", status == 200 and saved.get("changed"), saved)
        status, saved = await self.save_room(STOVA, self.room(STOVA))
        self.check("Room Stova saved", status == 200, saved)
        status, saved = await self.save_room(KAMAR, self.room(KAMAR, adjacent=[STOVA]))
        self.check("Adjacency saved", status == 200, saved)
        bad = self.room(KAMAR, sensors=[{"entity_id": "binary_sensor.door_kamar", "kind": "door", "role": "trigger"}])
        status, review = await self.app("api/rooms/review", {"area_id": KAMAR, "room": bad})
        self.check("An invalid room is refused with the reason", status == 200 and not review["valid"]
                   and "presence" in review["problems"][0]["message"], review)
        ok = await self.wait(lambda: self.is_state("binary_sensor.sod_kamar_nordur_occupied", "off", fresh=True))
        self.check("Native occupancy entity created (vacant, fresh)", ok, await self.state("binary_sensor.sod_kamar_nordur_occupied"))
        reg = {e["entity_id"]: e for e in await self.registry() if e["platform"] == "smart_occupancy_detection"}
        expected = {f"binary_sensor.sod_kamar_nordur_occupied": f"{KAMAR}_occupied",
                    f"sensor.sod_kamar_nordur_state": f"{KAMAR}_state",
                    f"sensor.sod_kamar_nordur_confidence": f"{KAMAR}_confidence",
                    f"sensor.sod_kamar_nordur_reason": f"{KAMAR}_reason",
                    f"sensor.sod_kamar_nordur_last_activity": f"{KAMAR}_last_activity",
                    "binary_sensor.sod_stova_occupied": f"{STOVA}_occupied"}
        self.check("Entity ids and unique ids (area id based)",
                   all(reg.get(e, {}).get("unique_id") == u for e, u in expected.items()), {e: reg.get(e, {}).get("unique_id") for e in expected})
        self.check("No unique id shared with Smart Comfort", not ({e["unique_id"] for e in reg.values()} & set(sc_before.values())))
        await self.detection()

    async def detection(self) -> None:
        occupied = "binary_sensor.sod_kamar_nordur_occupied"
        state = "sensor.sod_kamar_nordur_state"
        await self.drive("input_boolean.kamar_motion", "on")
        self.check("Motion: possible occupancy at once", await self.wait(
            lambda: self.is_state(occupied, "on"), 5) and (await self.state(state))["state"] in ("possible_occupancy", "confirmed_occupancy"))
        self.check("Sustained motion confirms", await self.wait(lambda: self.is_state(state, "confirmed_occupancy"), 12))
        await self.drive("input_boolean.kamar_motion", "off")
        self.check("No activity: vacancy pending (still occupied)", await self.wait(
            lambda: self.is_state(state, "possible_vacancy"), 8) and await self.is_state(occupied, "on"))
        self.check("Vacant after the vacancy timeout", await self.wait(lambda: self.is_state(occupied, "off"), 60))
        # Radar + PC: Smart Comfort sees the same radar.
        sc = await self.sc_occupancy_entity()
        await self.drive("input_boolean.nobara_pc", "on")
        self.check("Radar presence occupies the room", await self.wait(lambda: self.is_state(occupied, "on"), 8))
        if sc:
            self.check("Smart Comfort still decides its own room from the same radar", await self.wait(
                lambda: self.is_state(sc, "on"), 20), await self.state(sc))
        # Every sensor unavailable while occupied: uncertain, still on.
        for entity in ("binary_sensor.pir_kamar", "binary_sensor.radar_kamar"):
            await self.drive(entity, "unavailable")
        self.check("All sensors offline: uncertain, never vacant", await self.wait(
            lambda: self.is_state(state, "uncertain"), 8) and await self.is_state(occupied, "on"))
        status, room = await self.app(f"api/rooms/{KAMAR}")
        self.check("The room explains the outage", any(p["code"] == "all_offline" for p in room["room"]["problems"]),
                   room["room"]["problems"])
        await self.drive("input_boolean.nobara_pc", "off")
        await self.drive("input_boolean.nobara_pc", "on")  # the templates render again: sensors are back
        await self.drive("input_boolean.kamar_motion", "on")
        await self.drive("input_boolean.kamar_motion", "off")
        self.check("Sensors reconnect: occupied again", await self.wait(
            lambda: self.is_state(state, "confirmed_occupancy"), 12))
        await self.drive("input_boolean.nobara_pc", "off")
        # Door exit with movement in the adjacent room: the fast exit timeout (20 s) instead of 40 s.
        await self.drive("input_boolean.kamar_door_open", "on")
        await self.drive("input_boolean.kamar_door_open", "off")
        await self.drive("input_boolean.stova_motion", "on")
        status, room = await self.app(f"api/rooms/{KAMAR}")
        evidence = room["room"].get("engine", {}).get("vacancy_evidence")
        self.check("Door exit and adjacent movement give exit evidence", await self.wait(
            lambda: self.is_state(occupied, "off"), 32), evidence)
        await self.drive("input_boolean.stova_motion", "off")
        status, room = await self.app(f"api/rooms/{KAMAR}")
        kinds = [e["kind"] for e in room["room"]["timeline"]]
        self.check("The timeline records transitions and sensor health", "state" in kinds and "sensor" in kinds, kinds[:12])

    async def sc_occupancy_entity(self) -> str | None:
        for entry in await self.registry():
            if entry["platform"] == "smart_occupancy" and entry["unique_id"] == f"smart_occupancy_room_{KAMAR}_occupied":
                return entry["entity_id"]
        return None

    async def identity(self) -> None:
        await self.ws(self.tokens["llat"], [{"type": "config/area_registry/update", "area_id": STOVA, "name": "Stofa"}])
        async def renamed() -> bool:
            s = await self.state("binary_sensor.sod_stova_occupied")
            return bool(s) and s["attributes"].get("friendly_name") == "Stofa Occupancy"
        ok = await self.wait(renamed, 20)
        self.check("Area renamed: same entity id, new name", ok, await self.state("binary_sensor.sod_stova_occupied"))
        await self.ws(self.tokens["llat"], [{"type": "config/area_registry/update", "area_id": STOVA, "name": "Stova"}])
        status, room = await self.app(f"api/rooms/{STOVA}")
        status, saved = await self.save_room(STOVA, {**room["config"], "enabled": False})
        self.check("Detection turned off: entities stay, unavailable", status == 200 and await self.wait(
            lambda: self.is_state("binary_sensor.sod_stova_occupied", "unavailable"), 10))
        status, saved = await self.save_room(STOVA, {**room["config"], "enabled": True})
        async def available() -> bool:
            s = await self.state("binary_sensor.sod_stova_occupied")
            return bool(s) and s["state"] in ("on", "off")
        self.check("Turned on again", await self.wait(available, 10), await self.state("binary_sensor.sod_stova_occupied"))

    async def app_outage(self) -> None:
        occupied = "binary_sensor.sod_kamar_nordur_occupied"
        await self.drive("input_boolean.nobara_pc", "on")
        await self.wait(lambda: self.is_state(occupied, "on"), 8)
        self.stack("stop-app")
        self.check("App stopped: occupancy kept, marked not fresh", await self.wait(
            lambda: self.is_state(occupied, "on", fresh=False), 45), await self.state(occupied))
        self.check("App connection entity off", await self.is_state("binary_sensor.sod_app_connected", "off"))
        reason = (await self.state("sensor.sod_kamar_nordur_reason"))["state"]
        self.check("Reason says the App is not connected", reason.startswith("The App is not connected"), reason)
        await self.drive("input_boolean.nobara_pc", "off")  # the person left while the App was away
        await asyncio.sleep(3)
        self.check("Nothing inferred while the App is away", await self.is_state(occupied, "on"))
        self.stack("start-app")
        self.check("Restarted App publishes again without a human", await self.wait(
            lambda: self.is_state("binary_sensor.sod_app_connected", "on"), 30))
        status, room = await self.app(f"api/rooms/{KAMAR}")
        self.check("Restart: the occupied room is held (start-up hold)", room["room"]["occupied"] is True
                   and room["room"]["starting_up"], {k: room["room"][k] for k in ("state", "occupied", "starting_up")})
        self.check("Then it becomes vacant from real evidence", await self.wait(lambda: self.is_state(occupied, "off"), 70))

    async def ha_outage(self) -> None:
        occupied = "binary_sensor.sod_kamar_nordur_occupied"
        await self.drive("input_boolean.nobara_pc", "on")
        await self.wait(lambda: self.is_state(occupied, "on"), 8)
        self.ha_script("stop_ha.sh")
        await asyncio.sleep(3)
        status, overview = await self.app("api/overview")
        frozen = next(r for r in overview["rooms"] if r["area_id"] == KAMAR)
        self.check("Home Assistant down: the App freezes its decisions", frozen["frozen"] and frozen["occupied"], frozen)
        self.ha_script("start_ha.sh")
        async def restored() -> bool:
            s = await self.state(occupied)
            return bool(s) and s["state"] == "on"
        self.check("After the Home Assistant restart the entity comes back with the last decision", await self.wait(
            restored, 60), await self.state(occupied))
        self.check("The App reconnects and publishes again", await self.wait(
            lambda: self.is_state("binary_sensor.sod_app_connected", "on"), 90))
        await self.drive("input_boolean.nobara_pc", "off")
        self.check("Vacant afterwards from real evidence", await self.wait(lambda: self.is_state(occupied, "off"), 90))

    async def delete(self) -> None:
        status, review = await self.app("api/rooms/delete/review", {"area_id": STOVA})
        status, body = await self.app("api/rooms/delete", {"area_id": STOVA, "expected_revision": review["revision"]})
        self.check("Room deleted (reviewed)", status == 200, body)
        async def gone() -> bool:
            return not any(e["entity_id"].startswith("binary_sensor.sod_stova") for e in await self.registry())
        self.check("Its entities are removed from Home Assistant", await self.wait(gone, 15))
        status, room = await self.app(f"api/rooms/{KAMAR}")
        self.check("The neighbour reference was removed too", room["config"]["adjacent"] == [], room["config"])

    async def finish(self) -> None:
        async with self.session.get(f"{SUP}/_test/commands") as r:
            commands = (await r.json())["commands"]
        types = {c.get("type") for c in commands}
        self.check("The App sent only allowlisted commands", types <= ALLOWED_TYPES, types - ALLOWED_TYPES)
        self.check("The App never sent a service call", "call_service" not in types and "execute_script" not in types)
        events = {c.get("event_type") for c in commands if c.get("type") == "subscribe_events"}
        self.check("Event subscriptions are the allowlisted ones", events <= {
            "state_changed", "area_registry_updated", "device_registry_updated", "entity_registry_updated",
            "homeassistant_started"}, events)
        ours = [s for s in self.services if s["user_id"] == self.tokens["sod_user"]]
        self.check("No service call in Home Assistant came from the App's user", not ours, ours)
        if not hasattr(self, "sc_before"):  # a later run of the same installation: from the snapshot
            self.sc_before = json.loads(self._snapshot_path().read_text())["smart_comfort"]
        sc_after = {e["entity_id"]: e["unique_id"] for e in await self.registry() if e["platform"] == "smart_occupancy"}
        self.check("Smart Comfort's entities are unchanged", sc_after == self.sc_before,
                   set(sc_after.items()) ^ set(self.sc_before.items()))
        status = (await self.ws(self.tokens["llat"], [{"type": "smart_occupancy/companion/status"}]))[0]
        self.check("Smart Comfort's companion still works beside it", status.get("success"), status)

    # ------------------------------------------------------------- Phase 0.2
    def _snapshot_path(self) -> Path:
        return self.data_dir.parent / f"{self.data_dir.name}-snapshot.json"

    async def sod_entities(self) -> dict[str, str]:
        return {e["entity_id"]: e["unique_id"] for e in await self.registry()
                if e["platform"] == "smart_occupancy_detection"}

    async def snapshot(self) -> None:
        status, overview = await self.app("api/overview")
        data = {"entities": await self.sod_entities(), "revision": overview["revision"],
                "smart_comfort": getattr(self, "sc_before", None)}
        self._snapshot_path().write_text(json.dumps(data))
        self.check("Snapshot before the upgrade", len(data["entities"]) >= 6, len(data["entities"]))

    async def upgrade_check(self) -> None:
        before = json.loads(self._snapshot_path().read_text())
        self.check("Every entity kept its id and unique id", await self.sod_entities() == before["entities"],
                   set((await self.sod_entities()).items()) ^ set(before["entities"].items()))
        status, overview = await self.app("api/overview")
        self.check("The room configuration is unchanged", overview["revision"] == before["revision"])
        async def publishing() -> bool:
            _, o = await self.app("api/overview")
            return o["connection"]["companion"]["state"] == "publishing" and o["connection"]["fresh"]
        self.check("Publishing again without pairing again", await self.wait(publishing, 60))
        _, overview = await self.app("api/overview")
        link = overview["connection"]["companion"]
        self.check("Versions shown and compatible", overview["connection"]["versions"]["app"] == "0.2.0"
                   and link["companion_version"] == "0.2.0" and link["compatibility"]["level"] == "ok"
                   and link["counters"]["auto_pairs"] == 0, (overview["connection"]["versions"], link["compatibility"]))
        await self.drive("input_boolean.kamar_motion", "on")
        self.check("Detection works after the upgrade", await self.wait(
            lambda: self.is_state("binary_sensor.sod_kamar_nordur_occupied", "on", fresh=True), 10))
        await self.drive("input_boolean.kamar_motion", "off")

    async def reload(self) -> None:
        async with self.session.get(f"{HA}/api/config/config_entries/entry", headers=self.admin) as r:
            entry = next(e for e in await r.json() if e["domain"] == "smart_occupancy_detection")
        async with self.session.post(f"{HA}/api/config/config_entries/entry/{entry['entry_id']}/reload",
                                     headers=self.admin) as r:
            self.check("Integration reloaded", r.status == 200, await r.text())
        self.check("The App publishes again by itself after the reload", await self.wait(
            lambda: self.is_state("binary_sensor.sod_app_connected", "on"), 45))
        _, overview = await self.app("api/overview")
        self.check("No new pairing was needed", overview["connection"]["companion"]["counters"]["auto_pairs"] == 0)

    async def network(self) -> None:
        occupied = "binary_sensor.sod_kamar_nordur_occupied"
        await self.drive("input_boolean.nobara_pc", "on")
        await self.wait(lambda: self.is_state(occupied, "on"), 8)
        async with self.session.post(f"{SUP}/_test/drop_core") as r:
            dropped = (await r.json())["dropped"]
        self.check("Network interruption (the Core connection dropped)", dropped >= 1, dropped)
        async def held() -> bool:
            _, room = await self.app(f"api/rooms/{KAMAR}")
            return bool(room["room"]["hold"]) and room["room"]["hold"]["reason"] == "reconnect"
        self.check("After reconnecting the room shows the reconnect hold", await self.wait(held, 30))
        self.check("The room stayed occupied through the interruption", await self.is_state(occupied, "on"))
        self.check("Fresh again", await self.wait(lambda: self.is_state("binary_sensor.sod_app_connected", "on"), 30))
        await self.drive("input_boolean.nobara_pc", "off")

    async def transfer(self) -> None:
        status, exported = await self.app("api/export")
        self.check("Configuration exported", status == 200 and exported.get("format") == "smart_occupancy_detection.config")
        status, review = await self.app("api/import/review", {"document": exported})
        self.check("Importing the same export changes nothing", review["valid"] and review["changes"] == [], review)
        changed = json.loads(json.dumps(exported))
        changed["config"]["timing"]["inactivity_timeout"] = 45
        status, review = await self.app("api/import/review", {"document": changed})
        self.check("A changed export is reviewed", review["changes"] == [{"type": "global_timing",
                                                                         "keys": ["inactivity_timeout"]}], review)
        status, applied = await self.app("api/import/apply", {"document": changed, "expected_revision": review["revision"]})
        status, review = await self.app("api/import/review", {"document": exported})
        status, applied = await self.app("api/import/apply", {"document": exported, "expected_revision": review["revision"]})
        _, overview = await self.app("api/overview")
        self.check("Import and restore of the original applied", status == 200, applied)

    async def pilot(self) -> None:
        sc = await self.sc_occupancy_entity()
        if not self.check("Smart Comfort's occupancy entity exists for the pilot", sc is not None):
            return
        status, pilot = await self.app("api/pilot")
        self.check("Smart Comfort's entity is suggested as the reference", pilot["rooms"][KAMAR]["suggestion"] == sc,
                   pilot["rooms"][KAMAR])
        status, _ = await self.app("api/pilot/reference", {"area_id": KAMAR, "entity_id": sc})
        await self.drive("input_boolean.nobara_pc", "on")
        await asyncio.sleep(8)
        await self.drive("input_boolean.nobara_pc", "off")
        await asyncio.sleep(3)
        status, room = await self.app(f"api/rooms/{KAMAR}")
        summary = room["pilot"]
        self.check("The pilot compares both outputs", summary and summary["compared_seconds"] > 0
                   and summary["now"] in ("agree", "differ"), summary)
        async with self.session.get(f"{INGRESS}/api/pilot/report.csv") as r:
            text = await r.text()
        self.check("The difference report downloads", r.status == 200 and text.startswith("room,started_at"), text[:80])

    async def recreate(self) -> None:
        before = {e: u for e, u in (await self.sod_entities()).items() if "stova" in e}
        status, review = await self.app("api/rooms/delete/review", {"area_id": STOVA})
        await self.app("api/rooms/delete", {"area_id": STOVA, "expected_revision": review["revision"]})
        async def gone() -> bool:
            return not any("stova" in e for e in await self.sod_entities())
        self.check("Room deleted: its entities are gone", await self.wait(gone, 15))
        status, saved = await self.save_room(STOVA, self.room(STOVA))
        async def back() -> bool:
            return {e: u for e, u in (await self.sod_entities()).items() if "stova" in e} == before
        self.check("Room created again: the same entity ids and unique ids", await self.wait(back, 15),
                   {e: u for e, u in (await self.sod_entities()).items() if "stova" in e})

    async def diagnostics(self) -> None:
        async with self.session.get(f"{INGRESS}/api/diagnostics") as r:
            text = await r.text()
        leaks = [what for what, value in (("token", self.tokens["sod_llat"][:40]), ("admin token", self.tokens["llat"][:40]),
                                          ("user id", self.tokens["sod_user"]), ("address", "172.30.3"),
                                          ("supervisor token", "sod-e2e-supervisor-token")) if value in text]
        self.check("The diagnostics download is redacted", r.status == 200 and not leaks and '"redaction"' in text, leaks)

    async def main(self, phases: list[str]) -> int:
        async with aiohttp.ClientSession() as session:
            self.session = session
            listener = asyncio.create_task(self.listen_services())
            try:
                for phase in phases:
                    print(f"--- {phase}", flush=True)
                    await getattr(self, phase.replace("-", "_"))()
            finally:
                listener.cancel()
        failed = [r for r in self.results if not r[1]]
        print(f"\n{len(self.results) - len(failed)}/{len(self.results)} checks passed")
        (self.data_dir.parent / "e2e_results.json").write_text(json.dumps(self.results, indent=1))
        return 1 if failed else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--tokens", required=True, type=Path)
    parser.add_argument("--ha-dir", required=True, type=Path)
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("phases", nargs="*", default=["fresh", "identity", "app-outage", "ha-outage", "delete",
                                                      "finish"])
    args = parser.parse_args()
    run = Run(json.loads(args.tokens.read_text()), args.ha_dir, args.data_dir)
    sys.exit(asyncio.run(run.main(args.phases)))
