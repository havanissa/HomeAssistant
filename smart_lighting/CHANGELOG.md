# Changelog

## 0.5.0

Phase A4: Lighting, Moods and Switches (work in progress).

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
