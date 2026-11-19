# Phase A5: curtains and occupancy predictions migration (audit and design)

Baseline: integration 0.8.0, App 0.5.0 (main `a7c1417`). This document was written
before the runtime changes and updated as the work landed. It records the real
boundaries of the two remaining subsystems, how they move into the App without a
second implementation of their rules, how control and configuration change owner,
and what the integration still has to provide for A6.

The principle is the one of A3.2 and A4: **one implementation** (the integration's
own modules, vendored byte-identical into the App and fingerprinted), **one
authority** (the integration's ownership record; the App never decides on its own
that it is in control), **nothing applied at a change of owner** (no cover moves
and no forecast changes because control moved), and **every way back** (release,
return, administrator service, lease expiry, new App process, no resume after a
restart).

## 1. Audit

### 1.1 Curtains

The curtain subsystem is the `curtains/` package of the integration (about 6 900
lines) plus the permission gate in `supervision.py` and a few helpers of
`occupancy.py` (friendly names) and `conflicts.py` (automation references).

| Part | Modules | Home Assistant coupling |
|---|---|---|
| Configuration and persistence | `config.py` (windows, cover layers, presets, cover presets, Global Modes assignments, rule settings; store `smart_occupancy.curtains`, 1.4), `rules.py` (the settings schema, scopes window > room > global > default, consistency), `migration.py` (the 0.6.6 centralisation review) | `Store`, area registry (names) |
| Executor | `manager.py` (one `CurtainManager`: operations per window, verification of reported positions, manual-change detection with a settle window, state store `smart_occupancy.curtains_state`, sun-angle calibration samples), `executor.py` (pure plans: `set_cover_position` / `open_cover` / `close_cover` per capability) | `hass.services.async_call("cover", …)` (one call site: `CurtainManager._send`), state subscriptions on the covers and the sun sources, `async_call_later` (verification timeout), `Context` per operation, entity registry (renames) |
| Automatic rules | `auto.py` (one `WindowRuntime` per window: evidence, hysteresis, holds, applied presets, retries, daily budget, minimum interval, start-up settle 120 s, 15-min safety re-check, the household phase), `logic.py` (pure `decide()`), `schedule.py` (pure household phases, Evening latch, late evenings, day plans), `modes.py` (pure resolution of a window's or cover's preset per condition), `environment.py` (readings validity, stuck sensors, temperatures, weather, glare, comfort band) | state subscriptions on sensors / lamps / sleep sources, `async_track_point_in_utc_time`, `hass.config.units.temperature_unit`, `sun.sun` attributes for display, the published room occupancy (`occupancy.result()` / `async_add_listener()`) |
| Geometry | `solar.py` (pure exposure, incidence, calibration of a compensated sun-angle sensor) | state reads of the sun sources |
| Conflicts and discovery | `conflicts.py` (automations that may move the covers, through scenes and scripts), `discovery.py` (candidates, proposals, capabilities from `supported_features`) | automation / script entity components, registries, `CoverEntityFeature` |
| Websocket API (the card) | `websocket.py`, `global_modes.py`, `schedule_page.py`, `migration.py` — 21 commands | `websocket_api`, voluptuous, dispatcher (`subscribe`) |

Consumers of the curtain manager outside the package: `rooms_api.py` and
`presence_api.py` (read which windows a room has, informational), `diagnostics.py`,
`__init__.py` (entity renames). Curtains read nothing from lighting and never read a
mood; they read the published occupancy of a window's room (for the "until the room
has been empty" hold and the glare rule).

Everything that can move a cover goes through `CurtainManager.begin()` /
`async_run()` after `assert_curtain_command_permitted()`: a confirmed manual preset
(`curtains/apply`) or an Automatic window's decision (`auto._act`).

### 1.2 Predictions

The forecast is `forecast.py` (runtime, 1 100 lines), `forecast_history.py` (pure
replay of recorded states through the occupancy engine and daily summaries),
`forecast_model.py` (pure model: hierarchical rates, Markov-like room transitions,
calibration and Brier scores), `forecast_settings.py` (pure settings),
`forecast_websocket.py` (5 commands) and `forecast_entities.py` (the native
entities).

