"""Prepare the disposable Home Assistant for the e2e checks (never a production instance).

* first run: onboarding (owner "Test Admin"), a long-lived token, and the confirmation of the ``http:`` settings
  that Home Assistant 2026.11 asks for;
* a separate administrator "AB App" with its own long-lived token: the fake Supervisor proxies the App with it, so
  everything the App does in Home Assistant carries that user and can be told apart from the tests' own actions.

    python prepare.py <tokens.json>     ("llat" and "ab_llat" are written there)
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import secrets
import sys

import aiohttp

URL = "http://127.0.0.1:18123"
CLIENT = URL + "/"


async def ws_call(session, token, commands):
    async with session.ws_connect(URL + "/api/websocket") as ws:
        await ws.receive_json()
        await ws.send_json({"type": "auth", "access_token": token})
        assert (await ws.receive_json())["type"] == "auth_ok"
        out = []
        for i, command in enumerate(commands, start=1):
            await ws.send_json({"id": i, **command})
            while (msg := await ws.receive_json()).get("id") != i:
                pass
            out.append(msg)
        return out


async def onboard(session) -> dict:
    async with session.post(URL + "/api/onboarding/users", json={
            "client_id": CLIENT, "name": "Test Admin", "username": "admin", "password": secrets.token_hex(12),
            "language": "en"}) as r:
        user = await r.json()
    async with session.post(URL + "/auth/token", data={"grant_type": "authorization_code", "code": user["auth_code"],
                                                       "client_id": CLIENT}) as r:
        access = (await r.json())["access_token"]
    headers = {"Authorization": f"Bearer {access}"}
    for step, body in (("core_config", {}), ("analytics", {}), ("integration", {"client_id": CLIENT, "redirect_uri": CLIENT})):
        async with session.post(f"{URL}/api/onboarding/{step}", json=body, headers=headers) as r:
            print("onboarding", step, r.status)
    result = await ws_call(session, access, [{"type": "auth/long_lived_access_token", "client_name": "e2e", "lifespan": 365},
                                             {"type": "http/config/promote"}])
    print("http config confirmed:", result[1].get("success"))
    return {"llat": result[0]["result"]}


#: Rooms of the test home: area name -> device names (demo devices) and entities without a device.
ROOMS = {
    "Kitchen": (["Kitchen Lights", "Kitchen Window"], ["sensor.test_lux", "binary_sensor.test_motion"]),
    "Bedroom": (["Bed Light"], []),
    "Living Room": (["Ceiling Lights", "Living Room RGBWW Lights"], ["fan.living_room_fan"]),  # + the demo's Sonos device
    "Office": (["Office RGBW Lights"], []),
    "Hall": (["Entrance Color + White Lights"], ["binary_sensor.test_door"]),
}


async def rooms(session, token) -> None:
    """Areas for the room-first checks (idempotent). The rest of the test entities stay without a room."""
    areas, devices = (r["result"] for r in await ws_call(session, token, [
        {"type": "config/area_registry/list"}, {"type": "config/device_registry/list"}]))
    by_name = {a["name"]: a["area_id"] for a in areas}
    for name, (device_names, entity_ids) in ROOMS.items():
        if name not in by_name:
            by_name[name] = (await ws_call(session, token, [{"type": "config/area_registry/create", "name": name}]))[0]["result"]["area_id"]
        commands = [{"type": "config/device_registry/update", "device_id": d["id"], "area_id": by_name[name]}
                    for d in devices if (d.get("name_by_user") or d.get("name")) in device_names and d.get("area_id") != by_name[name]]
        commands += [{"type": "config/entity_registry/update", "entity_id": e, "area_id": by_name[name]} for e in entity_ids]
        if commands:
            await ws_call(session, token, commands)
    print("rooms:", sorted(by_name))


async def main(tokens_path: Path) -> None:
    async with aiohttp.ClientSession() as s:
        tokens = json.loads(tokens_path.read_text()) if tokens_path.exists() else await onboard(s)
        tokens_path.write_text(json.dumps(tokens))
        admin = tokens["llat"]
        if "ab_llat" not in tokens:
            password = secrets.token_hex(12)
            user = (await ws_call(s, admin, [{"type": "config/auth/create", "name": "AB App",
                                              "group_ids": ["system-admin"], "local_only": False}]))[0]["result"]["user"]
            await ws_call(s, admin, [{"type": "config/auth_provider/homeassistant/create", "user_id": user["id"],
                                      "username": "abapp", "password": password}])
            async with s.post(URL + "/auth/login_flow", json={"client_id": CLIENT, "handler": ["homeassistant", None],
                                                              "redirect_uri": CLIENT}) as r:
                flow = await r.json()
            async with s.post(f"{URL}/auth/login_flow/{flow['flow_id']}",
                              json={"client_id": CLIENT, "username": "abapp", "password": password}) as r:
                code = (await r.json())["result"]
            async with s.post(URL + "/auth/token", data={"grant_type": "authorization_code", "code": code,
                                                         "client_id": CLIENT}) as r:
                access = (await r.json())["access_token"]
            llat = (await ws_call(s, access, [{"type": "auth/long_lived_access_token", "client_name": "AB e2e",
                                               "lifespan": 365}]))[0]["result"]
            tokens.update(ab_llat=llat, ab_user=user["id"])
            tokens_path.write_text(json.dumps(tokens))
            print("created the AB App user", user["id"])
        await rooms(s, admin)
        states = (await ws_call(s, admin, [{"type": "get_states"}]))[0]["result"]
        wanted = ("light.flaky_lamp", "binary_sensor.sod_kamar_occupied", "light.bed_light", "sensor.test_lux")
        present = {e["entity_id"] for e in states}
        print("test entities:", {e: e in present for e in wanted})


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1])))
