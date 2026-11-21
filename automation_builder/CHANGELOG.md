# Changelog

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
