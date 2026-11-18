# Changelog

## 0.8.0 — Lighting bridge for the Smart Lighting App (App Phase A4)

The integration keeps controlling the lights by default, also after this update. Nothing changes until an administrator performs a reviewed handover in the Smart Lighting App (0.5.0). No lighting rule changed.

### Added

- `runtime_manifest.py`: the lighting runtime shared with the App (49 modules: the controller and everything it uses, and the lighting websocket handlers). The App runs byte-identical copies (`scripts/sync_lighting_runtime.py`); `fingerprint()` identifies the code.
- `lighting_sync.py`: the lighting bridge protocol (constants, configuration revision, the command allowlist, message validation), shared with the App.
- `lighting_bridge.py`: the one authority over which controller commands the lights — every path (automatic lighting, vacancy, possible occupancy, wall switches, profile tests, Room Supervisor corrections, Priority Inputs, global adjustment).
  - Ownership record `.storage/smart_occupancy.lighting_ownership` (owner, increasing epoch, hashed lease token, history); the App's published state in `.storage/smart_occupancy.lighting_mirror`.
  - Handover: the integration's controller is fenced, its in-flight commands finish (bounded), its configuration and state are handed over and it stops (dormant: it cannot send and never saves). Nothing is switched.
  - The App's commands are executed only from the owner's session and connection, with the current epoch, the lease token (30 s) and a strictly increasing sequence number, and only for light services of configured lights (managed room lights, group members, lights the configuration names), configured relays, and the scenes, scripts and selects of configured switch mappings, with allowlisted data; the App supplies the context, so attribution is unchanged.
  - The App's configuration is mirrored into the lighting store (written only by the App while it controls lighting), its state into the mirror; its occupancy inputs (possible occupancy signals, zone holds, switch activity) reach the occupancy manager; its per-room view is shown by the room entities.
  - Fallback (release, `return`, the service, lease expiry, a new App instance, no resume within 120 s after a restart or reload): the epoch increases first, in-flight App commands finish (as long as a command may take), then a new controller starts from the mirror (forecast decisions kept). Nothing is switched; manual overrides are kept. Only the same App process may resume. If that controller cannot be loaded, nothing commands lights, the status says so (`controller: failed`) and handovers are refused until a reload.
  - Written for the independent review: the lighting store is locked while the App controls lighting (only the App's published copy is written to it, so an editor or options step started before the handover cannot change it behind the App's back); a failed claim (store error, session gone) leaves the integration in control, unfenced; a bridge that was stopped never saves again (a reload cannot overwrite the next bridge's record); a session whose connection closes while it is opened is not kept; entity ids renamed while the App owns lighting but is away are kept and given to it when it resumes (or applied at a fallback); a `fired` stamp on the `config` and occupancy `result` events lets the App start its timers at the same instant as the integration.
- `lighting_bridge_api.py`: administrator-only commands `smart_occupancy/lighting/bridge/status|subscribe|claim|execute|heartbeat|publish|inputs|options|release|return`; service `smart_occupancy.lighting_return_to_integration`.
- While the App controls lighting, the lighting editors (cards, websocket commands, options flow steps) are read-only (`managed_by_app`; the cards say so and link to the App); reading and previews keep working.
- Universal moods: the mood source can be the three helpers, one existing `select` / `input_select` (no Auto/Manual helper needed, no problem reported), the **built-in mood selector** (`select.smart_lighting_mood` with moods you define, plus the Manual option; its selection survives restarts), or none. Command `smart_occupancy/lighting/mood_source/set`; the config flow no longer requires mood helpers. A global mood switch action uses the single selector when there is no Manual Mood helper.
- Tests: `tests/test_lighting_bridge.py` (19), `tests/test_universal_moods.py` (4), `tests/test_lighting_runtime_copy.py` (2), and 21 recorded parity scenarios (`tests/test_lighting_parity*.py`, recorder `tests/lighting_parity.py`) that the App replays.

### Changed

- The lighting websocket handlers moved from `websocket.py` to `lighting_websocket.py` (unchanged); `websocket.py` registers every command. `presence.py` and `health.py` no longer import the config flow / coordinator at runtime (`timing_validation.py`).
- Every lighting command goes through `LightingController.async_call_service` (fencing, in-flight tracking, an optional observer); the decision log and the runtime state have optional observers. Without a lighting bridge session they do nothing new. `export_state()` no longer counts as a save (a reload shortly after an App session opened could lose a manual override).

## 0.7.0 — Occupancy engine bridge for the Smart Lighting App (App Phase A3.2)

The integration stays the owner of room occupancy by default, also after this update. Nothing changes until an administrator performs a reviewed handover in the Smart Lighting App (0.4.0).

### Added

- `occupancy_core/`: the occupancy engine as a shared package without dependencies. The Smart Lighting App runs a byte-identical copy. It contains the 0.6.10 engine moved verbatim, plus:
  - `house.py`: the Home Assistant independent part of `OccupancyManager` (routing, start-up resync, live reconfiguration, preliminary signals, zone holds, wall-switch activity, transitions);
  - `wire.py`: validated JSON forms;
  - `protocol.py`: the bridge protocol;
  - `golden.py` with 20 golden scenarios recorded with the 0.6.10 engine;
  - exact state transfer (`export_state` / `import_state`) and the conservative `adopt`.

  `const.py`, `models.py` and `engine.py` re-export the same objects, so every consumer is unchanged. `CORE_VERSION` 1.0.0 and a SHA-256 fingerprint of the package identify the engine.
- `engine_bridge.py`: the one authority over who publishes occupancy.
  - The ownership record (`.storage/smart_occupancy.engine_ownership`) holds the owner, an increasing epoch, a bounded history and the last accepted decisions. Only a digest of the lease token is stored.
  - Fencing: the owner's session and connection, the current epoch, the lease token (30 s, renewed by a publish or a heartbeat), strictly increasing sequence numbers and the configuration revision. Everything else is refused and changes nothing.
  - Hot standby: the integration's engine keeps running.
  - Fallback when the lease expires, when a new App instance connects, on release, or when the App does not resume within 120 s after a restart or reload. Equal decisions keep their timers. An occupied room stays occupied with a new quiet period, and only rooms whose published result changed are notified.
  - Only the same App process may resume, by presenting its lease token. A recovered App never takes ownership back.
- `engine_api.py`: administrator-only websocket commands `smart_occupancy/engine/status`, `subscribe` (the bridge session: hello with the spec, the engine state, inputs and results; then live results, inputs, spec and ownership events), `claim` (compare-and-swap on the epoch; the engine fingerprint, configuration revision and readiness must match), `publish`, `heartbeat` and `release`.
- Service `smart_occupancy.engine_return_to_integration` (administrators only): rollback without the App.
- The room reason entity has the attribute `decided_by` (`integration` or `app`). The presence API shows the published result, `decided_by` and the owner. Ownership changes are written to the presence log.
- Tests:
  - `tests/test_occupancy_core.py` (358): golden scenarios with the shared and the frozen 0.6.10 engine (`tests/legacy_engine_0610.py`), 300 randomised homes in lockstep, state transfer, adopt, wire validation, and the App copy byte-identical;
  - `tests/test_engine_bridge.py` (19): fencing, concurrent claims, lease expiry during occupancy, possible occupancy and a vacancy countdown (no vacancy, no light command), App restart, resume and revoked resume, reload and restart, a damaged ownership record, the service, administrators only.

### Changed

- `OccupancyManager.result()` returns the published result: the App's accepted result while the App owns occupancy, otherwise the integration engine's. Transitions, the Recorder-based history, the presence log, the helper comparison and the listeners follow the published result. With the integration as owner, the behaviour is exactly that of 0.6.10.

### Not changed

- The occupancy rules, the start-up and reload behaviour, the entity ids and unique ids, `.storage/smart_occupancy.occupancy_state`, lighting, zones, switches, curtains and forecasts.

## 0.6.10 — Presence API for the Smart Lighting App (App Phase A3.1)

### Added

- `presence_api.py`: administrator-only websocket commands `smart_occupancy/presence/get`, `presence/review` and `presence/set`. `get` returns every room's live engine view (state, reason, confidence, evidence, last activity, quiet period, vacancy deadline and path, start-up hold, per-signal status and the moments it changes by itself: sustained confirmation, stuck, dropout bridge, recent door movement), what the engine waits for next, timing with its inheritance, the timing schema (ranges, what 0 means), diagnostics (`measured` from the engine and the states, `integration` from its health checks, `heuristic` suggestions from the configuration: presence sensor as Trigger, inverted motion sensor, fast vacancy slower than the vacancy timeout, stuck detection off, …), lighting zones and radar distance zones with their live status, and the presence log. `review` / `set` change one room's sensors (validated exactly like the Rooms API) and timing overrides, or the global timing defaults. Revisions: the room's (as in the Rooms API) or the global defaults' own. Timing, role, inverted and include/exclude are applied live (`coordinator.async_apply_presence`); adding or removing a sensor or changing its type reloads. Consequences that need confirmation: rooms with Automatic lighting affected, and a decision that changes at once. The review predicts each affected room's decision by evaluating a copy of its engine with the new settings (`predict`).
- `presence_log.py`: bounded presence log (400 entries, memory only, kept across reloads): engine transitions with their evidence, presence configuration changes (live or reload, App or integration) and starts.
- Tests: `tests/test_presence_api.py` (13).

### Changed

- `coordinator.async_apply_presence` accepts the exact options to store, so live presence changes from the Presence and Rooms APIs keep option keys the typed configuration does not know.

### Not changed

- The occupancy engine and its states, lighting, switches, curtains and forecasts; the options flow, the room card's Presence tab and its commands; the dashboard cards (versions follow the release number only).

## 0.6.9 — Room configuration API for the Smart Lighting App (App Phase A2)

### Added

- `rooms_api.py`: administrator-only websocket commands `smart_occupancy/rooms/get`, `rooms/review`, `rooms/set`, `rooms/restore/review` and `rooms/restore`. They read and write the existing room configuration in the config entry options (no second store) with the integration's own models (`RoomConfig`, `SensorAssignment`) and validation (`validation.validate`, the options flow's sensor domains). Revisions (SHA-256 of the stored JSON) refuse stale writes, including after a change in *Configure → Rooms*. Only the room's editable keys change; every other option and the room's other keys (timing, legacy mood overrides) are kept; assigned sensors keep their role, inverted flag and exclusion. Consequences that need explicit confirmation: disabling or removing a room, resuming activated automatic lighting, changing assignments in an *Automatic* room, removing sensors used by lighting zones, a restore. Writes are refused while the entry is not loaded.
- Tests: `tests/test_rooms_api.py` (11): reading, admin only, review without changes, saving one room with every other option preserved, invalid input (duplicates, wrong domains, missing or own entities, unknown areas), stale write after a change in the options flow, confirmations, no light command across the reload with Automatic lighting, live sensor exclusion, a new room for an unconfigured area, backup restore, not loaded.

### Not changed

- Occupancy, lighting, switches, curtains and forecasts; the options flow and the dashboard cards (card versions follow the release number only).

## 0.6.8 — Custom lux categories, Dumb Lights, radar distance zones and Priority Inputs (Phase 4.8)

### Added

