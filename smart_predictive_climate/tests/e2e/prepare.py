"""Prepare the disposable Home Assistant for the e2e checks (never a production instance).

* onboarding, when the instance is new (an owner "E2E Admin" and a long-lived token, written to tokens.json as
  "llat"); an instance that is already onboarded needs its administrator's "llat" in tokens.json;
* a separate administrator "SPC App" with its own long-lived token (the fake Supervisor proxies the App with it,
  so every action of the App in Home Assistant carries that user's context and can be told apart);
* the companion integration's config entry (through the config flow, like a user).

    python prepare.py <tokens.json>     ("llat" is read or, on a new instance, created; "spc_llat" is added)
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


async def onboard(session) -> str | None:
    """Onboard a new instance like the first-run wizard; returns the owner's long-lived token (None when the
    instance is already onboarded)."""
    async with session.get(URL + "/api/onboarding") as r:
        steps = await r.json() if r.status == 200 else []
    if not any(s.get("step") == "user" and not s.get("done") for s in steps):
        return None
    client = URL + "/"
    password = secrets.token_hex(12)
    async with session.post(URL + "/api/onboarding/users", json={
            "client_id": client, "name": "E2E Admin", "username": "e2eadmin", "password": password,
            "language": "en"}) as r:
        code = (await r.json())["auth_code"]
    async with session.post(URL + "/auth/token", data={"grant_type": "authorization_code", "code": code,
                                                       "client_id": client}) as r:
        access = (await r.json())["access_token"]
    headers = {"Authorization": f"Bearer {access}"}
    for step, body in (("core_config", {}), ("analytics", {}),
                       ("integration", {"client_id": client, "redirect_uri": client})):
        async with session.post(f"{URL}/api/onboarding/{step}", headers=headers, json=body) as r:
            print(f"onboarding {step}:", r.status)
    return (await ws_call(session, access, [{"type": "auth/long_lived_access_token", "client_name": "SPC e2e admin",
                                             "lifespan": 365}]))[0]["result"]


async def main(tokens_path: Path) -> None:
    tokens = json.loads(tokens_path.read_text()) if tokens_path.exists() else {}
    async with aiohttp.ClientSession() as s:
        if "llat" not in tokens:
            llat = await onboard(s)
            if llat is None:
                raise SystemExit("the instance is already onboarded: put its administrator's token into tokens.json "
                                 'as {"llat": "..."}')
            tokens["llat"] = llat
            tokens_path.write_text(json.dumps(tokens))
            print("onboarded the instance")
        admin = tokens["llat"]
        # Home Assistant 2026.11 keeps a changed http: configuration "pending" and reverts it (with a restart)
        # after five minutes unless an administrator confirms it.
        promoted = (await ws_call(s, admin, [{"type": "http/config/promote"}]))[0]
        print("http config promoted:", promoted.get("success"), promoted.get("error", ""))
        if "spc_llat" not in tokens:
            password = secrets.token_hex(12)
            user = (await ws_call(s, admin, [{"type": "config/auth/create", "name": "SPC App",
                                              "group_ids": ["system-admin"], "local_only": False}]))[0]["result"]["user"]
            await ws_call(s, admin, [{"type": "config/auth_provider/homeassistant/create", "user_id": user["id"],
                                      "username": "spcapp", "password": password}])
            client = URL + "/"
            async with s.post(URL + "/auth/login_flow", json={"client_id": client, "handler": ["homeassistant", None],
                                                              "redirect_uri": client}) as r:
                flow = await r.json()
            async with s.post(f"{URL}/auth/login_flow/{flow['flow_id']}",
                              json={"client_id": client, "username": "spcapp", "password": password}) as r:
                code = (await r.json())["result"]
            async with s.post(URL + "/auth/token", data={"grant_type": "authorization_code", "code": code,
                                                         "client_id": client}) as r:
                access = (await r.json())["access_token"]
            llat = (await ws_call(s, access, [{"type": "auth/long_lived_access_token", "client_name": "SPC e2e",
                                               "lifespan": 365}]))[0]["result"]
            tokens.update(spc_llat=llat, spc_user=user["id"])
            tokens_path.write_text(json.dumps(tokens))
            print("created the SPC App user", user["id"])
        headers = {"Authorization": f"Bearer {admin}"}
        async with s.get(URL + "/api/config/config_entries/entry", headers=headers) as r:
            entries = [e for e in await r.json() if e["domain"] == "smart_predictive_climate"]
        if not entries:
            async with s.post(URL + "/api/config/config_entries/flow", headers=headers,
                              json={"handler": "smart_predictive_climate"}) as r:
                flow = await r.json()
            async with s.post(f"{URL}/api/config/config_entries/flow/{flow['flow_id']}", headers=headers, json={}) as r:
                print("config entry:", (await r.json()).get("type"))
        else:
            print("config entry exists:", entries[0]["state"])


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1])))
