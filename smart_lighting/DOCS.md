# Smart Occupancy & Lighting

The **Smart Lighting** App is the home of Smart Occupancy & Lighting: every
engine and every configuration. The *Smart Occupancy & Lighting* integration
(1.0.0, the companion) gives it native entities, a fenced way to command lights
and covers, and compact dashboard cards.

## Phase A6.2 (0.9.0): Presence zones, the light inspector and brightness delays

Three improvements of existing functionality. Nothing changes on upgrade: every zone, light rule and brightness
matrix keeps its exact behaviour; delays are 0 s (immediate) until you set them. Needs the companion
integration 1.0.0.

### Zones are part of Presence (Presence → a room → Zones)

A zone is a part of a room where presence is detected on its own (a desk, a sink, a reading chair): by some of
the room's sensors, or by a radar's distance band. It is an occupancy signal of the room - it can keep the room
occupied, and Lighting can respond to it - and it exists whether or not any light responds to it.

- **Presence → a room → Zones** creates and deletes zones and edits everything about their detection: name
  (its id never changes), enabled, room sensors or radar distance (band, entry and exit delays, hysteresis,
  units, the test distance), occupied at the first detection or when confirmed, its own occupancy timing, what
  happens when its sensors are unavailable, and whether it keeps the room occupied. Each zone shows its live
  state, what responds to it, and a link to its lighting rules. **Presence** (the main page) lists every room's
  zones with an *Edit* link, and each room card says how many zones it has.
- **Lighting → a room → Zone lighting** sets which lights respond to each zone and how (On with brightness,
  colour temperature and fade, Off or Unchanged; priority between zones; manual changes; daylight; what happens
  when the zone ends), shows which rule controls each light now, and the room's shared space. Zones are added
  and their detection is changed in Presence (linked).
- Saving a zone sends no command. A zone whose state changes because you edited its detection (a sensor
  removed, a new zone that is occupied at once) is used from then on; its lights follow at the zone's next change
  or the next lighting trigger. Switching a zone off or deleting it ends it: if it is occupied, its lights follow
  the remaining rules at once (as a deletion always did). Deleting a zone that lights respond to asks for a
  confirmation; its brightness matrix column (values and delays) is deleted with it.
- Stored exactly as before (one zone record with a stable id, one zone engine): no migration.

### The light inspector: what controls this light?

Select the ⓘ button beside a light - in a room's lighting overview and lights, the brightness matrix, zone
lighting, the Supervisor table, a light placed on the floor plan, or the device lookup (Diagnostics) - for one
drawer that answers:

- **What is controlling it now**: the room's Automatic / Observing mode, Manual mood, the mood, occupancy, the
  active zone, the ambient light category, the winning rule and why, the rules it won over, a manual override
  with its remaining time, Priority Inputs, the Room Supervisor and automations that also control it.
- **Three things that need not agree**, side by side: what the winning rule wants, the last command Smart
  Occupancy sent, and what the device reports. When the last change was not made by Smart Occupancy and Home
  Assistant did not say by whom, it says exactly that.
- **A pending brightness delay** with its condition, remaining time and what happened to earlier ones.
- **Device**: area, device and integration, availability, state, brightness and colour, what it can do, group
  membership, relay or on/off-only.
- **Recent activity** from the room's decision log and Room Supervisor log, filtered to this light.

**Identify** (administrators) switches the light to full brightness for about 2.5 s and then back to exactly
what it was (off, or its brightness, colour temperature, colour or effect). It is not a manual change: no
override is registered or cleared, and no mood, occupancy, rule or matrix changes. An on/off light or relay is
just switched on and off again. If a person, a wall switch, a Priority Input or a newer lighting decision changes
the light meanwhile, that wins and nothing is restored over it. A group asks you to choose one of its lights, or
to identify the whole group (each light returns to its own state). If the App stops in the middle, it restores
the light at its next start only if that happens within 2 minutes and the light has not changed since.

### Delay On / Delay Off per brightness matrix cell (Lighting → a room → Brightness)

Select a cell with a value: besides its brightness (or On / Off) it has **Delay On** and **Delay Off** in
seconds (0-3600; 0 = at once, the default). A cell with a delay shows a small clock.

- **Delay On**: when this cell would switch the light on from off, it waits that long. If the situation ends
  first (the zone is left, the room is vacant), nothing is switched on. If another cell wins while it waits, the
  wait continues from the same start with that cell's own delay and brightness (never a stale value).
