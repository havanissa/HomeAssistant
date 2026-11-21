# Smart Occupancy Detection (companion integration)

Native Home Assistant entities for the **Smart Occupancy Detection** App, which decides room by room whether a room
is occupied from the sensors you already have (motion, mmWave presence, doors, PC activity, bed sensors).

This integration only shows the App's results. It contains no detection engine, registers no service and cannot
control any device. It accepts results only from the App it is paired with.

## Installation

1. Install the **Smart Occupancy Detection** App (Settings → Apps → App store → ⋮ → Repositories → add
   `https://github.com/havanissa/HomeAssistant`, then install *Smart Occupancy Detection*).
2. Install this integration with HACS: HACS → ⋮ → **Custom repositories** → add
   `https://github.com/havanissa/smart-occupancy-detection`, type **Integration** → open *Smart Occupancy Detection* →
   **Download** → restart Home Assistant.
3. Settings → Devices & services → **Add integration** → *Smart Occupancy Detection* (nothing to fill in).
4. In the App: Settings → **Pair with Home Assistant**.
5. Check: `binary_sensor.sod_app_connected` is on, and every room has `binary_sensor.sod_<room>_occupied`.

Updates come through HACS like any other integration. Pairing, rooms and entity ids are kept across updates,
restarts and reloads.

## Entities (per room)

| Entity | |
|---|---|
| `binary_sensor.sod_<room>_occupied` | occupied / vacant: the primary output for automations |
| `sensor.sod_<room>_state` | vacant, possible_occupancy, confirmed_occupancy, possible_vacancy, uncertain |
| `sensor.sod_<room>_confidence` | evidence quality in % |
| `sensor.sod_<room>_reason` | why, in words |
| `sensor.sod_<room>_last_activity` | last activity in the room |
| `binary_sensor.sod_app_connected` | the App is connected and its results are fresh |

When the App is away the entities keep their last decision (an absent App never makes a room vacant) and report
`fresh: false`.

## Compatibility

| Integration | App | Protocol |
|---|---|---|
| 0.1.x | 0.1.x – 0.2.x | 1 |
| 0.2.x | 0.1.x – 0.2.x | 1 |

The App checks the integration's version and protocol and says exactly what to update when they do not match.

## Source

Developed in [havanissa/HomeAssistant](https://github.com/havanissa/HomeAssistant)
(`custom_components/smart_occupancy_detection`); this repository is published from there by a script for HACS.
Please report issues here; changes are made in the source repository.

License: MIT.
