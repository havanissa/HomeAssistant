# Smart Occupancy & Lighting

A Home Assistant custom integration that will eventually provide intelligent occupancy detection, predictive room occupancy, mood-based lighting and lighting supervision.

**This is Phase 0.2: a multi-sensor occupancy engine per room, plus a read-only Room Status dashboard card (see [CHANGELOG.md](CHANGELOG.md)). It runs in Observe mode only.** It reads states and registries, computes its own occupancy for each room, and exposes diagnostics. It never:

- calls a light service (or any other service)
- changes automations, scripts, scenes, helpers or the mood system (existing occupancy helpers are only *read*, for comparison)
- acts on its own occupancy: nothing uses it except the dashboard and your own optional automations
- disables or deletes entities other than its own
- polls, uses the internet, or writes to the Recorder database directly

## Requirements

- Home Assistant 2026.10 or newer. Tested against **2026.11.1** (with its frontend build 20261028.5) and 2026.10.4.
- No Python dependencies

## Installation

### HACS (custom repository)

1. In HACS: ⋮ → **Custom repositories** → add `https://github.com/havanissa/HomeAssistant`, type **Integration**.
2. Install **Smart Occupancy & Lighting** and restart Home Assistant.

Updating from 0.1.x keeps your configuration: it's migrated automatically. See [Upgrading](#upgrading-from-01x).

### Manual

1. Copy `custom_components/smart_occupancy/` to `/config/custom_components/smart_occupancy/`.
2. Restart Home Assistant.

## Configuration

**Settings → Devices & services → Add integration → Smart Occupancy & Lighting.** Only one instance can be added; everything else is done in its options.

### Initial setup

1. **Mood system.** Select Auto Mood, Manual Mood and Main Mood. `input_select.auto_mood`, `input_select.mood` and `input_select.main_mood` are pre-filled if they exist. Set the Manual Mood option that means "Auto" (default `Auto`). Set the mismatch grace period (default 30 s) to cover the time your automations need to copy the mood.
2. **Light categories.** Map each of the seven initial categories to a Home Assistant label. An existing label with the same name as the category is pre-selected. Tick *Create labels for categories left empty* to create the missing labels. This only creates the labels; you apply them to lights yourself.
3. **Rooms.** Select the areas to enable. An area named **Kamar Norður** is pre-selected as the pilot room.
4. **Per room: assign signals and lights.** Fields are pre-filled from the devices discovered in the area, using device class, never names:
   - `motion` → PIR
   - `occupancy` and `presence` → radar
   - `door` → door contact

   Window and opening contacts are listed but not pre-assigned.

   **Check these suggestions.** Many Zigbee PIR and bed sensors report `occupancy` just like radars. Move them to the correct field.

   You can also select managed lights or groups, and an optional *existing occupancy helper*. That helper is only read, to compare it with this integration's occupancy.
