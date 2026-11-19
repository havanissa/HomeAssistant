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

## Shared automation runtime (Phase A4 lighting, Phase A5 curtains and forecasts)

`backend/smart_occupancy/` is a byte-identical copy of the integration's
automation runtime (`custom_components/smart_occupancy/runtime_manifest.py`
lists the modules: the lighting controller, the `curtains/` package, the
forecast modules and the bridge protocols); only its `__init__.py` is the App's
own. After changing any
of those modules in the integration, run from the repository root:

```sh
python3 scripts/sync_lighting_runtime.py           # copy
python3 scripts/sync_lighting_runtime.py --check   # what the tests check
```

The modules import Home Assistant's API; in the App those names come from the
**host interface** `backend/homeassistant/` (documented in its `__init__.py`).
`tests/test_host_interface.py` fails when a vendored module imports a name the
host does not provide. `backend/smart_lighting/lighting_host.py` connects one
controller to the App's websocket client, `lighting_runtime.py` runs the bridge
session, shadow and owner, `lighting_compare.py` compares, `lighting_api.py`
routes the interface's lighting commands. Phase A5 adds the same four modules
for curtains (`curtains_host.py`, `curtains_runtime.py`, `curtains_compare.py`,
`curtains_api.py`) and forecasts (`forecast_host.py` — history through
`history/history_during_period` —, `forecast_runtime.py`, `forecast_compare.py`,
`forecast_api.py`).

**Behavioural parity.** The integration's suite records scenarios with its own
controller (`tests/test_lighting_parity*.py`, recorder in
`tests/lighting_parity.py`): the starting picture, every event, every command
with its outcome, every decision and the answers to the read-only editor
commands. `LIGHTING_RECORD=1` (integration suite) writes them to
`tests/lighting_scenarios/*.json.gz`; without it the integration suite checks
that the recordings are still what the controller does.
`tests/test_lighting_parity.py` replays each one through the App's host in
virtual time and requires the same commands, decisions and answers. Re-record
after an intended behaviour change and review the difference.

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
`lighting_checks.py` (Phase A4) needs the integration 0.8.0 with its lighting
configuration reset (no `.storage/smart_occupancy.lighting*`), the A3.1 seed
and the stack started with `LIGHTING_MIN_SHADOW_SECONDS=60
LIGHTING_MIN_COMMANDS=3`: configuration through the App while the integration
controls lighting, shadow comparison, the browser handover, the App controlling
`light.bed_light` through the integration, read-only legacy editors, a crash of
the App container (fallback without a light command), the restarted App in
shadow, a manual override kept across "Return control"; `--phase
restart-check` after restarting Home Assistant (the same App resumes) and
`--phase finish` (the administrator service).
`curtains_checks.py` and `predictions_checks.py` (Phase A5) need the
integration 0.9.0 with the curtain stores reset (no
`.storage/smart_occupancy.curtains*`), the demo covers and the stack started
with `CURTAINS_MIN_SHADOW_SECONDS=60 CURTAINS_MIN_DECISIONS=1` or
`FORECAST_MIN_SHADOW_SECONDS=60 FORECAST_MIN_MATCHES=1`; both have `--phase
restart-check` and `--phase finish` like the lighting suite.
Primary targets: desktop (1440, 1920 px) and tablets (768 portrait, 1024 and
1366 landscape, with touch). Phone sizes (360, 390 px, 740 landscape) are
kept as a regression check of the fallback layout. It fails on any Home
Assistant service call during the run.