- **Custom ambient categories** (`ambient.py`, `ambient_levels.py`): any number of categories (2 to the global maximum, default 12, limit 24) with stable ids, names and start values; global definitions with per-room definitions and *Use Global Levels*. `ambient/levels` previews every change (ranges, added / removed / renamed / moved categories, affected matrix cells per room, rooms that keep their own categories) and saves only with `confirm` once every removed category with values has a resolution (move to another category, or delete). New categories are *Not configured* everywhere. Templates are mapped onto a room's categories. The category tracker persists by id.
- **Fixture Types** (`fixtures.py`; `fixture/set`, `fixture/remove`): Dumb Light — On/Off only, dimmable, tunable white, colour or a custom label, an optional Home Assistant label and a display name. A Dumb Light's capabilities are narrowed to on/off (never widened). `register_relay` explicitly adds a switch as a lighting output. On/off lights get On / Off matrix cells.
- **Radar Distance Zones** (`radar.py`; zone `detection: "radar"`): radar presence + distance (entity state or attribute; m / cm / mm), band, entry and exit delays, hysteresis, optional maximum age, the zone fail-safe for an unavailable distance or presence. Device-registry discovery of the radar's distance sensor (`radar` in the zones model), `zone/radar_test` (test distance, nothing sent), live band in the card.
- **Priority Inputs** (`priority.py`, `priority_runtime.py`, `priority_websocket.py`): per-room inputs (binary_sensor, input_boolean, explicit switch) with polarity, targets, brightness, appearance policy, debounce, release delay, unavailable policy and grace, manual-override authorization and release policy. Disabled until reviewed (`priority/preview`, then `priority/set` with `enabled` and `confirm`); global safety lock (`priority/lock`). An active claim is the winning rule in every plan (`arbitration.priority_item`), sent through the existing executor also in Observing rooms and Manual mood (gate `assert_priority_control_permitted`); release applies the current winning rule; multiple inputs per light (highest brightness wins); settle after restart; activation history.
- Card: Settings → Lighting → category table and editor with server review, On/Off matrix cells, *Fixture Types*, *Priority Inputs*; Settings → Presence → Zones → radar detection with the band visualization and test distance; Overview lines for active Priority Inputs and radar zones and *Why each light is on, off or dimmed*.
- Decision log entries for ambient category changes, radar zone activation and release, Priority Input activation, release and unavailable inputs.
- Tests: `tests/test_phase48_pure.py` (12) and `tests/test_phase48_runtime.py` (21): categories (validation, boundaries, rename, add, reviewed removal and reassignment, room vs. global, Use Global Levels, templates, migration, contamination), Dumb Lights (relay services, on/off lights, mood appearance ignored, unrelated switches untouched), radar zones (units, boundaries, hysteresis, delays, invalid / stale / unavailable values, presence clear, vacancy gating, restart, rename, discovery, websocket), Priority Inputs (vacant and bright room, release to the current rule, Observing and Manual mood, lock, disabled input, manual changes, several inputs, inversion and bounce, unavailable policies, restart, Dumb Light targets, Supervisor, websocket review).

### Changed

- Lighting store 1.8 → 1.9: the 0.6.7 categories become definitions with ids `none` / `low` / `bright` (values kept); `fixtures`, `priority_inputs`, zone `detection` / `radar`, `priority_lock`. `thresholds` is still written for 0.6.7.
- `ambient/set` `thresholds` set the boundaries of the room's current categories (any count).
- The Room Supervisor verifies a Priority Input's own switch-off like the other removals.

### Not changed

- Rooms without the new features, sensor zones, existing relay outputs, mood profiles, manual override behaviour, the Room Supervisor's settings, shared spaces and possible occupancy lighting.

## 0.6.7 — Ambient light levels and multi-zone brightness rules (Phase 4.7)

### Added

- **Ambient Light Levels** (`ambient.py`, `brightness_runtime.py`; room card Settings → **Lighting**): No light / Low light / Bright per room, default thresholds 5 and 20 lx (a value equal to a threshold is in the higher category), global defaults with room overrides, hysteresis (downwards only), stability period, fallback policy (*keep the last reliable category* or a fallback category). It consumes the daylight monitor's evidence (no new sensor subscription or estimator; the indoor sensors are the room's daylight sensors). A lamp-lit reading is an upper bound: it can keep or lower the category, never raise it (no feedback loop); the hold is shown and logged once per episode. The committed category survives a restart (lighting state store, key `ambient`).
- **Brightness Rules** (`brightness.py`): an optional matrix per enrolled light, ambient category × Room occupied / each zone that lists the light; 0 % = Off, *Not configured* distinct from Off; built-in templates (General, Floor, Task, Ceiling, Accent) and saved global templates; copy from another light, copy the Room column to a zone, restore defaults, pause. Enrolment needs a preview first (`brightness/preview`, then `brightness/set` with `confirm`) and never sends a command.
- **One rule engine**: the Occupied plan resolves the Room occupied cell, the zone arbitration replaces it with the winning zone's cell (priority, then list order; never combined). On/off and brightness come from the matrix, colour, colour temperature (also the global one) and effect from the room's mood. Category changes apply the enrolled lights (trigger `ambient`, optional minimum interval); zone changes apply at once. Brightness dead band, transition. *Resume automatic lighting* hands an enrolled light back to its matrix at once.
- Decision log: *“Room occupied, Low light (≈ 12 lx), Floor Light → 15 %, colour from Hygge (2700 K)”*, zone wins with the reason, *“Ambient sensor affected by room lighting. Holding No light classification …”*; details list each light's matrix cell. Ambient status and history in the integration diagnostics; compact summary in the room sensor.
- Card: Settings → **Lighting** with *Ambient Light Levels* (category now, measured vs. estimated value, confidence, sensors, thresholds, stability / fallback / transition / dead band / interval, global defaults) and *Brightness Rules* (light list, matrix with a slider per cell, preview, enrol, save, leave, resume); Overview line *Ambient light*; the Zones editor links each enrolled light to its matrix instead of showing its brightness inputs.
- Websocket: `ambient/set`, `brightness/preview`, `brightness/set`, `brightness/remove`, `brightness/template`; editor model key `brightness`.
- Tests: `tests/test_brightness.py` (27: boundaries, thresholds, hysteresis and dwell, stale/invalid data, lamp-lit readings and the feedback loop, matrix columns, priority and tie-break, Off and Not configured cells, mood vs. brightness ownership, every light kind and relays, possible occupancy and preliminary lighting, unavailable and late devices, Supervisor, Observing, rooms without the feature, Manual mood, wall switch, rate limit, dead band, restart, shared light, websocket API, migration and renames, the Kamar Norður acceptance scenario); card test for the Lighting section.

### Changed

- Lighting store 1.7 → 1.8 (global `ambient`, `brightness_templates`; per room `ambient` off and `brightness` empty). No light is enrolled and no room changes behaviour.
- The Room Supervisor treats a light within the brightness dead band as correct.

### Not changed

- Lights that are not enrolled, rooms without ambient brightness control, vacancy behaviour, possible occupancy lighting, zone occupancy and timing, manual override policy, wall switches, Manual mood, Observing / Automatic, shared spaces and the Room Supervisor's modes.

## 0.6.6 — Centralized curtain configuration (Phase 4.6.4 addendum)

### Changed

- **Global Schedule and Global Modes are the only configuration of the normal routine.** Schedule settings are global only (`rules.py`: room / window scopes refused, `wrong_scope`); room, window and cover overrides of mode presets can no longer be created (`modes/change` `set` with a value and `adopt` refused, `window/update` `conditions` with a value refused: `managed_globally`).
- `CurtainConfig.centralized`: once centralized only Global Modes is resolved (no hidden override can defeat a global assignment) and only the global schedule counts. A window without a Morning preset keeps its position during Morning (0.6.2–0.6.5: started its Daytime preset at the first daylight).
- Card navigation: **Overview · Global Modes · Global Schedule · Windows · Diagnostics** (`page:` any of `overview`, `modes`, `schedule`, `windows`, `diagnostics`). Window settings: no per-mode preset editor and no schedule group; a read-only *Presets per mode — managed in Global Modes* list with *Edit* links; the window lamp shows its Global Modes preset with a link. Overview cards link to their current mode in Global Modes and to the Global Schedule.

### Added

- **Reviewed migration** (`curtains/migration.py`, `migration/change` with `choices`, `dry_run`, `confirm`, `policy`): old overrides and room / window schedules stay active until applied; exact conversion per cover (new cover presets where needed, verified before saving); choices for hidden Global Modes conflicts, windows without a Morning preset, Glare / Heat “No action”, and differing room / window schedule times (required); impact preview; removes the old settings only after success. A store with nothing to migrate is centralized at load.
- **Windows** page (add window, suggestions, settings for every window, window list) and **Diagnostics** page (now, manual holds, prevented, failed movements, competing automations, problems, recent decisions).
- Tests: `tests/test_curtains_central.py` (migration exactness, required choices, conflicts and preview, auto-centralize, no hidden overrides, global schedule + modes driving windows with different presets, excluded windows, one command per cover, restart persistence); existing tests rewritten for the centralized model.

### Not changed

- Window presets (whole-window and single-cover), covers, direction, obstructions, sun / glare / thermal / sleep / lamp / manual settings, Off / Observing / Automatic, the decision engine, safety gates and the executor. 24-hour times everywhere.

## 0.6.5 — Global Curtain Schedule (Phase 4.6.4)

### Added

- **Curtains → Global Schedule** (`page: schedule`): household phase now (since, reason, what it waits for), next transition, today's 24-hour timeline, four phase rows (rule and time; click to edit), free days, late evenings, **Advanced** per-day times and the list of room / window schedules with *Use Global Schedule*, upcoming transitions and recent household phases. Draft + **Review & save**.
- `curtains/schedule.py`: the one schedule evaluation used by every window (`logic.decide`) and the household view. Morning earliest time (normal / free days, Morning on/off), Daytime rules (`duration`, `until_time`, new `not_before`), Evening rules (`time`, `event` with `sunset` or measured `dusk`, `first`, `last`; latched until Night), Night per evening with explicit late evenings (`before_free` or chosen `late_days`), per-day times (`day_overrides`: morning / day / evening / night per weekday), next transitions and the timeline.
- `schedule/change` (`schedule_page.py`): validated drafts and reviewed saves for all windows, a room or a window (`null` = use the less specific value); previous / proposed next transitions; per-window impact; `confirm` + policy `now` / `next_change` (reusing the Global Modes defer); admin only. Schedule settings changed in window settings are previewed the same way.
- Decision log: *“Morning is eligible, but daylight has not been confirmed. Holding current positions.”*, *“… Sleep Mode is on …”*, *“Evening started because dusk was confirmed (12 lx) before 18:00.”*; household log *“Night started according to the normal evening / late evening / Friday schedule.”*. Window model `auto.schedule` (Evening latch, note, next transitions).
- Tests: `tests/test_curtains_schedule.py` (21 tests: every activation rule, Morning in daylight / 0 lx / asleep / after sleep, Daytime rules, Evening time / sunset / dusk / first / last, passing clouds, Night normal / late / Fri–Sat / Sun→Mon / midnight / custom days / per-day times, DST in three zones, inheritance, unavailable sensors, sun and lamp precedence, manual holds, windows without assignment, layered presets, restart in the night, preview and next-transition application, 0.6.4 upgrade, the 11-12 early-morning failure with a 07:00 Morning, no effect on occupancy / lighting / forecasts).

### Changed

- Schedule settings keep their keys and values (`night_end_*` = earliest Morning, `night_start_*` = normal / late evenings, `morning_mode`, `morning_minutes`, `day_from_*`, `free_days`); labels say what they are in the phase model.
- Morning no longer begins before the day's Morning time even when Night is off (before, only the night kept it from starting earlier).
- Time fields accept up to 8 characters so a typed “7:45 PM” is refused instead of being cut to 07:45.

### Not changed

- With the defaults (Evening rule *0.6.4 behaviour*, late evenings *before a free day*, no per-day times) every window decides exactly as in 0.6.4 (the equality test against the frozen 0.6.2 logic still passes). Global Modes, presets, Off / Observing / Automatic, manual holds, safety gates, the executor, occupancy, lighting, forecasts and heating. No store migration is needed (new keys have defaults).

## 0.6.4 — Global Curtain Modes (Phase 4.6.3)

### Added

