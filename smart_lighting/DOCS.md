# Smart Occupancy & Lighting

The **Smart Lighting** App is the new home of the Smart Occupancy & Lighting
configuration. It replaces the integration's dashboard cards and options
screens module by module, and only after you approve each step.

## Phase A3.2: the occupancy engine (this version)

This version needs Smart Occupancy & Lighting **0.7.0**. The App runs the
occupancy engine next to the integration's. Both engines run the very same
code: a shared, versioned engine package, checked byte for byte.

**Nothing changes until you decide.** After installing or updating, the
integration still decides room occupancy, and the App's engine runs in
**shadow mode**. It calculates every room's occupancy from the same events and
compares its result with the integration's, but it publishes nothing and never
switches a device.

**Presence → Engine** shows:

- **Who decides occupancy now.** This is read from the integration, which is
  the only authority. It is never just the App's wish.
- Connection, versions (the engine fingerprints must match), configuration sync
  and the last result the integration accepted.
- The **shadow comparison**. A difference that settles within 3 s counts as a
  timing difference; a longer one is a disagreement. Each disagreement is
  recorded together with the events around it.
- **Readiness checks**, the ownership history, and two actions:
  - **Review handover to the App…** is available only when every check
    passes. That means 2 hours of uninterrupted comparison, 20 matched
    transitions, no disagreement in that time, the same engine and the same
    configuration. The first published decision is an exact copy of the
    current one, so no room changes because of the handover.
  - **Return control to integration** works at any time. An occupied room
    stays occupied, and nothing is switched.

**While the App decides**, the integration keeps publishing the same occupancy
entities (same entity ids and history). Lighting, curtains, forecasts and your
automations read them as before. The integration's own engine keeps running in
the background. If the App stops, crashes or loses its connection for about
30 seconds, the integration takes over by itself, without making rooms vacant.
A restarted or updated App does **not** take control back: it returns to shadow
mode until you hand over again. After a Home Assistant restart, the same App
process resumes within 2 minutes; otherwise the integration takes over.

If the App cannot be reached at all, Developer tools → Actions →
`smart_occupancy.engine_return_to_integration` (administrators only) returns
control without it.

## Phase A3.1: Presence configuration

- **Presence** shows every configured room's occupancy exactly as the Smart
  Occupancy integration decides it: the state (Vacant, Possible occupancy,
  Occupied, Vacancy pending, Uncertain), its confidence, the sensors and their
  live readings, time since the last activity, and the engine's own
  confirmation or vacancy countdown. Filter by occupied, possible, vacant,
  uncertain, unavailable sensors or problems.
- A room's **Presence workspace** has its sensors on the left (assigned and
  available in the area), the engine's decision in the centre (state machine,
  the reason in the engine's words, what happens next and when, the current
  visit step by step, lighting and radar distance zones) and an inspector on
  the right: type (with Home Assistant's own suggestion), role with an
  explanation, inverted, included, availability, device and advanced details.
- **Timing**: the engine's own parameters (vacancy timeout, fast vacancy
  timeout, occupancy confirmation, radar dropout bridge, PIR and presence
  stuck detection, start-up hold). **Global timing** sets the defaults; a
  room shows whether each value is inherited or its own, and *Use global*
  removes an override. Values are validated by the integration.
- **Timeline**: sensor activations and clears, unavailability, the engine's
  decisions with their reasons and evidence, and presence setting changes, for
  the last 15 min, 1 h, 6 h or 24 h (Home Assistant's Recorder, read only when
  you open or refresh it) plus everything live while the page is open. Filter
  by event type and sensor; select an event for its details. Times are
  24-hour, in Home Assistant's time zone.
- **Diagnostics**: missing, unavailable and stuck sensors, states the engine
  does not understand, disagreeing presence sensors, rooms that cannot
  confirm occupancy, suspicious roles and inverted sensors, unusual timing,
  and the integration's own health checks. Each item says whether it was
  *measured*, comes from a *health check* or is a *suggestion*, and links to
  the setting. Nothing is changed automatically.
- Saving: every change is reviewed first. The review lists the changes, the
  rooms affected and the integration's prediction of each room's decision
  right after saving (computed on a copy of the engine). Changes that affect
  rooms with Automatic lighting, or that change a decision at once, must be
  confirmed. Timing, role, inverted and included are applied at once without a
  reload; adding or removing a sensor or changing its type reloads the
  integration (occupancy is restored).
