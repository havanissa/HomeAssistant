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

## The engines (Phase A6: the App is the only copy)

`backend/smart_occupancy/` is **the** implementation of every engine: room
occupancy (`occupancy_core/`), lighting, the `curtains/` package, the forecast
modules, the rooms and presence API handlers, the diagnostics snapshot
(`snapshot.py`) and the problem tracker. `runtime_manifest.py` lists the
modules. Since 1.0.0 the integration ships none of them: it is the *companion*
(`custom_components/smart_occupancy/`: entities, the fenced executor, records,
cards). The engines' 1 199 Home Assistant tests still run from the repository
root (`python scripts/run_tests.py tests/engine`) through a frozen copy of the
0.9.0 glue in `tests/engine/custom_components/smart_occupancy/`, whose package
appends this folder to its path.

Until every installation has migrated, the modules listed in
`runtime_manifest.py` must stay byte-identical to 0.9.0 (its bridges check the
runtime fingerprint before the wizard's *Transfer control* step);
`app_manifest.py` lists the App-only modules and pins `FINGERPRINT_0_9_0`, and
`tests/test_host_interface.py` fails when a listed module changes. New engine
code goes into an App-only module.

Phase A6.2 (App 0.9.0) added three App-only engine modules (listed in
`app_manifest.py`): `brightness_delays.py` (pending Delay On / Delay Off of
brightness matrix cells; the controller's two executors consult its gate
before they send, `async_delay_due` revalidates when a delay ends),
`light_identify.py` (Identify: snapshot, full brightness, restore through the
controller's `_send`, superseded by newer commands; crash journal) and
`light_inspector.py` (the read-only `light/inspect` and the `light/identify`
commands). The App's `zones_api.py` is Presence's zone API: it composes the
existing zone commands (detection keys only), so there is still one zone
record (`zones.ZoneConfig` in the lighting store) and one zone engine
(`zone_runtime.ZoneManager`). Design and limits: `docs/A6.2-zones-inspector-delays.md`.

The one file that exists twice is the companion protocol:
`custom_components/smart_occupancy/protocol.py` (the source) and
`backend/smart_lighting/companion_protocol.py` (the App's copy, because the
container cannot read the integration). After changing it, run from the
repository root:

```sh
python3 scripts/check_protocol.py           # what both test suites check
python3 scripts/check_protocol.py --sync    # copy
```

The modules import Home Assistant's API; in the App those names come from the
**host interface** `backend/homeassistant/` (documented in its `__init__.py`).
`tests/test_host_interface.py` fails when an engine module imports a name the
host does not provide. Per subsystem the App has a host (`lighting_host.py`,
`curtains_host.py`, `forecast_host.py`), a runtime (`*_runtime.py`: the 0.9.0
bridge session with shadow and owner, and since A6 the **companion mode**
through `companion_mode.py`), a comparator (`*_compare.py`) and an API
(`*_api.py`). `core_config.py` is the App's configuration authority
(`/data/config/core.json`), `core_host.py` the host of the configuration-level
engines (snapshot, rooms and presence handlers, the system view),
`companion_client.py` the session with the companion, `migration.py` the
eight-step wizard and `onboarding.py` the setup of a new installation.
`tests/fake_companion.py` is the companion's side for the App tests
(`tests/test_companion_app.py`).

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
`a62_checks.py` (Phase A6.2) runs right after `system_checks.py --phase fresh`
in the same stack: a zone created and edited through Presence's commands, the
light inspector, Identify on a light that is on and off (through the
companion's executor, exactly two commands, no override), a brightness cell's
Delay On and Delay Off on `light.bed_light`, and the new screens at desktop,
tablet and phone sizes without a service call.
Primary targets: desktop (1440, 1920 px) and tablets (768 portrait, 1024 and
1366 landscape, with touch). Phone sizes (360, 390 px, 740 landscape) are
kept as a regression check of the fallback layout. It fails on any Home
Assistant service call during the run.
