# Phase A4: lighting, moods and switches migration (audit and design)

Baseline: integration 0.7.0, App 0.4.0 (main `f3a4f5f`). This document was written
before any runtime change and updated as the work landed. It records the real
boundaries of the lighting subsystem, how it moves into the App without a second
implementation of its rules, and how control and configuration change owner.

## 1. Audit

### 1.1 Size and coupling

The lighting subsystem is about 20 000 lines in the integration. The executor
`controller.py` (3 568 lines) owns one `LightingController` per config entry that
composes every other part:

| Part | Modules | Home Assistant coupling |
|---|---|---|
| Configuration and persistence | `lighting_config.py` (store `smart_occupancy.lighting`, schema 1.9, migrations), `models.py` (options: rooms, lights, categories, mood helpers) | `Store` |
| Moods and profiles | `mood.py` (three helpers), `profiles.py` (Occupied/Vacant tables, inheritance, fallback, `*`), `adaptive.py` (external brightness / colour temperature sources) | state reads, state subscriptions, automation/script components (conflicts) |
| Planning | `planner.py`, `preliminary.py`, `arbitration.py` (one winning rule per target), `brightness.py`, `ambient.py`, `ambient_levels.py`, `fixtures.py`, `targets.py`, `lighting.py` (capabilities, groups, labels) | registries, state reads |
| Daylight | `daylight.py` (pure), `daylight_monitor.py` | state subscriptions, sun (astral) |
| Zones and shared spaces | `zones.py`, `radar.py` (pure), `zone_runtime.py` | state subscriptions, timers |
| Room Supervisor | `supervisor.py`, `supervision.py` (permission gates) | state reads, timers |
| Switches | `switches.py` (pure), `switch_inputs.py` (event entities, sensors, MQTT, device triggers), `switch_discovery.py`, `switch_control.py` | state subscriptions, MQTT, device triggers, device automations, registries |
| Priority Inputs | `priority.py` (pure), `priority_runtime.py` | state subscriptions, timers |
| Decisions and explanations | `decisions.py`, `lighting_decisions.py` | timers |
| Conflicts | `conflicts.py` | automation and script components |
| Websocket API (cards) | `websocket.py` (lighting part), `profile_websocket.py`, `brightness_websocket.py`, `zone_websocket.py`, `switch_websocket.py`, `priority_websocket.py`, `preliminary_websocket.py` — 50 commands | `websocket_api`, voluptuous |

The controller's runtime import closure is 37 modules. Their whole Home Assistant
surface is: `hass.states.get/is_state`, `hass.services.async_call` (three call sites:
`controller._send`, `controller._send_for_person`, `switch_control._call`), `hass.bus`
(state changes, `automation_reloaded`, `script_reloaded`, start-up), the area / device /
entity / label registries, `async_track_state_change_event`, `async_call_later`,
`async_track_point_in_utc_time`, `Store`, `dt_util`, `helpers.sun`, MQTT subscribe,
device-trigger initialisation, `device_automation` trigger listing, the automation and
script entity components (for conflict detection) and `Context`.

### 1.2 Dependencies on other modules

