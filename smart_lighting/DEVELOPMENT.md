# Development

Layout of the App folder:

| Path | What |
| --- | --- |
| `config.yaml`, `Dockerfile`, `requirements.txt`, `rootfs/` | App package (built on the device by the Supervisor) |
| `backend/smart_lighting/` | Python backend (aiohttp): Ingress web server, allowlisted Home Assistant client, Rooms service (`rooms.py`), Presence service (`presence.py`) |
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

`npm test` runs the frontend unit tests (Vitest, `frontend/tests/`): floor
plan geometry, shared walls, openings, coordinates, calibration, undo/redo and
suggestions, and the presence logic (`frontend/src/presence/model.ts`: countdowns, timing
inheritance, the visit chain, the timeline). The floor plan's coordinate system and data format are described
in `backend/smart_lighting/floorplan_model.py` and `frontend/src/floorplan/model.ts`.

Always commit the rebuilt `static/` folder together with source changes;
`tests/test_package.py` fails when the build is older than the sources or
contains stale files.

## Backend tests

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements_test.txt
.venv/bin/pytest          # from this folder
```

The tests run the backend against a fake Home Assistant websocket API that
records every command (with a small stand-in for the integration's room API),
and check that only allowlisted commands are sent and that configuration
commands are only sent for an administrator, from the App's own page.

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

`browser_checks.py` drives the interface inside the sidebar-panel iframe and
writes nothing (A1 regression, every viewport). `rooms_checks.py` (Phase A2)
then edits rooms against the integration (0.6.9) on that Home Assistant:
discovery, assignment, review and save, the integration's own options flow,
stale writes, confirmations, the leave guard, connection loss, backups and
restore, layouts; `--phase restart-check` after restarting the App container
and Home Assistant checks persistence. It needs the Kamar Norður room seeded
through the options flow, demo lights and covers and a few template sensors.
`floorplan_checks.py` (Phase A2.1) draws, links, places, reviews an adjacency
through the Rooms editor, watches live states and checks sessions, backups,
export/import and that Home Assistant is unchanged apart from that reviewed
adjacency; it needs a fresh App data folder and has a `--phase restart-check`.
`presence_checks.py` (Phase A3.1) needs the integration 0.6.10 and Kamar
Norður seeded with the template radar (following `input_boolean.nobara_pc`,
which the script moves with state writes, not service calls) and the door;
it checks the Presence dashboard, workspace, timing, sensor changes, a stale
write, the timeline, global defaults, diagnostics and the floor plan overlay,
and has a `--phase restart-check`.
Primary targets: desktop (1440, 1920 px) and tablets (768 portrait, 1024 and
1366 landscape, with touch). Phone sizes (360, 390 px, 740 landscape) are
kept as a regression check of the fallback layout. It fails on any Home
Assistant service call during the run.