- **Curtains → Global Modes** in the Smart Curtains card (`page: modes` opens it directly): mode cards Morning, Daytime, Evening, Night, Sleep, Sun Protection (Window Lamp, Glare, Excessive Heat under Advanced) with counts; the selected mode's assignments (window or single cover, room, type, preset dropdown, status, remove, effective preset and source, hidden-by-override warnings with *Use Global Setting*) and **Add Window / Cover**; overlap errors offer *Replace*. Selecting a card never calls the integration.
- **All windows** matrix (rooms, windows, covers × modes; preset / Inherit / No action / — with source tag), with a compact cell editor; scrolls horizontally on phones.
- **Draft, review and confirmed save** (`modes/change` with `dry_run`, `confirm`, `policy`): changes, per-window previous / proposed preset, current condition, movement now, hold, block and competing automations. Saving a change to an Automatic window's active target needs `confirm: true` and a policy: `now` (normal safety gates) or `next_change` (kept until the condition changes; survives restarts; Waiting for daylight does not count as a change).
- **Cover presets** per layer (`cover_preset/save`, `cover_preset/delete`; Covers tab), default Fully open 100 / Half open 50 / Fully closed 0, stable ids.
- **Resolution per cover** (`curtains/modes.py`): cover override → window override → room override → Global Modes → no action; explicit **No action** distinct from inherit; Glare / Excess heat fall back per cover to Sun Protection; one target per physical cover.
- Window settings → Automatic: **Presets per mode** (Use Global Setting / No action / preset, with the effective result and source), single-cover overrides, and *Move them to Global Modes…* (reviewed `adopt`).
- Decision log entries carry `source` and a `summary` (“Morning. Stova Suður → Half shade (Global Mode).”, “… has no assignment; leaving positions unchanged.”, lamp bypass); *Recent automatic decisions* on the Global Modes page.
- Tests: `tests/test_curtains_modes.py` (modes, assignments, cover presets, layered presets by reference, stable ids, no assignment = no action, inherit vs No action, four-level precedence, 0.6.3 migration and adopt, overlaps, editing and Observing never move, arbitration, sun fallbacks, missing / deleted presets, renamed and replaced covers, restart persistence, review / confirm / policies, admin only, 24-hour texts); the existing scenario tests now assign their mappings through Global Modes.

### Changed

- The automatic controller resolves each decided condition through `modes.py`; its arbitration is unchanged (pure-logic equality with the frozen 0.6.2 logic still holds).
- New windows start **without** a condition mapping (0.6.1–0.6.3 suggested one); a mode without an assignment keeps the covers where they are.
- Deleting a window preset sets window overrides that used it back to *inherit* and removes Global Modes assignments that used it.
- Movement log text ends with “Preset from <source>”; Observing status names the source.
- Curtain store minor version 4 (`modes`, `room_modes`, layer `presets`, window `cover_modes`); state store adds `defer`. 0.6.3 data loads unchanged: mappings become window overrides, no window changes mode.

### Not changed

- Solar and thermal calculations, sleep / night / privacy / lamp / manual-hold arbitration, safety gates, the executor, occupancy, lighting, forecasts. No existing Home Assistant automation is changed.

## 0.6.3 — Solar and thermal curtain control, 24-hour time (Phase 4.6.2)

### Added

- **Environmental evidence** (`curtains/environment.py`): outdoor / indoor light, room / outdoor temperature (sensor, thermostat or weather; converted to °C), weather condition and cloud cover; each reading judged Valid / Not chosen / Not found / Unavailable / Stale / **Appears stuck** / **Implausible**. Selectable for all windows, a room or a window (`lux_entity`, `indoor_lux_entity`, `room_temp_entity`, `outdoor_temp_entity`, `weather_entity`, `cloud_entity`, `lux_stuck`, `temp_stale`). Discovery with a **sensor-quality summary** and *Use for all windows*.
- **Direct sunshine** kept apart from geometry and daylight: `direct_sun` assessment (not in front / confirmed / likely / diffuse daylight / cannot be confirmed). Optional weather evidence (`sun_evidence`, `cloud_max`) only for windows without a light sensor, never for opening; if it disappears while shading, the position is kept.
- **Temperature-aware sun** (`thermal_enabled` and its settings): Cold / Comfortable / Warm / Too warm with hysteresis and a stable period; conditions **Useful solar heating** (keeps the Morning / Normal daytime preset), **Excess solar heat** and **Glare protection** (glare-sensitive windows, optionally only while occupied or above an indoor light level). New condition mappings *Glare protection* and *Excess solar heat* fall back to the Direct sunlight preset. Thermostats are only read.
- **Overhang** limit per window (`solar.max_elevation`), sun incidence and the window's angle read through the validated `sensor.sun_angle` calibration.
- `auto/simulate`: the predicted decision and moves of every affected window before a setting is saved; the card uses it before switching on temperature-aware sun, glare or weather evidence.
- Smart Curtains card: per window the sun angle and elevation, direct sun, outdoor light, temperatures, glare / heat decision, next re-evaluation; new **Environment** tab (measurements with validity, assessments, environment / temperature / glare settings); overhang field; decision log as `HH:mm — explanation`.
- **24-hour time everywhere**: one shared helper in the curtains and lighting cards formats every time as `HH:mm` in Home Assistant's time zone (`hourCycle: "h23"`, independent of the browser locale; honours a profile set to local time); chart axes `00:00 … 18:00`; time settings are 24-hour text fields (`HH:mm`, 00:00–23:59) instead of native time inputs.
- Tests: `tests/test_curtains_solar.py` (geometry for south, east, west, obstructed and overhung windows, the compensated sun angle, sensor validity, temperature bands, arbitration against sleep / night / privacy / lamp / manual holds, equality with the frozen 0.6.2 logic over 4000 random situations, DST and midnight schedules, layered presets, passing clouds, stuck sensor, missing sources, discovery, restart, upgrade from a 0.6.2 store) and `tests/test_time24.py` (Node under en-US, en-GB, Faroese and Danish locales, browser in another zone, midnight, 12:00, 23:59, date changes, DST, time-field parsing).

### Changed

- **Stuck light sensors** (same value for 2 h while it should vary) and implausible readings are no longer evidence: the window keeps its position. This is the only new default that is on; it never moves anything.
- Decision texts: “Direct sunlight detected (… lx)”, “Night protection active until 06:30”, “Lamp override bypassed: …”; a movement is logged as “<reason>. Applying <preset> (<condition>): moving N covers”.
- Light settings moved from *Daylight* to the new *Environment sensors* group (same keys and values).
- Curtain store minor version 3; state store adds `sensors`, `thermal`, `last_phase`. 0.6.2 data loads unchanged.

### Not changed

- Occupancy, lighting, zones, the Room Supervisor, switches, forecasts and heating. With the new options at their defaults, every 0.6.2 window decides exactly as before (verified against the frozen 0.6.2 logic).

## 0.6.2 — Autonomous curtain control (Phase 4.6.1)

### Added

- **Automatic mode per window** (`curtains/logic.py` pure decisions, `curtains/auto.py` runtime, `curtains/rules.py` settings): Off / Observing (default; decides and explains, sends nothing) / Automatic (sends the decided preset without further confirmation). Automatic is enabled only through `auto/preview` + `auto/enable` (`confirm: true`), which show the intended condition and preset, every cover's plan, blockers, warnings and precedence; `window/update` refuses it.
- **Conditions**: Sleep, Night (workday / free-day times, free days), Evening privacy (measured dusk or sun below −4°), Waiting for daylight (keeps the position), Sun protection (geometry *and* measured strong light, with hold times and hysteresis), Window lamp, Morning (duration or until a time) and Normal daytime; Manual override. Each uses the window's condition → preset mapping (new mapping: *Window lamp on*).
- **Daylight evidence**: outdoor illuminance sensor with daylight / dusk thresholds, stability periods, hysteresis, a minimum sun elevation and staleness (`last_reported`); optional conservative sun fallback for windows without a sensor. A clock never opens a window; missing evidence never opens one; sleep blocks opening in sleep-protected rooms; an unavailable sleep source never authorises an opening.
- **Sleep mode** settings: source entity (default `input_boolean.sleep`), what happens when sleep ends (morning / current condition / keep until Resume), opening after sleep needs daylight, sleep wins over the lamp, sleep ends earlier manual holds.
- **Window lamp override**: lamp entity, Lamp On preset, release delay, conditions that win over the lamp; after release the current condition applies.
- **Manual holds** from manual moves and from presets applied by hand: until the next phase, for N minutes, until the room has been empty (occupancy), until resumed; protective conditions can end earlier holds; **Resume automatic curtains** (`resume`); holds persist across restarts.
- **Movement safety**: start-up settle (120 s), never contradicting a running movement, unavailable / busy covers block, dead band, no repeated targets, minimum interval, daily budget (Sleep and Night exempt), bounded retries (1 min, 5 min), no replay after restart.
- **Competing automations** block enabling and automatic sending until disabled or explicitly allowed per window (`automation/allow`); scripts called as actions are now detected too.
- Settings for all windows, per room and per window (`auto/settings`), with the source of every value; card forms generated from the settings schema.
- **Smart Curtains card**: automatic condition, preset, explanation, status and evidence chips on every window, desired positions, Resume, the enable review, the Automatic tab (conditions, flags, settings by scope), Diagnostics (decision log, precedence, evidence, allow automation). Diagnostics section `curtains.automatic`.
- Tests: `tests/test_curtains_auto.py` (incident replay of 11-12 / 11-13, modes, per-window activation, single and layered windows, sleep, lamp and bypasses, manual holds and Resume, stale / unavailable light, flapping, sun protection, failures and delayed reports, budget and interval, restart, settings scopes, no effect on occupancy / lighting / forecasts) and pure-logic tests.

### Changed

- `assert_curtain_command_permitted` permits `CurtainOrigin.AUTOMATIC` for windows in Automatic only (`AUTOMATIC_CURTAIN_CONTROL = True`).
- A preset applied by hand in Observing / Automatic becomes a manual hold.
- After a restart, the rest of an interrupted movement is not mistaken for a manual change.
- Curtain store minor version 2 (`defaults`, `rooms`, window `auto`, `allowed_automations`); 0.6.1 data loads unchanged and every window stays as it was (Observing).

### Not changed

- Occupancy, mood lighting, zones, the Room Supervisor, wall switches and forecasts. Lighting moods are not read for curtains.

### Not included

- Thermal optimisation (temperatures, solar gain) and weather / cloud input.


## 0.6.1 — Smart Window & Curtain Controller (Phase 4.6.0)

### Added

- **Curtains** (`curtains/` package, store `.storage/smart_occupancy.curtains` plus `.storage/smart_occupancy.curtains_state` for results): windows with a name, a room (area), one or more cover layers (blackout, semitransparent / sheer, shade, custom, optional own name), a compass direction or exact angle, mode (Off / Observing; Automatic reserved and refused), and Privacy / Sleep protection / Sun protection switches. Stable window, layer and preset ids; entity renames are followed.
- **Discovery**: every `cover.*` entity with its area and capabilities; proposed windows from the covers no window uses (area + entity id without the layer word), confirmed or corrected before creation. One cover (or cover group member) can belong to only one window.
- **Preset library per window**: Fully open / Half open / Fully closed, or Fully open / Half shade / Full shade / Evening privacy / Full blackout for sheer + blackout windows; every value editable; unlimited (24) named presets; rename, duplicate, delete; per-cover target or *Leave unchanged*; 0–100 % validation; selected preset; recognised preset or *Custom position*.
- **Explicitly confirmed manual preset execution**: preview (no service calls), confirmation listing every cover and its target, `apply` requires `confirm: true` and the confirmed targets; one coordinated operation with one service call per cover (`set_cover_position`, or `open_cover` / `close_cover` for open/close-only covers); already-correct covers are skipped; reported positions verified (tolerance and timeout configurable); per-cover and overall results (reached, already there, failed, timed out, unavailable, not supported, partly succeeded, interrupted by restart); busy covers refuse contradictory commands; last result and history per window.
- **Manual change detection** (position changes Smart Occupancy did not command), shown and kept for the next phase.
- **Condition → preset mapping** per window (Morning, Normal daytime, Direct sunlight, Evening privacy, Night, Sleep, Manual override), stored only.
- **Sun**: azimuth / elevation from `sun.sun` or sensors, potential direct-sun exposure per window (exposure width, minimum elevation, obstructions), `sensor.sun_angle` shown and validated against the azimuth from readings (sign and offset are fitted, never assumed).
- **Competing automations**: automations whose actions may move a window's covers (directly, by group, device, area, label, scene or script) are listed per cover; enabled ones are warnings.
- **Smart Curtains card** (`custom:smart-occupancy-curtains-card`): overview grouped by room; window view with General, Covers, Presets, Automation, Sun protection and Diagnostics; usable on phones.
- Safety gate `assert_curtain_command_permitted`: only `CurtainOrigin.MANUAL_CONFIRMED` for a window that is not Off; automatic origins are always refused.
- Websocket commands `smart_occupancy/curtains/model`, `subscribe`, `window/create`, `window/update`, `window/remove`, `preset/save`, `preset/duplicate`, `preset/delete`, `preset/select`, `preview`, `apply`, `manual/clear`, `settings`, `rescan`, `calibration/reset`; diagnostics section `curtains`.

