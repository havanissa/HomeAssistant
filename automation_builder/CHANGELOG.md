# Changelog

## 0.4.0

The Lighting Designer, redesigned (version 2): a light's normal behaviour comes from Truth and Brightness
Matrices and optional Colour & Mood; switches and remotes are mapped graphically; Advanced Rules and manual
overrides take precedence by an explicit, visible priority; and when an override or a rule ends, the light returns
to the **currently** calculated state by itself. Everything stays native Home Assistant automations.

- **Truth Matrices** (ON / OFF / NO ACTION / USE DEFAULT / UNSET) and **Brightness Matrices** (0-100 % / NO ACTION /
  USE DEFAULT / UNSET), several of each, combined explicitly. Columns of any kind: presence situations (rooms,
  zones, position zones, persons), number ranges (light level with a delay), an entity's state (switches, mood
  selectors, media players …), times of day, *Always*. Columns that can hold together need an explicit policy
  (a ranking you set, ON/OFF wins, highest/lowest, nothing while they disagree) - never the table order.
- **Truth gating**: brightness and colour never switch a light on and never override an OFF; **0 % is never
  silently an OFF** (its meaning is a required choice).
- **Colour & Mood**: colour temperature or colour, brightness, effect and transition per light and mood; each field
  empty = unchanged.
- **Priority & Resolution**: per property (on/off, brightness, colour, effect, transition), layers in a
  configurable order (default: Advanced Rules, switch overrides, mood, brightness, truth); the page shows who
  controls each light now and what its automation would send.
- **Switches & Remotes**: room → device → entity or event; device triggers (ZHA, Zigbee2MQTT, Hue …), event
  entities and latched switches, discovered from Home Assistant, plus manual ZHA / MQTT / event mappings. Actions:
  toggle, on, off, set or change brightness, apply a mood, override only, resume, run a one-shot Advanced Rule,
  dim while held, light action with data. One-shot by default; optionally a **manual override** that lasts until
  resumed, a time, a room/zone vacancy or an event, optionally also started by a change by hand (Home Assistant's
  context, not every state change).
- **Override helpers**: the override state lives in Home Assistant helpers that the App creates at deployment and
  owns (`input_boolean.ab_<device>_<light>_override`, and `timer.…` for a timed override, restored across
  restarts). Listed in the review, recorded, deleted when no longer needed or with the device; helpers the App did
  not create are never touched. New allowlisted websocket commands: `input_boolean/{list,create,delete}`,
  `timer/{list,create,delete}`, limited to these names.
- **Advanced Rules**: “while …” rules (conditional overrides that release by themselves) and “when …” one-shot
  rules, in an explicit order; the generic IF / AND-OR / THEN editor stays available for ordinary rules.
- **Native form**: one automation per light (mode queued): `choose` branches in the explicit order set variables,
  templates resolve the properties, and a single `light.turn_on` / `light.turn_off` sends exactly the resolved
  attributes - only when the light differs, never while it is unavailable. Switch mappings live in the device's
  shared automation; hold-to-dim gets an automation of its own (it waits).
- **Simulation** with the compiler's own plan (the same branches and conditions): matrices, mood, rules, override
  and its end, the winning layer of each property, the command; a press is an event, kept apart from the states.
- **Version 1 designers** (0.3) are kept and compile byte for byte as before; their page offers an explicit,
  lossless **conversion** (with what changes), and refuses the parts that would need a guess.
- Gallery: new lighting templates (Presence, Light level, Brightness by situation, Colour & mood, Wall switch or
  remote). New designers are version 2.

## 0.3.0

Specialised graphical designers on the same automation model. A designer is a different way to write the rules of
a device: what it configures becomes ordinary rules of that device, compiled into its one native automation, which
needs nothing of this App to run. Devices without a designer generate exactly the automation they did before.

