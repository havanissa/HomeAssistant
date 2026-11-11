# Changelog

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
