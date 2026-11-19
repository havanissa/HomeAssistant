# Changelog

## 0.8.0

Phase A6.1: two extensions of existing behaviour. Needs the companion integration 1.0.0. Nothing changes on
upgrade: both features are off until you configure them.

- **Multi-sensor sunlight** (Curtains → a window → Automatic → *Multi-sensor sunlight*). A window can use several
  illuminance sensors of its own as its sunlight evidence. As soon as **any** enabled sensor reads more than
  *Activate above*, sun protection is requested; it ends only when **every** enabled sensor reads below
  *Release below* for the whole *Release delay*. A sensor that is missing, unavailable, non-numeric, stale, stuck
  or implausible never counts as sunshine, and while protection is on it prevents the release (the window says
  which sensor and why) until you disable or remove it. The levels use the usual window / room / all-windows
  scopes; release must be lower than activation. Sleep, night, evening privacy, manual holds, unavailable covers,
  movement limits and Global Modes keep their precedence: a release re-evaluates the whole decision. The active
  state survives a restart; the release countdown starts again after one, so a restart never shortens it. Sensor
  chips with live values and health, who triggered it, what prevents the release, the countdown, and a read-only
  test with your own readings and elapsed time. Windows without the feature decide exactly as before.
- **Cross-room switch targets** (Switches → a switch → a button → *Lights*). A light action (on, off, toggle,
  brightness steps, hold-to-dim, set brightness, set colour temperature) can target lights outside the switch's
  room: a light or light group, all lights of a room, or several at once. The picker groups this room, adjacent
  rooms (configured adjacency and shared spaces) and, with a search, every other room, shows each light's room
  ("Stairway Lamp (Trappa)"), a summary ("Toggle Stairway Lamp (Trappa)") and the physical lights that will be
  affected. A light switched from another room counts as a manual change **in the room that manages it**, with
  that room's override time; *Resume* there releases it, and that room's occupancy is not touched. Every command
  goes through the same fenced executor; group members are never commanded twice; on/off-only lights never get a
  brightness or colour. Existing mappings keep their exact meaning.
- **Migration from 0.9.0**: these features changed engine code that integration 0.9.0 checks before a lighting,
  curtain or forecast handover. The wizard now refuses those handovers with a clear message: update the
  integration to 1.0.0 and continue with *Activate* (occupancy still hands over as before).

## 0.7.3

- **The Rooms and Presence editors work once the App holds the configuration.** The App runs the rooms and
  presence handlers itself, and they read the config entry's state as Home Assistant's enum; the App's host
  stored a plain string, so every request failed with "'str' object has no attribute 'value'" (Diagnostics:
  *Room configuration API* in error). The host now uses an enum like Home Assistant.
- **Transfer control no longer blocks the wizard with the companion installed.** The step hands control over
  through the 0.9.0 bridges; with companion 1.0.0 there are none, and the App takes each subsystem itself as
  soon as it holds the configuration. Every domain button was then disabled (already the App's) and the step
  could never complete, so *Activate* was out of reach. With the companion the step now offers *Continue
  (nothing to hand over)* and records who owns each subsystem.

## 0.7.2

- **Validate no longer blocks an App installed fresh next to companion 1.0.0.** Its *Configuration
  integrity* check looked only at the App's own configuration and at the 0.9.0 lighting bridge, so with
  neither (a reinstalled App, the integration already replaced) it reported "No configuration" and the
  wizard could not continue past step 3. It now also checks the configuration in the companion's legacy
  data, the one *Transfer config* imports, and says which source it checked.

## 0.7.1

- **Transfer config imports the integration's stores too.** With the companion installed, *Transfer config*
  (and *Activate*, which runs it when the App has no configuration) now also brings the lighting profiles,
  switches, zones and ambient rules, the curtain windows, presets and state, and the learned forecast days and
  settings from the companion's legacy export into the App's storage, each only when the App has no copy of
  its own. Before, an App installed fresh next to companion 1.0.0 (or an App whose storage was reset) started
  its engines with empty lighting, curtain and forecast configurations while the data still sat in the
  integration's retired stores. The review of the step lists which stores would be imported and which the App
  keeps.

## 0.7.0

Phase A6: the App is the product; the integration becomes the companion. Works with
Smart Occupancy & Lighting **0.9.0** (as before, through its bridges, for the
migration) and **1.0.0** (the companion). Nothing changes on upgrade until an
administrator runs the migration wizard.

- **The App holds every configuration.** `CoreConfigStore` (`/data/config/core.json`):
  rooms, sensors, lights, light types, mood source and occupancy timing, with revisions,
  backups, atomic writes, stale detection, renames, export and reviewed import
  (Settings → Migration & System, `GET /api/config/export`, `POST /api/config/import`).
  Lighting, curtain and forecast settings were already the App's.
- **Companion mode of every runtime.** With the companion installed the App claims
  occupancy, lighting, curtains and predictions (epoch, 30 s lease, heartbeats) and
  publishes the native entities' views; lights and covers are commanded only through the
  companion's fenced executor. A lost lease stops the engine at once and the App claims
  again with a new epoch; a restart never reuses a lease.
- **Settings → Migration & System**: who runs what (as the companion acknowledges it),
  the configuration authority, the companion (pair, claim, stop, revoke), engine health,
  and the eight-step wizard: Inspect, Backup, Validate, Transfer config, Transfer
  control, Activate, Verify, Retire — with Abort and Restore legacy data. Every step is
  started by an administrator and recorded.
- **Settings → Setup**: the guided setup of a new installation (safe defaults, no
  automatic control until you enable it per room). Refused while 0.9.0 is installed.
- **Dashboard** rebuilt from the App's own runtimes: house occupancy, rooms (occupancy,
  lighting mode, mood, daylight, prediction), mood, curtains, predictions, unavailable
  devices, health, who runs what, with freshness.
