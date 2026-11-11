# Changelog

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
