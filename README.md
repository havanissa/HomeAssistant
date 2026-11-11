# Smart Occupancy & Lighting

A Home Assistant custom integration that will eventually provide intelligent occupancy detection, predictive room occupancy, mood-based lighting and lighting supervision.

**This is Phase 0.1: the foundation. It runs in Observe mode only.** It reads states and registries and exposes diagnostics. It never:

- calls a light service (or any other service)
- changes automations, scripts, scenes, helpers or the mood system
- disables or deletes entities other than its own
- creates occupancy entities (your existing occupancy helpers stay the only source of truth)
- polls, uses the internet, or writes to the Recorder database directly

## Requirements

- Home Assistant 2026.10 or newer (tested against 2026.10.4)
- No Python dependencies

## Installation

### HACS (custom repository)

1. Push this repository to GitHub, then replace `OWNER` in `custom_components/smart_occupancy/manifest.json` with your GitHub account.
2. In HACS: ⋮ → **Custom repositories** → add the repository URL, type **Integration**.
3. Install **Smart Occupancy & Lighting** and restart Home Assistant.

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

   You can also select managed lights or groups, and an optional *existing occupancy helper*. That helper is only observed, for comparison in later phases.
5. **Per room: signal roles.** Choose *Trigger occupancy*, *Maintain occupancy*, *Confirm vacancy* or *Ignore* for each signal. The defaults are:
   - PIR and door: trigger
   - radar, PC and bed: maintain
   - other: ignore

### Options (⚙ Configure)

| Menu | What it does |
|---|---|
| Mood helpers and operating mode | Change the mood entities, the Auto option and the grace period. Supervise and Full Control are listed but rejected in this phase. |
| Rooms | Pick any area (✓ marks enabled rooms). Then enable or disable it, change its assignments and roles, or *Remove this room's configuration*. Disabling keeps the configuration. |
| Light categories | Add, rename or re-label, and remove categories, or *Create missing labels*. A category's internal key stays stable when you rename it. Removing a category also removes its profile entries. |
| Mood profiles | Prepare profile entries at three levels: **Global** (mood × category), **Room override** (room × mood × category) and **Individual light override** (room × mood × light). Each entry has an action (On/Off/Inherit/Ignore), brightness %, colour temperature (K), RGB, effect and transition. An empty field inherits from the less specific level. Entries are validated and stored only. |

Each options run makes one change and saves it. The integration then reloads with the new configuration.

### Inheritance rules (prepared for later phases)

`Global[mood][category]` → `Room override[mood][category]` → `Light override[mood][light]`, merged field by field. The most specific level wins.

- *Inherit* leaves the action as decided by the less specific level. If no level sets one, the result is *Ignore*.
- Choosing RGB at a more specific level clears an inherited colour temperature, and vice versa.
- *Off* drops brightness, colour and effect.

The diagnostics download includes a *preview* of the resolved profile for every managed light in the current mood. Nothing is applied.

## Entities

All entities belong to one service device, *Smart Occupancy & Lighting*. Unique IDs are stable: room entities are keyed by `area_id`, so they survive area renames.

| Entity | State | Notes |
|---|---|---|
| Status | ok / warning / error | Error and warning counts, effective mode |
| Operating mode | observe | `configured_mode` attribute; lighting control always false |
| Effective mood | Main Mood value | Attributes: `auto_mood`, `manual_mood`, `expected_main_mood`, `follows` (auto/manual), `consistent` |
| Mood mismatch | on / off / unknown | On only if Main Mood disagrees with the rule for longer than the grace period. Unknown when a helper is unavailable. |
| Configured rooms | count | Room → status |
| Light categories | count | Per category: label and number of classified lights |
| Discovered sensors | count | Candidate signals found in enabled rooms |
| Missing entities | count | Configured entities that don't exist |
| Unavailable entities | count | Configured entities that are unavailable, unknown or not loaded yet |
| Configuration problems | count (warnings + errors) | Full structured problem list |
| *Room* observation | ok / degraded / not_configured / area_missing | Health of the room's signals and lights. **Not** an occupancy state. |

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
- List-valued attributes are excluded from the Recorder.
- States change only when the observed situation changes. Live signal activity (which sensor is currently on) is only in the diagnostics download, never in entity state.
- Logs are structured `key=value` lines:
  - `problem_detected` (warning, once per problem)
  - `problem_resolved` (info)
  - `entity_renamed` (info)
  - `observe_summary` (info, once after start-up)
  - `observe_snapshot` (debug)
- While Home Assistant is starting, no problems are logged. Integrations that are still loading would otherwise look like missing entities.

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

The suite covers configuration persistence, the UI flows, entity discovery, mood observation, label classification, missing and unavailable entities, restart behaviour and renames. It also asserts that no service call is ever made, and any test that logs an ERROR fails. CI (`.github/workflows/validate.yml`) runs hassfest, HACS validation and the tests.

## Known limitations (Phase 0.1)

- **Group overlap detection needs exposed membership.** It works for Home Assistant light groups, which expose the `entity_id` attribute or have group helper options. Zigbee2MQTT and Hue bridge groups don't expose their members, so overlap with their member lights **cannot be detected**. Hue groups get an informational note; Zigbee2MQTT groups can't be recognised as groups at all.
- **The PIR, radar and bed split is only a suggestion.** Device class alone can't tell these apart when all of them report `occupancy`.
- **The role step labels fields with entity IDs.** Home Assistant cannot translate dynamically created form fields.
- **One change per options run.** Each run saves and closes. There is no bulk profile editor and no import from the existing mood automations.
- **Capability checks need a loaded light.** RGB, colour-temperature and effect checks run only for light-level overrides, and only while the light has a state.
- **Problems don't appear in Repairs.** They show up in the entities, the log and the diagnostics.
- **Recreated entities are not remapped.** An entity deleted and recreated under a different ID is reported as missing; it isn't remapped automatically.
- **Area renames reload the integration**, so the room entity names update.
- **Placeholder URLs.** `documentation` and `issue_tracker` in the manifest still contain `OWNER`.

## Next-phase readiness

| Area | Ready now | Next step |
|---|---|---|
| Configuration | Versioned options model (`config_version` 1) with typed dataclasses and a migration hook. Everything is set up through the UI. | Add fields with defaults, bump the version, migrate in `async_migrate_entry` |
| Occupancy | Per-room signals with kind and role, health tracking, optional reference helper | Compute a *shadow* occupancy in diagnostics only and compare it with the existing helper before exposing anything |
| Prediction | `OccupancyPredictor` protocol with a `NullPredictor` | Plug in a model fed by signal history |
| Mood | Observation of the existing rule, with mismatch detection and grace period | Use the effective mood as the profile selector |
| Lighting | Label classification, group expansion, capability checks, `profiles.resolve()` | Dry-run supervision: compare the resolved profile with the actual light state and report deviations |
| Supervision | `supervision.py` gate: only Observe is permitted, and `assert_control_permitted()` refuses control | Explicitly extend `PERMITTED_MODES` in a future phase, with its own tests |
