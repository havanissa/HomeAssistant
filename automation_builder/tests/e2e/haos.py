"""Drive a Home Assistant OS **test** installation for the end-to-end checks (never a production system).

Everything goes through Home Assistant's websocket command ``supervisor/api`` with an administrator's long-lived
token, the way the frontend talks to the Supervisor. Settings (environment):

    AB_HAOS_URL      base URL of the test Home Assistant (default http://127.0.0.1:28080, the VirtualBox NAT port)
    AB_HAOS_TOKENS   JSON file with {"llat": "<long-lived access token of an administrator>"}
    AB_HAOS_SLUG     the App's slug in that installation, e.g. 1a2b3c4d_automation_builder

Commands:

    python haos.py sup <endpoint> [get|post|delete] ['<json body>'] [timeout]   one Supervisor API call
    python haos.py env                 shell "export" lines for e2e_checks.py / e2e_02_checks.py (a new Ingress session)
    python haos.py app stop|start|restart|kill      the App (kill: SIGKILL of the container, through the Supervisor)
    python haos.py core restart|stop|start          Home Assistant Core (waits until it runs again)
    python haos.py supervisor restart               the Supervisor itself
    python haos.py wait                             until Core runs and the App answers through Ingress

See docs/AB-0.2 ("Home Assistant OS test procedure") for the whole sequence.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys
import time
from typing import Any
import urllib.error
import urllib.request

import aiohttp

URL = os.environ.get("AB_HAOS_URL", "http://127.0.0.1:28080").rstrip("/")
TOKENS = Path(os.environ.get("AB_HAOS_TOKENS", "tokens.json"))
SLUG = os.environ.get("AB_HAOS_SLUG", "")


def token() -> str:
    return json.loads(TOKENS.read_text())["llat"]


async def _ws(commands: list[dict[str, Any]], timeout: float = 300) -> list[dict[str, Any]]:
    async with aiohttp.ClientSession() as session, session.ws_connect(f"{URL}/api/websocket", max_msg_size=0) as ws:
        await ws.receive_json()
        await ws.send_json({"type": "auth", "access_token": token()})
        assert (await ws.receive_json())["type"] == "auth_ok"
        out = []
        for i, command in enumerate(commands, 1):
            await ws.send_json({"id": i, **command})
            while True:
                message = await ws.receive_json(timeout=timeout)
                if message.get("id") == i and message.get("type") == "result":
                    out.append(message)
                    break
        return out


def ws(*commands: dict[str, Any], timeout: float = 300) -> list[dict[str, Any]]:
    return asyncio.run(_ws(list(commands), timeout))


def sup(endpoint: str, method: str = "get", data: Any = None, timeout: int | None = None) -> Any:
    """One Supervisor API call. Returns the result, or raises with the Supervisor's error."""
    command: dict[str, Any] = {"type": "supervisor/api", "endpoint": endpoint, "method": method}
    if data is not None:
        command["data"] = data
    if timeout:
        command["timeout"] = timeout
    reply = ws(command, timeout=(timeout or 60) + 30)[0]
    if not reply.get("success"):
        raise RuntimeError(f"{method} {endpoint}: {reply.get('error')}")
    return reply.get("result")


def http(path: str, method: str = "GET", body: Any = None, headers: dict[str, str] | None = None, timeout: float = 30) -> tuple[int, Any]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(f"{URL}{path}", data=data, method=method, headers={
        "Content-Type": "application/json", "Authorization": f"Bearer {token()}", **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw, status = response.read(), response.status
    except urllib.error.HTTPError as err:
        raw, status = err.read(), err.code
    try:
        return status, json.loads(raw) if raw else None
    except ValueError:
        return status, raw.decode(errors="replace")


def core_running() -> bool:
    try:
        status, config = http("/api/config", timeout=5)
    except (OSError, ValueError):
        return False
    return status == 200 and isinstance(config, dict) and config.get("state") == "RUNNING"


def wait_core(timeout: float = 600) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if core_running():
            return True
        time.sleep(2)
    return False


def ingress(slug: str = "") -> tuple[str, str]:
    """``(Ingress base URL of the App, cookie)`` with a fresh Ingress session, as a logged-in browser has."""
    info = sup(f"/addons/{slug or SLUG}/info")
    session = sup("/ingress/session", "post", {})["session"]
    return f"{URL}{info['ingress_url'].rstrip('/')}", f"ingress_session={session}"


def app_ready(slug: str = "", *, timeout: float = 240) -> bool:
    """Until the App answers through Ingress and is connected to Home Assistant."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            base, cookie = ingress(slug)
            request = urllib.request.Request(f"{base}/api/overview", headers={"Cookie": cookie})
            with urllib.request.urlopen(request, timeout=10) as response:
                overview = json.loads(response.read())
            if overview["connection"]["home_assistant"]["connected"]:
                return True
        except Exception:  # noqa: BLE001 - anything means "not yet"
            pass
        time.sleep(3)
    return False


def app(action: str, slug: str = "") -> None:
    """stop / start / restart the App through the Supervisor.

    ``kill`` is a stop, too: the Supervisor offers no SIGKILL, and Home Assistant OS gives an App (also the SSH
    App) no access to Docker. A stop ends the App's process within a second (SIGTERM), also in the middle of a
    request, which is what an update or a restart of the App does to a running deployment.
    """
    slug = slug or SLUG
    sup(f"/addons/{slug}/{'stop' if action == 'kill' else action}", "post", {}, 180)


def core(action: str) -> None:
    try:
        sup(f"/core/{action}", "post", {}, 300)
    except Exception as err:  # noqa: BLE001 - the websocket closes while Core restarts: expected
        if action == "start":
            raise
        print(f"(connection closed during core {action}: {type(err).__name__})", file=sys.stderr)
    if action != "stop":
        time.sleep(5)
        if not wait_core():
            raise RuntimeError("Home Assistant Core did not come back")


def supervisor(action: str) -> None:
    try:
        sup(f"/supervisor/{action}", "post", {}, 120)
    except Exception as err:  # noqa: BLE001
        print(f"(connection closed during supervisor {action}: {type(err).__name__})", file=sys.stderr)
    time.sleep(20)
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        try:
            sup("/supervisor/info")
            return
        except Exception:  # noqa: BLE001 - not back yet
            time.sleep(5)
    raise RuntimeError("the Supervisor did not come back")


def main(argv: list[str]) -> int:
    what = argv[0] if argv else ""
    if what == "sup":
        result = sup(argv[1], *(argv[2:3] or ["get"]), json.loads(argv[3]) if len(argv) > 3 else None,
                     int(argv[4]) if len(argv) > 4 else None)
        print(json.dumps(result, indent=1))
    elif what == "env":
        base, cookie = ingress()
        version = (http("/api/config")[1] or {}).get("version", "")
        print(f"export AB_E2E_TARGET=haos AB_E2E_HA={URL} AB_E2E_INGRESS={base} AB_E2E_COOKIE='{cookie}' "
              f"AB_E2E_HA_VERSION={version}")
    elif what == "app":
        app(argv[1])
    elif what == "core":
        core(argv[1])
    elif what == "supervisor":
        supervisor(argv[1])
    elif what == "wait":
        ok = wait_core() and app_ready()
        print("ready" if ok else "not ready")
        return 0 if ok else 1
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
