"""Prepare the disposable Home Assistant for the e2e checks (never a production instance).

    python prepare.py <tokens.json> [seed]

* first run: onboarding (owner "Test Admin"), a long-lived token, the confirmation of the ``http:`` settings
  that Home Assistant 2026.11 asks for, and a separate administrator "AIE App" with its own long-lived token (the
  fake Supervisor proxies the App with it, so everything the App does carries that user);
* ``seed``: (re)creates the test automations of fixtures.py exactly as Home Assistant's editor does, with their
  labels, categories, areas, entity ids, names, icons, aliases, hidden/disabled flags, assistant exposure and on/off
  states. Automations in automations.yaml that are not fixtures are removed first (leftovers of earlier runs).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import secrets
import sys

import aiohttp
from fixtures import AREAS, AUTOMATIONS, CATEGORIES, LABELS
from hactl import HA, URL

CLIENT = URL + "/"


async def onboard(s: aiohttp.ClientSession) -> dict:
    async with s.post(URL + "/api/onboarding/users", json={
            "client_id": CLIENT, "name": "Test Admin", "username": "admin", "password": secrets.token_hex(12),
            "language": "en"}) as r:
        user = await r.json()
    async with s.post(URL + "/auth/token", data={"grant_type": "authorization_code", "code": user["auth_code"],
                                                 "client_id": CLIENT}) as r:
        access = (await r.json())["access_token"]
    headers = {"Authorization": f"Bearer {access}"}
    for step, body in (("core_config", {}), ("analytics", {}), ("integration", {"client_id": CLIENT, "redirect_uri": CLIENT})):
        async with s.post(f"{URL}/api/onboarding/{step}", json=body, headers=headers) as r:
            print("onboarding", step, r.status)
    async with HA(s, access) as ha:
        llat = await ha.cmd("auth/long_lived_access_token", client_name="e2e", lifespan=365)
        await ha.cmd("http/config/promote")
    print("http config confirmed")
    return {"llat": llat}


async def app_user(s: aiohttp.ClientSession, tokens: dict) -> None:
    password = secrets.token_hex(12)
    async with HA(s, tokens["llat"]) as ha:
        user = (await ha.cmd("config/auth/create", name="AIE App", group_ids=["system-admin"], local_only=False))["user"]
        await ha.cmd("config/auth_provider/homeassistant/create", user_id=user["id"], username="aieapp", password=password)
    async with s.post(URL + "/auth/login_flow", json={"client_id": CLIENT, "handler": ["homeassistant", None],
                                                      "redirect_uri": CLIENT}) as r:
        flow = await r.json()
    async with s.post(f"{URL}/auth/login_flow/{flow['flow_id']}",
                      json={"client_id": CLIENT, "username": "aieapp", "password": password}) as r:
        code = (await r.json())["result"]
    async with s.post(URL + "/auth/token", data={"grant_type": "authorization_code", "code": code,
                                                 "client_id": CLIENT}) as r:
        access = (await r.json())["access_token"]
    async with HA(s, access) as ha:
        tokens["aie_llat"] = await ha.cmd("auth/long_lived_access_token", client_name="AIE e2e", lifespan=365)
    tokens["aie_user"] = user["id"]
    print("created the AIE App user", user["id"])


async def ensure_metadata(ha: HA) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    labels, cats, areas = await ha.names()
    for name, definition in LABELS.items():
        if name not in labels:
            labels[name] = (await ha.cmd("config/label_registry/create", name=name, **definition))["label_id"]
    for name, icon in CATEGORIES.items():
        if name not in cats:
            params = {"scope": "automation", "name": name, **({"icon": icon} if icon else {})}
            cats[name] = (await ha.cmd("config/category_registry/create", **params))["category_id"]
    for name in AREAS:
        if name not in areas:
            areas[name] = (await ha.cmd("config/area_registry/create", name=name))["area_id"]
    return labels, cats, areas


async def seed(ha: HA, only: list[str] | None = None) -> None:
    labels, cats, areas = await ensure_metadata(ha)
    # remove leftovers (automations.yaml only; external YAML automations are not addressable)
    for uid, entry in (await ha.by_unique()).items():
        if await ha.config(uid) is not None and (only is None or uid in only):
            await ha.delete(uid)
    await asyncio.sleep(1)
    for config_id, (config, meta, state) in AUTOMATIONS.items():
        if only is not None and config_id not in only:
            continue
        await ha.post(config_id, {"id": config_id, **config})
        entry = await ha.wait_entity(config_id)
        entity_id = entry["entity_id"]
        if meta.get("entity_id") and meta["entity_id"] != entity_id:
            await ha.cmd("config/entity_registry/update", entity_id=entity_id, new_entity_id=meta["entity_id"])
            entity_id = meta["entity_id"]
        update = {"labels": [labels[n] for n in meta.get("labels", [])],
                  "categories": {"automation": cats[meta["category"]] if meta.get("category") else None},
                  "area_id": areas[meta["area"]] if meta.get("area") else None,
                  "name": meta.get("name"), "icon": meta.get("icon"), "aliases": meta.get("aliases", []),
                  "hidden_by": "user" if meta.get("hidden") else None}
        await ha.cmd("config/entity_registry/update", entity_id=entity_id, **update)
        if meta.get("expose"):
            await ha.cmd("homeassistant/expose_entity", assistants=["conversation"], entity_ids=[entity_id],
                         should_expose=True)
        for _ in range(40):
            if entity_id in await ha.states():
                break
            await asyncio.sleep(0.25)
        current = (await ha.states()).get(entity_id, {}).get("state")
        if state in ("on", "off") and current != state:
            await ha.cmd("call_service", domain="automation", service=f"turn_{state}", target={"entity_id": entity_id})
        if meta.get("disabled"):
            await ha.cmd("config/entity_registry/update", entity_id=entity_id, disabled_by="user")
    print("seeded", len(only or AUTOMATIONS), "automations")


async def main(tokens_path: Path, action: str | None) -> None:
    async with aiohttp.ClientSession() as s:
        tokens = json.loads(tokens_path.read_text()) if tokens_path.exists() else await onboard(s)
        tokens_path.write_text(json.dumps(tokens))
        if "aie_llat" not in tokens:
            await app_user(s, tokens)
            tokens_path.write_text(json.dumps(tokens))
        if action == "seed":
            async with HA(s, tokens["llat"]) as ha:
                await seed(ha)


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1]), sys.argv[2] if len(sys.argv) > 2 else None))