- **Delay Off**: when the cell that kept the light on stops applying and the light would be switched off while
  the room is still occupied, the light keeps its state for that long. If the situation returns, or another rule
  wants it on, nothing is switched off.
- A change from one brightness to another is never delayed. A manual change, a wall switch, a Priority Input,
  Manual mood, vacancy, the room set to Observing or Automatic, *Resume* and Identify act at once and end a
  pending delay. The single winning rule per light is unchanged, and only that rule's delay applies.
- A pending delay is shown in the light inspector and the Room Supervisor waits for it. Delays are kept in
  templates, copies, previews, exports and backups. After a restart a pending delay is dropped, never replayed:
  the next decision starts afresh. Saving a matrix sends no command.

## Phase A6.1 (0.8.0): multi-sensor sunlight and cross-room switch targets

Both are extensions of existing behaviour and off until you configure them. They need the companion
integration 1.0.0.

### Multi-sensor sunlight (Curtains → a window → Automatic)

Switch on *Use this window's sunlight sensors* and add one or more illuminance sensors as chips. Each chip shows
the sensor's value and health; *Disable* keeps it in the list without using it, the bin removes it.

- **Activate above**: as soon as **any** enabled sensor with a valid reading is above this level, sun protection
  is requested (no agreement of the others needed, no averaging).
- **Release below** and **Release delay**: protection ends only when **every** enabled sensor reads below
  *Release below*, continuously for the delay. A sensor at or above the level, or above *Activate above*, starts
  the countdown again. *Release below* must be lower than *Activate above*.
- A sensor that is missing, unavailable, non-numeric, stale, stuck or implausible is never sunshine. While
  protection is on it **prevents the release** ("Release cannot be confirmed") and the window lists it as a
  problem, until you disable or remove it.
- The levels are set like every other curtain rule: for this window, its room or all windows.
- The card shows which sensor triggered it, which sensors are still above activation, which prevent the release,
  the remaining countdown and why. *Test the rules* runs your own readings and elapsed time without saving or
  moving anything.
- Precedence is unchanged: sleep, night, evening privacy, manual holds (with their policy), unavailable covers,
  competing automations and the movement limits come first, and Global Modes choose the preset. A release
  re-evaluates the whole decision; it never forces a curtain open.
- After a restart protection stays on and the release delay starts again: a restart never shortens it.
- With the feature on, the window's sensors are its sunlight evidence (instead of the house's outdoor light
  sensor and the sun geometry); with it off, the window decides exactly as before.

### Cross-room switch targets (Switches → a switch → a button)

For light actions (on, off, toggle, brightness steps, hold-to-dim, set brightness, set colour temperature) the
*Lights* field holds one or more targets as chips. *Add a target…* lists **This room**, the **adjacent rooms**
(the rooms configured as adjacent, and shared open-plan spaces; floor-plan suggestions count once you accepted
them in Rooms) and, with the search box, every **other room**: each room's "all lights" or a single light or
group, shown with its room ("Trappa → Stairway Lamp"). Under the chips you see a summary ("Toggle Stairway
Lamp (Trappa)") and *Affects n lights*: the physical lights, which ones a group already covers, and what a light
does not support (an on/off lamp never gets a brightness or colour).

- The press still belongs to the switch's room. The lights in another room count as a **manual change in their
  own room**, with that room's override time; *Resume* in that room releases it, and the next press sets it
  again. That room's occupancy is not changed.
- A switch press is a deliberate command: it is executed even when the other room is empty or bright.
- Commands go through the same executor as all automatic lighting, only to configured lights; a group's members
  are never commanded twice; a hold-to-dim run stops on release and when the App loses control.
- Targets are resolved at every press: a renamed light follows; a removed room or light is reported, not guessed.
- Existing mappings keep their meaning exactly.

### Migrating from 0.9.0 with this version

These features changed engine code that integration 0.9.0 checks before a lighting, curtain or forecast
handover, so *Transfer control* refuses those three (occupancy still hands over). Update the integration to
1.0.0 and continue with *Activate*: the App then takes every subsystem itself, and nothing is switched by it.

## Phase A6: the App is the product

This version works with the integration **0.9.0** (through its bridges, exactly
as in A5, for the time of the migration) and with the companion **1.0.0**.
Nothing changes when you update: the integration keeps running everything until
you run the migration wizard.

### Settings → Migration & System

- **Overview**: who runs each subsystem (as the companion or the 0.9.0
  integration acknowledges it), the configuration authority (the integration, a
  staged copy, or the App), the companion (version, paired, entities), the
  configuration host (revision, backups, published to the companion), *Take
  control* / *Stop* per subsystem, *Revoke the App's control* (an emergency stop:
  nothing is switched by it), *Export configuration*.
