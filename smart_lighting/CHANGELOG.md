# Changelog

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