### Not changed

- Occupancy, mood lighting, zones, the Room Supervisor, wall switches and forecasts. Lighting moods are not read for curtains.
- No cover moves on its own, at start-up, at discovery, on a configuration change or migration. Existing curtain automations are not changed, disabled or deleted.

### Not included

- Automatic curtain control (Phase 4.6.1): condition evaluation, daylight / sun / sleep rules, manual hold policies.

## 0.6.0 — Multi-zone lighting and the Room Supervisor (Phase 4.5)

### Added

- **Zones** per room (Settings → Presence → Zones): a subset of the room's sensors, *occupied at the first detection* or *when confirmed*, timing overrides (hold after the last detection, exit confirmation, confirmation window, dropout grace), a fail-safe for unavailable sensors (*keep last state* / *vacant* / *occupied while the room is*), *keeps the room occupied*, lights with On (brightness, colour temperature, transition) / Off / Unchanged, priority, manual-change policy, daylight rule (room / always / own light level) and what happens when the zone ends. Each zone runs the existing occupancy engine; zone transitions are kept in a bounded history (diagnostics) for later learning.
- **One lighting rule engine** (`arbitration.py`): every plan (Occupied, Vacant / vacancy, possible occupancy, gradual updates) gets one winning rule per light — protection, high-priority zones, manual overrides, zones, Occupied profile, possible occupancy, Vacant profile — before anything is sent. Plan items, previews, the decision log and the Supervisor name the rule and what it took precedence over.
- **Shared spaces** (open-plan rooms): optional shared mood, shared lights never switched off by one room while another member room is occupied and commanded by one room at a time, and optional joint vacancy.
- **Relay lighting outputs**: switches explicitly ticked in Settings → Advanced are lights of the room (on/off only, `switch.turn_on` / `switch.turn_off`, never light attributes); they take part in profiles, possible occupancy, vacancy, zones, manual overrides, wall switches and the Supervisor; automations calling `switch.*` on them are detected as conflicts.
- **Room Supervisor** (`supervisor.py`): compares desired and actual states with tolerances and verification delays; Observing (default) / Automatic / Off per room or globally; bounded corrections with growing delays and a per-device hourly limit; never corrects a manual change; reports (and optionally corrects once) changes by other automations, then *Conflict suspected*; *Device failure* after the last correction, verified again when the device comes back online; verifies the removal of preliminary and zone lights. Statuses Healthy, Observing, Waiting for verification, Mismatch detected, Correcting, Degraded sensor availability, Device failure, Conflict suspected.
- **Room card**: Overview → Room Supervisor (status, active zones, discrepancies, last check, health; per light: winning rule, expected, actual, last command, verification, correction, error; supervisor log; *Check now*); Settings → Presence → Zones and Shared space; Settings → Advanced → Relay lighting outputs and Room Supervisor settings.
- Decision log: zone activated / ended (with the lights' new rule), vacancy waiting for the shared space (room log); mismatch, correction, verified, device failure, conflict, manual change respected, sensor availability, room healthy again (supervisor log `<area>#supervisor`).
- Websocket commands `zone/set`, `zone/remove`, `relay/set`, `space/set`, `space/remove`, `supervisor/set`, `supervisor/room`; diagnostics `zones`, `supervisor`, `relays`, `shared_space`; occupancy diagnostics `zone_holds`.
- Lighting store 1.7 (`zones`, `relays`, `supervisor` per room; global `supervisor`, `shared_spaces`).

### Changed

- Commands to a target are sent with the service of its domain (`light.*` or `switch.*`).
- A light a zone switched on counts as off for the daylight rules after the zone ends, so it is not left on in daylight.
- The room mood-lighting sensor shows a compact zone / supervisor summary (unrecorded attributes).

### Not changed

- Rooms without zones, relays or a shared space: occupancy, mood lighting, possible occupancy, Vacant profiles, manual overrides, wall switches and the decision log behave as in 0.5.0. Forecasts stay observation only and never activate a zone.

### Not included

- Predictive heating (Phase 5).

## 0.5.0 — Occupancy forecast (Phase 4, observation only)

### Added

- **Occupancy forecast per room**: P(occupied 5, 15, 30 and 60 minutes from now), P(entered within the horizon) for vacant rooms, the usual occupancy at that time, a likely window and the most likely next rooms. Learned from the Recorder's history by replaying the configured signals through the same occupancy engine; a time-inhomogeneous two-state Markov chain with hierarchical (shrunk) rates by weekday, day type and hour, visit-age and vacancy-age factors, learned influences (bed and PC activity signals automatically; sleep, person or any entity on request; a connected room occupied) and room-to-room movement statistics. Recent days weigh more (half-life).
- **Room connections** (door / open plan / shared transition area), suggested from adjacent rooms and shared sensor devices; detections of one person by a shared sensor are not counted as movements.
- **Confidence** (insufficient data / low / moderate / high) from days and weekdays observed, visits, measured skill and sensor availability. With insufficient data no probability is published.
- **Prequential accuracy**: each completed day is scored before it is learned from, against the schedule and persistence baselines (Brier score, skill, calibration, confusion at the threshold, daily series).
- **Entities**: `sensor.smart_occupancy_<room>_forecast`, `binary_sensor.smart_occupancy_<room>_predicted` (probability **and** confidence thresholds), `sensor.smart_occupancy_<room>_forecast_confidence`, `sensor.smart_occupancy_predicted_next_room`, `sensor.smart_occupancy_forecast_learning`.
- **Room card**: an *Occupancy forecast* block on the Overview; Settings → Presence → *Occupancy forecast* with explanation, learning status and history coverage, hourly chart and weekday × hour heatmap, movement, accuracy, influences, settings (room overrides and global defaults), connections and the forecast log.
- **Forecast log** in the shared decision log (subjects `forecast:<area>` and `forecast:house`): threshold crossings, confirmed or missed predictions, predicted movements and their outcome, unavailable forecasts, confidence changes, changed learned patterns. Bounded and merged.
- Websocket commands `smart_occupancy/forecast/room`, `/settings`, `/connection`, `/learn`, `/rebuild`; diagnostics section `forecast`; `ForecastManager.room()` for other modules.

### Not changed

- Occupancy detection, automatic lighting, possible occupancy, Vacant profiles, manual overrides and wall switches. No forecast controls any device or changes any occupancy state.

### Not included

- Predictive heating (Phase 5) and any active use of forecasts.
- Reuse of the "History Forecast" devices (*Local Occupancy Analytics*): they belong to another integration, not this repository (see the README audit).

## 0.4.0 — Occupied and Vacant lighting profiles

### Added

- **Vacant profile** per room and mood, next to the existing mood profiles (now called the **Occupied** profile). It is edited like them: per light type and single light, On (brightness, colour temperature, RGB, effect, transition), Off, Leave unchanged or not set, with the same inheritance: light → room → global, each with the mood, its parent and fallback moods, and *every mood*. Global Vacant defaults are in the Smart Lighting card; rooms override them in the Overrides tab. Choose **Occupied | Vacant** above the light types.
- **Vacancy behaviour *Vacant profile*** (was *Vacant mood*): when the room becomes vacant, its Vacant profile of the current mood is applied. Unlike the 0.3.8 vacant mood it can switch decorative lights **on** in the empty room. Lights it does not set are switched off (default) or left unchanged (room value or global default). Turn off and Leave unchanged never use a Vacant profile.
- **Mood changes in an empty room** apply the new mood's Vacant profile at once, without a sensor event. Ambient lights keep following the Occupied profile in the same plan, so the two profiles never control the same light.
- **Daylight condition per light type or light**: *Only when dark* (default) or *Also in daylight* (an explicit exception, for example a window lamp), in both profiles. The Vacant profile can have its **own light level** (room value or global default) with the room's hysteresis and stability period. In an empty room a light it wants only in the dark is switched on when it becomes dark and off again when daylight is enough.
- **Copy, reset, preview and test** per profile and mood: copy a mood or the other profile (for example Occupied into Vacant as a starting point); reset a room's values to the inherited ones; preview a profile as if the room were occupied or vacant; and **Test on the lights…**, which asks first, sends the profile to the lights now, and keeps the tested lights as a manual change (*Resume automatic lighting for these lights* ends it).
- Decision log entries *Apply Vacant profile (…)* and *Test … profile …*; diagnostics `vacancy.effective_others`, `effective_on_below`, `vacant_profile_in_charge`, `daylight_gate`.
- Websocket commands `vacant/set`, `profile/copy`, `profile/reset`, `profile/test`; `profile` on `editor`, `profile/set` and `preview`.

### Changed

- **Editing a profile no longer changes any light.** Before 0.4.0 a changed profile entry was applied at once to the lights it affected. Now it is used at the next trigger (mood change, confirmed occupancy, darkness, vacancy), or when you Test the profile.
- *Vacant mood* no longer needs a mood chosen.

### Upgrade

- Lighting store 1.5 → 1.6. Every Vacant table starts empty. A room that used the 0.3.8 *Vacant mood* keeps exactly its behaviour (*Lights it does not set: Like a mood*): the lights the Vacant profile does not set get that mood's normal settings while they are on, and none is switched on. Nothing else changes: Turn off and Leave unchanged rooms are not affected, and no room gets a new light command from the upgrade. A 0.3.13 install reads a 1.6 store and ignores the new keys.
- Restart Home Assistant after updating; reload the browser for the cards.

### Not included

- **Scenes as profile targets.** A scene sets lights itself. As a target it would compete with the per-light settings of the same profile, and there is no reliable way to tell whether it is already applied (to avoid repeating it). Lights and light groups are supported; a scene can still be assigned to a wall switch button.

## 0.3.13 — Turn off on confirmation waits until the normal lighting is really on

### Changed

- *Turn off* on confirmation no longer counts the normal lighting as on just because its command was accepted (0.3.12). It waits until at least one of those lights **reports on** (up to 10 s plus their transition), then switches the preliminary lights off. A light changed by a person during the wait is left alone. If none reports on in time (a device that did not react), the preliminary lights stay on, so the room is never left dark, and the log says so: *kept on: … did not report on within 10 s*. The trace records `waiting_for_normal_lighting` and then `normal_lighting_reported` or `normal_lighting_not_reported`.

## 0.3.12 — Fix: Turn off on confirmation kept the preliminary lights on

### Fixed

- **Preliminary lights set to *Turn off* stayed on after occupancy was confirmed.** Found from the live diagnostics trace of 2026-11-12 18:38 (Kamar Norður). Occupancy was confirmed by the radar and Nobara 22 s into the stage. The normal lighting (Kvøld) switched the spots group on. Then both preliminary lights got the outcome `kept_dark` and no command was sent. Root cause, in `_confirm_preliminary`: *Turn off* keeps a preliminary light on when no other light of the room is on, so the room is never left dark, and it checked that against Home Assistant's light states right after the normal lighting's `light.turn_on`. The spots group (Zigbee2MQTT) reports its new state only a moment after the call returns, so it still read "off". Lights the normal lighting has just switched on successfully now count as on. The trace's `confirmed` step lists `scene_switched_on` and `other_lights_on`.

## 0.3.11 — Fix: possible occupancy lights stayed on with a PIR that holds "occupied"

### Fixed

- **Preliminary lights did not go off when nobody stayed** (reproduced with a Zigbee lamp and a Wi-Fi LED strip, alone and together). Root cause, in the occupancy engine (`engine.RoomEngine._corroborated`): "a trigger active for the whole correlation window" confirms occupancy. A PIR that reports *occupied* for a hold time (the Aqara PIR through Zigbee2MQTT: about 60 s after one movement) stays active longer than the 10 s window after every single movement. So 10 s after the detection the engine reported **confirmed occupancy** from that detection alone. The controller then did what confirmation means: it cancelled the preliminary timeout (`_close_preliminary`), applied the normal lighting and handed the preliminary lights over (`_confirm_preliminary`). The timeout cleanup never ran. The lights stayed on until the vacancy timeout, 10 min after the PIR cleared, or for good with *Leave unchanged* or *Vacant mood*. The 0.3.9 and 0.3.10 tests released the PIR after 1–3 s, so they never reached the window. It was not device-specific: the lights were never commanded off.
- Now, in a room with possible occupancy lighting, the signals that start it cannot confirm occupancy by themselves: not by staying active, and not together with each other. Another signal must agree (radar, PC activity, a door that is not selected, ...). Rooms without a preliminary light keep the engine's previous rules. The card lists the signals that can confirm, and warns when there is none.
- **Ownership is now ended only by a recorded change by a person** (a manual override). A brightness change by an automation or an adaptive lighting system no longer makes the stage give up a light it switched on (0.3.10 did).
- **A light whose switch-on call failed or timed out is still owned:** it was commanded on from off, so it is switched off at the end if it came on (for example a slow Wi-Fi device). Each preliminary light now gets its own command, so one failing light cannot hide another.

### Added

- Developer trace of the possible occupancy pipeline per room. It is in the diagnostics download (`preliminary.trace`) and in the debug log (`preliminary_trace`), bounded and in memory, with no sensor events logged. It records: occupancy before, the trigger, each target's state, the activation calls, results and states after, the timeout scheduled, fired or cancelled, the cleanup decision and what blocks it, the `light.turn_off` calls, results and states after, and any switch-on after a cleanup with its source. The occupancy diagnostics show each room's `preliminary_signals`.

### Upgrade

- The engine change applies only to rooms that have a preliminary light and have it enabled. A Home Assistant restart is needed after updating, because the integration's Python code is loaded at start; a browser reload is needed for the card.

## 0.3.10 — Fix: preliminary lights stayed on when possible occupancy was not confirmed

### Fixed

- **A light switched on for possible occupancy could stay on after the timeout.** Root cause: the stage switched a light off only while the controller still held its own last command for it. Every light state change that was not recognised as the controller's own erased that record, and so did a real device's own state report: a **member of a light group** (the Kamar Norður spots) reporting through Zigbee2MQTT with a context of its own, or a light reporting late (after 20 s) or with a rounded brightness. The timeout fired and the cleanup ran, but it concluded "changed since" and left the light on. Then nothing else switched it off: a door-only stage never makes the room occupied, and *Leave unchanged* or a *Vacant mood* leave lights on at vacancy. The 0.3.9 tests used simulated lights that reported back with the controller's own context, so they never hit this.
- The ownership rule is now explicit. The stage owns the lights it switched on (never one that was already on). It lets go only when someone else took the light: a manual override, a change attributed to a person or an automation, or a brightness more than 5 points away from what it set. Device reports are not such evidence. The same rule applies to *Turn off* on confirmation, which had the same fault.
- **A switch-off that could not be sent was dropped**: the room set to *Observing* or Main Mood *Manual* when the timeout came, an unavailable light, or a Home Assistant error. It is now kept as due (persisted) and sent when the room is Automatic again, when Manual ends, when the light is back, or on a retry every minute (up to 5 times). Observing still never sends anything. It is dropped when occupancy is confirmed, because the light then belongs to the room. The vacancy action leaves such lights to it, so no light gets two commands.

### Added

- Decision log: the start entry gives the timeout deadline. The confirmation entry says how long the room waited and that the timeout was cancelled (also noted on the start entry when nothing was switched on). The end entry gives each light's cleanup result: switched off (verified), failed (retried), already off, left (manual override / changed by a person or an automation / brightness changed), or blocked (Observing, Manual) with when it will be done. Retries get their own entries.
- Room card (*Settings → Automatic → Possible occupancy*): *Still to switch off: …* while a switch-off is due. Room diagnostics: `preliminary.cleanup_due`.

### Tests

- New `tests/test_preliminary_cleanup.py` with lights that behave like Zigbee2MQTT devices (own-context reports, group members, rounded and late brightness reports, failures). It covers: the Kamar Norður spots and pilot entities timing out, repeated motion, unavailable sensors, confirmation before the timeout, a light already on, manual changes, Vacant mood, an unavailable light, a failed switch-off, Observing, Manual mood, a restart while due, and no double commands at vacancy. 9 of these 15 tests fail on 0.3.9.

## 0.3.9 — Settings submenu and possible occupancy lighting

### Added

- **Possible occupancy lighting** (per room, *Settings → Automatic → Possible occupancy*; `preliminary.py`). When the occupancy engine reports **possible occupancy** from a selected signal (default: the room's Trigger/PIR sensors; radar, PC activity and doors can be ticked), chosen lights that are off are switched on dimly: brightness, optional colour temperature, transition, per light. The normal lighting waits for confirmation. This replaces the first-motion switch-on in that room. On **confirmation** the normal lighting is applied first, then each preliminary light is **handed over** or **switched off**. The occupied scene has precedence: a light it switches on is never switched off. A light it would not switch on because of daylight is switched off. *Turn off* keeps the light when nothing else in the room is on. **Without confirmation** the lights it switched on are switched off after the timeout (default 30 s, not extended by more motion) or when the room is vacant again, whichever is first. Lights a person or another automation changed meanwhile are left alone. One stage per visit; a room that confirms at once skips it.
- **Daylight for the preliminary light**: the room's daylight policy, sensors and fallback for unknown readings, and by default its threshold. An optional preliminary threshold (room, or global default) inherits the normal value when empty. Decided at the start of the stage and again if the room becomes dark during it. Nothing is switched off because of daylight.
- **Global defaults** (enabled, brightness, transition, timeout, light level, confirmation behaviour) with room and per-light overrides, shown as *Room override* / *Global default* with *Use global default*.
- **Decision log**: *Preliminary light on* / *Keep preliminary light off* (daylight, uncertain daylight, manual override, a person switched the room off, Manual mood, unavailable or failing light, Observing), *Preliminary light handed over* / *Preliminary light off* on confirmation, and *Preliminary light off* when possible occupancy was not confirmed.
- Websocket: `smart_occupancy/lighting/preliminary/set` (admin; with `area_id` a room's values, `null` = global default; without it the global defaults). The `editor` room model has `preliminary` (settings, global defaults, effective values with their sources, candidate lights, signals, the running stage). Room diagnostics include the effective settings and the stage.

### Changed

- **Room card navigation**: the main bar is **Overview · Log · Lights · Overrides · Preview · Settings** (gear). Settings opens a compact second row, **Automatic · Presence · Switches · Advanced**, with an icon and name each, the selected one highlighted, a **Back** button, tooltips, arrow keys / Home / End, and Escape back to the gear. It remembers the last section. Nothing wraps out of sight on narrow cards.
- The **Overview** shows the room's **Observing / Automatic** control (double-click the status, or the selector), and every wall switch's **Observing / Active** control. Switch settings stay in *Settings → Switches*, with the same controls. It also shows *Occupancy now* (with a running possible occupancy stage) and *Possible occupancy*.
- While a preliminary light is on, gradual global-adjustment updates and mood changes leave it alone until occupancy is confirmed. The vacancy action leaves it to its own stage, so no light gets two commands.

### Upgrade and compatibility

- The lighting store is migrated from 1.4 to 1.5: global defaults with possible occupancy lighting **disabled**, and every room without a preliminary light. **No room's behaviour changes** until a preliminary light is chosen and the setting is enabled. A 0.3.8 install reads a 1.5 store and ignores the new keys.
- Tested with simulated sensors and lights only; not verified on real devices.

## 0.3.8 — Vacancy lighting, Presence tab, icon tabs

### Fixed

- **Lights stayed on after a room became vacant.** Root cause: there was no vacancy action at all. The controller cleared manual overrides on vacancy and logged *"Smart Occupancy does not switch lights off when a room becomes vacant"*; vacancy shutdown had been left for a later phase. No setting was being ignored, no override or profile was holding the lights, and it affected every room, not only Kamar Norður.
- **A presence sensor could show "reports a state that is not understood" until it was removed and re-added** (seen with the Nobara PC activity sensor). Root cause: each signal's state was read when the occupancy engines loaded, but the state subscription started only later, after the lighting store and the first refresh. A signal that changed in between (typical while Home Assistant starts and a PC bridge connects) kept its old value in the engine (*unknown* while still unavailable) until it changed again. Home Assistant meanwhile showed it as available, so the health check flagged it. Removing and re-adding it reloaded the integration, which hid the problem. The engines now read every signal again when the subscription starts. Binary sensors were always interpreted from their raw `on` / `off` / `unknown` / `unavailable` state; display labels are never used for decisions.

### Added

- **When the room becomes vacant** (per room, *Automatic* tab): **Turn off** (lights that are on are switched off), **Vacant mood** (a chosen mood's profile is applied to the lights that are on: On with its brightness and colour, Off, or left unchanged per light type; no light is switched on in an empty room), or **Leave unchanged**. The action runs once, when the occupancy engine confirms vacancy. Its timeout is the room's existing vacancy timeout (*inactivity timeout*, or *exit confirmation* with exit evidence), so there is no second timer. Presence that returns during the timeout cancels it, and nothing is switched off and on again. Ambient (persistent) lights keep following the mood. Manual mood, Observing, unacknowledged automation conflicts, exclusions and groups apply as for every mood command.
- **Manual overrides at vacancy**: an override made while the room was occupied ends with the vacancy, as before, and the vacancy action then applies to that light. An override made after the room went quiet (a person changed a light in the empty room, e.g. from the app) is respected until it expires; then the vacancy action runs for that light if the room is still vacant. A light that was unavailable at vacancy is handled when it comes back.
- **Presence tab** (room card, after Automatic): every signal assigned to the room, with its name, entity id, kind, role, live state (labelled by device class, e.g. *Detected / Clear*, *Open / Closed*, with the raw state beside it), what it contributes now (*Active, Clear, Not used here, Unavailable, Unexpected state, Stuck*), and when it last changed. A switch per signal **includes or excludes it from this room's occupancy** without deleting it, changing the entity, or touching other rooms. The room's **presence settings** (vacancy timeout, fast vacancy timeout, occupancy confirmation, radar dropout bridge; more under *More presence settings*) are each marked **Global default** or **Room override**, with *Use global default* per setting, *Use global defaults for all*, and the **global defaults** (all rooms) in the same tab.
- **Decision log**: *Vacancy timeout started* (only when lights are on and the action would change them), updated in place to *Vacancy cancelled* (presence returned, with how long the room was quiet) or *Vacancy timeout expired*; then the vacancy result: *Lights off*, *Apply vacant mood …* or *Leave lights unchanged*, with the result *Executed*, *Sent · not confirmed*, *Partly failed*, *Failed* (unavailable or missing lights, Home Assistant errors), *Blocked* (manual override, Manual mood, no mood), *Observed only*, *No change needed*, or the new **Not done (setting)** for Leave unchanged (*"Lights left unchanged: room vacancy behavior is configured to Leave Unchanged."*). Repeated interruptions of the timeout fold into one counted entry. A deferred action that runs later (override ended, light back) is logged with that trigger.
- Websocket: `room/set` takes `vacancy_action` and `vacancy_mood`; `presence/sensor` (include/exclude) and `presence/timing` (global or room values, `null` = use the default, `reset_all`); the `editor` room model has `presence` and `vacancy`. All writes are admin only.

### Changed

- **Room card tabs are compact icons** (Overview · Log · Lights · Automatic · **Presence** · Switches · Overrides · Preview · Advanced, same order). The name is shown in a tooltip on hover and keyboard focus, is the accessible label, and is written beside the selected icon (below the bar on narrow cards). Arrow keys, Home and End move between tabs. The bar wraps instead of scrolling, so tabs after *Overrides* are no longer cut off.
- Presence settings changed in the card are applied at once, without reloading the integration. The engines keep every signal's value and re-evaluate, so there is no start-up hold and the quiet period is not restarted. Changes made in *Configure* still reload, as before.
- An excluded signal no longer counts for room health or the "no occupancy signals" check.

### Upgrade and compatibility

- The lighting store is migrated from 1.3 to 1.4: every room gets **Turn off** as its vacancy action (there was no setting to keep). Everything else is unchanged. **Rooms in Automatic will now switch their lights off when they become vacant.** Choose *Leave unchanged* in the Automatic tab to keep the old behaviour. Ambient lights are never switched off by vacancy, and Observing rooms send nothing.
- Switching a vacant room to Automatic does not switch anything off. The action runs when a room *becomes* vacant.
- Options: a signal's `enabled: false` is written only for excluded signals, so options saved before 0.3.8 keep their exact shape. 0.3.7 ignores the key after a downgrade (an excluded signal would be used again) and ignores the store's `vacancy` key.

## 0.3.7 — Decision log: what Smart Occupancy decided, why, and whether it worked

Observability only: automatic lighting, occupancy, daylight, overrides, switch mappings and operating modes behave as in 0.3.6. No setting is changed by the upgrade; the decision log starts empty.

### Added

- **Decision log** (`decisions.py`, `lighting_decisions.py`): a structured record (id, timezone-aware time, module, room or switch, trigger, kind, action, explanation, mode at that time, result, target, details) for each meaningful decision, built by the backend from the inputs it actually used (plan and per-light decisions, occupancy result and its inputs, daylight gate and light level, mood, overrides, commands sent, Home Assistant's errors). At most 10 per room and 10 per switch.
- **Results**: `executed` (accepted, and the targets reported the requested state; checked at once and again after 5 s plus any transition), `dispatched` (accepted, not confirmed), `partial`, `failed`, `observed`, `blocked`, `no_change`, `no_action`, `noted`. Nothing is shown as executed only because it was sent.
- **Observing rooms** also log what a mood change or a room mood would do (evaluated only, nothing is sent).
- **Room card**: *Last decision* at the top of Overview; a new **Log** tab (after Overview) with the last 10 decisions and their details on demand (keyboard accessible). **Switch cards**: *Last decision* and a **Log** of the switch's last 10 presses. Updates live in every open dashboard.
- Websocket: the `editor` room model has `decisions` (`latest`, `entries`, `size`), and each switch item too.

### Logging rules

- Logged: lighting applied or evaluated (visit, darkness, mood, room mood, profile change, activation, reconnection), gradual updates, manual overrides (also from switches), vacancy with lights left on or overrides cleared, a queued change dropped by Manual mood, Observing being set, switch presses and learning.
- Folded: the same decision with the same result updates the newest entry (count, time; within 15 min; gradual updates always; button repeats within 1 min); a confirmed occupancy after the first motion of the same visit adds nothing; several state reports of one manual change are one entry; a learning session is one entry.

### Storage

- Kept in the existing lighting state store (`smart_occupancy.lighting_state`, key `decisions`), written by its delayed, atomic save. No entity, helper or Recorder history; decisions are not entity attributes. A removed switch's log is removed with it.

### Changed

- Room card tabs: Overview · **Log** · Lights · Automatic · Switches · Overrides · Preview · Advanced. The switch card's *Result* row became *Last decision* (the old row is shown until the first logged press).

## 0.3.6 — Operating modes on the main screens; wall switch actions no longer suppressed by Observe

### Fixed

- **Active wall switches did nothing in rooms whose mood lighting was not enabled.** In 0.3.5 every switch action had two gates: the switch's own mode (*Smart Occupancy control*) **and** the room's mood-lighting activation, which starts in Observe and could only be changed under *Advanced*. Presses were received and logged ("the room is in Observe: no command is sent"), but nothing was executed, while the switch card still showed *Smart Occupancy control* as if it worked. Switch actions are now gated by the switch's own mode only (`assert_switch_control_permitted`), checked again when a queued press runs; the room's automatic lighting mode no longer blocks them. Automatic lighting in an Observing room still sends nothing.

### Added

- **Automatic → Automatic lighting** shows the room's operating mode first: **Observing** / **Automatic** (also *Automatic · paused* by Manual mood and *Automatic · blocked*, with the reason). Double-click the status to switch between Observing and Automatic (tooltip), or use the **Observing / Automatic** selector. It uses the existing `room/activate` / `room/deactivate` commands, so the change is the real, persisted backend state; the full review stays under *Advanced*.
- **Switches → every switch card** shows its mode with the same controls (**Observing** / **Active**): double-click the status or use the selector, without opening *Configure*. Enabled automations that also react to the switch are acknowledged right on the card; a directly bound switch cannot be set to Active. Mappings and buttons are never changed by a mode change.
- The switch model has `operating` (`mode`, effective `state`: observing / active / blocked / attention, `blocked_by`, `text`): a switch is never shown as plainly *Active* while its actions are suppressed (room not enabled) or cannot arrive (no input).
- **Press outcomes**: `observed`, `not_mapped`, `blocked`, `succeeded`, `failed` in the switch status (`last_outcome`, persisted over restarts) and in each history entry; shown as *Observed / Not assigned / Blocked / Executed / Failed* on the card and in *Recent presses*. Dispatches are logged at debug level, executed / failed / blocked presses at info, observed presses at debug only (no new log noise, no Recorder events).

### Changed

- Wording: *Listen only* → **Observing**, *Smart Occupancy control* → **Active** (switches); *Observe / Active* → **Observing / Automatic** (room). Stored values (`listen` / `control`, `control_enabled`) are unchanged: no migration, no mode is changed by the upgrade.
- `LightingController.async_switch_lights` takes the `switch_id` and checks that switch (`switch_active`), not the room's activation.

## 0.3.5 — Native wall switches, remotes and Zigbee2MQTT button control

Wall switches and remotes become part of Smart Occupancy: assign a switch to a room, learn its buttons, give each button an action, and every press counts as a manual change that automatic lighting respects. The upgrade sends no command and keeps every setting; every new switch starts in **Listen only**. Tested with simulated Zigbee2MQTT messages, device triggers and event entities through Home Assistant's MQTT integration; **not tested with physical Wiser hardware**.

### Added

- **Switches tab** in Smart Room Lighting (Overview · Lights · Automatic · **Switches** · Overrides · Preview · Advanced): assigned switches with connection, availability, last button and result, what they control, buttons configured and an active manual override; *Configure*, *Learn buttons* and *Remove*. The button table lists the device's actual button events with an action each (unassigned buttons do nothing). *Advanced switch settings*: input source, MQTT topic, event entity, manual override time, dimming, debounce, activity evidence, the Zigbee2MQTT base topic, conflicts and listener diagnostics.
- **Device selection** instead of typing entity IDs: Home Assistant devices with Zigbee2MQTT identities, MQTT device triggers or event entities, and Zigbee2MQTT devices from its device list, with name, manufacturer, model, sources, availability, known button actions, direct bindings and the room they are assigned to. Manual MQTT topics (exact topics only, no wildcards) or event entities under *Advanced*.
- **Input sources** (`switch_inputs.py`), one authoritative per switch:
  - **Zigbee2MQTT** through Home Assistant's MQTT integration: the `action` of the device topic (`zigbee2mqtt/avb_ksu`), base topic configurable (default `zigbee2mqtt`); retained messages, battery and link-quality updates, cleared actions and malformed or oversized payloads are ignored; the availability topic is read; Zigbee2MQTT's retained device list gives declared actions, bindings and renames (followed by IEEE address). Subscriptions to the assigned switches' topics only; subscribed again after an MQTT reconnect or reload; nothing is published.
  - **MQTT device triggers**, attached like automation triggers (replays right after attaching are ignored).
  - **Event entities**: the action is the `event_type` (or `button` + `event_type`); the state is a timestamp; restored and stale events are ignored.
- **Normalised button events** (device, source, raw action, button, press type, time, reference) and **deduplication**: the same press through raw MQTT, a device trigger and an event entity is executed once; deliberate repeated presses are kept; repeats within the debounce time (default 100 ms) count once.
- **Learn buttons**: listens on every available path for 60 s (cancel with *Done*), shows the detected action, saves learned buttons at once (persisted), and never executes anything while learning. Press types are taken from the device, never synthesised. Buttons the device declares are added when the switch is assigned; recognisable ON / OFF / toggle / brightness buttons get a suggested action.
- **Button actions**: Turn lights on (current mood, last used or fixed values) · off · toggle · increase / decrease brightness · start dimming up / down · stop dimming · set brightness · set colour temperature · select mood (this room, optionally for N minutes, or all rooms) · return to Auto · activate scene · run script · activate WLED preset · no action. Targets: the room's managed lights (through managed groups) or one light or group. Capabilities are respected. Only these fixed actions on validated entities; no templates or arbitrary services.
- **Listen only / Smart Occupancy control** per switch. Control is an explicit, confirmed activation; it is refused while Zigbee2MQTT reports a direct binding of the switch to lights or groups, and while an enabled automation that reacts to the switch (device trigger, event entity or MQTT topic) is not acknowledged. Bindings and automations are never changed.
- **Manual overrides from switches** (`switch_control.py`): every press marks the room's next light changes as a person's (also for directly bound switches in Listen only); executed commands register the override directly (switch, action, reason) for the room's or the switch's override time, so occupancy, moods and the global adjustment do not undo the choice. ON is not gated by daylight. Overrides are shown in *Advanced → Manual overrides* and in the switch's card.
- **Manual mood**: wall switches keep working while Main Mood is `Manual` (automatic lighting stays paused).
- **Mood buttons**: a room mood uses the existing room mood override; *all rooms* selects the option on the Manual Mood selector and lets your Main Mood automation resolve it. Main Mood is never written. Moods are discovered from the selectors.
- **Hold-to-dim**: start/stop dimming with one paced brightness command per interval (default 8 % every 0.4 s), waiting for each command, stopping on STOP/release, at the minimum or maximum brightness, or after a safety timeout (default 10 s); brightness steps per press for remotes that repeat messages. Dimmed brightness is kept as an override.
- **Activity evidence** (optional, off by default): a press refreshes the activity of an occupied room in the existing occupancy engine; it never occupies an empty room.
- **Websocket commands** (admin only): `smart_occupancy/lighting/switch/candidates`, `…/add`, `…/set`, `…/mapping`, `…/mode`, `…/learn`, `…/remove`, `…/settings`. The room model of `smart_occupancy/lighting/editor` has `switches`.
- `scripts/component_requirements.py`: installs the libraries a Home Assistant release declares for the components the tests start (used by CI on 2026.11.1).

### Changed

- The integration now lists `mqtt` and `device_automation` as after-dependencies (optional: without MQTT, event entities still work).
- Diagnostics include the switch inputs (MQTT state, sources, last events and results; never raw payloads).

### Configuration

- **Lighting store 1.2 → 1.3**: `switches` per room (empty after the migration) and the global `switch_settings` (base topic). Every other setting is kept unchanged. The wall switch entities of earlier versions (`manual_switches`) are kept and stay listen-only sources; none is converted into a command source. 0.3.4 can still read a 1.3 store.
- **State store**: the last event and result of each switch and temporary room moods.

## 0.3.4 — Room lighting card cleanup

A dashboard-only release. Lighting behaviour, the occupancy engine, the websocket API and every saved setting (including the chosen light sensors) are unchanged.

### Changed

- **Smart Room Lighting** tabs: **Overview · Lights · Automatic · Overrides · Preview · Advanced**. The tabs are the only navigation: the *Review lights*, *Configure overrides*, *Preview room* and *Advanced* buttons at the bottom of Overview are gone.
- **Automatic** tab (new): the automatic lighting settings moved here from Overview (turn on when occupied and dark, indoor light sensor, the inherited outdoor light, darkness threshold, daylight now, natural light, last decision). Sensor priority, contamination, calibration, hysteresis and diagnostics are in the collapsible *Advanced daylight settings* in the same tab, and no longer in the general Advanced tab.
- **Overview** is compact: mood, mode, occupancy and lighting health, managed lights, last applied, the global adjustment, a one-line daylight summary and warnings. No settings forms.
- **Outdoor light sensor** is shown as one global setting. It is configured in the Smart Lighting card (*Advanced → Settings → Outdoor light (all rooms)*); a room's Automatic tab shows it as inherited, with *change for all rooms*, which edits the same global setting.
- Slightly narrower tabs on phones.

## 0.3.3 — Daylight-aware occupancy activation

Occupancy switches a room's mood lighting on when someone arrives **and it is dark**. The daylight decision says whether lights are switched on; the mood profile still says what they look like. Nothing is switched off because of daylight, and no brightness or colour is derived from lux. The upgrade sends no command and keeps every setting.

### Added

- **Automatic lighting** in the Smart Room Lighting card (Overview): *Turn on when occupied and dark*, an indoor light sensor picker (illuminance sensors, the room's own first), the inherited outdoor sensor, a darkness threshold slider, and *Daylight now* (*Dark* / *Bright* / *Uncertain*) with a reason and how natural light was determined (measured or estimated). *Last decision* says what the last arrival or darkness change did and why.
- **Daylight decision per room** (`daylight.py`, `daylight_monitor.py`):
  - evidence: sun after civil dusk; indoor readings with the lights off and settled; "at most" readings with lights on that are still dark; outdoor darkness; the most recent indoor evidence; outdoor light × the room's daylight factor; twilight
  - **lamp contamination**: managed lights, group members, lights in the area and configured extra lights or switches mark indoor readings as not usable for daylight while on, and for a settling time after they turn off. A reading counts only if it was *taken* after that (a changed value by default, or every report). Bright readings with lights on are never used; the last trustworthy reading and its time are kept
  - several indoor sensors per room in priority order (preferred + fallbacks), each with a maximum age, a report mode and a reliability
  - optional global outdoor sensor, smoothed with a time-weighted median (spikes are ignored)
  - learned daylight factor (median of trusted indoor/outdoor pairs) or a factor set by the user
  - hysteresis (turn on below 50 lx, not needed from 100 lx) and a stability period (90 s) for changes while occupied; arrivals use the latest evidence at once
  - `unknown` / `unavailable` / invalid readings are never 0 lx
- **Switch-on triggers**: the first motion of a Trigger sensor (PIR) starts a visit at once (*Advanced → Switch on at → Confirmed occupancy* for fewer false triggers); radar and other Maintain sensors hold occupancy; an occupied room becoming dark; mood changes and the other existing triggers.
- **Policies** per room: *When occupied and dark* (default), *When occupied, regardless of daylight*, *Never automatically*; *When uncertain*: *Turn lights on* (default) or *Leave lights off*. Ambient (persistent) settings are never gated.
- **Advanced → Daylight**: policy, timing, fallback, thresholds, stability, settling, sensor priority and freshness, influencing lights, calibration (learned/set factor, reset), the global outdoor sensor, and daylight diagnostics.
- **Entity** `sensor.smart_occupancy_<room>_daylight` (`dark` / `bright` / `uncertain`) with the natural-light text, rounded lux, confidence, basis, contamination, sensor, outdoor lux, sun elevation, reason and the last activation. Attributes are not recorded; it is written only when the rounded summary changes.
- **Daylight health** in the room health (separate from occupancy and lighting) and problems: light sensor missing / not usable (with the fallback in use), outdoor sensor missing / not usable, influencing light missing, a sensor that reads bright after dusk (a light not listed as influencing).
- Plans and previews report the daylight decision; blocked lights say why (`daylight`, `daylight_uncertain`, `auto_on_never`). The mood lighting sensor has an `auto_on` attribute (policy, last activation and its reason).
- Websocket commands `smart_occupancy/lighting/room/daylight` and `smart_occupancy/lighting/daylight/set` (admin only).

### Changed

- Rooms switch on at the first PIR motion instead of waiting for confirmed occupancy, except rooms that were already activated (see Configuration).

### Configuration

- **Lighting store 1.1 → 1.2** (`.storage/smart_occupancy.lighting`): `daylight` per room and globally. Rooms with mood lighting already enabled keep switching on at confirmed occupancy (`on_first_motion: false`). Everything else is kept. 0.3.2 can still read a 1.2 store.
- **State store**: the last trusted indoor readings and the calibration samples per room.
- Without a light sensor nothing changes in practice: daylight is uncertain (or dark by the sun) and the default fallback switches lights on as before.

## 0.3.2 — Global light adjustment and Manual mode

Moods, light types and single lights can follow your **existing** adaptive brightness and colour-temperature system. Smart Occupancy has no adaptive-lighting engine of its own: it reads the values your system publishes. The upgrade changes no setting and sends no command. Every room keeps its Observe/Active state.

### Added

- **Global light adjustment** (Smart Lighting → *Global light adjustment*):
  - an optional brightness source and an optional colour-temperature source, chosen independently
  - live values and a status: *Connected*, *Unavailable* or *Not configured*
  - supported sources: sensors, `number` and `input_number` helpers, lights, and Adaptive Lighting switches (`brightness_pct`, `color_temp_kelvin` / `color_temp_mired` attributes)
  - unit handling: % or 0–255, and K or mired, detected automatically or chosen under *Advanced*
  - values arrive through state-change subscriptions, not polling
  - an unavailable source is never read as 0: the last good value is used (it is persisted), or the property is skipped and reported
- **Per-profile modes for brightness and for colour temperature**, independent of each other: *Follow global*, *Global + offset* (percentage points / K), *Fixed*, *Ignore*. Not set means inherit. Results are clamped to 1–100 % and 1000–10000 K, then to the light's own range.
- **Every mood** entries (pseudo mood `*`), for global light types, room light types and single lights or groups. They apply in every mood unless a mood sets its own value, and they never switch lights on or off.
- **Colour and effect precedence**: an RGB/HS colour or an effect is never overwritten by the global white temperature. RGB lights can follow the global brightness. Colour and temperature are never sent together.
- **Capability awareness**: from `supported_color_modes`, brightness-only strips follow brightness only, colour-only lights get no global temperature, and on/off lights get neither (reported as information).
- **Gradual updates** when a global value changes:
  - only for lights that are already on; a global change never switches a light on
  - only meaningful changes (default 3 % / 100 K)
  - at most one command per light per interval (default 60 s), with coalescing so the newest value wins
  - optional transition (default 10 s) where supported
  - only the properties that follow the global value are sent
  - the usual protections apply: manual overrides, exclusions, groups, availability, occupancy, conflicts, Observe
  - a light switched on later by something other than a person gets the current values right away
- **Competing controllers**: an Adaptive Lighting switch used as a source, and enabled automations or scripts that use a source entity and command lights, are detected. Their lights get no global updates from Smart Occupancy, and a warning explains why. Nothing is disabled or changed.
- **Manual mode**: Main Mood = `Manual` (configurable) pauses all automatic lighting.
  - No command is sent for moods, occupancy, activation, profile edits or global updates.
  - Queued applications and updates are dropped and never replayed.
  - Manual light changes are not corrected or recorded.
  - Occupancy, diagnostics and editing continue.
  - Leaving Manual applies the newly selected mood with the current values, in activated rooms only.
  - Manual beats every mood, room mood and adjustment setting. It needs no profile and is not listed as a mood.
- **Preview**: each light shows its global-adjustment state (*will/would follow*, *up to date*, *queued*, *blocked* with the reason, *not adjusted*).
- **Room card**: one line per light, *Brightness: Global · Temperature: Global · Effective now: 62 % / 3200 K*, and a *Global adjustment* line in the Overview.
- Websocket command `smart_occupancy/lighting/adaptive/set` (admin only), and `manual_option` in `lighting/settings/set`.
- Room lighting sensor state `paused`; attributes `manual_mode`, `global_adjustment`, `last_adaptive(_at)`, `adaptive_conflicts`. The room status sensor has `lighting_paused`.
- Problems:
  - `global_source_not_configured`
  - `global_source_unavailable`
  - `adaptive_controller_conflict`
  - `lighting_manual_mode` (info)

### Changed

- The room card says *default* (instead of *follows global*) for light types without a room override, so it can't be confused with *Follow global* brightness.
- A light's own-change window now includes the transition time, so a light still fading after our command is not mistaken for a manual change.

### Configuration

- **Store**: `.storage/smart_occupancy.lighting` keeps version 1.
  - New keys: `adaptive`, `manual_option`.
  - Profile entries can have `brightness_mode`, `brightness_offset`, `color_temp_mode`, `color_temp_offset`.
  - A value without a mode stays fixed, so every 0.3.1 profile behaves exactly as before.
- **State store**: the last good global values.

## 0.3.1 — Simple dashboards

A usability redesign of the 0.3 cards: simple by default, advanced on request. No lighting behaviour changes; Observe stays the default and nothing is applied by the upgrade.

### Changed

- **Smart Lighting Profiles** (Global Mood Editor):
  - Pick a mood and see one line per light type ("Loft ljós — 40 % · 2700 K", "Uppljós + RGB — Amber · 30 %"), with a colour dot.
  - Types with nothing set are one line of chips. Types without lights are hidden. No blocks of empty categories or "Ignore", and no validation dump.
  - **Focused editor** for one mood and one light type:
    - On / Off / Leave unchanged
    - brightness
    - white presets, colour swatches, effects
  - Only the controls the lights support are shown. Transition, ambient and the exact inherited values are behind *Advanced* in the editor.
  - Preview is a compact per-room list. Inheritance, light types, settings and validation are behind **Advanced**.
- **Smart Room Lighting** (Room Mood Lighting), tabs **Overview · Lights · Overrides · Preview · Advanced**:
  - The **Overview** shows mood, mode, health, managed lights and one line per issue.
  - Lights are grouped by light type; single lights appear only when expanded.
  - Overrides: per type *follow global* / *overridden*, and single lights.
  - Activation, automation conflicts, manual overrides and diagnostics are under Advanced.
- **Room Status card:** three health chips, one short line per warning (*Details* for the full text), and a one-line helper comparison.
- **Short health reasons:**
  - The *Lighting* health lists "2 lighting conflicts (…)", "Some settings not supported by …" and similar short lines.
  - The full explanations are in `details`.
- Lights with several category labels are normal now; the *Light in several categories* warning is gone.

### Added

- **Light type / function / RGB tag** (category *kind*).
  - A light gets the settings of all its labels, merged field by field (types < functions < RGB tag < "type + RGB" < "function + RGB").
  - **"type + RGB" targets** apply only to the RGB-tagged lights of a type, e.g. Veistla *Uppljós + RGB* purple.
- **Uppljós** default function category for new installations.
- **Label suggestions:** existing *Uppljós*/*Rgb*-style labels are offered with one click.
- **Group question:** *As a group* / *Individually* / *Ignore for now*. A warning you ignore becomes a note under Advanced (stored per room in `dismissed`).
- Websocket commands `lighting/room/group_choice`, `lighting/category/set`, `lighting/category/remove` (admin only).

### Configuration

- **Store:** lighting store version unchanged; rooms get an optional `dismissed` list.
- **Categories:** they get a `kind`, derived for existing ones, and are written to the options only when a light type is changed.

## 0.3.0 — Mood lighting, dashboard editors and controlled activation

Every room still starts in **Observe** mode. Light commands are sent only in rooms where an administrator explicitly enabled mood lighting. Existing automations and the mood selectors are never changed.

### Added

- **Generic mood lighting executor.** One executor works for every mood. Hygge is an ordinary mood: there is no special function or button.
  - It follows Main Mood, or a per-room mood.
  - It applies on a mood change, on newly confirmed occupancy (Phase 0.2 engine), on activation, on profile changes (only the affected lights), and when an offline light comes back.
  - Nothing is applied on start-up or restart.
  - Identical commands are batched, and commands already in effect are not repeated.
  - Every decision has a recorded reason.
- **Occupancy rules for lighting:**
  - Vacant and uncertain rooms are not changed.
  - Lights are not switched off on unconfirmed occupancy.
  - *Ambient* settings follow the mood even while a room is vacant.
- **Global mood profiles** per light category, with:
  - On / Off / Inherit / Ignore
  - brightness 1–100 %
  - colour temperature, RGB or HS
  - effect, transition and *ambient*
- **Mood inheritance**: a parent mood per mood, plus a fallback mood. Loops are refused.
- **Three-level inheritance**, merged field by field: global → room override → individual light/group override. Values are adapted to each light's capabilities, and what can't be applied is reported.
- **Moods are discovered** from the three selectors. New moods appear without code changes, and moods missing from Main Mood are explained.
- **Configurable handling of moods Main Mood cannot represent** (e.g. `Onki`, which is not treated as Auto): follow Main Mood (default), no mood, or another mood. A warning names the missing option.
- **Label-based discovery**: labelled lights and groups in a room's area are managed automatically. Excluding is possible per room or with an exclusion label.
- **Group de-duplication**: a managed Home Assistant light group is commanded instead of its members. Groups that hide their members (Zigbee2MQTT, Hue) are reported.
- **Per-room activation** in the Room Mood Lighting card:
  - a review of the lights, the exclusions, the groups and the existing automations that appear to control the same lights
  - explicit two-step confirmation, administrators only
  - lights of enabled automations that you have not acknowledged are left alone
- **Automation conflict detection** (read only). It follows the automations' actions through entity, group, member, device, area, floor and label targets, scenes and scripts.
- **Manual overrides**:
  - App/UI changes, and wall-switch changes (configured switch entities), are respected for a configurable time (default 60 min; 0 = until vacant).
  - Overrides are cleared on vacancy or from the card.
  - Our own commands, including MQTT echoes, and other automations are not mistaken for manual changes. Changes without any context are not manual by default.
- **Dashboard cards** (served by the integration, with visual editors):
  - **Global Mood Editor** (`custom:smart-occupancy-mood-editor-card`)
  - **Room Mood Lighting** (`custom:smart-occupancy-room-lighting-card`)

  Both have:
  - inherited/overridden display with sources
  - inline validation and immediate saving
  - a live preview that never sends commands
  - light/dark themes and phone layout
  - no polling (one subscription)
- **Websocket API** (`smart_occupancy/lighting/*`): authenticated, and every write is `require_admin`.
- **Per-room sensor** `sensor.smart_occupancy_<room>_mood_lighting` (`observe` / `active` / `blocked`). Its attributes hold:
  - the global and room mood
  - managed, offline and unsupported lights
  - conflicts and manual overrides
  - the last applied profile, with its time, result and reason
- **New problems:**
  - mood not in Main Mood, mood cannot be represented, unknown or looping parent moods
  - managed light in another area, possible duplicate group commands, automation conflict
  - unsupported or adapted light settings, failed command
  - missing wall switch, room mood unknown

### Changed

- **Mood profiles are stored in `.storage/smart_occupancy.lighting`**, owned by the integration and saved without a reload. On the first start the 0.2 profiles are copied there. The config entry options are not changed (a downgrade still loads).
- The options-flow *Mood profiles* step edits the same store, without reloading.
- **Managed lights** of a room are now the configured lights plus the labelled lights of its area.
- **Health split:** mood-lighting problems are shown under *Lighting*, never under *Occupancy*.
- A group and its member in the **same** room is now an info note (de-duplicated automatically). In **different** rooms it is still a warning.
- Profile brightness is 1–100 % (use *Off* instead of 0). Several colours in one entry are reported as `several_colors`.
- The Room Status card shows *Mood lighting* instead of *Observe only* for activated rooms.

### Recorder and efficiency

- No polling.
- One state subscription for:
  - the mood helpers
  - managed lights, and the members of managed groups
  - wall switches
- Registry-derived data (labels, areas, categories) is cached and invalidated by registry events. Automation references are cached and refreshed on reloads and on enable/disable.
- Every attribute of the new sensor is excluded from the Recorder.
- Runtime state (overrides, last applied profile) is kept in `.storage/smart_occupancy.lighting_state`, with delayed writes.

## 0.2.0 — Occupancy engine and Room Status card

Still **Observe only**: no light is controlled, no automation or helper is changed, nothing acts on the new occupancy.

### Added

- **Occupancy engine** for every enabled room, using the configured sensors and their roles:
  - *Trigger* (PIR): immediate occupancy; a trigger that stays on longer than *Trigger stuck after* (30 min) is ignored as stuck.
  - *Maintain* (radar, PC activity, bed): while active, vacancy can't be confirmed. Clears shorter than the *dropout grace* (30 s) are bridged.
  - *Confirm vacancy*: like Maintain, and its cleared state counts as vacancy evidence (vacancy after *exit confirmation* instead of the *inactivity timeout*).
  - *Door contacts* count only as movement: never occupancy on their own, but a door movement followed by nothing (optionally with movement in an *adjacent room*) is exit evidence.
  - `unknown` and `unavailable` are never treated as "off". A presence sensor that drops out while reporting presence holds the room for an extra inactivity timeout. If every sensor is unavailable the room is `uncertain` and keeps its last state.
- **States:** `vacant`, `possible_occupancy`, `confirmed_occupancy`, `possible_vacancy`, `uncertain`. The full rules are in the README.
- **Timing**, configurable globally (*Configure → Occupancy timing*) and per room (*Configure → Rooms → room → Occupancy*):
  - correlation window: 10 s
  - exit confirmation: 300 s
  - inactivity timeout: 600 s
  - dropout grace: 30 s
  - trigger stuck after: 1800 s
  - presence stuck after: off
  - start-up hold: 120 s
- **Per room entities:**
  - `binary_sensor.smart_occupancy_<room>_occupied`
  - `sensor.smart_occupancy_<room>_state`, with the evidence in its attributes
  - `sensor.smart_occupancy_<room>_confidence`
  - `sensor.smart_occupancy_<room>_last_activity`
  - `sensor.smart_occupancy_<room>_reason`
  - `sensor.smart_occupancy_<room>_helper_comparison`, when an existing occupancy helper is configured
- **Comparison with the existing helper:**
  - agreement
  - the number and duration of disagreements, counted only when they last longer than the correlation window
  - premature clears by the helper
  - possible over-retention by the engine, with its duration
  - helper-only occupancy

  Counters are kept in `.storage`. The helper is only read.
- **Health split** per room into *occupancy*, *lighting* and *configuration*, built on the Phase 0.1.1 problems. An offline light is a lighting warning; it does not make occupancy look degraded.
- **Smart Occupancy Room Status card** (`custom:smart-occupancy-room-card`), served by the integration and loaded automatically. It is read only and responsive, and has a visual editor with room selection.
- **Inverted signals**, for sensors that mean activity when they're off (e.g. a "PC idle" sensor).
- `prediction.OccupancyHistory`: an interface that receives every occupancy transition, for the future learning engine. Phase 0.2 discards them.
- Brand icon (`brand/icon.png`, `icon@2x.png`) and an MIT `LICENSE`.

### Changed

- **Configuration version 1.2** (options `config_version` 2). It is migrated automatically and every existing setting is kept: mood helpers, categories, labels, lights, roles, profiles and reference helpers. Version 0.1.1 can still load a 1.2 entry after a downgrade.
- *Confirm vacancy* now counts as an occupancy role for room health.
- New problems:
  - *Only door contacts* (warning)
  - *Adjacent room not enabled* (info)
- Motion (plain on/off changes of sensors) no longer rebuilds the diagnostics snapshot. Only availability, name, mood and light-capability changes do.

### Recorder and efficiency

- No polling. There's one state subscription for all rooms and at most one timer per room, set only for a pending deadline. A pending vacancy is a single timer, not a countdown: the card counts down in the browser.
- Entities write state only when their state or attributes change. Evidence attributes are excluded from the Recorder. There are no `state_class` values, so no statistics.
- Engine state is kept in `.storage/smart_occupancy.occupancy_state` and is written only on transitions, coalesced over 15 s.

## 0.1.1 — Diagnostic improvements and automatic recovery

Still Observe only. No lights are controlled. Existing configuration is used unchanged (`config_version` 1), and all entity unique IDs are kept.

### Changed

- **Status classification.** *Status* and every *Room observation* sensor now report:
  - `healthy`
  - `warning`
  - `config_error`
  - `unavailable` (the room cannot be observed)

  These replace `ok` / `warning` / `error` and `ok` / `degraded` / `not_configured` / `area_missing`.
- **Optional parts don't make a room unavailable.** A room is `unavailable` only when every Trigger and Maintain signal is unavailable. An offline light or a single offline sensor makes it a `warning`, and the `usable` attribute stays `true`.
- **Operational problems are kept apart from configuration problems.** An unavailable device is an operational problem, so it no longer counts as a configuration problem. A deleted, disabled or wrong-type configured entity is a configuration error.
- **Entity health is more precise.** It now distinguishes:
  - `missing` (deleted)
  - `disabled`
  - `not_provided` (registered, but its integration no longer provides it)
  - `unavailable`
  - `unknown` (no value reported)
  - `pending` (still loading during start-up)

  Before 0.1.1, *disabled* and *not provided* entities appeared as plain "unavailable", with no hint that they never recover by themselves.

### Added

- **Structured problem details on the existing sensors.** Each problem has:
  - type, title, classification, severity and category (configuration / operational)
  - a plain-language explanation and a recommended action
  - the room, the entity ID and its friendly name
  - `first_detected` and `last_checked`

  The new attributes are:
  - *Unavailable entities* and *Missing entities*: `items` (one readable line per entity) and `entities`
  - *Configuration problems*: `problems` and `notes`
  - *Status*: `summary` and `problems`
- **Room diagnostics.** Each *Room observation* sensor now exposes:
  - `configuration_valid`, `usable` and `operating_mode`
  - `reasons`
  - `sensors_available` / `sensors_unavailable`, `lights_available` / `lights_unavailable`, `pending`
  - `problems`
- **Persistent problem history.** `first_detected` survives reloads and restarts. It is stored in `.storage/smart_occupancy.problem_history`, not in the Recorder, and is written only when the set of problems changes.
- **Start-up grace period.** For 120 s after Home Assistant has started, entities whose integration hasn't loaded yet are `pending`, not reported. They're reported afterwards if they still haven't appeared.
- **Re-check diagnostics button.** It re-evaluates immediately. It reads states only.

### Recorder

- All new list attributes and `last_checked` are excluded from the Recorder.
- While problems exist, `last_checked` is republished at most every 15 minutes. A re-evaluation that finds nothing changed writes no state.
- Nothing polls: evaluations still happen only on events.