- **Migration wizard**: eight steps, each started by you and recorded:
  1. **Inspect** (changes nothing), 2. **Backup** (one file in the App's storage,
  without credentials), 3. **Validate**, 4. **Transfer config** (review, then
  apply as a staged copy while 0.9.0 still runs; with the companion it also
  imports the lighting, curtain and forecast stores the App does not have yet), 5. **Transfer control**
  (occupancy → lighting → curtains → predictions, the reviewed handovers of
  A3.2–A5), 6. **Activate** (after you updated the integration to 1.0.0 and
  restarted Home Assistant: pairs, compares the staged copy with the companion's
  legacy data, makes the App the authority, claims every subsystem),
  7. **Verify**, 8. **Retire** (the companion backs the legacy data up inside
  Home Assistant and removes it from the active installation). *Abort* gives
  control back or restores the legacy data for a rollback to 0.9.0.
- **Outage behaviour**: what works while the App is away. In short: nothing is
  switched or moved, the entities keep their last published state marked as not
  fresh, no vacancy is inferred, physical switches keep working through their
  own integrations, and the App claims control again with a fresh session when
  it is back. **There is no fallback engine any more**: an App outage means no
  automatic decisions until the App is back. Keep the App's watchdog on.

### Settings → Setup

A new home, without the monolithic integration: install the companion
(optional, for entities and device control), pair it, **Create the
configuration** (no rooms, no mood source, nothing automatic), then rooms,
sensors, lighting, moods, switches, curtains, predictions, a floor plan, and the
explicit review that switches a room to Automatic. Every room starts Observing.

### Dashboard

The house now, from the App's own runtimes: occupied rooms, every room's
occupancy, lighting mode, mood, daylight and prediction, the mood, curtains,
predictions, unavailable devices, health and who runs what, with a mark when a
value is not fresh.

### Companion cards (phones)

The companion's compact cards keep the names of the old editor cards so your
dashboards keep working: **Smart Occupancy: Home** (every room with Resume and
Automatic / Observe), **Smart Lighting** (the mood and a mood picker), **Smart
Room Lighting** (one room) and **Smart Curtains** (windows by room, presets,
holds, modes). Their actions are relayed to the App and need an administrator;
settings are edited in the App.

## Phase A5: Curtains and Predictions

This version needs Smart Occupancy & Lighting **0.9.0**. The last two
automation subsystems move into the App the way lighting did: the App runs the
integration's own curtain and forecast code (byte-identical copies, checked by
both test suites and by the bridges), in **shadow mode** until you hand each one
over on its Control page. Nothing changes until you decide.

**Curtains** (Overview, Windows, Global Modes, Global Schedule, Environment,
Diagnostics, one window workspace, Control): every window and cover with its
position, Automatic / Observing / Off, the current and desired preset, the
automatic condition and its explanation, the next expected action, warnings,
unavailable covers, manual changes and holds; window creation and discovery,
rooms, orientation, cover layers and their capabilities (a cover that cannot set a
position gets only open / closed targets), individual and whole-window presets,
manual-move policies, safety settings; the Global Modes assignment matrix per
window and cover with a review of what would move; the Global Schedule timeline,
phases, workdays and days off, sunset and daylight conditions and the next
transition; the environment: indoor and outdoor temperatures, light, sun
direction and elevation, exposure, weather, glare, thermal protection, sensor
quality and fallbacks; diagnostics: the last movement, requested and reached
positions, failed and interrupted operations, manual interventions, safety
blocks, competing automations, the decision and operation history, export and a
reviewed import. Until the handover every change is saved in the integration
(its own commands are forwarded), so the Smart Curtains card and the App show the
same configuration.

**Curtains → Control**: who commands the covers (read from the integration, the
only authority), the shadow comparison (cover commands: same service, cover and
position within 3 s; window decisions within 10 s), readiness (2 h, 5 matched
decisions, no command mismatch, no cover moving, the same code and
configuration), the ownership history, backups and:

- **Review handover to the App…**: the integration fences its manager, lets a
  running movement finish (and **refuses the handover while a cover is still
  moving**), hands its configuration and runtime state over (manual holds,
  applied presets, move budgets, deferred changes, calibration, history) and
  stops. The App starts from exactly that copy; nothing moves because of the
  handover, and the start-up settle period passes before any rule may send.
  Afterwards the App moves a cover only through the integration's fenced
  executor, which accepts a command only from the App session that holds the
  lease, for `set_cover_position`, `open_cover` or `close_cover` of one
  configured cover, with the App's own context. The configuration lives in the
  App and is mirrored to the integration; the curtain card is read-only.
- **Return control to the integration**: the App stops at once (a running
  movement is first allowed to finish) and hands its final configuration and
  state back; nothing moves. If the App stops answering for about 30 s, the
  integration takes the curtains back by itself from the mirror: an operation the
  App left running is marked interrupted and never replayed, the covers' late
  reports are not taken for manual changes, and the windows are evaluated again
  only after the settle period. A restarted or updated App does **not** take
  control back. Without the App: Developer tools → Actions →
  `smart_occupancy.curtains_return_to_integration`.
  *Force return* is refused while a cover is being moved (the integration could
  take that movement for a manual change); tick "Return even while a cover is
  moving" to return anyway.
- **Export / import** (Curtains → Diagnostics): the import is reviewed first.
  Windows the file would switch to *Automatic* are listed and restored as
  *Observing* unless you confirm them in the review; a restored file never
  starts moving covers on its own.

**Predictions** (House, Patterns, Accuracy, Settings, one room workspace,
Control): the prediction status, learning progress and history coverage, the
predicted next room, every room's probability over 5 / 15 / 30 / 60 minutes, the
probability of entering within a horizon, likely arrival periods, confidence with
its reasons (probability and confidence are different things and are shown as
such; with insufficient data the pages say so instead of showing numbers),
plain-language explanations and factors; hourly, weekly, workday / day-off and
movement patterns, observed against learned; accuracy with Brier scores against
the schedule and persistence baselines, calibration and daily history; settings
(global defaults, room overrides, learning window, decay, minimum data,
probability and confidence thresholds, context entities, workday source, room
connections, learn now and rebuild), export and import. Forecasts are
observation only: nothing on either side ever commands a device.

**Predictions → Control**: who produces the forecasts, the comparison (both
sides' predictions per room; agreement when status and prediction match and the
probabilities are within 2 points), readiness (1 h, 10 agreements, same code,
settings and learned days), history, backups and the handover: the integration
exports everything it learned (validated and verified against the same model
code), stops learning and shows the App's published forecasts on its unchanged
entities (`sensor.smart_occupancy_<room>_forecast` and the others). The App reads
history through Home Assistant's API (`history/history_during_period`), never from
the database, and never re-fetches the days it received. Return control, the
automatic fallback and `smart_occupancy.forecast_return_to_integration` give the
learned data back; nothing is retrained.

**Floor plan**: a drawn window can be linked to a curtain window; the plan can
suggest the window's orientation from its wall and north, applied only after
review; Live mode shows cover positions, modes, holds, sun protection and
warnings. A 2D plan gives direction only, not sun exposure or obstructions.

## Phase A4: Lighting, Moods and Switches

Needs Smart Occupancy & Lighting **0.8.0** or later. The App runs the
integration's own lighting code: every rule (moods and their Occupied and
Vacant profiles, possible occupancy, vacancy, daylight, ambient light levels
and brightness rules, zones and shared spaces, the Room Supervisor, wall
switches and remotes, Priority Inputs, manual overrides) is the same code,
copied byte for byte and checked.

**Nothing changes until you decide.** After installing or updating, the
integration still controls the lights, and the App's lighting runs in
**shadow mode**: it receives the same events, decides the same way and records
what it would send, but sends nothing.

The new pages:

- **Lighting** — every room's automatic lighting, live decisions and warnings;
  a room's workspace (lights, Automatic or Observing, vacancy, daylight,
  possible occupancy, ambient levels and brightness rules, zones, the Room
  Supervisor, the decision log); global settings; **Lighting → Control**.
- **Moods** — where the mood comes from (the three helpers, one existing
  selector, or the built-in mood selector with moods you define), mood
  inheritance and fallback, light types, Occupied and Vacant profiles with
  previews and explicit tests.