- **Floor plan → Live → Presence overlay**: rooms with their decision and
  countdown, presence sensors as the engine sees them, warnings; select a
  sensor or a room to open it in Presence. Radar distance zones are listed
  with their configured band and the measured distance; no coverage or
  person position is drawn.

Since Phase A3.2 these views show the published occupancy, whichever engine
decides it.

## Phase A2 and A2.1

- **Rooms** is a full room editor. Every Home Assistant area is listed, with
  its status (Configured, Not configured, Disabled, Problem), current
  occupancy, assigned sensors and lights, device health, who runs the
  automation, and warnings.
- Open a room to see the devices found in its area, assign or remove sensors
  and lights, set each sensor's type and role, mark inverted signals, include
  or exclude sensors, choose a reference occupancy entity and adjacent rooms.
- **Floor plan** (optional): draw your home, link rooms to areas, place
  devices on the plan and watch occupancy, lights and devices live.
- Every change is a draft until you **review** it. Saving stores it in the
  Smart Occupancy & Lighting integration, the only place the room
  configuration lives; the integration's own *Configure → Rooms* keeps
  working on the same settings.
- The other modules (Lighting, Moods, Switches, Curtains, Predictions) are
  shown read-only or as **Migration pending** and are still configured in the
  integration's dashboard cards.
- Dashboard and Diagnostics: connection, health, the integration's status,
  safety and the migration progress.

It does **not**:

- control lights, covers or any other device, or call any Home Assistant action (service)
- run automations: the integration keeps doing all the work, unchanged
- change Home Assistant's areas, devices or entities, Zigbee bindings or MQTT
- change anything in the integration other than the room and presence settings you save
- run its own occupancy engine
- migrate anything automatically
- need the internet while it runs

Saving a room needs **Smart Occupancy & Lighting 0.6.9 or newer**; the
Presence pages need **0.6.10 or newer** (update it in HACS first). With an
older version, Rooms are shown read-only and Presence asks for the update.

## Installation (Home Assistant OS)

Requirements: Home Assistant OS (or a Supervised installation) with Home
Assistant 2026.10 or newer, on a 64-bit system (amd64 or aarch64, for example
a Raspberry Pi 4/5, Home Assistant Green/Yellow or an x86 PC).

1. **Add the repository.** In Home Assistant go to **Settings → Apps → App
   store**, open the **⋮** menu (top right) and choose **Repositories**. Add
   `https://github.com/havanissa/HomeAssistant` and close the dialog.
2. **Install.** Open the **⋮** menu again and choose **Check for updates**
   (or reload the page). Scroll to **Smart Occupancy & Lighting Apps**, open
   **Smart Occupancy & Lighting** and select **Install**. Home Assistant builds
   the App on your device. This needs the internet once and takes a few
   minutes (longer on a Raspberry Pi).
3. **Start.** On the App's **Info** tab, turn on **Show in sidebar** if it is
   off, optionally turn on **Start on boot** and **Watchdog**, then select **Start**.
4. **Open.** Select **Smart Lighting** in the sidebar.

Only administrators see the sidebar entry. The App works the same in the Home
Assistant Companion app on phones and tablets.

### Updating

When a new version is in the repository, Home Assistant offers the update on
the App's page (Settings → Apps). Updating rebuilds the App on your device.

### Uninstalling

Settings → Apps → Smart Occupancy & Lighting → **Uninstall**. The integration
and its configuration are not affected.

## Using the App

The App is made for **desktops and tablets** (portrait and landscape): it is
where you configure, inspect and diagnose. For everyday use on a **phone**,
use the compact Lovelace dashboard cards (status, moods, Automatic /
Observing, overrides, curtain presets); today these are the integration's
cards. The App also opens on phones, with a simpler layout.

- **Desktops and landscape tablets:** a sidebar with every module; wide
  screens show up to three columns of cards.
- **Portrait tablets:** a navigation rail on the left; the menu button at its
  top expands it to show every module by name.
- **Phones:** the main screens are in the bottom bar; **More** lists every module.
- **Status:** the top bar always shows the Home Assistant connection
  (Connected, Reconnecting, …). If the App backend itself cannot be reached,
  it shows "App unreachable" and keeps retrying.
- **Theme:** Settings → Appearance: Automatic (follows your device), Light or
  Dark. It is remembered in that browser.