5. **Per room: signal roles.** Choose *Trigger occupancy*, *Maintain occupancy*, *Confirm vacancy* or *Ignore* for each signal (see [Occupancy engine](#occupancy-engine)). The defaults are:
   - PIR and door: trigger
   - radar, PC and bed: maintain
   - other: ignore

   You can also mark signals as *inverted*, for sensors that mean activity when they're off.

### Options (⚙ Configure)

| Menu | What it does |
|---|---|
| Mood helpers and operating mode | Change the mood entities, the Auto option and the grace period. Supervise and Full Control are listed but rejected in this phase. |
| Occupancy timing | Global timing defaults for every room (see [Timing](#timing)). |
| Rooms | Pick any area (✓ marks enabled rooms). Then enable or disable it, change its assignments, roles and inverted signals, and finally its *Occupancy* settings: per-room timing overrides (empty = global) and adjacent rooms. *Remove this room's configuration* deletes the room's stored settings. Disabling keeps the configuration. |
| Light categories | Add, rename or re-label, and remove categories, or *Create missing labels*. A category's internal key stays stable when you rename it. Removing a category also removes its profile entries. |
| Mood profiles | Prepare profile entries at three levels: **Global** (mood × category), **Room override** (room × mood × category) and **Individual light override** (room × mood × light). Each entry has an action (On/Off/Inherit/Ignore), brightness %, colour temperature (K), RGB, effect and transition. An empty field inherits from the less specific level. Entries are validated and stored only. |

Each options run makes one change and saves it. The integration then reloads with the new configuration.

### Inheritance rules (prepared for later phases)

`Global[mood][category]` → `Room override[mood][category]` → `Light override[mood][light]`, merged field by field. The most specific level wins.

- *Inherit* leaves the action as decided by the less specific level. If no level sets one, the result is *Ignore*.
- Choosing RGB at a more specific level clears an inherited colour temperature, and vice versa.
- *Off* drops brightness, colour and effect.

The diagnostics download includes a *preview* of the resolved profile for every managed light in the current mood. Nothing is applied.

## Occupancy engine

Each enabled room has its own state machine, fed by state-change events of its signals. There is no polling, and timers run only while something is pending.

### Sensor roles

| Role | While active | When cleared |
|---|---|---|
| **Trigger** (PIR) | Immediate occupancy. Ignored once it has been on longer than *trigger stuck after*, so a stuck PIR can't hold a room forever. | Nothing |
| **Maintain** (radar, PC activity, bed) | Holds occupancy: vacancy can't be confirmed | Not evidence of vacancy by itself. Clears shorter than the *dropout grace* are bridged. |
| **Confirm vacancy** | Holds occupancy, like Maintain | **Vacancy evidence**: vacancy needs only *exit confirmation* of quiet instead of the *inactivity timeout* |
| **Ignore** | — | — |

**Door contacts** count only as movement, whatever their role (except Ignore):
- A door movement alone never makes a room occupied. It shows as *possible occupancy* for the correlation window, with *occupied* staying off.
- A door movement followed by nothing new in the room is exit evidence. Movement in an *adjacent room* right after it strengthens that evidence.
- A door that stays open or closed means nothing.

**Unknown and unavailable are never "off".**
- An unavailable signal is neither presence nor vacancy evidence, and while any presence signal is unavailable the fast exit path is disabled.
- A presence sensor that drops out *while reporting presence* holds the room for one extra inactivity timeout.
- When it reconnects, the quiet period restarts from the reconnect.
- When every occupancy sensor is unavailable, the room is `uncertain` and keeps its last occupied value.

### States

| State | Occupied | Evidence required |
|---|---|---|
| `vacant` | off | No activity for the required quiet time, or never occupied |
| `possible_occupancy` | on (off for a door alone) | New, uncorroborated evidence: one trigger event, a presence signal that just turned on, or a door movement |
| `confirmed_occupancy` | on | A presence signal active for the correlation window, two different signals within the window, or a trigger active for the whole window. Stays confirmed while any evidence remains. |
| `possible_vacancy` | on | It was occupied, and no evidence remains. Vacancy is confirmed once the room has been quiet for *exit confirmation* (if there is positive vacancy evidence, or occupancy was never confirmed) or the *inactivity timeout* (otherwise). Any new activity returns to occupancy. |
| `uncertain` | last value | No occupancy sensor has a known state, or none is configured |

Occupied → vacant always needs at least *exit confirmation* of quiet, and presence dropouts are bridged, so the output doesn't toggle when sensors briefly disagree. Events older than a signal's current value (out of order) are ignored.

**Restart and reload.** The previous decision is restored from `.storage`. An occupied room stays occupied during the *start-up hold*, and its quiet period starts again from the restart, because nothing is known about the time Home Assistant was down. So a restart can't cause a false vacancy.

### Timing

Every value can be set globally and overridden per room.

| Parameter | Default | Meaning |
|---|---|---|
| Movement correlation window | 10 s | Signals within this window corroborate each other. A single signal must last this long to confirm occupancy on its own. |
| Exit confirmation | 300 s | Quiet time before vacancy when there is positive vacancy evidence |
| Normal inactivity timeout | 600 s | Quiet time before vacancy otherwise |
| Radar dropout grace period | 30 s | Bridges short clears of Maintain and Confirm-vacancy signals |
| Trigger stuck after | 1800 s | Trigger signals active longer than this are ignored (0 = off) |
| Presence sensor stuck after | off | The same for Maintain and Confirm-vacancy signals. Off by default, because people can sit still for hours. |
| Start-up hold | 120 s | Minimum time the restored state is kept after start-up or reload |

### Confidence

`sensor.…_confidence` is an **evidence-quality score**, not a predicted probability or a measured accuracy. It is calculated as:

> 100 × *evidence strength* × (0.5 + 0.5 × *sensor coverage*)

- *Sensor coverage* is the share of the room's occupancy sensors that report a usable, non-stuck state.
- *Evidence strength* depends on the state:
  - `confirmed_occupancy`: 0.6, plus 0.2 if a presence sensor is active, plus 0.2 if two or more signals are active
  - `possible_occupancy`: 0.4 (0.25 for a door alone)
  - `possible_vacancy`: 0.4 to 0.5
  - `vacant`: 0.8 (1.0 when every presence sensor reports clear)
  - `uncertain`: 0

Both factors are in the sensor's attributes.

### Pilot: Kamar Norður

1. Open **Configure → Rooms → Kamar Norður** and check the assignments:
   - PIR `binary_sensor.pir_kan_occupancy`
   - radar `binary_sensor.rdr_kno_occupancy`
   - PC activity: your Nobara PC sensor
   - door `binary_sensor.swh_kno_contact`
   - existing occupancy helper `input_boolean.tilstadar_kamar_nord`

   The Aqara PIR reports device class `occupancy`, so discovery suggests it as a radar. Move it to *PIR sensors*.
2. Keep the default roles: PIR = Trigger, radar = Maintain, PC = Maintain, door = Trigger. Set the radar to *Confirm vacancy* if you trust its clear state enough for the faster exit path.
3. Optionally, on the room's *Occupancy* step, choose adjacent rooms such as the hallway.

With that:
- PIR motion gives immediate occupancy.
- The radar and PC activity hold it, so a radar clear never ends occupancy while the PC is active.
- A stuck PIR is ignored after 30 min.
- An unavailable sensor never leads to vacancy.

The existing lighting automation and helper are not touched. Compare them with `sensor.smart_occupancy_kamar_nordur_helper_comparison`.

## Room Status card

The integration serves and loads the card itself: there is no resource to add and nothing to install. After updating, reload the browser once.

Add it with **Edit dashboard → Add card → Smart Occupancy Room Status**, and pick the room in the visual editor. Or use YAML:

```yaml
type: custom:smart-occupancy-room-card
area: kamar_nordur        # the area id of the room
# entity: sensor.smart_occupancy_kamar_nordur_state   # alternative to area
# title: Kamar Norður     # optional
```

The card is read-only and shows:
- the room name
- the occupancy state
- the evidence quality
- the active sensors
- a pending vacancy, with a live countdown
- the reason for the current state
- the observe-mode badge
- three separate health rows. Each row names the exact sensors or lights involved and its reason:
  - **Occupancy detection**: whether presence and vacancy can be determined, marked *usable* or *not usable*. Only sensors count here.
  - **Lighting**: whether the lights are available.
  - **Configuration**: whether the room's assignments and settings are valid.
- if a helper is configured, the comparison with it

An unplugged light shows under *Lighting* only. Occupancy stays healthy if its sensors are. The layout adapts from phone width to wide dashboards (sections grid supported). It uses only Home Assistant theme variables, so light and dark themes both work.

## Entities

All entities belong to one service device, *Smart Occupancy & Lighting*. Unique IDs are stable: room entities are keyed by `area_id`, so they survive area renames.

| Entity | State | Notes |
|---|---|---|
| Status | healthy / warning / config_error / unavailable | One-line `summary`, counts per classification, every problem with details |
| Operating mode | observe | `configured_mode` attribute; lighting control always false |
| Effective mood | Main Mood value | Attributes: `auto_mood`, `manual_mood`, `expected_main_mood`, `follows` (auto/manual), `consistent` |
| Mood mismatch | on / off / unknown | On only if Main Mood disagrees with the rule for longer than the grace period. Unknown when a helper is unavailable. |
| Configured rooms | count | Room → status |
| Light categories | count | Per category: label and number of classified lights |
| Discovered sensors | count | Candidate signals found in enabled rooms |
| Missing entities | count | Configured entities that are deleted or disabled. `items` and `entities` hold the details. |
| Unavailable entities | count | Configured entities that are unavailable, have no value, or are not provided by their integration. `items` and `entities` hold the details. |
| Configuration problems | count (configuration errors + warnings) | `problems` (actionable) and `notes` (informational). Offline devices are **not** counted here. |
| *Room* observation | healthy / warning / config_error / unavailable | Health of the room's signals and lights. **Not** an occupancy state. |
| Re-check diagnostics (button) | — | Re-evaluates immediately; reads states only |

**Per room (Phase 0.2).** IDs are suggested from the area name when the entities are created. They stay the same when the area is renamed: unique IDs are keyed by `area_id`.

| Entity | State | Notes |
|---|---|---|
| `binary_sensor.smart_occupancy_<room>_occupied` | on / off | Device class occupancy. Keeps its last value while the room is `uncertain`. |
| `sensor.smart_occupancy_<room>_state` | `vacant` / `possible_occupancy` / `confirmed_occupancy` / `possible_vacancy` / `uncertain` | The evidence is in its attributes: `active_sensors`, `unavailable_sensors`, `stuck_sensors`, `recent_door_movement`, `pending_vacancy`, `vacancy_confirm_at`, `quiet_since`, `required_quiet_seconds`, `vacancy_evidence`, `reason`, `signals` (per-signal detail), `health` (occupancy / lighting / configuration), `comparison`, `timing`. These attributes are not recorded. |
| `sensor.smart_occupancy_<room>_confidence` | 0–100 % | Evidence quality (see [Confidence](#confidence)) |
| `sensor.smart_occupancy_<room>_last_activity` | timestamp | Start of the latest motion, presence or door movement. `source` is the entity. |
| `sensor.smart_occupancy_<room>_reason` | text | Why the room has its current state |
| `sensor.smart_occupancy_<room>_helper_comparison` | agree / disagree / unknown | Only with an existing occupancy helper. Attributes: `disagreements`, `disagreement_since`, `total_disagreement_seconds`, `helper_cleared_prematurely`, `engine_possibly_retained_too_long`, `over_retention_total_seconds` / `_max_seconds`, `helper_only_occupied`, `statistics_since`. A disagreement counts only after it has lasted longer than the correlation window. |

**How the comparison counts.** When the helper turns off while the engine still says occupied, an episode starts. It ends in one of two ways:
- *Premature clear*: the helper turns on again, or new activity is seen, before the engine reaches vacancy.
- *Possible over-retention*: the engine reaches vacancy without new activity. The overhang is recorded.

Only these aggregate counters are kept, in `.storage`, never in Recorder tables.

### Reading problems in the UI

Open any diagnostic entity and expand **Attributes**, or go to **Developer tools → States**. You don't need the log.

- **`items`** has one line per affected entity, for example `light.x (Spot 3) — Kamar Norður — Light unavailable`.
- **`entities`**, `problems` and `notes` hold structured details for each problem:

  | Field | What it holds |
  |---|---|
  | `type`, `title` | Problem type and its short name |
  | `classification` | `config_error`, `unavailable`, `warning` or `info` |
  | `severity` | How serious it is |
  | `category` | `configuration` or `operational` |
  | `explanation` | What is wrong, in plain language |
  | `action` | What to do about it |
  | `room`, `entity_id`, `name` | Where it is, and the entity's friendly name |
  | `first_detected`, `last_checked` | When it was first seen and last re-evaluated |

**Room observation attributes**

| Attribute | Meaning |
|---|---|
| `configuration_valid` | No configuration error in this room |
| `usable` | At least one Trigger/Maintain signal is available |
| `reasons` | Why the room is not healthy |
| `sensors_available` / `sensors_unavailable` | Sensors by health |
| `lights_available` / `lights_unavailable` | Lights by health |
| `pending` | Entities still loading during start-up |
| `operating_mode` | The mode the integration is running in |

**Room status**

| Status | When |
|---|---|
| `config_error` | The room's configuration points at something unusable: a deleted area, a deleted or disabled entity, or something that isn't a light |
| `unavailable` | Every Trigger/Maintain signal is unavailable, so occupancy can't be observed |
| `warning` | Anything else that needs attention. An offline optional light only makes the room a warning. |
| `healthy` | No problems |

**Entity health**

| Health | Meaning | Clears by itself? |
|---|---|---|
| `unavailable` | The device is offline | Yes, when it reconnects |
| `unknown` | No value reported yet | Yes, on the next report |
| `not_provided` | Registered, but its integration doesn't provide it (integration not loaded, or device removed) | No |
| `disabled` | Disabled in Home Assistant | No: enable it or remove it from the room |
| `missing` | Deleted | No: select its replacement or remove it |
| `pending` | Still loading during start-up. It gets a 120 s grace period after start-up and is not reported. | — |

**When problems clear**

Problems clear automatically: every evaluation rebuilds the full picture. An evaluation runs on each of these events:
- a configured entity changes state
- an area, device, entity or label registry change, such as an entity being added, removed, renamed or disabled
- an options change
- a reload
- Home Assistant start-up

`first_detected` survives reloads and restarts.

### Classification

A light belongs to a category when the category's label is on the light entity or on its device. This matches how Home Assistant resolves `label_id` targets. Names are never used.

### Problems detected

- Missing, unavailable or not-loaded entities
- Deleted areas and labels
- Mood helpers with the wrong domain or missing
- Auto option not found among the Manual Mood options
- Auto Mood options that Main Mood lacks
- Categories without a label, or sharing a label
- Managed lights with no category, or with several categories
- Lights managed by several rooms
- **A group and its member both selected for independent control**
- Invalid profile values: kelvin together with RGB, *Off* with attributes, values out of range
- Profile entries for an unknown mood, category or light
- RGB, colour temperature or effect requested on a light that doesn't support it

### Recorder and logging

- No sensor has a `state_class`, so no long-term statistics are created.
- List-valued attributes and `last_checked` are excluded from the Recorder.
- While problems exist, `last_checked` is republished at most every 15 minutes.
- Problem history is kept in `.storage`, not in the Recorder.
- States change only when the observed situation changes. Occupancy entities write only when their state or attributes change. A pending vacancy is one scheduled callback: nothing is written while waiting, because the card counts down in the browser.
- Plain on/off changes of sensors go to the occupancy engine only. They don't rebuild the diagnostics snapshot.
- Logs are structured `key=value` lines (problem details are in entity attributes, so the log is optional):
  - `problem_detected` (warning, once per problem)
  - `problem_resolved` (info)
  - `entity_renamed` (info)
  - `observe_summary` (info, once after the start-up grace period)
  - `observe_snapshot` (debug)
- No problems are logged until the start-up grace period has passed. Integrations that are still loading would otherwise look like missing entities.

To see debug logs:

```yaml
logger:
  logs:
    custom_components.smart_occupancy: debug
```

**Download diagnostics** (integration page → ⋮) gives you:

- the full configuration and snapshot
- discovery results for every area
- all labels
- group memberships
- live active signals per role
- the resolved-profile preview

### Robustness

- **Event driven.** The integration listens only to state changes of configured entities and to updates of the area, device, entity and label registries. Updates are debounced.
- **Renames.** When an entity ID is renamed in the entity registry, the stored configuration follows it automatically.
- **Removed entities.** Removed entities are reported, not deleted from the configuration, so they recover if they come back.
- **Not yet loaded.** An entity that is in the registry but has no state yet counts as unavailable, not missing.
- **Mode lock.** A hand-edited `operating_mode` other than `observe` still runs as Observe and is reported as an error.

## Development and tests

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements_test.txt
.venv/bin/pytest
```

To test against a Home Assistant release newer than the harness (2026.11.1 at the time of writing), install it over the harness and use the runner, which aliases one helper that moved in 2026.11:

```bash
.venv/bin/pip install homeassistant==2026.11.1 home-assistant-frontend==20261028.5
.venv/bin/python scripts/run_tests.py
```

The suite covers:
- configuration persistence and migration
- the UI flows
- entity discovery, mood observation and label classification
- missing and unavailable entities
- restart behaviour and renames
- every occupancy rule: pure engine tests with explicit time, and tests in Home Assistant with the pilot's entity IDs
- the helper comparison
- card serving through the real frontend component

It also asserts that no service call is ever made. Any test that logs an ERROR, or in which Home Assistant reports deprecated usage, fails. CI (`.github/workflows/validate.yml`) runs hassfest, HACS validation and the tests on Home Assistant 2026.10.4 and 2026.11.1.

## Upgrading from 0.1.x

Configuration version 1.1 is migrated to 1.2 on the first start. Everything you configured is kept: mood helpers, categories and labels, rooms, lights, roles, reference helpers and profiles. The migration adds occupancy timing (defaults), per-room overrides (empty), adjacent rooms (none) and inverted flags (off). The 0.1.1 entities keep their IDs. Downgrading to 0.1.1 still loads, because the new keys are ignored.

## Known limitations (Phase 0.2)

- **Observe only.** Occupancy is computed and shown; no light or automation acts on it in this phase.
- **Confidence is not a probability.** It describes the quality of the current evidence, not how often the engine is right. That needs the historical learning planned for a later phase.
- **Non-binary PC sensors.** States are understood as active/inactive by a fixed list (`on`, `active`, `playing`, `unlocked`, `home`… / `off`, `idle`, `standby`, `locked`, `away`…). Numeric sensors, such as idle seconds, are `unknown`: wrap them in a template binary sensor.
- **Door sequences need the door to change state.** A door left open all day gives no exit evidence. Adjacency needs the adjacent room to be enabled.
- **The comparison counters are cumulative.** Open episodes are not restored after a restart, and there is no reset button yet.
- **The card shows its labels in English only.**

### From Phase 0.1

- **Group overlap detection needs exposed membership.** It works for Home Assistant light groups, which expose the `entity_id` attribute or have group helper options. Zigbee2MQTT and Hue bridge groups don't expose their members, so overlap with their member lights **cannot be detected**. Hue groups get an informational note; Zigbee2MQTT groups can't be recognised as groups at all.
- **The PIR, radar and bed split is only a suggestion.** Device class alone can't tell these apart when all of them report `occupancy`.
- **The role step labels fields with entity IDs.** Home Assistant cannot translate dynamically created form fields.
- **One change per options run.** Each run saves and closes. There is no bulk profile editor and no import from the existing mood automations.
- **Capability checks need a loaded light.** RGB, colour-temperature and effect checks run only for light-level overrides, and only while the light has a state.
- **Problems don't appear in Repairs.** They show up in the entities, the log and the diagnostics.
- **Recreated entities are not remapped.** An entity deleted and recreated under a different ID is reported as missing; it isn't remapped automatically.
- **Area renames reload the integration**, so the room entity names update.

## Next-phase readiness

| Area | Ready now | Next step |
|---|---|---|
| Configuration | Versioned options model (`config_version` 1) with typed dataclasses and a migration hook. Everything is set up through the UI. | Add fields with defaults, bump the version, migrate in `async_migrate_entry` |
| Occupancy | Engine per room with explainable states, evidence and comparison with the existing helper | Use it for lighting decisions in Supervise mode (Phase 0.3+) |
| Prediction | `OccupancyPredictor` protocol, plus `OccupancyHistory`, which already receives every transition (`NullHistory` in 0.2) | Store transitions and train a predictor |
| Dashboard | Read-only Room Status card, served by the integration | Mood-lighting editor (Phase 0.3) |
| Mood | Observation of the existing rule, with mismatch detection and grace period | Use the effective mood as the profile selector |
| Lighting | Label classification, group expansion, capability checks, `profiles.resolve()` | Dry-run supervision: compare the resolved profile with the actual light state and report deviations |
| Supervision | `supervision.py` gate: only Observe is permitted, and `assert_control_permitted()` refuses control | Explicitly extend `PERMITTED_MODES` in a future phase, with its own tests |
