# Smart Occupancy Detection 0.1 (Phase 0.1)

A standalone Home Assistant App that only detects room occupancy, plus a minimal companion integration for its
native entities. Smart Comfort (App `smart_lighting`, integration `smart_occupancy`) is not changed and keeps
running beside it.

| Part | Where | Version |
|---|---|---|
| App (slug `smart_occupancy_detection`, panel "Occupancy") | `smart_occupancy_detection/` | 0.1.0 |
| Engine copy | `smart_occupancy_detection/backend/sod_engine/` | 1.0.0 (from `occupancy_core` 1.0.0) |
| Companion integration (domain `smart_occupancy_detection`) | `custom_components/smart_occupancy_detection/` | 0.1.0 |

## 1. Audit: what was reused

| Existing component | Decision |
|---|---|
| `smart_lighting/backend/occupancy_core/engine.py`, `types.py`, `house.py`, `golden.py` | **Copied** into `sod_engine` (never imported at runtime). Removed: preliminary signals (possible occupancy lighting), zone holds (`external_hold`, lighting zones) and the bridge's state transfer (`export_state`, `import_state`, `adopt`, `decision`). These code paths were no-ops when unused, so no rule changed. Kept: `note_activity_evidence`, now fed by user-chosen **activity entities** instead of Smart Comfort's wall-switch controller. Added (no rule change): `House.hold_all` (restart semantics after a connection gap) and `House.rebuild` (live configuration change that keeps the engines of rooms whose signals did not change). |
| `occupancy_core/golden/*.json` | 18 of 20 scenarios copied **byte-identically**; `10_preliminary_lighting` and `12_zone_holds` are lighting-only. Run at start-up as a self-test; nothing is published if it fails. |
| `occupancy_core/wire.py`, `protocol.py` | Not reused: they serve the integration ⇄ App occupancy bridge (ownership, handover), which this App does not have. |
| `smart_lighting/occupancy_runtime.py` | Not copied (bridge, shadow comparison, handover). Its adapter rules were re-implemented in `sod/runtime.py`: `state_changed` at `last_changed`, one timer per room at `next_deadline`, restore with the start-up hold, transitions saved coalesced. |
| `smart_lighting/ha.py` | Re-implemented smaller (`sod/ha.py`): same reconnect, start-up resync and ping handling; an allowlist with reads, event subscriptions and the companion's protocol only. |
| `smart_occupancy/presence_api.py`, `presence_fields.py`, `presence_log.py`, `occupancy.py` | Ideas reused, code not: the kind → domain table (`presence_fields.KIND_FIELDS`) is in `sod/config.py`; the "predict before saving" review (deep copy of the engine) is `OccupancyRuntime.predict`; the bounded presence log became the persisted timeline (`sod/timeline.py`). |
| `smart_occupancy/discovery.py` | Its device-class → kind suggestions are in `sod/discovery.py` (from Home Assistant's states and registries over the websocket). Occupancy outputs of either system are never suggested and are refused as sensors. |
| `custom_components/smart_occupancy/binary_sensor.py`, `sensor.py`, `entity.py` | Same entity set per room (occupied, state, confidence, reason, last activity) and the same "keep the last value while the App is away" policy; new domain, unique ids and entity ids. |
| `smart_occupancy/forecast*`, lighting, moods, curtains, switches, climate, ownership/lease framework for several domains, executor | Not used. |

## 2. Architecture

```
Home Assistant ──state_changed / registries──▶ App: sod/ha.py (allowlist) ─▶ runtime.py ─▶ sod_engine (House)
       ▲                                                   │                        │
       │ native entities                                   │ view changes            │ decisions, timeline
companion integration ◀── session / pair / claim / publish ─┘ publisher.py          ▼
 (hub.py: fence, views, store)                                               /data/state, /data/config
```

* **Detection**: event driven, no polling. While the Home Assistant connection is down the timers are frozen; when it
  is back every room gets the restart semantics (`hold_all`) and every sensor is read again.
* **Configuration**: `/data/config/config.json`, schema 1, SHA-256 revision, strict validation (an invalid document
  is refused with every problem listed), atomic writes, `config.json.bak` plus 30 timestamped backups, recovery
  that never uses or overwrites a damaged file, export and reviewed import, reviewed read-only Smart Comfort import.
* **Decisions**: `/data/state/occupancy.json` (written 2 s after a transition, at shutdown, `.bak` kept); timeline
  `/data/state/timeline.json` (1000 entries, transitions, sensor health, configuration and connection events only).
* **Interface**: Overview, Rooms, Settings (vanilla JS, no build step, strict CSP, Ingress only, administrator and
  `X-SOD` header for every change, review with "changes immediately / stays" before every save).

## 3. Publishing protocol (`protocol.py`, byte-identical in App and companion)

One domain only: occupancy reporting.

1. `smart_occupancy_detection/session` (subscription; admin) opens a session bound to the websocket connection; the
   companion answers `hello` (paired, trusted).
2. `pair` (an administrator presses *Pair* in the App): a random token, only its SHA-256 is stored; pairing again
   needs `replace` and fences every other session. The token is kept in `/data/companion.json` (mode 0600).
3. `claim` (trusted session): epoch + 1 (persisted, survives restarts), random lease token, sequence 0. A valid lease
   of another session is never taken (`busy`).
4. `publish` / `heartbeat`: session on the same connection, current epoch, lease token, valid lease (30 s), strictly
   increasing sequence. `publish` carries the **complete** validated snapshot (rooms and disabled rooms); an invalid
   snapshot is refused before the sequence moves.
5. Lost codes (`not_owner`, `stale_epoch`, `lease_invalid`, `unknown_session`, `sequence`) make the App claim again;
   nothing is replayed.

There is no executor, no service and no command that reaches a device. Freshness: the entities keep the last
accepted view when the lease expires, the session closes or Home Assistant restarts (`fresh: false`,
`binary_sensor.sod_app_connected` off); an absent App never makes a room vacant.

Entity identity: unique id `<area_id>_<key>`, one device per room (identifier = area id, never placed in the area
automatically so it does not appear in other integrations' area discovery), entity id suggested once as
`<platform>.sod_<room slug>_<key>`. Renaming the area renames the device and friendly names only. Disabled rooms keep
their entities, unavailable. Only a room an administrator deleted in the App (sent as `deleted` with the next
snapshot) loses its entities; any room that is merely missing from a snapshot (a reset App, an error, an import)
keeps them, unavailable, until it is configured again or its device is deleted in Home Assistant. Views count as
fresh only while the lease is valid and they were accepted in the current epoch. Room device identifiers are
`room_<area_id>` (no area id can collide with the App device `app`).

## 4. Future handover procedure (not executed in 0.1)

1. Run both systems side by side; compare `binary_sensor.sod_<room>_occupied` with Smart Comfort's room occupancy in
   the Recorder for several days.
2. Move automations to the `sod_` entities one at a time. If existing entity ids must be kept, first rename Smart
   Comfort's entity in Home Assistant, then give the `sod_` entity the freed id - never two entities with one id, and
   never by changing unique ids.
3. When nothing uses Smart Comfort's occupancy any more, remove the room from Smart Comfort (or Smart Comfort
   entirely). Smart Occupancy Detection does not depend on it.

## 5. Tests

| Suite | Command | Result |
|---|---|---|
| App unit tests (engine parity incl. 400 + 150 differential seeds, runtime, config, import, publisher, web, package) | `cd smart_occupancy_detection && pytest` | 628 passed |
| Companion in a real Home Assistant 2026.11.1 (security, fencing, entities, restart, reload, rename, deletion, orphans, coexistence with `smart_occupancy`, the App's real link end to end) | `python scripts/run_tests.py tests/sod_companion` | 35 passed |
| End to end: real App image, disposable HA 2026.11.1, Smart Comfort App and companion running beside it (fresh install, pairing, discovery, detection, outages, App and HA restarts, rename, disable, delete, no service call from the App's user) | `tests/e2e/stack.sh up` + `e2e_checks.py` | 51/51 |
| Browser (Playwright through Ingress; desktop, tablet, phone; light and dark; add-room flow) | `tests/e2e/browser_checks.py` | 49/49 |
| Smart Comfort regression (unchanged runtime) | its App suite, `tests/companion`, `tests/engine` | all pass |

The differential test compares every field of every evaluation (state, occupied, reason, code, confidence, quiet and
vacancy times, signal statuses, next deadline) between this engine and Smart Comfort's on randomised houses (shared
sensors, adjacency, all kinds and roles, inversion, exclusion, unknown states, late events, activity, restarts, live
reconfiguration): 21 000+ evaluations, every reason code reached; an injected rule change is caught.

## 6. Independent review

Three independent reviews (publishing bridge and companion; App runtime, configuration and engine parity; web
interface) ran before merging. No security problem and no engine rule change was found. Fixed:

* blocking: the App did not recover when the companion was reloaded or closed its session (now it opens a new
  session and claims again);
* important: a configuration saved while Home Assistant was disconnected was evaluated on old knowledge (saves are
  refused while disconnected, and a suspended runtime never evaluates); a room missing from a snapshot deleted its
  entities (now only explicit deletions do); a claim counted as fresh before its first snapshot; a claim timeout
  was not retried; a deleted orphan device could not get entities again; a late delayed save could bring the
  companion's storage back after removal; polling closed the interface's open sections; an unknown binary sensor
  defaulted to a motion trigger; asynchronous results could draw over another page; a stale revision could not be
  recovered in the review;
* minor: overlapping state writes, timeline entries lost during a write, unvalidated stored records, activity
  entities returning from unavailable counted as activity, Smart Comfort import defaults (now read exactly as
  Smart Comfort reads them), recovered configuration backed up, area id `app`, Recorder rows on the primary entity,
  UTC timestamps in notes, labels and touch targets.

Accepted: an administrator can pair another App instance (administrators are trusted in Home Assistant); some
configuration file operations run on the event loop (small files, administrator actions only).

## 7. Known limitations

* The companion is installed by copying its folder (HACS installs one integration per repository; this repository's
  HACS entry is Smart Comfort's integration). A separate repository would make it installable through HACS.
* A room is always a Home Assistant area; rooms without an area are not supported.
* Room devices are not assigned to their area automatically (an occupancy entity in the area would be offered as a
  sensor by Smart Comfort's room discovery). They can be assigned by hand.
* Not verified on Home Assistant OS hardware; verified with the real image, a simulated Supervisor and a real
  Home Assistant 2026.11.1.
* No occupancy prediction, spatial zones or lighting: out of scope for 0.1 by design.