The controller uses the coordinator for exactly: `config` (options),
`occupancy.result()`, `occupancy.async_add_listener()`, `occupancy.async_set_preliminary()`,
`occupancy.async_set_holds()` (zones), `occupancy.async_note_activity()` (switches),
`async_schedule_refresh()`, `data` (validation problems, for the editor), `forecast`
(the editor's room summary) and `settled`.

Consumers of the lighting controller outside lighting: the room sensors
(`sensor.py`: control mode and diagnostics, daylight decision, last activation),
`diagnostics.py`, `rooms_api.py` and `presence_api.py` (read the lighting
configuration of a room), `forecast.py` (writes its own entries into the lighting
decision log) and the options flow (profile and category steps write the lighting
store). Curtains do not read lighting.

### 1.3 Persistence

* `.storage/smart_occupancy.lighting` (1.9): the whole lighting configuration as one
  document (profiles, rooms, switches, zones, matrices, daylight, ambient, priority
  inputs, supervisor, shared spaces, templates).
* `.storage/smart_occupancy.lighting_state`: runtime state (per-room runtime with
  manual overrides, preliminary stages, zone ownership; adaptive, daylight, ambient,
  switches, decision log, zones, priority inputs).
* Config entry options: rooms (sensors, lights), light categories, mood helpers.
  Categories are written by `lighting/category/set|remove` (integration reload).

### 1.4 Command attribution

Manual changes are recognised from the `Context` of light state changes: a user id
means the UI; known contexts are our own commands; a recent physical switch press
attributes unparented changes to the wall switch; an expected echo (same direction,
shortly after our command) is ours. Every command is sent with a context the
controller created *before* the call.

## 2. Design

### 2.1 One implementation, vendored byte-identical

The App runs the integration's own lighting modules. `scripts/sync_lighting_runtime.py`
copies the runtime closure (listed in `runtime_manifest.py`, itself part of the copy)
into `smart_lighting/backend/smart_occupancy/`. Both test suites fail when one byte
differs, and the SHA-256 fingerprint of the set is exchanged on the bridge: a
handover is refused when the App runs different lighting code than the integration.

The modules keep importing `homeassistant.*`. In the App these names come from a
**host interface**, `smart_lighting/backend/homeassistant/`: a documented, tested
implementation of exactly the subset listed in 1.1, on top of the App's websocket
connection (states and contexts from `state_changed`, registries, `subscribe_trigger`
for device triggers, `mqtt/subscribe` for MQTT inputs, `automation/config` for
conflict detection, astral for the sun, JSON files for `Store`, asyncio timers). A
test enumerates every `homeassistant` name the vendored modules import and fails when
the host interface lacks one.

The integration was refactored only where the closure would otherwise drag in
unrelated modules: the lighting websocket handlers moved from `websocket.py` to
`lighting_websocket.py`, two timing helpers moved out of `config_flow.py`, and
`health.py` imports the coordinator for type checking only. No rule changed.

### 2.2 Exactly one lighting controller commands

Lighting ownership is one record for **every** lighting command path (automatic
lighting, vacancy, preliminary lights, switches, profile tests, supervisor
corrections, priority inputs, global adjustment). The integration is the only
authority (`lighting_bridge.py`, `.storage/smart_occupancy.lighting_ownership`):
owner, epoch, hashed lease token (30 s), session, sequence, fingerprint, configuration
revision and history — the A3.2 rules.

* **Integration owns** (default, also after every install or upgrade): the
  integration's controller runs as before. The App runs the same controller in
  **shadow**: its service calls go to a recorder, never to Home Assistant.
* **App owns**: the integration's controller is **stopped** (cold standby: it keeps
  only its configuration for read access) and its editors are read-only. The App's
  controller sends commands only through `smart_occupancy/lighting/execute`, which the
  integration accepts only from the owner's session, on the same connection, with the
  current epoch, the lease token and a strictly increasing sequence number, for an
  allowlisted service (`light.turn_on|turn_off|toggle`, `switch.turn_on|turn_off|toggle`
  for configured relays, and `scene.turn_on`, `script.turn_on`,
  `select.select_option` only for entities used by configured switch mappings), for
  targets in the App's configuration, with allowlisted data keys. The App supplies the
  context id, so attribution works exactly as before. There is no other path from the
  App to a device: the App's websocket client has no `call_service`.

A stopped controller cannot command, so the two cannot compete; a refused execute
cannot reach a device, so a fenced App cannot either.

### 2.3 Handover and rollback

Handover (reviewed, administrator, compare-and-swap on the epoch):

1. The App's readiness checks pass (2.5).
2. The integration fences its controller (no new command batches), waits for its
   in-flight commands (bounded), stops it and exports its configuration document and
   runtime state.
3. The App starts a fresh controller from exactly that configuration and state. Start
   takes the current mood, occupancy and lights as the baseline and applies nothing,
   as after a Home Assistant restart. Manual overrides, preliminary stages, zone
   ownership, temporary room moods and priority claims are part of the state.