- **Advanced details:** Settings → Show advanced settings.

## Rooms

**Overview.** Search by room, sensor or light; filter by status; sort by
name, status, occupancy or what needs attention. Select a room to open it.

**Room workspace.** Three panels on wide screens, collapsible with the buttons
at the top right:

- *Left:* the room list and **Available in this room**: sensors and lights
  found in the area that are not assigned. **+** assigns one; **Add sensor** /
  **Add light** search every area (a device from another area gets a note; its
  Home Assistant area is not changed).
- *Centre:* the tabs **Overview** (observe occupancy on/off, occupancy now,
  device health, who runs what, warnings, remove the room configuration),
  **Devices** (every device of the room, assigned or not, with live status),
  **Presence** (sensor type, role, inverted, included; reference entity;
  adjacent rooms; presence timing read-only), **Lighting**, **Switches** and
  **Curtains** (read-only, configured in the integration's cards for now) and
  **Diagnostics** (entity statuses, conflicts, the integration's checks and
  occupancy inputs, the stored configuration).
- *Right:* the **inspector** of the selected device: live state, availability,
  assignment controls, why it was classified as it is, capabilities and device
  identity (manufacturer, model, Zigbee address). On tablets it slides over.

**Statuses.** *Device offline*: every entity of the device is unavailable
(power, battery, radio). *Entity unavailable*: only this entity. *Missing*:
the entity does not exist. *Present, not assigned*: found in the area, not
used. *Conflict*: used by several rooms, or from another area.

**Saving.** Edits show in a bar at the top (**Discard** or **Review
changes**). The review lists what is added, removed and changed, potential
conflicts, missing or unavailable devices, and what saving does. Consequences
such as stopping occupancy for a room, or changes in a room with *Automatic*
lighting, must be ticked before **Save**. Saving reloads the integration (about
a second; no light or cover is switched by it). If the room was changed
elsewhere (for example in *Configure → Rooms*) while you edited, the App says
so and reviews your edits against the current configuration. Leaving the room
with unsaved edits asks first.

**Backups.** Rooms → **Backups**: the room configuration as it was before
each save or restore (latest 30), **Export current configuration**, download a
backup, or **Restore** one (from the list or a file) after reviewing it.

## Floor plan

Smart Lighting → **Floor plan** (also from Rooms and from each room). The
floor plan is optional and is stored in the App only; it is not part of the
integration's configuration and changes no automation.

**Build.** Add a floor (or *Add Home Assistant floors*). Choose **Rectangle
room** and drag, or click two corners; **Polygon room** and click each corner,
then click the first corner (or double-click, Enter, *Finish room*). Draw
rooms side by side: corners snap to existing corners and walls, so neighbouring
rooms share their walls. Select a room to move it (drag it once it is
selected) or reshape it by its corners; select a wall to drag it sideways
(the whole straight wall, or only that part), type its length, or add a
corner (also by double-clicking the wall). **Door**, **Window** and
**Open-plan opening** go on a wall with a click and can be slid along it.
Measurements are optional: coordinates are metres; a floor is a sketch until
you calibrate it or type exact lengths. Undo / redo, then **Save**.

**Background image.** Import a PNG, JPEG or WebP plan (up to 15 MB), adjust
its opacity, position, size and rotation, then **Calibrate**: click two
points whose real distance you know and enter it. Trace the rooms over it.

**Configure.** Select a room and pick its Home Assistant area. The *Areas* tab
lists rooms without an area and areas not on the plan. An area normally
belongs to one drawn room; for an open-plan space drawn as several parts,
choose *Model as one shared space*. In the *Devices* tab, drag a device onto
the plan, or click it and then click or tap where it is. Click an icon to see
its state, device, area and how the integration uses it. **Placing an icon
changes nothing**: to assign a device to a room, use *Assign … in the Rooms
editor*, which prepares the change there for review.

**Relationships.** The *Relations* tab suggests adjacent rooms from the
drawing (a shared wall, a door, an open-plan opening) and explains why.
In Smart Occupancy an adjacent room is extra evidence when someone leaves
through a door; occupancy never spreads between rooms. **Review** opens the
Rooms editor with the adjacency prepared; nothing is applied automatically.

**Live.** Read-only: room occupancy as the integration reports it, lighting
mode and manual overrides, lights, covers, sensors and contacts. Grey hatching
means uncertain, red hatching or a red ring means unavailable: these are never
shown as vacant or off. Icons are where you put them, not measured positions.

**Saving, sessions and backups.** The status next to the title shows unsaved
changes; leaving asks first. If the plan was saved in another browser
session, you are told; saving over it asks first. ⋮ → **Export** (with
images), **Import a file** (reviewed, replaces the plan, the old one is kept as
a backup) and **Backups**.

## Migration: one system in control

Modules move from the integration to the App one at a time, only after you
approve each step. For each module the App reports where it is **configured**,
where the configuration is **stored**, and which system **runs** it. In this
version Rooms and Presence are configured in the App and stored in the
integration. Room occupancy is decided by the integration's engine until you
hand it over to the App on Presence → Engine; Presence then counts as migrated
for as long as the integration acknowledges the App as the owner. Every other
module is configured, stored and run in the integration. The App's engine never
controls a device, and the integration and the App never control the same
devices.

## Configuration

| Option | Description |
| --- | --- |
| `log_level` | How much the App writes to its **Log** tab: `debug`, `info` (default), `warning` or `error`. |

## Troubleshooting

- **"Home Assistant: Reconnecting…" stays on.** Open Diagnostics: the Home
  Assistant connection card shows the last error. Restarting Home Assistant
  or the App reconnects automatically.
- **"App unreachable".** The App is stopped or restarting. Check the App's
  **Info** and **Log** tabs.
- **Reporting a problem.** Diagnostics → **View data** (then **Copy**) or
  **Download**. The report contains no passwords or tokens.
- **Opening the App's address directly** (not through the sidebar) is refused
  on purpose: it only accepts Home Assistant's Ingress.

## Security

- Only Home Assistant administrators can save (the App checks the user Home
  Assistant's Ingress reports). Requests must come from the App's own page.
- The App backend sends only a fixed list of commands to Home Assistant: reads
  (including the Recorder history, for a bounded window when you open a
  timeline), the integration's own validated room and presence commands, and
  the integration's occupancy bridge. The bridge works only over Home
  Assistant's own connection, is for administrators only, and is fenced:
  stale, duplicated or malformed messages are refused. It cannot call
  services, write states or change registries. Its token never reaches the
  browser.
- Handover and rollback need a Home Assistant administrator.
- Floor plans and their images are stored in the App's data folder. Images are
  checked by their content (PNG, JPEG, WebP only; no SVG) and are only served
  through Home Assistant's Ingress, never from a public address.

## Known limitations (Phase A3.2)

- The comparison thresholds (2 h, 20 transitions) are fixed. Development
  setups can lower them with the environment variables
  `SL_ENGINE_MIN_SHADOW_SECONDS` and `SL_ENGINE_MIN_TRANSITIONS` only.
- The integration's own engine keeps running while the App decides (hot
  standby). Removing it is a later, separately approved phase.
- Lighting, curtains and the other modules are not migrated. They keep
  reading the published occupancy.

- Floor plan: no curved walls, wall thickness, stairs or furniture (it is not a
  CAD program); sensor coverage and sun analysis are not drawn (the
  integration does not know a sensor's coverage or direction).
  Live occupancy needs rooms observed by Smart Occupancy.
- Presence: lighting zones and radar distance zones are shown, not edited
  (they stay in the Smart Room Lighting card). The integration's presence log
  keeps the latest 400 decisions and changes while Home Assistant runs;
  older history comes from the Recorder (room state, reason and sensors), so
  decisions before the last restart show the state and reason only.
- Confidence is the integration's evidence-quality score, not a probability.
- Only rooms, presence sensors and occupancy timing are edited here. Lighting
  behaviour, moods, switches, curtains and forecasts are configured in the
  integration's dashboard cards as before.
- Saving needs the integration to be running; while it is not, rooms are shown
  from its stored configuration.
- The theme follows your device (or your choice in Settings), not the Home
  Assistant user theme.
- The interface is in English.
- The first installation and every update need the internet to build the App
  (base image and Python packages). Normal operation does not.
- Live updates cover the integration's entities, the connection status and
  the devices of the open room. The Home Assistant lookup reads states when you
  open it.
- Tested with a simulated Supervisor and Ingress against Home Assistant
  2026.11.1, including crashing and restarting the App container and
  restarting Home Assistant. Not yet tested on a real Home Assistant OS
  installation, so the handover has not been verified on real hardware.