- **Switches** — wall switches and remotes (Home Assistant event entities,
  MQTT device triggers, Zigbee2MQTT through Home Assistant's MQTT), button
  actions, Observing or Active, manual overrides with "Resume automatic
  lighting", and Priority Inputs.

Until the handover, changes you make on these pages are saved in the
integration (the App sends the integration's own commands), so its dashboard
cards and the App show the same configuration.

**Lighting → Control** shows who controls the lights (read from the
integration, the only authority), the shadow comparison (a command matches
when the same service, targets and data appear on both sides within 3 s;
every mismatch is kept with both sides' commands), the readiness checks, the
ownership history, an export of the lighting configuration and state, and:

- **Review handover to the App…** — available when every check passes: 2 hours
  of comparison, 10 matching commands, no mismatch, the same lighting code and
  the same configuration. The integration stops its lighting controller after
  the commands it is sending have finished and hands its configuration and
  state over; the App starts from exactly that copy. Manual overrides are kept,
  rooms keep their Automatic or Observing choice, and nothing is switched by
  the handover. The configuration then lives in the App; the integration
  keeps a mirror of it, and its dashboard cards and options steps for
  lighting are read-only and say so.
- **Return control to the integration** — works at any time: the App stops at
  once and hands its final configuration and state back; the integration
  starts from them, keeps manual overrides and switches nothing.

**While the App controls the lights**, it sends every command through the
integration, which accepts it only from the App session that holds the
current lease, and only for light services of lights in the lighting
configuration, configured relays, and the scenes, scripts and WLED presets
your switch buttons use. If the App stops or loses its connection for about
30 seconds, the integration takes the lights back by itself, from the App's
last configuration and state, without switching anything. A restarted or
updated App does **not** take control back: hand over again when you are
ready. After a Home Assistant restart, the same App process resumes within
2 minutes; otherwise the integration takes over.

If the App cannot be reached at all, Developer tools → Actions →
`smart_occupancy.lighting_return_to_integration` (administrators only) returns
control without it.

**Moods without helpers.** Moods are names. Besides the three-helper scheme,
the mood can come from one existing `select` / `input_select`, or from the
**built-in mood selector**: define your moods on Moods → Mood source and the
integration creates `select.smart_lighting_mood` (with the Manual option, which
pauses automatic lighting). Automations, dashboards and voice assistants change
it like any select. A new installation needs no helper.

## Phase A3.2: the occupancy engine

Needs Smart Occupancy & Lighting **0.7.0** or later. The App runs the
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
5. **Companion** (recommended): install the *Smart Occupancy & Lighting*
   integration 1.0.0 with HACS, add it under Settings → Devices & services, then
   pair it in the App (Settings → Setup, or Settings → Migration & System).
   Without it the App configures everything but has no native entities and
   cannot command lights or covers.

Only administrators see the sidebar entry. The App works the same in the Home
Assistant Companion app on phones and tablets.

### Updating

When a new version is in the repository, Home Assistant offers the update on
the App's page (Settings → Apps). Updating rebuilds the App on your device.

### Uninstalling

Settings → Apps → Smart Occupancy & Lighting → **Uninstall**. The companion
integration keeps its entities with their last published state (not fresh) and
runs no automation; export the App's configuration first (Settings → Migration &
System → Export configuration) if you may want it back.

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

Since Phase A6 the whole move is one wizard (Settings → Migration & System, see
above). The earlier, module-by-module path still works on 0.9.0 and is what the
wizard's *Transfer control* step runs: For each module the App reports where it is **configured**,
where the configuration is **stored**, and which system **runs** it. In this
version Rooms and Presence are configured in the App and stored in the
integration. Room occupancy is decided by the integration's engine until you
hand it over to the App on Presence → Engine; Presence then counts as migrated
for as long as the integration acknowledges the App as the owner. Lighting,
Moods and Switches are configured in the App; they are stored in and run by the
integration until you hand lighting over on Lighting → Control, and in and by
the App afterwards (the three move together: one lighting controller, one
configuration). Curtains and Predictions (Phase A5) each have their own
ownership: configured in the App, stored in and run by the integration until
their handover on Curtains → Control / Predictions → Control, and in and by the
App afterwards. Exactly one manager commands the covers at any time, and exactly
one side produces the forecasts.

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
  timeline), the integration's own validated room, presence and lighting
  commands, the integration's occupancy bridge and its lighting bridge. It has
  no command to call a service: after a lighting handover its light commands go
  through the lighting bridge, where the integration checks the lease, the
  service and the target of each one. The bridge works only over Home
  Assistant's own connection, is for administrators only, and is fenced:
  stale, duplicated or malformed messages are refused. It cannot call
  services, write states or change registries. Its token never reaches the
  browser.
- Handover and rollback need a Home Assistant administrator.
- Floor plans and their images are stored in the App's data folder. Images are
  checked by their content (PNG, JPEG, WebP only; no SVG) and are only served
  through Home Assistant's Ingress, never from a public address.

## Known limitations (Phase A6)

- **No automation while the App is away.** The companion runs no engine. Keep
  the App's watchdog on; check Settings → Migration & System after an update.
- The App needs the Supervisor (Home Assistant OS or a Supervised installation).
- The Room Status card still reads the room sensor's attributes; the editor tabs
  the 0.9.0 cards had are gone (the App is the editor).
- The *Restart recovery* check in **Verify** is informational until Home
  Assistant was restarted once with the companion.
- Rolling back to 0.9.0 after **Retire** needs *Abort → Restore the legacy data*
  first (or the companion's `legacy/restore` command), then the downgrade.

## Known limitations (Phase A5)

- The curtain comparison thresholds (2 h, 5 matched window decisions) and the
  forecast ones (1 h, 10 agreements) are fixed; development setups lower them with
  `SL_CURTAINS_MIN_SHADOW_SECONDS`, `SL_CURTAINS_MIN_DECISIONS`,
  `SL_FORECAST_MIN_SHADOW_SECONDS`, `SL_FORECAST_MIN_MATCHES` only. Cover
  commands are rare (a few a day), so the readiness counts matched decisions and
  requires zero command mismatches.
- While the App controls the curtains the integration still executes its cover
  commands (after checking them), publishes the occupancy the rules read, and
  keeps the mirror for a return of control. While the App produces the forecasts
  the integration still provides the forecast entities and the Recorder's oldest
  timestamp (there is no websocket API for it).
- The App's forecast observations (predicted, confirmed, missed, pattern
  changed) go to the App's own log while it produces the forecasts, not to the
  integration's lighting decision log.
- Rooms, their sensors and lights, light types and the mood source stay in the
  integration's options; moving them into the App is Phase A6, together with the
  minimal companion integration and the retirement of the legacy runtimes.
- Not verified on a real Home Assistant OS installation (tested with the real App
  image against a throwaway Home Assistant).

## Known limitations (Phase A4)

- The lighting comparison thresholds (2 h, 10 commands) are fixed. Development
  setups can lower them with `SL_LIGHTING_MIN_SHADOW_SECONDS` and
  `SL_LIGHTING_MIN_COMMANDS` only.
- Rooms, their sensors and lights, light types and the mood source stay in the
  integration's options (edited from the App through its validated commands);
  moving them into the App is a later phase.
- While the App controls lighting, the integration still executes its light
  commands (after checking them) and still decides occupancy unless that was
  handed over too; it also provides the room entities and the built-in mood
  selector.
- After the handover the integration's room lighting entities show what the
  App publishes (every 2 s at most).

## Known limitations (Phase A3.2)

- The comparison thresholds (2 h, 20 transitions) are fixed. Development
  setups can lower them with the environment variables
  `SL_ENGINE_MIN_SHADOW_SECONDS` and `SL_ENGINE_MIN_TRANSITIONS` only.
- The integration's own engine keeps running while the App decides (hot
  standby). Removing it is a later, separately approved phase.
- Curtains and forecasts are not migrated. They keep reading the published
  occupancy.

- Floor plan: no curved walls, wall thickness, stairs or furniture (it is not a
  CAD program); sensor coverage and sun analysis are not drawn (the
  integration does not know a sensor's coverage or direction).
  Live occupancy needs rooms observed by Smart Occupancy.
- Presence: zones are edited in Presence → a room → Zones since 0.9.0 (before:
  shown only). The integration's presence log
  keeps the latest 400 decisions and changes while Home Assistant runs;
  older history comes from the Recorder (room state, reason and sensors), so
  decisions before the last restart show the state and reason only.
- Confidence is the integration's evidence-quality score, not a probability.
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
