# Phase A6: the App becomes the product, the integration becomes infrastructure (audit, design, operations)

Baseline: integration 0.9.0, App 0.6.0 (main `536b40c`). Result: **integration 1.0.0** (the
*companion*: native entities, a fenced executor, ownership records, events, legacy export
and retirement) and **App 0.7.0** (every engine, every configuration, the migration wizard,
the setup for new homes, the dashboard). This document records the dependency audit, the
architecture that replaced the hot-standby design of A3.2–A5, the safety policy while the
App is away, the migration and rollback procedures, how it was verified, and what is left.

The principle changed in one place. Until 0.9.0 the integration held a complete copy of every
engine and took a subsystem back when the App stopped. That hot standby is gone: **the App is
the only implementation**, the companion **runs no automation of its own**, and an App outage
means *no automatic decisions until the App is back* (a reduced capability that is reported,
never a hidden fallback). Everything else of the earlier phases stays: one authority (the
companion's record, never the App's wish), nothing applied at a change of owner, fenced and
allowlisted device commands with the App's context, every way back explicit.

## 1. Dependency audit (integration 0.9.0 → companion 1.0.0)

| # | Capability of 0.9.0 | Decision | Where it lives now |
|---|---|---|---|
| 1 | Config entry **options** (rooms, sensors, lights, light types, mood source, occupancy timing; `IntegrationConfig` v2) | **Move to App** | `CoreConfigStore` (`/data/config/core.json`, schema 1): revision-checked saves, atomic writes with `.bak`, 20 backups, export / reviewed import, renames, stale detection against the file. The config entry keeps its identity (domain `smart_occupancy`, entry id, title); the companion stores only `{"companion": …}` in the options after retirement. |
| 2 | Config flow and **options flow** (*Configure → Rooms / Light categories / Mood …*) | **Retire** | The companion's config flow adds one instance and has no options; every setting is edited in the App. |
| 3 | **Occupancy engine** (`occupancy_core`, `occupancy_manager.py`, engine bridge) | **Move to App** | `occupancy_runtime.py` runs the engine from the core configuration (`companion_start_owner`); the companion publishes the room views on the native entities. No engine code in `custom_components`. |
| 4 | **Lighting** (controller, brightness, daylight, ambient, switches, moods, supervision; lighting bridge) | **Move to App** | `lighting_runtime.py` in companion mode: its controller commands lights only through `smart_occupancy/companion/execute` (allowlist: `light`, `switch`, `scene`, `script`, `select`, `input_select`; published targets only). |
| 5 | **Curtains** (`curtains/` package; curtain bridge) | **Move to App** | `curtains_runtime.py` in companion mode: `cover.set_cover_position` (0–100), `open_cover`, `close_cover`, one configured cover per command, through the executor. |
| 6 | **Forecast** (`forecast*.py`, Recorder history) | **Move to App / Replace with HA API** | `forecast_runtime.py` learns from `history/history_during_period` over the App's own websocket; nothing commands a device. |
| 7 | **Native entities** (`sensor`, `binary_sensor`, `select`, `button`; unique ids `smart_occupancy_room_<area>_<key>`, `smart_occupancy_<key>`) | **Keep in Companion** | `entity.py`, `sensor.py`, `binary_sensor.py`, `select.py`, `button.py`: fed by the published views (`{"value","attributes","available"}` per entity), same unique ids, same attributes, `fresh` attribute false while the App is away. Room entities are created and removed when the published system view changes. |
| 8 | **Built-in mood selector** (`select.smart_lighting_mood`) | **Keep in Companion** | `BuiltinMoodSelect` (restored over restarts); the App reads its state like any other entity. Options come from the published system view. |
| 9 | Websocket **editor APIs** (`rooms_api`, `presence_api`, `lighting_websocket`, `brightness_websocket`, `curtains/websocket`, `forecast_websocket`) | **Move to App** | The App runs the same handlers locally against its own configuration (`CoreHost.call()`, the runtimes' APIs). The companion offers none of them. |
| 10 | **Dashboard cards** (Room Status, Smart Lighting Profiles, Smart Room Lighting, Smart Curtains) | **Keep in Companion (compact) / Replace** | `frontend/smart-occupancy-cards.js`: four compact cards (House, Smart Lighting, Smart Room Lighting, Smart Curtains) under the old element names; data from `smart_occupancy/companion/view`, actions relayed through `smart_occupancy/companion/action` to the App's validated handlers. The Room Status card is unchanged. |
| 11 | **Diagnostics** download | **Keep in Companion** | `diagnostics.py`: the companion's records, sessions, views summary, legacy status. The App has its own diagnostics and dashboard. |
| 12 | **Services** (`engine_return_to_integration`, `lighting_return_to_integration`, `curtains_return_to_integration`, `forecast_return_to_integration`) | **Retire / Replace** | One administrator service: `smart_occupancy.revoke_app_control` (optional `domain`). Nothing to return control *to*: a revoke stops the App's control until it claims again. |
| 13 | **Storage** (`.storage/smart_occupancy.*`: lighting, curtains, forecast, overrides, ownership records, mirrors, presence log, ambient, …) | **Move to App** | Every engine's store lives under the App's `/data`. The companion exports the 15 legacy stores (`legacy/export`), retires them to one backup store (`legacy/retire`) and can restore them (`legacy/restore`). The companion's own record is `.storage/smart_occupancy.companion`. |
| 14 | **Registries, discovery, renames, areas** | **Replace with HA API** | The App subscribes to the registries itself. The companion relays entity renames (also for an App that is away) and area changes as session events; the App rewrites every configuration and confirms with `renames_applied`. |
| 15 | **Translations and strings** | **Keep in Companion** | Entity names and the service in `strings.json` / `translations/en.json`; the App's interface has its own texts. |
| 16 | **Problem tracker, decision logs, presence log, repairs-like notices** | **Move to App** | `problems.py`, `presence_log.py`, `decisions.py` run in the App (`CoreHost` builds the diagnostics snapshot; the system view carries the status, unavailable and missing entities, configuration problems to the companion's global sensors). |

Also retired: `scripts/sync_lighting_runtime.py`, `scripts/sync_occupancy_core.py` and the
byte-identical-copy tests (there is one copy now). One constraint remains for the duration of
the migration: the modules `runtime_manifest.py` lists (the 0.9.0 runtime) stay **byte-identical
to 0.9.0**, because its bridges check the runtime fingerprint before a handover (wizard step 5).
`app_manifest.py` lists the App-only additions (`snapshot.py`, `entity_ids.py`, `rooms_api.py`,
`presence_api.py`, `presence_fields.py`, …) and pins `FINGERPRINT_0_9_0`; a test fails when a
listed module changes. Kept as a test harness only:
`tests/engine/custom_components/smart_occupancy/` holds the frozen 0.9.0 glue so the 1 199
engine tests keep running against a real Home Assistant; the harness `__init__` appends the
App's engine package to its `__path__`.

## 2. Architecture

```
Home Assistant                                   Smart Lighting App (container)
┌───────────────────────────────────┐             ┌──────────────────────────────────────┐
│ companion 1.0.0 (smart_occupancy) │ websocket   │ CompanionClient  ──┐                 │
│  • Companion: records per domain  │◄───────────►│ CompanionMode ×4   │ claims, leases   │
│    (owner, epoch, lease, seq,     │  one session│  occupancy │ lighting │ curtains │ fc │
│     targets, views, history)      │             │ CoreConfigStore (core.json, backups) │
│  • fenced executor (allowlist)    │             │ CoreHost (snapshot, system view,     │
│  • native entities ← views        │             │   rooms/presence handlers locally)   │
│  • compact cards ← view, → action │             │ MigrationService (8 steps)           │
│  • legacy export / retire / restore│            │ OnboardingService (fresh install)    │
│  • pairing (token hash)           │             │ Dashboard, Settings → System / Setup │
└───────────────────────────────────┘             └──────────────────────────────────────┘
```

### 2.1 The companion protocol (`protocol.py` = `companion_protocol.py`, byte-identical)

One module, shipped on both sides and checked by both test suites and
`scripts/check_protocol.py`. Protocol version 1. Domains `occupancy`, `lighting`, `curtains`,
`forecast`; command domains `lighting` and `curtains`.

- **Session.** `smart_occupancy/companion/subscribe` opens a session (`hello`: companion
  version, protocol, boot id, pairing state, ownership records, pending renames, the entity
  map, rooms, legacy summary). Events: `ownership`, `pairing`, `renames`, `areas`, `action`,
  `recheck`. At most 4 sessions.
- **Pairing.** An administrator pairs from the App: the companion issues a 32-hex token, stores
  only its SHA-256, and the App keeps the token in `/data/companion/pairing.json` (mode 600).
  A session is *trusted* when its token hashes to the stored value. Only trusted sessions may
  claim; `unpair` revokes every lease.
- **Claim.** `claim(domain, expected_epoch, config_revision, targets)`: compare-and-swap on the
  epoch, refused while another live session holds a valid lease, targets validated (`lights`,
  `relays`, `actions` for lighting; `covers` for curtains) and required for command domains.
  Grants `epoch + 1`, a lease token (30 s), heartbeat every 10 s.
- **Owner commands** carry `session_id`, `domain`, `epoch`, `lease_token`, strictly increasing
  `seq`: `heartbeat`, `publish` (the view of the domain's entities, optional new targets and
  configuration revision), `execute`, `release`. A wrong epoch, an expired lease or a repeated
  sequence number is refused (`stale_epoch`, `lease_invalid`, `sequence`) and counted.
- **Execute** (`check_command`): service domain and service from the domain's allowlist, the
  `data` keys allowed for that service (`entity_id` plus `brightness`, `brightness_pct`,
  `color_temp_kelvin`, `transition`, … for lights; `position` 0–100 for covers), every target
  in the published targets, no `area_id` / `device_id` / templates, one cover per command.
  Runs `hass.services.async_call(..., blocking=True, context=Context(id=<the App's id>))` with
  a 20 s timeout; the result carries the sequence number and the renewed lease.
- **System view** (`publish` with domain `system`): rooms (names, reference entities), the
  global sensors, every room's observation sensor, the mood mismatch and the mood selector's
  options. Published only by a trusted session that owns a domain.
- **Actions** (cards → App): `room/resume`, `room/mode`, `mood/select`, `curtains/apply`,
  `curtains/resume`, `curtains/mode`; administrators only; relayed to the owner session,
  answered with `action_result` within 20 s or refused (`not_owner` when no App session owns
  anything).
- **Legacy**: `legacy/status`, `legacy/export`, `legacy/retire` (requires `confirm` and the
  expected options revision; writes one backup store, then removes the 15 stores and the
  options), `legacy/restore`. `revoke(domain?)` for administrators and the service.

### 2.2 Ownership states and the native entities

A record's `state` is `none` (nobody), `app` (the owner's session is connected and its lease is
valid) or `app_away` (the App owns the domain but is not connected or its lease expired). Leases
never survive a Home Assistant restart: on start every record is `app_away` with the reason
`home_assistant_restarted`, nothing is executed until a fresh session claims (new epoch).

The entities show the last published view and the attribute `fresh` (true only in state
`app`). An entity whose key is absent from the view is unavailable. Room entities follow the
published rooms; a renamed entity id (in Home Assistant) is relayed to the App.

### 2.3 Configuration authority

`config_authority` is `integration` (0.9.0 still holds the options), `staged` (the App has a copy
that does not count yet) or `app`. The runtimes refuse to claim (`no_config`) until the App is
the authority. Every change in the App goes through `CoreConfigStore.save` (revision check,
backup, atomic write) and is pushed to the running engines (`companion_config_changed`) and to
the companion (`publish` with the new `config_revision`, the system view).

## 3. Safety policy while the App is away

| Situation | What happens | What does not happen |
|---|---|---|
| App stops, crashes or loses the connection | The companion closes the session; every record stays owned (`app_away`), the views stay, entities report `fresh: false`, the cards say "App away: last seen". | No light is switched, no cover moves, no vacancy is inferred, no owner changes. |
| A stale App session sends a command | Refused: `not_owner` (other session), `stale_epoch`, `lease_invalid` or `sequence`; counted in the record. | It never reaches `hass.services`. |
| App reconnects | A fresh session (new instance id) claims each domain with `expected_epoch` = the current one → `epoch + 1`, re-reads Home Assistant's states, starts its engines as after a restart (start-up settle periods apply). | No replay of anything that was in flight; the old lease is never reused. |
| Home Assistant restarts | Records reload as `app_away` with no lease; the App's `reprobe` opens a session and claims. | No command before a claim. |
| Administrator revokes (`smart_occupancy.revoke_app_control`, or the App's own page) | Owner `none`, epoch + 1; the App stops its engine at once (`lose`) and claims again after 3 s unless it was released from the App (then `auto_claim` is off). | Nothing is switched by the revoke. |
| Lease expires (no heartbeat for 30 s) | `app_away`; the App's next command is refused and it reclaims. | – |
| Physical switches | Keep working through their own integrations; the App's mappings resume with the App. | – |

**This is a reduced capability compared with 0.9.0's hot standby**: an App outage means no
automatic lighting, curtain or occupancy decisions until the App is back. Keep the App's
watchdog on (Settings → Apps → Smart Lighting) and check Settings → Migration & System after an
update. The reasons for dropping the standby: two engines meant two copies of every rule, a
fingerprint that had to match at every release, and a fallback whose activation itself was a
risk (a different controller applying a different baseline).

## 4. Migration (Settings → Migration & System)

Eight steps, each started by an administrator, each recorded with its result; **nothing runs on
upgrade**. The wizard is idempotent: every step can be repeated.

1. **Inspect** – versions, mode (`legacy` / `companion` / `none`), owners, revisions, entities,
   unavailable devices, runtime health, backups, blockers. Changes nothing.
2. **Backup** – one JSON file in `/data/migration/backups/` with the core configuration (or the
   staged copy), the lighting / curtain / forecast exports, the floor plan and, when the
   companion is connected, its legacy export. The file is refused if it would contain a
   credential (the Supervisor token or the pairing token).
3. **Validate** – readiness of every subsystem the integration still runs, configuration
   integrity, storage, companion compatibility, targets, one owner per domain, no cover moving.
4. **Transfer config** – review (what changes, which referenced entities do not exist), then
   apply: on 0.9.0 as a *staged* copy (the integration's options still count), with the
   companion as the authority.
5. **Transfer control** – the A3.2 / A4 / A5 handovers on 0.9.0 in the safe order occupancy →
   lighting → curtains → forecast (each refused unless the earlier ones are done).
6. **Activate** – requires the companion (update the integration to 1.0.0 in HACS and restart
   Home Assistant first; the App keeps running meanwhile). Pairs if needed, compares the staged
   revision with the companion's legacy options revision (blocked with a review when they
   differ; *Re-import* applies the companion's copy), promotes the configuration to the
   authority, claims every domain.
7. **Verify** – companion connected and paired, presence decisions for every room, native room
   entities present (live entity map), ownership, switch inputs, overrides kept, curtains,
   forecasts, configuration persisted, restart recovery (reported as not exercised until a
   restart happened).
8. **Retire** – requires a passed verification and an explicit confirmation: the companion
   writes the legacy backup store and removes the 15 stores and the options. The App's backups
   are never deleted.

**Abort** at any point: drops the authority of a staged copy, hands control back to 0.9.0 (its
runtimes' `release`), or with *restore legacy* writes the retired data back.

### 4.1 Production procedure (0.9.0 + App 0.6.0 → 1.0.0 + App 0.7.0)

1. Update the **App** to 0.7.0 (Settings → Apps). The integration 0.9.0 keeps running
   everything; the App reconnects to its bridges as before.
2. Open **Smart Lighting → Settings → Migration & System → Migration wizard** and run steps 1–5.
   After step 5 the App runs every subsystem through the 0.9.0 bridges (as in A5).
3. Update the **integration** to 1.0.0 in HACS and **restart Home Assistant**. The 0.9.0
   bridges disappear; the App's runtimes stop (nothing is switched) and wait for the companion.
   The entities keep their ids (and their registry entries, history and customizations) but are
   *unavailable* until step 4: the companion has no published state yet and invents none.
4. Back in the wizard: **Activate** (pairs, promotes, claims), then **Verify**.
5. Check the dashboard and the cards for a day. Then **Retire**.
6. Optional: remove the retired `.storage` backup later by hand (never done by software).

### 4.2 Rollback

- **Before step 3** (still on 0.9.0): *Abort* returns every subsystem to the integration;
  downgrade the App if wanted.
- **After step 3, before Retire**: downgrade the integration to 0.9.0 in HACS and restart. Its
  stores are still there; it starts as before. The App 0.7.0 reconnects to the 0.9.0 bridges
  and keeps the staged copy (demoted by *Abort*).
- **After Retire**: *Abort → Restore legacy data* (or `smart_occupancy/companion/legacy/restore`),
  then downgrade to 0.9.0 and restart. The companion's backup store is the only copy of the
  retired data: it is never deleted by software.

## 5. Fresh installation (Settings → Setup)

Without legacy data: install the App, (optionally) install and add the companion, pair it,
**Create the configuration** (safe defaults: no rooms, no mood source, nothing automatic), then
rooms, sensors, lighting, moods, switches, curtains, predictions, floor plan, and the explicit
review that switches a room to Automatic. Every room starts Observing. Starting fresh is
refused while the monolithic integration is installed (use the wizard).

## 6. Verification

- `tests/companion` (43 tests, real Home Assistant): setup and entity ids, pairing and sessions,
  claims (epoch, busy, targets), heartbeats and lease expiry, execute (allowlist, targets,
  context, sequence, timeouts), views and entities (freshness, dynamic rooms, renames), the
  legacy export / retire / restore, the cards' API (actions, view subscription, renames,
  recheck), the protocol fences, the compact cards (names, no service calls, parse).
- `tests/engine` (1 199 tests): the engines in a real Home Assistant through the frozen 0.9.0
  harness (unchanged rules; the daylight time-of-day test now runs on a fixed clock).
- `smart_lighting/tests` (244 tests): the runtimes in legacy mode (A3.2–A5 unchanged) and in
  companion mode (`fake_companion.py`): fresh install, pairing, claims, published views, the
  fenced executor, losing the lease, revoke and reclaim, App outage and restart with new
  epochs, release / claim / revoke endpoints, card actions, renames, the configuration store
  (revisions, backups, stale detection on disk, import review, renames, promote / demote), the
  configuration endpoints, the whole wizard from 0.9.0 to the companion and retirement, the
  stale-staged-copy block and re-import, the setup refusal on a legacy installation, the
  protocol copy.
- Frontend (vitest, 61 tests): system and dashboard helpers.
- End-to-end on a throwaway Home Assistant (`smart_lighting/tests/e2e`): see the PR report for
  the three scenarios (legacy migration, fresh install, failure and recovery).

### 6.1 Independent review (before the merge)

A separate review of command safety, entity continuity and data loss found and led to these
fixes: *Activate* re-imports the legacy options only when there is no configuration yet or the
administrator explicitly asks for a re-import (a retry never replaces an edited configuration);
room entities are pruned only after a system view was published; the legacy export reads the
0.9.0 stores without rewriting their version headers; pairing again revokes the previous App's
leases at once; *Start fresh* is refused while the companion still holds legacy data; the legacy
backup store is never replaced by a weaker export; Home Assistant's own data validation errors
are reported as `invalid`; the subscribe command answers once; the audit trail records the
connection's user; an action may only be answered by the session it was sent to; a session that
holds a lease is never evicted; `core.json` is fsynced, its previous version copied (not moved)
and used when the file is missing; the configuration backup routes need an administrator. The
protocol now also range-checks light command values (brightness, colour temperature, colours,
transition).

## 7. Limitations

- **No automation while the App is away** (section 3). The companion cannot and will not take
  over.
- The App needs the Supervisor (Home Assistant OS or Supervised). Without the companion the App
  configures everything but has no native entities and cannot command devices.
- The Room Status card still reads the room state sensor's attributes; its tabs that edited
  settings in 0.9.0 are gone (the App is the editor).
- The `restart` verification check is informational until a Home Assistant restart was seen.
- The companion keeps one legacy backup store; a second retirement overwrites it (there is
  nothing to retire twice without a restore first).
- A5's parity test `test_unchanged_reports_reach_the_host` stays expected-to-fail: the curtain
  comparator's unchanged-report path is only reachable through the retired bridge.