| Part | Home Assistant coupling |
|---|---|
| Learning | `recorder.history.get_significant_states` in the Recorder's executor (one local day at a time, warm-up before and look-ahead after, significant changes only, compressed rows), `recorder.get_instance(hass).states_manager.oldest_ts` (bounds the backfill; not a stable API, falls back to probing), `async_add_executor_job` for the replay and the model build, a learning timer every `refresh_minutes`, `EVENT_HOMEASSISTANT_STARTED` |
| Live | the published room occupancy (`occupancy.result()`, listeners), state subscriptions on context entities and the workday sensor, a 5-minute tick |
| Persistence | `smart_occupancy.forecast_settings` (1), `smart_occupancy.forecast_days` (1: learned `HouseDay` summaries, dates without data, the Recorder bound, the last update) |
| Logging | the lighting controller's decision log (subjects `forecast:<room>`, `forecast:house`) |
| Entities | `sensor.smart_occupancy_<room>_forecast`, `…_forecast_confidence`, `binary_sensor.smart_occupancy_<room>_predicted`, `sensor.smart_occupancy_predicted_next_room`, `sensor.smart_occupancy_forecast_learning` (`forecast_entities.py`; they read `coordinator.forecast.room()`, `.next_room`, `.status`, `.days`) |
| Readers | `lighting_websocket.py` (the room editor's forecast summary), the room card |

Forecasts are observation only on every path; nothing in either runtime acts on
them.

## 2. Design

### 2.1 One implementation, vendored

`runtime_manifest.MODULES` now lists the curtain package (`curtains/*.py`,
sub-package paths), the five forecast modules and the three protocol modules
(`lighting_sync`, `curtain_sync`, `forecast_sync`) next to the lighting runtime
(74 files). `scripts/sync_lighting_runtime.py` mirrors sub-packages; both test
suites fail when a copy differs, and the one SHA-256 fingerprint is exchanged on
every bridge (a handover is refused when the App runs different code).
`RUNTIME_VERSION` is 1.1.0.

The host interface (`smart_lighting/backend/homeassistant/`) grew by exactly what
the new modules import: `components.cover.CoverEntityFeature`,
`const.UnitOfTemperature` / `PERCENTAGE` / `EntityCategory`,
`util.unit_conversion.TemperatureConverter`, `hass.config.units.temperature_unit`
(from `get_config`), `hass.is_stopping`, and a `components.recorder` placeholder
that refuses to be used (the App reads history through the API, see 2.4). The
existing test enumerates every `homeassistant` name the vendored modules import.

### 2.2 Curtains: exactly one manager commands

`curtain_bridge.py` is the one authority over cover commands, with its own record
(`.storage/smart_occupancy.curtains_ownership`: owner, epoch, hashed lease token
(30 s), session, sequence, versions, history) and mirror
(`.storage/smart_occupancy.curtains_mirror`), independent of lighting: curtains
are a separate physical-device domain and change owner separately.

* **Integration owns** (default, also after every install or upgrade): the
  integration's manager runs as before. The App runs the same manager in
  **shadow**: a fresh `CurtainManager` from an exact copy of the configuration and
  runtime state (`hello`), fed the same state changes, the same published occupancy
  and the same configuration changes; its service calls go to a recorder, never to
  Home Assistant. The integration streams every cover command it sends
  (`CurtainManager.command_observer`), every automatic decision (status changes and
  decision-log entries, `decision_observer`) and every configuration change.
* **App owns**: the integration's manager is **dormant** (stopped, fenced, its
  store locked: only the App's published copy is written to it) and the curtain
  editors are read-only (`managed_by_app`; reads, previews and simulations keep
  answering). The App's manager commands covers only through
  `smart_occupancy/curtains/bridge/execute`, accepted only from the owner's session
  on the same connection with the current epoch, the lease token and a strictly
  increasing sequence number, for `cover.set_cover_position` (whole-number position
  0–100, nothing else), `cover.open_cover` or `cover.close_cover` (no data), **one
  configured cover** (a layer of a configured window) per command, with the App's
  context (attribution unchanged). The App's websocket client has no
  `call_service`; the allowlist is enforced before anything is sent.

A dormant manager cannot command and a refused execute cannot reach a cover, so
no two engines ever command the same cover.

### 2.3 Curtains: handover, in-flight movements, rollback

Handover (reviewed in Curtains → Control, compare-and-swap on the epoch):

1. Readiness: bridge connected, identical fingerprints, configuration revisions
   equal, integration the owner, shadow running for at least 2 h with at least 5
   matched window decisions, no cover-command mismatch, no cover moving. Decision
   differences are shown (warning) and must be reviewed; only command mismatches
   block.
2. The integration fences its manager (no command leaves it: `_send` marks the item
   failed instead; an Automatic evaluation reports "prevented while control changes
   owner") and waits up to 10 s for running operations to finish. **If a cover is
   still moving the claim is refused** ("a cover is still being moved … try
   again"); the movement is never contradicted or replayed.
3. It flushes, exports configuration and state, stops and makes the manager dormant
   (no awaits between the copy and the switch).
4. The App backs the copy up (`/data/curtains/backups`), starts a new manager from
   it in `/data/curtains/store`, verifies the copy key by key and reports it. Start
   applies nothing: the handed-over state carries every manual hold, applied
   preset, move budget, deferred mapping change, calibration sample and history;
   the start-up settle period (120 s) passes before any rule may send.

While the App owns curtains it pushes configuration and state to the mirror about
two seconds after every save (each on its own). Rollback (release from the App,
`return`, the service `smart_occupancy.curtains_return_to_integration`, lease
expiry, a new App instance, no resume within 120 s after a Home Assistant restart
or integration reload): the epoch increases first (every later execute is
refused), in-flight App commands finish (bounded by the command timeout), then a
**new** integration manager is loaded from the mirror. An operation the App left
running is marked interrupted, its covers' late reports stay "ours" for the settle
window (not a manual change), and the windows are evaluated again only after the
settle period — the same reconciliation as after a restart. Nothing moves because
of the switch. A recovered App never takes ownership back by itself.

Two points from the independent review of this phase:

* **A return while a cover moves.** The mirror lags the App by about two seconds,
  so a movement the App started just before a `return` may not be in it. The App
  therefore refuses its own `return` (code `moving`) while a cover is being moved
  unless the user forces it (checkbox in Curtains → Control), and the integration's
  new manager treats every configured cover's reports as its own for the
  verification and settle window whenever the mirror state is younger than
  `RECENT_STATE_SECONDS` (5 s) or records a running operation
  (`CurtainManager.assume_moving`). The service and the lease-expiry fallback
  cannot wait for the App, so they rely on the second rule.
* **Execute timeout.** The integration's executor keeps verifying a command the
  cover platform accepts late; the App's manager receives the bridge's refusal
  after `COMMAND_SECONDS` (15 s) and marks that item failed. Both outcomes reach
  the household log; the covers are not commanded again by either path. This is
  the one accepted behavioural difference between the two runtimes.

A restored curtain export never switches a window to Automatic unreviewed: the
import review lists the windows the file would switch on, and they are restored as
Observing unless the restore confirms them (`confirm_automatic`).

### 2.4 Predictions: who learns and publishes

Forecasts never command anything on either side, so the forecast bridge
(`forecast_bridge.py`, record `.storage/smart_occupancy.forecast_ownership`,
mirror `.storage/smart_occupancy.forecast_mirror`) decides only who *produces*
them:

* **Integration owns** (default): its `ForecastManager` learns and publishes. A
  bridge session's `hello` carries its complete learned data (settings, daily
  summaries, dates without data, the Recorder bound, the last update — the
  validated export of 2.6) and its current view. The App's copy learns alongside
  (today and newly completed days only; the handed days are not re-fetched) and
  the comparison pairs both sides' views: a room agrees when status and prediction
  match and the probabilities are within 2 points.
* **App owns**: the integration's manager is dormant (no learning, no recompute)
  and shows what the App publishes — the view (every room's forecast, the predicted
  next room, the learning status) on the **same entities** (`apply_published`
  rebuilds `RoomForecast` objects, the entities are unchanged), and the App's
  learned data, which the dormant manager adopts so its read-only room view keeps
  its patterns and so that a rollback has the complete data. The forecast editors
  refuse with `managed_by_app`.

After a handover the native forecast entities shift to the App's view within about
two seconds (the App publishes right after it starts) and show no `today` summary
until the App's first learning run (a minute after its start) — the only visible
effect of the switch; the model and the learned days are identical on both sides.
A published view the dormant manager cannot apply is refused and not mirrored; a
mirrored export whose days do not validate for this model version is adopted
without them and the fallback logs how many were dropped.

**Recorder access in the App.** The App's manager uses the websocket command
`history/history_during_period` (bounded windows, one local day at a time,
`significant_changes_only`, `minimal_response`, `no_attributes`,
`include_start_time_state`) — the same rows the integration reads through the
Recorder's API, through a supported API, with no assumption about the database or
its backend. The Recorder's oldest timestamp has no websocket equivalent; the
integration reports it over the bridge, and without it the backfill probes the
configured window once, as the integration does. Replay and model building run in
the executor, off the event loop. Learned days are handed over, so a handover
never re-fetches history and never duplicates a day (days are keyed by date).

### 2.5 Configuration ownership and storage

| Subsystem | Before the handover | After the handover |
|---|---|---|
| Curtains | the integration's store `smart_occupancy.curtains` (1.4), edited with the card or the App (which forwards the same 21 commands) | the App's `/data/curtains/store` in the integration's format; the integration's store is the mirror (locked, written only by `publish`); the App runs the same 21 handlers locally; backups in `/data/curtains/backups`; export (configuration and state) and a reviewed import (`import/review` with changes and missing entities, then `import` with the revision) |
| Predictions | `smart_occupancy.forecast_settings` / `forecast_days`, edited with the integration's 4 write commands (forwarded) | the App's `/data/predictions/store`; the mirror holds the published learned data; the App runs the same handlers locally; backups in `/data/predictions/backups`; export and a reviewed import of learned data |

Every transfer validates with the shared parser (`CurtainConfig.from_dict`, the
forecast model's `HouseDay.from_dict` for every day), checks the revision
(`config_revision` / `learned_revision`), compares source and destination
(`verify_copy`), writes atomically (the host's `Store`) and keeps unknown
compatible fields the way the integration's parsers do. Entity renames while the
App owns curtains but is away are kept in the mirror and applied when it resumes
(or at a fallback); a connected App follows renames itself. Missing covers are
reported as "unavailable" by the executor and never guessed.

### 2.6 Learned data migration

`ForecastManager.export_learning()` is the validated document
(`forecast_sync.check_export`: schema, days, settings, dates without data, the
Recorder bound). `async_import_learning()` loads it (every day through the model's
own parser) and rebuilds the model. The integration suite proves that a fresh
manager importing an export produces the identical view (every room's
probabilities, prediction, confidence and explanation) at the same evaluation
time; the App's handover verifies that the loaded days and settings have the
source's revisions and reports the result. Rollback writes the App's last
published learned data into the integration's stores and restarts its manager
from them: nothing is retrained.

### 2.7 Floor plan

The optional floor plan links a drawn window opening to a curtain window
(`curtain_window_id` on the opening, stored only in the plan). Configure mode
offers the link and an orientation *suggestion* from the wall's outward normal and
the plan's north; applying it is a reviewed `window/update` of the curtain window's
bearing, never automatic, and shared walls get no suggestion. Live mode draws the
cover positions and shows mode, holds, condition, sun protection state and
warnings. The plan is stated to give direction only, not exposure or obstructions.

## 3. Behaviour that must not change

Every curtain rule of 0.8.0 (conditions and their precedence, holds and their
policies, Global Modes resolution, the Global Schedule, environment and thermal
rules, safety order: start-up settle, running movement, unavailable covers, busy
covers, competing automations, applied targets and retries, daily budget, minimum
interval); every forecast rule and the model mathematics; entity ids; the default
owner for both subsystems is the integration and installing or upgrading never
changes it; nothing moves and no forecast changes because control changes owner.

## 4. Verification

* **Host interface:** every name the new modules import exists; the vendored copies
  are identical and fingerprinted.
* **Integration (bridges):** default owner and the shadow copy; claim refusals
  (code, configuration, readiness, epoch, a moving cover); handover stops and fences
  the manager and sends nothing; fenced and allowlisted execute (services, data,
  targets, position range, context, lease, sequence); mirror and read-only editors;
  every rollback path with holds kept and nothing sent at the switch; restarts and
  resume; renames; administrators only. Forecasts: the export and view in `hello`,
  claim, published views on the entities, adopted learned data, rollback without
  retraining, no service call anywhere, export/import parity.
* **App (runtimes):** shadow never sends; the comparators; handover and commands
  only through execute with the App's context; losing control; release and return;
  reconnect resumes and a new process does not; the command routers follow the
  owner; export/import; the forecast runtime's bounded history requests and its
  comparison; no device path in the client.
* **Parity:** curtain scenarios recorded from the integration's manager in a real
  (test) Home Assistant and replayed through the App's host in virtual time (same
  cover commands at the same instants, same decisions, same editor answers);
  forecast export/import produces identical predictions.
* **Review follow-up:** a fallback parked on in-flight App commands is cancelled by
  an entry reload (no second manager); a refused execute consumes no sequence
  number; a return while a cover moves is refused unless forced; a restored export
  cannot switch a window to Automatic unreviewed.
* **Regression:** both complete suites (integration 1224 tests, App 248 + 1 expected
  failure, frontend 56) and the browser checks of every module. One integration test
  outside this phase, the decision log's daylight detail, depends on the real time
  of day and fails before sunrise on `main` as well.
* **Live (2026-11-19):** the end-to-end suites with the real App image against a
  throwaway Home Assistant 2026.11.1 (`tests/e2e/curtains_checks.py`: 81 + 5 + 3
  checks over a restart; `predictions_checks.py`), plus the A1 browser regression.
  **Not verified on a real Home Assistant OS installation.**

## 5. Remaining legacy dependencies (for A6)

While the App owns a subsystem the integration still provides:

1. **Fenced executors** (`lighting/bridge/execute`, `curtains/bridge/execute`) and
   the ownership records and mirrors. The App has no other path to a device, by
   design. A minimal companion integration keeps the executors and the records; the
   legacy controllers behind them can go.
2. **Occupancy**: the integration's engine (unless handed over) and the published
   results, which the curtain runtime reads like the lighting runtime does.
3. **Configuration entry options** (rooms, sensors, lights, categories, mood source)
   edited through the integration's validated commands. Moving them into App storage
   is the main A6 item.
4. **Native entities**: room sensors, the built-in mood selector, and the **forecast
   entities**, which now show the App's published view when the App produces the
   forecasts (`apply_published`); an App cannot create entities.
5. **Recorder bound**: the oldest recorded timestamp has no websocket API; the
   bridge reports it.
6. **Legacy dashboard cards** (read-only while the App owns a subsystem) and the
   options flow.
7. The forecast decision log: while the App produces the forecasts its
   observations go to the App's own bounded log (`/data/predictions/store`), not to
   the integration's lighting decision log.

Documented limits: the curtain comparison needs real cover commands, which are rare
(a few per day); the readiness therefore counts matched window *decisions* and asks
for zero command mismatches. The `history/history_during_period` command returns
the Recorder's compressed rows, so timestamps are the Recorder's own; the App has no
access to the Recorder's purge settings and relies on the integration-reported
bound or probing. Nothing in A5 removes the integration, its executors or its
rollback paths — that is A6.
