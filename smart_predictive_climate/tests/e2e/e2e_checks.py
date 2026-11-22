"""End-to-end checks of the real App image against a disposable Home Assistant 2026.11.1 (never production).

Prerequisites (see the stack and prepare scripts): the disposable instance on 127.0.0.1:18123 with
``tests/e2e/ha/configuration.yaml``, the areas "Kamar suður" (the template room sensor, the window sensor and the
demo heat pump's device) and "Stova", ``prepare.py`` run (tokens.json), the App's data folder seeded by
``seed.py`` and ``stack.sh up``.

    python e2e_checks.py <tokens.json> <phase>...

Phases (in this order on a fresh stack): setup, entities, unknown, simulate, app_restart, ha_restart (after
restarting Home Assistant), backup, commands. Each prints PASS/FAIL lines; the exit code is the number of FAILs.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
import time
from typing import Any

import aiohttp

HA = "http://127.0.0.1:18123"
SUP = "http://127.0.0.1:18500"
INGRESS = f"{SUP}/api/hassio_ingress/spcIngressToken_XyZ789"
ROOM = "kamar_sudur"
PREFIX = "sensor.spc_kamar_sudur_"
KEYS = ("predicted_temperature", "heating_rate", "cooling_rate", "estimated_warmup_minutes",
        "recommended_preheat_start", "recommended_target_temperature", "solar_gain", "prediction_confidence",
        "model_status")
BINARY = "binary_sensor.spc_kamar_sudur_preheat_recommended"
CONNECTED = "binary_sensor.spc_app_connected"
ROOM_CONFIG = {
    "name": "Kamar suður", "temperature": "sensor.kamar_temperature", "thermostats": ["climate.heatpump"],
    "outdoor": "sensor.outdoor_temperature", "openings": [{"entity_id": "binary_sensor.kamar_window", "kind": "window"}],
    "comfort": {"day_c": 21.0, "weekday": {"start": "06:30", "end": "22:30"},
                "weekend": {"start": "06:30", "end": "22:30"}}}
# What the App may send to Home Assistant (reads, its own companion, two subscriptions).
ALLOWED = {"auth", "get_states", "get_config", "config/area_registry/list", "config/device_registry/list",
           "config/entity_registry/list", "config/entity_registry/list_for_display", "auth/current_user",
           "history/history_during_period", "recorder/statistics_during_period", "recorder/list_statistic_ids",
           "manifest/get", "subscribe_events", "subscribe_entities", "unsubscribe_events", "weather/subscribe_forecast",
           "ping", "supported_features", "config/auth/list"}
FORBIDDEN_PREFIXES = ("call_service", "execute_script", "fire_event", "config/entity_registry/update",
                      "config/device_registry/update", "config/area_registry/create", "config/area_registry/update",
                      "config/area_registry/delete", "recorder/purge", "recorder/update_statistics", "set_state")

fails = 0


def check(ok: bool, what: str, detail: Any = "") -> None:
    global fails
    print(("PASS " if ok else "FAIL ") + what + (f"  [{detail}]" if detail != "" and not ok else ""), flush=True)
    fails += 0 if ok else 1


class Client:
    def __init__(self, session: aiohttp.ClientSession, tokens: dict[str, str]) -> None:
        self.s = session
        self.admin = {"Authorization": f"Bearer {tokens['llat']}"}

    async def get(self, path: str) -> Any:
        async with self.s.get(INGRESS + path) as r:
            return r.status, await r.json()

    async def post(self, path: str, data: dict[str, Any], *, csrf: bool = True) -> Any:
        headers = {"X-SPC": "1"} if csrf else {}
        async with self.s.post(INGRESS + path, json=data, headers=headers) as r:
            return r.status, await r.json()

    async def revision(self) -> Any:
        return (await self.get("/api/overview"))[1]["revision"]

    async def state(self, entity_id: str) -> dict[str, Any] | None:
        async with self.s.get(f"{HA}/api/states/{entity_id}", headers=self.admin) as r:
            return await r.json() if r.status == 200 else None

    async def states(self) -> list[dict[str, Any]]:
        async with self.s.get(f"{HA}/api/states", headers=self.admin) as r:
            return await r.json()

    async def service(self, domain: str, service: str, data: dict[str, Any]) -> None:
        async with self.s.post(f"{HA}/api/services/{domain}/{service}", headers=self.admin, json=data) as r:
            assert r.status == 200, r.status

    async def ws(self, **command: Any) -> Any:
        async with self.s.ws_connect(HA + "/api/websocket") as ws:
            await ws.receive_json()
            await ws.send_json({"type": "auth", "access_token": self.admin["Authorization"][7:]})
            await ws.receive_json()
            await ws.send_json({"id": 1, **command})
            while (msg := await ws.receive_json()).get("id") != 1:
                pass
            return msg

    async def commands(self) -> list[dict[str, Any]]:
        async with self.s.get(SUP + "/_test/commands") as r:
            return (await r.json())["commands"]


async def until(predicate: Any, timeout: float, step: float = 2.0) -> Any:
    end = time.monotonic() + timeout
    while True:
        value = await predicate()
        if value or time.monotonic() > end:
            return value
        await asyncio.sleep(step)


async def setup(c: Client) -> None:
    status, health = await c.get("/api/health")
    check(status == 200 and health["ok"], "the App answers through Ingress", health)
    ov = (await c.get("/api/overview"))[1]
    check(ov["connection"]["home_assistant"]["connected"], "connected to Home Assistant", ov["connection"])
    check(ov["connection"]["versions"]["home_assistant"] == "2026.11.1", "Home Assistant 2026.11.1",
          ov["connection"]["versions"])
    areas = (await c.get("/api/areas"))[1]["areas"]
    kamar = next((a for a in areas if a["area_id"] == ROOM), None)
    check(kamar is not None, "the area list has Kamar suður", [a["area_id"] for a in areas])
    status, area = await c.get(f"/api/areas/{ROOM}")
    check(status == 200 and area["configured"] is None, "nothing is configured before the user chooses", area.get("configured"))
    devices = {e["entity_id"] for d in area["devices"] for e in d.get("entities", [])}
    check({"sensor.kamar_temperature", "climate.heatpump"} <= devices, "the room's sensor and heat pump are listed", devices)
    check(bool(area["devices"]), "the area's devices are listed", area.get("devices"))
    sug = json.dumps(area["suggestions"])
    check("sensor.kamar_temperature" in sug, "the room sensor is suggested", area["suggestions"])
    del devices
    # CSRF: a change without the App's header is refused.
    status, _ = await c.post("/api/rooms/save", {"area_id": ROOM, "room": ROOM_CONFIG, "expected_revision": 0}, csrf=False)
    check(status == 403, "a change without the CSRF header is refused", status)
    status, pair = await c.post("/api/companion/pair", {"replace": True})
    check(status == 200, "pairing with the companion", pair)
    ok = await until(lambda: _is(c, CONNECTED, "on"), 60)
    check(bool(ok), "binary_sensor.spc_app_connected is on")
    status, saved = await c.post("/api/rooms/save", {"area_id": ROOM, "room": ROOM_CONFIG,
                                                      "expected_revision": await c.revision()})
    check(status == 200, "the room is saved", saved)
    settings = (await c.get("/api/settings"))[1]
    status, saved = await c.post("/api/settings/save", {"settings": {**settings["settings"], "history_days": 30,
                                                                     "weather": "weather.heima"},
                                                       "expected_revision": settings["revision"]})
    check(status == 200, "settings saved (30 days, weather.heima)", saved)

    async def trained() -> Any:
        view = (await c.get(f"/api/rooms/{ROOM}"))[1]
        return view if view["model"] and view["model"]["current"] and ((view.get("view") or {}).get("prediction") or {}).get("available") else None
    view = await until(trained, 600, 5)
    check(bool(view), "the backfill ran and a current model was trained", view and view.get("status"))
    if not view:
        return
    acq = (await c.get("/api/settings"))[1]["acquisition"]
    check(acq["state"] == "done" and acq["windows"] >= 1, "the Recorder was read in windows", acq)
    print("  model:", json.dumps({k: view["model"][k] for k in ("status", "preliminary", "confidence", "structure")}))
    print("  view:", json.dumps({k: view["view"]["prediction"].get(k) for k in ("state", "predicted_1h", "heating_rate", "cooling_rate",
                                                                  "warmup_min", "preheat_start", "recommended_target")}))


async def _is(c: Client, entity_id: str, state: str) -> bool:
    s = await c.state(entity_id)
    return bool(s and s["state"] == state)


async def entities(c: Client) -> None:
    async def published() -> Any:
        states = {s["entity_id"]: s for s in await c.states()}
        return states if all(PREFIX + k in states for k in KEYS) and BINARY in states else None
    states = await until(published, 120)
    check(bool(states), "all room entities exist in Home Assistant",
          sorted(e for e in {s["entity_id"] for s in await c.states()} if "spc_" in e))
    if not states:
        return
    pred = states[PREFIX + "predicted_temperature"]
    check(pred["state"] not in ("unknown", "unavailable") and 10 < float(pred["state"]) < 30,
          "predicted temperature is a plausible number", pred["state"])
    check(pred["attributes"].get("unit_of_measurement") == "°C" and pred["attributes"].get("device_class") == "temperature",
          "predicted temperature is a °C temperature", pred["attributes"])
    status = states[PREFIX + "model_status"]["state"]
    check(status in ("preliminary", "low", "moderate", "high"), "model status is a learned level", status)
    conf = states[PREFIX + "prediction_confidence"]["state"]
    check(conf in ("none", "low", "moderate", "high"), "confidence is one of none/low/moderate/high", conf)
    start = states[PREFIX + "recommended_preheat_start"]
    check(start["attributes"].get("device_class") == "timestamp", "preheat start is a timestamp sensor", start["attributes"])
    binary = states[BINARY]["state"]
    reliable = conf in ("moderate", "high")
    check(binary in ("on", "off") if reliable else binary == "unknown",
          "preheat recommendation is on/off only when reliable, else unknown", (binary, conf))
    for k in KEYS:
        print(f"  {PREFIX + k} = {states[PREFIX + k]['state']}")
    print(f"  {BINARY} = {binary}")
    reg = await c.ws(type="config/entity_registry/list")
    uids = {e["entity_id"]: e["unique_id"] for e in reg["result"] if e["platform"] == "smart_predictive_climate"}
    check(uids.get(PREFIX + "predicted_temperature", "").endswith(f"{ROOM}_predicted_temperature")
          or ROOM in uids.get(PREFIX + "predicted_temperature", ""), "unique ids are made from the area id", uids)
    Path("/tmp/abw/e2e/uids.json").write_text(json.dumps(uids))


async def unknown(c: Client) -> None:
    await c.service("input_boolean", "turn_off", {"entity_id": "input_boolean.kamar_sensor_ok"})
    ok = await until(lambda: _is(c, PREFIX + "predicted_temperature", "unknown"), 150)
    check(bool(ok), "room sensor unavailable -> predicted temperature unknown",
          (await c.state(PREFIX + "predicted_temperature") or {}).get("state"))
    b = (await c.state(BINARY) or {}).get("state")
    check(b == "unknown", "room sensor unavailable -> preheat recommendation unknown, never off", b)
    view = (await c.get(f"/api/rooms/{ROOM}"))[1]
    print("  reason:", ((view.get("view") or {}).get("prediction") or {}).get("code"))
    await c.service("input_boolean", "turn_on", {"entity_id": "input_boolean.kamar_sensor_ok"})
    ok = await until(lambda: _not_unknown(c, PREFIX + "predicted_temperature"), 150)
    check(bool(ok), "room sensor back -> predicted temperature is a number again")


async def _not_unknown(c: Client, entity_id: str) -> bool:
    s = await c.state(entity_id)
    return bool(s and s["state"] not in ("unknown", "unavailable"))


async def simulate(c: Client) -> None:
    before = len(await c.commands())
    status, sim = await c.post("/api/rooms/simulate", {"area_id": ROOM, "scenario": {
        "heating": "window", "heat_from_min": 60, "heat_until_min": 240, "outdoor_offset": -3, "window_open": True,
        "window_from_min": 0, "window_until_min": 30}})
    check(status == 200 and sim.get("available", True), "a simulation runs", sim if status != 200 else "")
    after = await c.commands()
    sent = [x.get("type") for x in after[before:]]
    check(not any(t and t.startswith(FORBIDDEN_PREFIXES) for t in sent), "the simulation sent nothing to devices", sent)


async def app_restart(c: Client) -> None:
    proc = await asyncio.create_subprocess_exec(str(Path(__file__).with_name("stack.sh")), "stop-app")
    await proc.wait()
    ok = await until(lambda: _is(c, PREFIX + "predicted_temperature", "unavailable"), 180)
    check(bool(ok), "App stopped -> the room entities become unavailable")
    check(await _is(c, CONNECTED, "off"), "App stopped -> spc_app_connected is off",
          (await c.state(CONNECTED) or {}).get("state"))
    proc = await asyncio.create_subprocess_exec(str(Path(__file__).with_name("stack.sh")), "start-app")
    await proc.wait()
    ok = await until(lambda: _not_unknown(c, PREFIX + "predicted_temperature"), 180)
    check(bool(ok), "App started -> predictions published again")
    view = (await c.get(f"/api/rooms/{ROOM}"))[1]
    check(bool(view["model"] and view["model"]["current"]), "the model survived the App restart", view.get("status"))


async def ha_restart(c: Client) -> None:
    ok = await until(lambda: _not_unknown(c, PREFIX + "predicted_temperature"), 240)
    check(bool(ok), "after a Home Assistant restart the entities are published again")
    ov = (await c.get("/api/overview"))[1]
    check(ov["connection"]["home_assistant"]["connected"], "the App reconnected to Home Assistant")
    reg = await c.ws(type="config/entity_registry/list")
    uids = {e["entity_id"]: e["unique_id"] for e in reg["result"] if e["platform"] == "smart_predictive_climate"}
    before = json.loads(Path("/tmp/abw/e2e/uids.json").read_text())
    check(uids == before, "entity ids and unique ids unchanged across the restart", set(uids) ^ set(before))


async def backup(c: Client) -> None:
    settings = (await c.get("/api/settings"))[1]
    backups = settings["backups"]
    check(len(backups) >= 1, "configuration backups are kept (one per change after the first)", backups)
    store_before = settings["store"]
    first = backups[-1]["name"] if backups else None
    status, review = await c.post("/api/backups/review", {"name": first})
    check(status == 200, "a backup can be reviewed", review)
    status, restored = await c.post("/api/backups/restore", {"name": first, "expected_revision": settings["revision"]})
    check(status == 200, "the oldest backup is restored", restored)
    after = (await c.get("/api/settings"))[1]
    check(after["store"].get("records", 0) >= store_before.get("records", 0) or after["store"] == store_before,
          "restoring kept the observations", (store_before, after["store"]))
    # Put the room back for the remaining checks.
    status, _ = await c.post("/api/rooms/save", {"area_id": ROOM, "room": ROOM_CONFIG, "expected_revision": after["revision"]})
    view = (await c.get(f"/api/rooms/{ROOM}"))[1]
    check(status == 200 and view["model"] is not None, "the model was kept through restore and re-adding the room",
          view.get("status"))


async def commands(c: Client) -> None:
    sent = await c.commands()
    types = sorted({x.get("type") for x in sent if x.get("type")})
    print("  command types:", types)
    companion = {t for t in types if t.startswith("smart_predictive_climate/")}
    other = set(types) - companion - ALLOWED
    check(not other, "the App sent only reads, subscriptions and its companion's commands", other)
    check(not any(t.startswith(FORBIDDEN_PREFIXES) for t in types), "no service call, state write or registry change")
    check("history/history_during_period" in types, "history was read from the real Recorder")
    services = (await c.ws(type="get_services"))["result"]
    check("smart_predictive_climate" not in services, "the companion registers no services", services.get("smart_predictive_climate"))


PHASES = {"setup": setup, "entities": entities, "unknown": unknown, "simulate": simulate, "app_restart": app_restart,
          "ha_restart": ha_restart, "backup": backup, "commands": commands}


async def main() -> None:
    tokens = json.loads(Path(sys.argv[1]).read_text())
    async with aiohttp.ClientSession() as s:
        c = Client(s, tokens)
        for phase in sys.argv[2:]:
            print(f"== {phase}", flush=True)
            await PHASES[phase](c)
    print(f"{fails} failed")
    sys.exit(fails)


if __name__ == "__main__":
    asyncio.run(main())
