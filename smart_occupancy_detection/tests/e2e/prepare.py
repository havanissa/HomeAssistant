"""Prepare the disposable Home Assistant for the e2e checks (never a production instance).

* a separate administrator "SOD App" with its own long-lived token (the fake Supervisor proxies the App with it,
  so every action of the App in Home Assistant carries that user's context and can be told apart);
* the test motion sensor placed in its area (``binary_sensor.pir_kamar`` -> ``kamar_nordur``);
* the companion integration's config entry (through the config flow, like a user).

    python prepare.py <tokens.json>     (tokens.json has the main administrator's "llat"; "sod_llat" is added)
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import secrets
import sys

import aiohttp

URL = "http://127.0.0.1:18123"


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


async def main(tokens_path: Path) -> None:
    tokens = json.loads(tokens_path.read_text())
    admin = tokens["llat"]
    async with aiohttp.ClientSession() as s:
        if "sod_llat" not in tokens:
            password = secrets.token_hex(12)
            user = (await ws_call(s, admin, [{"type": "config/auth/create", "name": "SOD App",
                                              "group_ids": ["system-admin"], "local_only": False}]))[0]["result"]["user"]
            await ws_call(s, admin, [{"type": "config/auth_provider/homeassistant/create", "user_id": user["id"],
                                      "username": "sodapp", "password": password}])
            client = URL + "/"
            async with s.post(URL + "/auth/login_flow", json={"client_id": client, "handler": ["homeassistant", None],
                                                              "redirect_uri": client}) as r:
                flow = await r.json()
            async with s.post(f"{URL}/auth/login_flow/{flow['flow_id']}",
                              json={"client_id": client, "username": "sodapp", "password": password}) as r:
                code = (await r.json())["result"]
            async with s.post(URL + "/auth/token", data={"grant_type": "authorization_code", "code": code,
                                                         "client_id": client}) as r:
                access = (await r.json())["access_token"]
            llat = (await ws_call(s, access, [{"type": "auth/long_lived_access_token", "client_name": "SOD e2e",
                                               "lifespan": 365}]))[0]["result"]
            tokens.update(sod_llat=llat, sod_user=user["id"])
            tokens_path.write_text(json.dumps(tokens))
            print("created the SOD App user", user["id"])
        result = await ws_call(s, admin, [{"type": "config/entity_registry/update", "entity_id": "binary_sensor.pir_kamar",
                                           "area_id": "kamar_nordur"}])
        print("pir_kamar area:", result[0].get("success"))
        headers = {"Authorization": f"Bearer {admin}"}
        async with s.get(URL + "/api/config/config_entries/entry", headers=headers) as r:
            entries = [e for e in await r.json() if e["domain"] == "smart_occupancy_detection"]
        if not entries:
            async with s.post(URL + "/api/config/config_entries/flow", headers=headers,
                              json={"handler": "smart_occupancy_detection"}) as r:
                flow = await r.json()
            async with s.post(f"{URL}/api/config/config_entries/flow/{flow['flow_id']}", headers=headers, json={}) as r:
                print("config entry:", (await r.json()).get("type"))
        else:
            print("config entry exists:", entries[0]["state"])


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1])))
