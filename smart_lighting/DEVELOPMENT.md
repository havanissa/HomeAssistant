# Development

Layout of the App folder:

| Path | What |
| --- | --- |
| `config.yaml`, `Dockerfile`, `requirements.txt`, `rootfs/` | App package (built on the device by the Supervisor) |
| `backend/smart_lighting/` | Python backend (aiohttp): Ingress web server, read-only Home Assistant client |
| `backend/smart_lighting/static/` | **Built** web interface (committed, so the device build needs no Node.js) |
| `frontend/` | Web interface source (Lit + TypeScript + Vite) |
| `tests/` | Backend and package tests (pytest) |
| `tests/e2e/` | End-to-end stack: fake Supervisor + real App container + Playwright checks |

## Frontend

```sh
cd frontend
npm ci
npm run build   # type-checks, then writes backend/smart_lighting/static/
```

Always commit the rebuilt `static/` folder together with source changes;
`tests/test_package.py` fails when the build is older than the sources or
contains stale files.

## Backend tests

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements_test.txt
.venv/bin/pytest          # from this folder
```

The tests run the backend against a fake Home Assistant websocket API that
records every command, and check that only read-only commands are sent.

## Running locally against a Home Assistant

```sh
SL_HA_WS_URL=ws://127.0.0.1:8123/api/websocket SL_HA_TOKEN=<long-lived token> \
SL_SUPERVISOR_URL= SL_ALLOWED_CLIENTS=127.0.0.1,::1 SL_DEV=1 \
PYTHONPATH=backend python3 -m smart_lighting
```

Then `cd frontend && npm run dev` serves the interface with hot reload and
proxies `/api` to the backend on port 8099.

## End-to-end tests

`tests/e2e/stack.sh` builds the real image and starts it on a Docker network
with the HAOS addresses (fake Supervisor at 172.30.32.2, reproducing Core's
and the Supervisor's Ingress headers). It needs a Home Assistant reachable
from that network (for example bound to the network's gateway 172.30.32.1).

```sh
HA_URL=http://172.30.32.1:18123 HA_TOKEN=<token> tests/e2e/stack.sh up
python -I tests/e2e/browser_checks.py --ha-url http://127.0.0.1:18123 --ha-token <token> \
    --ha-config <HA config dir> --out /tmp/e2e
tests/e2e/stack.sh down
```

`browser_checks.py` drives the interface inside the sidebar-panel iframe.
Primary targets: desktop (1440, 1920 px) and tablets (768 portrait, 1024 and
1366 landscape, with touch). Phone sizes (360, 390 px, 740 landscape) are
kept as a regression check of the fallback layout. It fails on any Home
Assistant service call during the run.