While the App owns lighting it pushes its configuration and its runtime state to the
integration's mirror (`.storage/smart_occupancy.lighting_mirror`) about two seconds after
every change (configuration and state separately: a refused configuration never holds
back the state). The integration's copy of the configuration is locked meanwhile: only
the App's published copy is written to it, so an editor or options step that started
before the handover cannot change it behind the App's back.

Rollback (`release` from the App, or the admin service
`smart_occupancy.lighting_return_to_integration`), lease expiry, a new App instance,
or no resume after an HA restart: the integration bumps the epoch first (every later
execute is refused) and starts a new controller from the mirror (its own forecast entries
in the decision log are kept). A return from the App's interface while it controls
lighting is a `release`: the App fences its controller, waits for its commands and hands
over its final configuration and state with the release. When the App is gone (lease
expiry, a new App process) the mirror is at most about two seconds old. Again nothing is
applied at the switch. If the new controller cannot be loaded, nothing commands lights,
the status says so (`controller: failed`) and handovers are refused until the integration
is reloaded. Entity ids renamed while the App owns lighting but is not connected are kept
in the mirror and given to the App when it resumes (or applied when the integration takes
over); a connected App follows renames itself. A recovered App never
takes ownership back by itself; only the same App process may resume within the
grace period after an HA restart or integration reload.

### 2.4 Configuration ownership

The lighting configuration has one authority at a time: the owner of lighting
control. Before the handover it is the integration's store, edited with the existing
cards or the App (which forwards the same commands). The handover copies it (with a
backup in `/data/lighting/backups`, schema validation by the shared parser, revision
check and a key-by-key comparison of source and destination) into the App's store
`/data/lighting/` in the integration's own format. Afterwards the App runs the same
50 command handlers locally against its own controller, and the integration refuses
lighting writes with "managed by the Smart Lighting App". The mirror keeps the
integration able to take over and is the rollback export. The configuration entry
options (rooms, sensors, lights, categories, mood source) stay in the integration and
are edited through its validated commands, as since A2.

### 2.5 Shadow comparison and readiness

The integration streams every command it sends (purpose, area, service, targets,
data, context) and its decision log entries. The App's shadow controller records the
commands it would send. A command matches when the same purpose, area, service,
target and data appear on both sides within 3 s. The report groups by purpose and
trigger and lists the unmatched commands with both sides' decisions. Readiness:
bridge connected, protocol supported, identical fingerprint, configuration revisions
equal, integration the owner, shadow running for at least 2 h with at least 10
matched commands, no unexplained mismatch in that window.

### 2.6 Occupancy

The App's controller consumes the published occupancy (the integration's engine or,
after the A3.2 handover, the App's own) — the same input as the integration's
controller. Its occupancy inputs (preliminary signals, zone holds, switch activity)
go to the integration's occupancy manager over the bridge while the App owns lighting.
Lighting works with either occupancy owner.

### 2.7 Universal moods

Moods are names: the profiles are keyed by name, nothing is hard-coded. The mood
source can be the three helpers (compatibility), a single `select`/`input_select`
entity, or the built-in mood selector — a `select` entity of the integration whose
options are moods defined in the App. A new installation needs no helper.

## 3. Behaviour that must not change

Every lighting rule of 0.7.0; start-up settling; nothing applied on start, upgrade or
handover; manual overrides; existing room Automatic/Observing choices; entity ids.
Default lighting owner is the integration; installing or upgrading never changes it.

## 4. Verification

* **Behavioural parity.** 21 scenarios recorded from the integration's own controller in a
  real (test) Home Assistant with simulated Zigbee2MQTT-like devices (PIR and radar visits,
  possible occupancy, vacancy and Vacant profiles, mood changes, Manual mood, manual and
  unattributed changes and their expiry, offline and failing lights, daylight, ambient
  levels and brightness matrices, zones, relays and shared spaces, Supervisor corrections,
  Priority Inputs, global brightness, event-entity and Zigbee2MQTT switches, conflicting
  automations, five randomized sequences). The App replays each through its host in virtual
  time and must send the same commands (with the same outcomes) at the same instants, make the
  same decisions and answer the read-only editor commands the same way. Items of one instant
  are compared as a set: timers of the integration's occupancy engine and of the App's
  lighting run in two processes.