- **Lighting Designer** (lights): a mood matrix (lights × the moods you choose), a presence matrix (lights ×
  rooms, named zones, position zones, persons) and a light-level matrix (lights × bands you name and limit), a
  schedule (time windows and timed entries), manual override (a signal, and a pause after a change by hand) and
  occupancy rechecks where they are safe. Exactly one matrix decides; the mood is the palette for cells that say
  “Use mood”; light level and presence can permit (“allow ON only when dark”) without ever sending a command by
  themselves. Capability-aware: brightness, colour temperature, colours and effects only where a light has them.
- **Cover Designer** (covers): sun shading per window with 0° = South (converted to Home Assistant's 0° = North),
  sector, elevation range, a fixed position or one that follows the sun's elevation or profile angle, hysteresis,
  minimum change, minimum interval, light-level / temperature / weather conditions, an explicit fallback for
  unavailable data, and weather protection for awnings. Cover modes you name (a matrix of covers × modes),
  driven by a selector or by an optional mapping from household moods, and per mode an explicit choice whether
  the mode or the sun shading wins. Tilt, positions and open/close only where a cover supports them.
- **Presence sources** (new page): rooms (with “possibly occupied” where a source has it), named zones with their
  own entities, position zones drawn on a sensor's coordinates (a native template tests the targets against the
  shape), and persons at geographic zones. Entry and exit delays, “fresh: false” honoured on request, several
  zones occupied at once, and an explicit policy when situations overlap (priority, nothing while ambiguous, or
  both when compatible - different commands are then a conflict to resolve).
- **Strict unset semantics**: Unset, Use default, No Action, Off/Closed and explicit values are different things,
  stored and shown differently (“40% · 2700K”, “OFF”, “DEFAULT: 30%”, “NO ACTION”, “UNSET”). Defaults are optional
  (per light or cover, per matrix) and never created for you; a missing source blocks deployment with the reason.
- **Matrix editing**: select cells, rows or columns; copy and paste; edit many cells at once; set back to Unset.
- **Templates**: Mood, Presence, Lux-Adaptive, Combined, Zone-Specific Lighting; Sun-Angle Shading, Privacy
  Presets, Cover Mood Matrix, Mood-Linked and Scheduled Covers. A template turns controls on and lists what it
  needs; it chooses no entity and no value. Adding a light or a cover opens the gallery.
- **Simulation**: choose what just happened and the states (live values or your test values); see which rules
  run, which conditions fail, what each light or cover receives, conflicts and missing inputs. Nothing is sent.
- **Blueprints**: designers export like any rule: their entities, sources, thresholds, delays, sun angles, cover
  positions and cell values can be inputs (one input per setting, wherever it is used).
- Conflict check: rules that exclude each other by construction (two moods of one selector, a zone and its
  negation) are recognised as such.

## 0.2.1

Room-first usability. The automations and how rules are combined are unchanged, except for the optional default.

- **Add a device in three steps: room → device → entities.** Rooms are Home Assistant's areas (plus
  "Unassigned"); only the devices of the chosen room are listed, then only that device's controllable
  entities. Nothing is ticked for you; going back keeps what you ticked, so entities of several devices or rooms
  can still be combined on purpose. After "Add device" an empty rule opens straight away.
- **Overview by room.** Expandable groups with the number of devices and rules, each device with its status,
  and "+ Add device" that starts in that room. A device whose entities are in several rooms is listed once,
  under "Multiple rooms"; it keeps its one automation. Rooms only organise the page.
- **"Only show entities from this room"** in the rule editor's entity pickers, on by default when the device
  belongs to exactly one room. It can be unticked at any time; rules across rooms work as before, and the
  entity that is chosen now is always listed.
- **Default rule (optional).** One per device, below its rules: actions that run when one of the device's rules
  was triggered and none of the rules matched. It has no trigger of its own, never runs periodically or in
  addition to a rule that matched, can be disabled, and can be told to do nothing while a manual override
  signal is on. It compiles into the device's own native automation. A device without a default generates
  exactly the automation it did before.
- Friendly names first, entity ids as secondary text; icons for rooms, devices and kinds of entities.

## 0.2.0

Blueprint export, more building blocks, import of existing automations and rule-level diagnostics. Validated on
Home Assistant OS and with the entities of the real Smart Occupancy Detection App.

