# Changelog

## 0.2.0

Distribution, compatibility, diagnostics and pilot readiness. The detection rules are unchanged.

- The companion integration has its own HACS repository: https://github.com/havanissa/smart-occupancy-detection
  (HACS → Custom repositories → Integration). Companion 0.2.0.
- Settings → Connection shows the App, integration and engine versions, checks that they are compatible (with what to
  update when they are not), the last successful communication, whether the data in Home Assistant is fresh (also as
  the integration sees it) and the recent connection errors.
- Room pages show why a room is held (start-up, reconnect, changed sensors) and each sensor's data age.
- Changes are saved as you make them (no separate review step); deleting a room, imports and restores still ask first.
- When the integration was removed and added again, the App pairs again by itself (not after an explicit unpair).
- Pilot comparison: compare a room with Smart Comfort's occupancy entity and log every difference longer than a minute
  with timestamps, sensor evidence and an explanation; download the log as CSV or JSON. Observation only.
- Diagnostics download is redacted (secrets, user ids, addresses, people).

## 0.1.0

First release of Smart Occupancy Detection, a standalone App that only detects room occupancy.

- Its own copy of the proven occupancy engine (Smart Comfort's `occupancy_core` 1.0.0, whose rules are those of
  the Smart Occupancy & Lighting integration 0.6.10): PIR, mmWave radar, door, PC activity and bed sensors,
  roles, inversion, correlation, possible and confirmed occupancy, fast and normal vacancy, radar dropout
  grace, stuck sensors, adjacent-room movement, conservative handling of unavailable sensors, start-up hold.
  Proven identical to Smart Comfort's engine by the golden scenarios and a differential test.
- Optional activity entities (wall switches, remotes, buttons) that keep an occupied room occupied.
- Event driven (Home Assistant state changes and per-room deadline timers, no polling). While the connection to
  Home Assistant is down, decisions are frozen; afterwards every room gets the start-up hold.
- Native entities per room through the minimal companion integration `smart_occupancy_detection`:
  occupancy (binary sensor), detailed state, confidence, reason and last activity, plus an App connection sensor.
  Results are accepted only from the paired App session that holds the current lease.
- Overview, Rooms and Settings: sensor discovery with suggested roles, reviewed changes that say whether a room's
  decision would change at once, a bounded detection timeline, diagnostics.
- Own versioned configuration with revision checks, atomic writes, automatic backups, recovery from a damaged
  file, export and reviewed import, and a reviewed, read-only import from a Smart Comfort export.
- Never controls a device. Smart Comfort is not changed and keeps running beside it.