* **Fidelity issues the replay found and fixed:** Home Assistant queues events fired during
  another event's dispatch (the host does too); tasks start eagerly; a start-up event must not
  be fired for a controller started on a running Home Assistant; the extended registry entries
  (device classes) are needed; equal configurations must have equal revisions (2 and 2.0).
* **Found live (end-to-end, App container):** events reach the App a little after they fired in
  Home Assistant (Home Assistant also coalesces websocket messages). A debounce started from the
  event's arrival could end after the integration's reaction to the same event had already
  arrived, so the shadow saw the light at its new level and sent nothing (a command mismatch).
  The host now starts such timers from the event's time in Home Assistant (at most two seconds
  earlier; otherwise from now), as Home Assistant does. The same held for the bridge's own events
  that start timers (a configuration change such as switching a room to Automatic, an occupancy
  result): they now carry a full-precision `fired` stamp and the shadow handles them at that
  instant.
* **Bridge (integration):** fencing (epoch, lease, sequence, session), the allowlist, the
  mirror, inputs, options, handover with commands in flight, every way back (release, return,
  service, lease expiry, new App instance), restart and reload (dormant, resume, no resume),
  read-only editors, administrators only.
* **Runtime (App):** shadow, handover, commands only through `execute`, losing control,
  release, return, reconnect and resume, the command router in both modes.
* **Live, on a throwaway Home Assistant 2026.11.1:** shadow comparison on real events (commands
  and decisions matched), a handover, the App switching `light.bed_light` through the
  integration, and the end-to-end suite `smart_lighting/tests/e2e/lighting_checks.py` with the
  real App image. **Not verified on a real Home Assistant OS installation.**

## 5. Remaining legacy dependencies (A5/A6)

While the App controls lighting, the integration still provides:

1. **The fenced executor** (`lighting/bridge/execute`): every light, relay, scene, script and
   select command of the App. Once the legacy controller is removed, ownership checks and the
   allowlist can stay in a minimal companion integration, or the App can call services
   directly.
2. **The ownership record and the mirror** (rollback target). They become unnecessary when the
   legacy controller is removed.
3. **Occupancy**: the occupancy engine (unless handed over in A3.2) and the published results;
   the occupancy manager also receives the App's lighting inputs (possible occupancy signals,
   zone holds, switch activity) and forwards them to the App's engine.
4. **Configuration entry options**: rooms, their sensors and lights, occupancy timing, light
   types (categories) and the mood source are still stored in the integration's options and
   edited through its validated commands (`rooms/*`, `presence/*`, `category/*`,
   `mood_source/set` → `lighting/bridge/options`). Moving them into App storage is the main A5
   item.
5. **Native entities**: the room sensors (occupancy, lighting state, daylight) and the
   built-in mood selector `select.smart_lighting_mood`; the lighting state sensor shows the
   App's published view. A minimal companion integration needs these (an App cannot create
   entities).
6. **The integration's own problem list** (validation of rooms, sensors and mood helpers, the
   problem history) and room health; the App's lighting pages show the lighting problems from
   its own copy of the validation, the Presence pages the occupancy ones.
7. **Curtains and forecasts** (not migrated). Forecasts log into the dormant controller's
   decision log; the log is merged at a return of control. The dormant controller never saves,
   so forecast entries logged while the App controls lighting are lost when Home Assistant
   restarts in that time (the decision log only; nothing that commands lights).
8. Legacy dashboard cards (read-only while the App controls lighting) and the options flow.

**Known limits (accepted for A4):**

* The execute allowlist is derived from the configuration the owner App publishes. A switch
  mapping can therefore name any existing `script.*`/`scene.*` and any `switch.*` relay. Both
  sides are administrator-only, so this is no privilege escalation, but the allowlist does not
  contain a compromised App. Pinning relays and scripts to an integration-approved set is an A5
  item.
* A command the integration's controller tries to send during the (at most ten second) fence of
  a handover is refused and not retried; the light follows at the next trigger.