- **Blueprint export.** “Export as blueprint” on a device turns one of its generated automations into a native
  Home Assistant automation blueprint. You choose which values become inputs (controlled entities, trigger and
  sensor entities, thresholds, times, durations, brightness, colour temperature, other action parameters); each
  gets a readable name, a description, a default and a selector, and a stable identifier. Download the YAML, or
  install it into `blueprints/automation/automation_builder/` (only when you press Install). The managed device
  and its automation are not changed, and the blueprint needs nothing of this App to run.
- **Triggers.** Buttons and remotes that Home Assistant shows as event entities (single, double, long press …
  as the device reports them), device triggers discovered from Home Assistant (ZHA, Zigbee2MQTT over MQTT
  discovery and others), events entered by hand (for example `zha_event`), MQTT messages entered by hand,
  templates, helper changes, sun elevation, time patterns, a date/time helper as the time, Home Assistant
  start and shutdown, attributes of an entity.
- **Conditions.** Templates, “which trigger started the rule”, device conditions, sun, “none of” groups next
  to “all of” and “any of”, attributes.
- **Actions.** If / then / else, choose between branches, wait for a trigger, wait until a condition holds
  (both with a timeout that continues or stops), repeat (count, while, until), variables, templates in action
  data, notifications, scripts (run, or run and wait), scenes, device actions.
- **Consolidation.** Rules whose actions finish at once still share one automation per device, always in mode
  `queued`. A rule that waits now always gets its own automation in the mode chosen for it, with the reason
  shown. **Changed:** in 0.1 a waiting rule with “run both at the same time” stayed in the shared automation
  and switched it to `parallel`; such a device shows “changes not deployed yet” after the update.
- **Import.** Browse the automations that exist in Home Assistant, see what one consists of and whether it can
  be represented faithfully, and copy it into a new device (or, if you choose so, an existing one). Anything
  that cannot be represented is named and blocks the conversion; nothing is dropped silently. The original is
  never changed; while it is still turned on, deploying its copy is refused unless you allow both.
- **Diagnostics per rule.** Last triggered, last executed, last blocked (and by which condition), the outcome,
  a link to the trace in Home Assistant, the availability of the rule's entities and whether the rule is
  deployed as it is now. Where it is reliable, the state an entity reports a few seconds after a rule's action
  is compared with what the rule commanded (“reported state matches / differs”). Nothing is corrected from
  there.
- **Interrupted deployments.** If the App or Home Assistant stops in the middle of a deployment, the next
  start checks what reached Home Assistant and completes its records accordingly.
- **Rules from a dropdown helper.** A shortcut that creates one ordinary rule per chosen value of an
  `input_select` (for example moods), for the one device you are on.
- Buttons: the first event a button ever reports counts; becoming available again does not.

## 0.1.0

The first version: an independent App with a visual rule editor and a compiler to native Home Assistant automations.

- Device-first: Overview, Devices, Rule Editor, Settings. A managed device is one or more entities you select
  (lights, switches, relays, fans, covers, climate, locks, valves, helpers and more).
- Visual IF → AND/OR → THEN rules: state, numeric threshold, time, sun and occupancy triggers; state, numeric and
  time conditions in AND/OR groups; ready-made actions per domain, hand-entered actions, delays. Readable summary
  and generated YAML for every rule.
- All rules of a device are compiled into one native automation (stable ids, deterministic output). Contradictory
  rules are reported and block deployment until resolved or explicitly confirmed. A rule that needs another
  execution mode gets its own automation, with the reason.
- Optional occupancy triggers with generated rechecks (2 s, 15 s and every 5 min by default; configurable) that
  re-evaluate the current occupancy and conditions, never repeat toggles, never infer an "off", respect manual
  changes and stale or unavailable occupancy.
- Safe publishing: validation by Home Assistant, review with differences, explicit deployment, confirmation,
  automatic undo of a failed deployment, detection of changes made outside the App, restore of earlier versions.
- Diagnostics per device: automation identity, status, last synchronisation, conflicts, missing entities,
  differences, last runs with links to Home Assistant's traces.
- Versioned configuration with automatic backups, export, import and restore.
