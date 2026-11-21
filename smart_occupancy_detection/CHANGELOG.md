# Changelog

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