- **Outage behaviour page** (Settings → Migration & System → Outage behaviour): what works
  and what does not while the App is away.
- Rooms and Presence editors run locally against the App's configuration once the App is
  the authority; the "pending module" page is gone.
- Live message `system` for owner / authority / companion changes; `GET /api/system`,
  `/api/dashboard`, `/api/migration`, `/api/setup`.
- Removed: the sync scripts and the byte-identical-copy tests (the App is the only copy);
  `scripts/check_protocol.py` checks the shared companion protocol instead.

## 0.6.0

Phase A5: Curtains and Predictions. Needs Smart Occupancy & Lighting 0.9.0.
Nothing is migrated automatically: the integration keeps controlling the
curtains and producing the forecasts until an administrator performs a reviewed
handover for each.

- **The integration's curtain runtime in the App.** The whole `curtains`
  package (windows, presets, the executor with position verification, the
  automatic rules, Global Modes, the Global Schedule, the environment and solar
  geometry, conflicts, discovery, the 21 websocket commands) is a byte-identical
  copy on the host interface (new: cover features, temperature units and
  conversion, the unit system). Shadow mode by default: a fresh manager from the
  integration's exact copy follows the same events and the published occupancy
  and sends nothing; the comparison matches cover commands (service, cover,
  position within 3 s) and window decisions. **Curtains → Control**: readiness
  (2 h, 5 matched decisions, no command mismatch, no cover moving, same code and
  configuration), the reviewed handover (backup, key-by-key check), covers moved
  only through the integration's fenced `execute` with the App's context,
  configuration and state in `/data/curtains/store` mirrored to the integration,
  release / return / rollback, export and a reviewed import. A handover or a
  return is refused while a cover is moving; a crash of the App hands the covers
  back without a movement; a restarted App never takes control back.
- **Curtains pages**: Overview, Windows (creation, discovery, covers, presets,
  confirmed Apply), Global Modes (assignment matrix with review and policy),
  Global Schedule (timeline, phases, transitions), Environment (sensor quality,
  sun, calibration) and Diagnostics (operations, holds, blocks, conflicts, log),
  one window workspace, on the integration's own commands (forwarded until the
  handover, answered by the App's copy afterwards). The floor plan links drawn
  windows to curtain windows, suggests an orientation from the wall (applied only
  after review) and shows cover positions, modes, holds and warnings live.
- **The integration's forecast runtime in the App.** Learning, the model, the
  history replay, settings and the 5 websocket commands are byte-identical copies.
  History is read through Home Assistant's `history/history_during_period`
  (bounded, one day at a time, significant changes only), never from the
  database; replay and model building run off the event loop. Shadow mode learns
  alongside the integration from its handed-over days and compares predictions.
  **Predictions → Control**: readiness, a reviewed handover that moves every
  learned day (validated and verified), the App publishing its view to the
  integration's unchanged forecast entities and its learned data as the rollback
  copy, release / return / rollback without retraining, export and import.
  Forecasts command nothing on either side.
