# Changelog

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