- **Predictions pages**: House (learning, coverage, next room, every room's
  forecast), Room (probabilities per horizon, entry, confidence and its reasons,
  explanation, factors), Patterns (hourly, weekly, workday / day off, movements),
  Accuracy (Brier, baselines, calibration, daily), Settings (defaults, room
  overrides, contexts, workday source, connections, learn and rebuild).
- Dashboard, modules and safety report the curtain and forecast owners; live
  updates for both; `SL_CURTAINS_MIN_*` / `SL_FORECAST_MIN_*` thresholds (tests
  only). End-to-end suites `tests/e2e/curtains_checks.py` and
  `predictions_checks.py`.
- **From the independent review**: a lost connection's instance stop and the
  next session's start are serialized (no two instances on the same store); a
  forced `return` while a cover is moving is refused unless the user ticks
  "Return even while a cover is moving"; a restored curtain export lists the
  windows it would switch to Automatic and restores them as Observing unless
  that is confirmed; a changed curtain configuration is published at once so a
  newly added cover can be commanded right away.

## 0.5.0

Phase A4: Lighting, Moods and Switches. Needs Smart Occupancy & Lighting
0.8.0. Nothing is migrated automatically: the integration keeps controlling the
lights until an administrator performs a reviewed handover.

- **The integration's lighting runtime in the App.** The lighting controller and
  everything it uses (49 modules, listed in the integration's
  `runtime_manifest.py`) are byte-identical copies; both test suites and the
  bridge (SHA-256 fingerprint) check it. They run on a **host interface**
  (`backend/homeassistant/`): exactly the part of Home Assistant's Python API
  they use, on the App's websocket connection — states and contexts, the event
  bus (with Home Assistant's queueing of nested events and eager tasks), timers,
  registries (including the extended entity entries), the sun, JSON stores, MQTT
  through Home Assistant (`mqtt/subscribe`), device triggers
  (`subscribe_trigger`) and automation / script configurations for conflict
  detection.
- **Shadow mode** (default): a fresh controller from an exact copy of the
  integration's configuration and state for every bridge session; it follows
  configuration and live option changes and sends nothing. The comparison
  matches commands (service, targets, data within 3 s) and decisions, and keeps
  every mismatch with both sides' commands.
- **Handover and rollback** (Lighting → Control): readiness checks (2 h, 10
  matching commands, no mismatch, same code, same configuration), a reviewed
  handover (backup of the copy in `/data/lighting/backups`, key-by-key check of
  the copied configuration), the App's configuration and state stored in
  `/data/lighting/store` and mirrored to the integration, light commands only
  through the integration's fenced `execute`, occupancy inputs to the
  integration's engine, the room view for the integration's entities;
  "Return control" (a release: the App's final configuration and state go with
  it, also when the forced return is used while the App controls lighting), an
  export. Configuration and state are mirrored about two seconds after every
  change, each on its own. A refused command that means "not the owner" stops
  the App's controller at once; a reconnect resumes with the lease token,
  restarts the controller from its own stored state (nothing applied) and
  applies entity renames made while it was away; a new App process never
  resumes. A failed return leaves the App's controller working. Connected
  while Home Assistant is still starting (a Home Assistant restart with the
  App running), the client re-reads the states once Home Assistant runs, so
  the resumed controller knows every light.
- **Host timing as in Home Assistant:** a timer started while an event is
  handled (a state change, a configuration change from the bridge, an occupancy
  result) counts from when the event fired in Home Assistant, not from when it
  reached the App (at most two seconds), so the shadow's debounce ends at the
  same instant as the integration's. Found with the end-to-end stack: without
  it the shadow sometimes saw the integration's command land first and
  planned "already so".
- **Lighting**, **Moods** and **Switches** pages, on one command API: the
  integration's own 50 lighting editor commands, forwarded while the
  integration controls lighting and answered by the App's copy of the same
  handlers afterwards. A forwarded command that changes the controller's
  runtime state (override cleared, profile test, switch learning, calibration
  reset, Priority Inputs, supervisor check) also runs on the shadow's copy, so
  the comparison stays meaningful.
- **Universal moods**: one selector or the built-in mood selector instead of the
  three helpers.
- Tests: 21 recorded scenarios replayed through the host (same commands, same
  decisions and the same answers to the editor commands as the integration),
  the host interface, the runtime against a simulated bridge, the command
  router; end-to-end `tests/e2e/lighting_checks.py`.

## 0.4.0

Phase A3.2: occupancy engine migration. Needs Smart Occupancy & Lighting
0.7.0. With older versions the App's engine runs on its own, without
comparison, and a handover is not possible. Nothing is migrated automatically:
the integration still decides occupancy until an administrator performs a
reviewed handover.

- **Occupancy engine in the App:** the shared engine package (a byte-identical
  copy of the integration's) runs event driven on Home Assistant's state
  changes and timers. Its configuration and its inputs that are not sensors
  (possible-occupancy lighting signals, zone holds, wall-switch activity) come
  from the integration. Every bridge session starts from an exact copy of the
  integration engine's state. A golden-scenario self-test runs at start-up.
  The state is stored in `/data/occupancy` (versioned, with a backup); an
  incompatible file is not used.
- **Shadow mode** by default: both engines are compared per room (state,
  occupied, reason code; vacancy deadlines), with timing differences,
  disagreements and their events in a bounded history.
- **Presence → Engine:**
  - the acknowledged owner, connection, versions and fingerprints,
    configuration sync, last accepted result, self-test;
  - comparison statistics, per-room comparison and discrepancies;
  - readiness checks and ownership history;
  - **Review handover to the App…** and **Return control to integration**.

  The Presence dashboard shows who decides. The floor plan's Presence overlay
  can mark rooms where the engines differ (*Engine comparison*).
- **Handover and fallback:** the App publishes only with the integration's
  lease (epoch, token, sequence numbers, configuration revision), and goes back
  to shadow mode as soon as the integration refuses it. A new App process is
  a new instance and never resumes an old ownership.
- The App's engine never controls a device: the Home Assistant client still
  has no command that could. Its bridge commands are a separate allowlist.

## 0.3.0

Phase A3.1: Presence configuration and live occupancy debugger. Needs Smart
Occupancy & Lighting 0.6.10 for the Presence pages (older versions: Presence
asks for the update; Rooms and the floor plan work as before). The occupancy
engine stays in the integration; nothing is migrated automatically.

- **Presence dashboard:** every configured room with the engine's state,
  confidence (evidence quality, not a probability), sensors and their live
  readings, availability, time since the last activity, the engine's own
  confirmation or vacancy countdown, timing (global or overrides), Automatic
  lighting, warnings. Filters (occupied, possible, vacant, uncertain,
  unavailable sensors, problems) and search. Live: the page refreshes when a
  room's state entity or a sensor changes, never on a timer.
- **Room Presence workspace:** sensors (assigned, available in the area, add
  any other sensor through the search) · the decision (state machine with the
  latest transition, the engine's reason, confirmation and vacancy progress,
  what happens next and when, evidence grouped by what it contributes, the
  current visit step by step, lighting and radar distance zones with the
  configured band and the measured distance) · inspector (live value,
  engine status and deadlines, type with Home Assistant's suggestion, role
  with what it does, inverted, included, device, availability, advanced).
  Tabs: Live, Timeline, Timing, Diagnostics. Fast room switching.
- **Timing:** the engine's seven parameters with readable units, global
  defaults (Presence → Global timing) and room overrides (inherited / room
  value, *Use global*, *Use global for all*), client-side checks matching the
  integration's; global defaults can be loaded from a backup into the editor.
- **Review and save** through the integration's presence API: changes, rooms
  affected, the decision right after saving (computed by the integration on a
  copy of each engine), consequences; confirmations for Automatic rooms and
  for decisions that change at once. Stale writes are refused (room revision,
  or the global defaults' own revision, so different rooms and the defaults
  can be edited at the same time); a configuration changed elsewhere is
  reported and re-reviewed. A backup of the room configuration (now with the
  global defaults) is stored before each save.
- **Timeline:** Recorder history (15 min to 24 h, read on request, cached
  30 s, at most 40 entities and 2000 changes each) merged with the
  integration's presence log and live changes: lanes per sensor and the room,
  events with details (reason, evidence, required quiet time, confidence),
  filters by type and sensor; 24-hour times in Home Assistant's time zone.
- **Diagnostics** (Presence → Diagnostics and per room): the integration's
  measured and heuristic findings plus the App's (device offline, sensor in
  another area, type differs from Home Assistant's classification), each with
  its basis and a link to the setting. Nothing is changed automatically.
- **Floor plan:** Live mode → *Presence overlay*: room decisions and
  countdowns, presence sensors as the engine sees them, warnings; sensor →
  its Presence inspector, room → its Presence workspace. No coverage,
  direction or person positions are drawn.
- Rooms editor: the Presence tab links to the workspace and its timing.
- Migration: Presence is configured in the App (interface migrated); stored in
  and run by the integration.
- Safety: new allowlisted commands `smart_occupancy/presence/get`, `review`,
  `history/history_during_period` (reads) and `smart_occupancy/presence/set`
  (the only new write). No service call, no device control, no engine.
- Version 0.3.0; App tests 140 (backend) + 45 (frontend unit); end-to-end
  `tests/e2e/presence_checks.py` (with `--phase restart-check`).

## 0.2.1

Phase A2.1: Floor plan. Optional: everything else works without a floor plan.
No change to the integration (0.6.9 is enough) and no change to any
automation.

- **Floor plan page** (sidebar, and from Rooms and every room): three modes.
  **Build** draws floors and rooms; **Configure** links rooms to areas and
  places devices; **Live** shows the home read-only. Tool palette and floors
  on the left, a large canvas (zoom, pan, pinch, fit, reset), an inspector on
  the right; both panels collapse. Save status, undo and redo (Ctrl+Z,
  Ctrl+Shift+Z), Ctrl+S, keyboard tools and arrow-key nudging.
- **Drawing:** multiple floors (name, level, optional Home Assistant floor);
  rectangular and polygon rooms; rooms that meet share their corners and walls
  (a corner on another room's wall splits it), so moving a shared corner or
  wall reshapes both rooms; move a whole straight wall or one part; add and
  delete corners; type a wall length; doors (with hinge side), windows and
  open-plan openings that stay on their wall when it moves or is split; grid
  and snapping to corners, walls and the grid; measurements on request.
  Overlapping rooms and crossing walls are refused. Deleting a room or floor
  asks first.
- **Background image:** PNG, JPEG or WebP up to 15 MB, checked by its content
  and stored in the App; position, scale, rotation, opacity, lock, hide;
  calibration with a known distance (optionally scaling the drawing with it).
  Images are served only through Home Assistant's Ingress.
- **Areas:** link a drawn room to an area by its stable id; unlinked areas,
  rooms without an area, renamed and deleted areas are shown. Linking an area
  that is already linked asks: move the link, or model one shared (open-plan)
  space. Areas are never created, renamed or deleted. Works without areas.
- **Devices:** searchable palette by type (presence, lights, switches,
  temperature and light level, covers, other) from the App's discovery; drag
  onto the plan, or click / tap a device and then tap the plan. Icons can be
  moved, relabelled, replaced with another entity or removed. The inspector
  shows name, entity id, device, Home Assistant area, state, availability,
  capabilities, the integration's assignments (with type and role) and, apart
  from that, where the icon is on the plan. Assigning a device placed in a room
  opens the Rooms editor with the change prepared; nothing changes until it is
  reviewed and saved there. Entities renamed in Home Assistant are found by
  their registry id.
- **Live:** room occupancy from the integration's own room entities (Occupied,
  Possibly occupied, Vacancy pending, Vacant, Uncertain, Unavailable, Not
  observed), Automatic / Observing / Paused / Blocked lighting and manual
  overrides, lights with brightness, cover positions, sensor values, contacts
  and motion. Unknown and unavailable are drawn differently from off and
  vacant. Filters, a legend and the devices that need attention. No coverage,
  person positions or estimated data are drawn.
- **Relationships:** rooms that share a wall, a door or an open-plan opening
  are suggested as adjacent, with the reason; window directions once north is
  set; adjacency the integration has but the plan does not show is reported.
  **Review** opens the Rooms editor with the adjacency prepared; the
  integration reviews and saves it. Suggestions can be dismissed.
- **Storage** (`/data/floorplan`): versioned format (with migrations and
  protection against files from a newer App), validation of every save,
  atomic writes, recovery from a damaged or interrupted save, backups (before
  changes, at most one per 10 minutes while editing, always before an import
  or a restore; latest 30), export with images and reviewed import, and
  revisions: a save based on an older version (another browser session) is
  refused and asks; other sessions are told about every save.
- **Safety:** reads only one more Home Assistant command (the floor registry).
  No service call, no registry change; the floor plan never writes the
  integration's configuration.
- Version 0.2.1; App tests 130 (backend) + 31 (frontend unit); end-to-end
  `tests/e2e/floorplan_checks.py`.

## 0.2.0

Phase A2: Rooms and device management. Needs Smart Occupancy & Lighting
0.6.9 to save (older versions: Rooms are shown read-only).

- **Rooms overview:** every Home Assistant area, configured or not, as a card
  grid with status (Configured, Not configured, Disabled, Problem), current
  occupancy, sensors and lights, online / offline / unavailable devices, who
  runs the automation, and what needs attention. Search, status filter, sort.
- **Room workspace:** room list and the devices found in the area (left), the
  room in tabs (Overview, Devices, Presence, Lighting, Switches, Curtains,
  Diagnostics) and an inspector for the selected device (right): live state,
  availability, capabilities, device identity, classification and its
  assignment. Panels collapse; the inspector slides over on tablets.
- **Discovery:** PIR and radar / presence sensors, door and window contacts,
  temperature and light sensors, lights and light groups, lighting relays,
  wall switches and remotes, covers, PC activity and other presence inputs,
  from Home Assistant's registries. Devices from other areas can be added
  (with a note). Home Assistant's own areas, devices and entities, Zigbee and
  MQTT are never changed.
- **Editing:** enable a room, assign and remove sensors and lights, sensor type
  and role, inverted, included in occupancy, reference occupancy entity,
  adjacent rooms. Changes stay in a draft (with an unsaved-changes bar and a
  warning before leaving) until **Review changes** shows what is added,
  removed and changed, conflicts, missing or unavailable devices and what
  saving does; disruptive consequences must be ticked before **Save**.
- **One configuration:** saved in the integration through its validated room
  commands; its own *Configure → Rooms* keeps working on the same settings. A
  change made there while you edit is noticed; your edits are then reviewed
  against the current configuration (stale saves are refused).
- **Backups:** the room configuration is kept before every save or restore
  (latest 30, in the App's data); export the current configuration, download
  a backup, or restore one after a review.
- **Live diagnostics:** live states and timestamps, the integration's
  occupancy decision and checks, and distinct statuses for an offline device,
  an unavailable entity, a missing entity, a device present but not assigned,
  and configuration conflicts.
- **Safety:** the backend sends only allowlisted commands; the only ones that
  change anything are the integration's room commands, and only for a Home
  Assistant administrator (checked against the user Ingress reports), from the
  App's own page (custom header, same origin). No service call is ever sent.
- Module registry: Rooms is configured in the App, stored in the integration
  and run by the integration; every other module is unchanged. Health checks
  and the Diagnostics safety card report configuration writes.

## 0.1.1

Design direction update (still Phase A1, read-only).

- Desktop and tablet (including landscape) are the primary targets. Card
  columns now follow the available width: up to three on wide screens and
  landscape tablets, two on portrait tablets. The Home Assistant lookup shows
  the selector and the details side by side on desktops. The phone layout
  stays as a simpler fallback; everyday phone use is through Lovelace cards.
- Each module shows which system is **in control** (the integration for
  every module in this phase). Migration moves one module at a time and never
  lets the integration and the App control the same devices together.

## 0.1.0

Phase A1: App foundation (read-only).

- New Home Assistant App "Smart Occupancy & Lighting", installable from this
  repository. Opens from the sidebar as **Smart Lighting**
  (`mdi:lightbulb-auto`) through Ingress, for administrators. No host port.
- Backend (Python, aiohttp) connected to the Home Assistant websocket API with
  the Supervisor-provided token, which never reaches the browser. Only a fixed
  list of read-only commands can be sent; automatic reconnects; live state
  updates; health checks.
- Web interface (Lit, TypeScript), designed phone-first: bottom navigation on
  phones, a collapsible rail on tablets, a sidebar on desktops; light / dark /
  automatic theme; touch targets of at least 44 px; text scaling and safe areas.
- Dashboard: Home Assistant connection, App health, detection of the existing
  integration (version, state, live entity states), modules and migration progress.
- Rooms, Presence, Lighting, Moods, Switches, Curtains and Predictions:
  "Migration pending" screens that say where each is configured today. No controls.
- Diagnostics: health checks, safety status (service calls sent: 0),
  connection details, system information, searchable entity / device / area
  lookup, recent log, diagnostics data to view, copy or download.
- Settings: theme; advanced (technical) details on request.
