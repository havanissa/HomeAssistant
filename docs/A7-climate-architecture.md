# Phase A7: Smart Comfort climate — A7.0 foundation and the interfaces for A7.1–A7.3

A7.0 (App **0.10.0**) renames the product to **Smart Comfort** and adds the **Climate** module: configuration,
discovery, room and thermal-zone relationships, live monitoring, history and the observation store that thermal
learning will use. **It observes only.** Nothing in the App can command a thermostat (see §4).

## 1. Rebranding: what changed and what did not

| Changed (user-facing) | Kept (technical identity) |
| --- | --- |
| App name *Smart Comfort*, subtitle *Intelligent Home Automation* (`config.yaml` `name`, `const.APP_NAME`, `APP_SUBTITLE`) | App slug `smart_lighting` (the installed App, its `/data` folder and Supervisor backups) |
| Sidebar panel *Smart Comfort*, icon `mdi:home-automation` | Integration domain `smart_occupancy`, its entity ids and unique ids |
| Brand mark, page title, favicon glyph, onboarding text, App strings | Storage paths and file formats (`smart_lighting.floorplan_backup`, rooms backups, engine stores …) |
| Download file names (`smart-comfort-*.json`) | The CSRF header `X-Smart-Lighting`, every API route and websocket command |
| Docker labels, S6 log lines, docs | The companion integration 1.0.0 (unchanged; HACS name and its cards keep "Smart Occupancy & Lighting") |

Mentions of the Home Assistant integration by its real name ("Smart Occupancy integration", "Smart Occupancy &
Lighting 1.0.0") stay, because that is what Home Assistant shows. Engine-internal values that feed the parity
recordings (for example the hosted config entry title) are unchanged.

## 2. The A7.0 module

Backend (App-only modules; none is in `runtime_manifest.py`, so the 0.9.0 fingerprint question does not arise):

| Module | Role |
| --- | --- |
| `climate_model.py` | Pure rules: configuration schema 1 and validation, units (°C/°F/K; a climate entity's own `temperature_unit` wins), discovery heuristics, reading quality, thermostat views, the room temperature, completeness, change lists |
| `climate_observations.py` | The observation store (append-only daily JSONL), coverage and gaps, Recorder row parsing, chart series |
| `climate.py` | `ClimateService`: configuration storage (revision, atomic write, backups, recovery, import/export), discovery and suggestions, live views, the recorder hooked to Home Assistant's state stream, the Recorder gap fill, rename following |

HTTP: `GET /api/climate` (document), `/overview`, `/discovery`, `/rooms/<area>`, `/rooms/<area>/history?window=`,
`/backups`, `/backups/<id>`, `/export`; administrator + CSRF: `POST /review`, `/save`, `/restore/review`, `/restore`,
`/import/review`, `/import`. Live channel: `{"type": "climate", "what": "config" | "states"}`.

Frontend: `pages/climate.ts` (Overview, Rooms, a room with its history chart), `pages/climate-settings.ts` (General,
Rooms and devices, Thermal zones, Outdoor temperature, Backups; one draft, reviewed before saving),
`climate/api.ts` (types, unit conversion), `climate/chart.ts` (inline SVG).

### 2.1 Configuration (`/data/climate/config.json`, format `smart_comfort.climate`, schema 1)

Documented in `climate_model.py`. Every temperature is stored in °C. Key rules enforced by `validate()`:

* `mode` is `"observing"`; anything else is refused (A7.3 adds a reviewed activation, §5.3).
* Targets within the hard limits 5–30 °C, and `min_target ≤ setback ≤ comfort ≤ max_target` (room overrides are
  checked together with the defaults).
* A thermostat belongs to exactly one room; a primary sensor to one room; a room to at most one thermal zone; a
  zone's `shared_thermostats` must be devices of its own rooms.
* Entity references carry the entity registry id (`uid`), filled by the backend, never by the browser; a thermostat
  carries a `capabilities` snapshot taken when it was saved (shown as "changed since saved" when the device changes).

Presence zones and thermal zones are separate: presence zones live in the lighting store (`zones.ZoneConfig`) and say
where people are; thermal zones live here and say which rooms' heat is related (`single`, `connected`, `open_plan`,
a shared device or circuit). Lighting's shared spaces and Presence's adjacent rooms are offered only as suggestions.

### 2.2 Measurements

* **Room temperature** = the primary sensor; if it is not usable, the mean of the usable secondary sensors; else
  *not measured*. A thermostat's `current_temperature` is a separate measurement ("its own reading") and is never
  used as the room temperature.
* **Quality**: `ok`, `stale` (no report within `stale_minutes`, from `last_reported`), `unavailable`, `unknown`,
  `missing` (no such entity), `not_numeric`, `wrong_unit`, `implausible`, `none` (nothing assigned). The original
  state and unit are always kept.
* **Heating activity** is `reported` only when the entity has `hvac_action`; without it the activity is `unknown /
  not_reported`. A setpoint above the room temperature is shown as a *possible demand*, explicitly labelled as an
  inference and never as "heating".
* **External sensors**: Smart Comfort uses any sensor for its own calculations. It detects a device's own
  external-temperature input (`number.*` on the thermostat's device) as information only and never writes to it.

### 2.3 Observations (`/data/climate/observations`)

```
obs-YYYYMMDD.jsonl   {"t", "e", "k", "s", "u", "v", "q", "a", "src"} per line (see climate_observations.py)
state.json           coverage {entity: {from, to}} and gaps {entity: [[from, to], ...]}
```

* Kinds: `room_temp`, `thermostat` (`a`: `cur`, `tgt`, `lo`, `hi`, `action` (reported or null), `preset`),
  `outdoor`, `opening`, `occupancy` (the companion's room entity, found by unique id), `sun` (`sun.sun`, at most
  every 10 min unless it crosses the horizon), `cover` (the room's curtain covers from the curtain configuration).
* `src`: `live` (a state change), `report` (an unchanged value re-confirmed, at most every `report_minutes`, so a
  silent sensor shows up as missing records), `recorder` (read to fill a gap).
* No polling: values arrive with Home Assistant's state stream; the App's existing 60 s `last_reported` refresh
  delivers unchanged reports.
* Gap fill: at start, after a reconnect and for a newly tracked entity, the time since its last observation is read
  once from `history/history_during_period` in 24-hour windows, at most `backfill_days` (≤ 30) back, 20 entities per
  call, with a pause between calls. Gaps shorter than 2 minutes are ignored. For a newly tracked entity, observation
  starts at its first Recorder value; for an observed entity a window the Recorder no longer has becomes a recorded
  gap. The missed time is recorded as a gap as soon as observation resumes and removed only for the windows the
  Recorder answered (a timeout or a lost connection leaves it). Without the Recorder the current value is stored
  and the downtime stays a gap. Charts break lines and bands at gaps and shade them. A renamed entity's earlier
  records are read under its new id (`aliases` in `state.json`).
* Retention: `retention_days` (default 90) and a 300 MB cap; a line cut short by a power loss is skipped on read.
* Reading: `ObservationStore.read(entity_ids, start, end)` returns each entity's records in the window, preceded by
  its last record before the window (bounded to 31 days per read).

## 3. What A7.0 does not do

No learning, no model, no prediction, no simulation, no preheating, no thermostat command, no change to a device's
configuration (including external-sensor inputs). The comfort / setback / bounds preferences are stored and shown
but used by nothing yet. `prediction.min_history_days` only drives the "days observed" indicator.

## 4. Why no heating command is possible

1. The App's Home Assistant client (`ha.py`) has no service-call command; every websocket command is checked against
   fixed allowlists before it is sent (`ReadOnlyViolation`). Climate uses only reads and
   `history/history_during_period`.
2. The only device path is the companion's fenced executor, whose policy (`protocol.check_command`) accepts
   `light.*`, relay `switch.*`, scene/script/select actions for the lighting owner and `cover.*` for the curtain
   owner. A `climate.*` service is refused for every owner.
3. `tests/test_climate.py::test_no_heating_command_can_be_sent` pins both; the e2e driver
   (`tests/e2e/a7_checks.py`) watches Home Assistant's `call_service` events and compares every thermostat's state
   before and after the run.

A7.3 must therefore add a new, separately fenced climate path to the companion (a new companion release); nothing in
A7.0 has to be undone for it.

## 5. Interfaces for the next phases

These are contracts, not code: A7.0 deliberately ships no placeholder engine.

### 5.1 A7.1 — Thermal learning

Input: the observation store, read through `ObservationStore.read()` (or a thin per-room iterator built on it), plus
the configuration (rooms, zones, primary/secondary sensors, devices, openings, outdoor source).

```python
@dataclass(frozen=True)
class RoomThermalModel:            # per room, versioned, stored in /data/climate/models/<area>.json
    area_id: str
    fitted_at: str
    data_days: float               # usable history the fit used (coverage minus gaps, windows excluded)
    heating_rate_c_per_h: float | None        # while a device REPORTS heating (never inferred periods)
    cooling_rate_per_h: float | None          # Newton's-law coefficient towards the outdoor temperature
    time_constant_h: float | None             # thermal inertia
    outdoor_coefficient: float | None
    solar_gain_c_per_h: float | None          # from sun elevation/azimuth and curtain positions, when observed
    zone_coupling: dict[str, float]           # influence of other rooms of the same thermal zone
    accuracy: dict[str, float]                # MAE/RMSE of one-step-ahead predictions, prequential
    confidence: str                           # insufficient_data | low | moderate | high
```

Rules for the learner: train only on `q == "ok"` records; exclude intervals with an open window/door; treat
`action is null` devices as *unknown activity* (fit their rooms' heating only from setpoint changes with a clearly
labelled lower confidence, or not at all); respect gaps (never interpolate across them); report per-room accuracy;
never write to the configuration. Readiness: `prediction.min_history_days` of coverage.

### 5.2 A7.2 — Prediction and simulation

```python
def predict(room: RoomThermalModel, start_c: float, outdoor_c: list[float], sun: list[float],
            heating_plan: list[bool], horizon_min: int) -> Trajectory   # temperatures with an uncertainty band

def preheat_start(room, target_c, at: datetime, forecast) -> PreheatPlan   # latest start, confidence, energy estimate
```

Comfort targets come from `effective_targets()` plus occupancy (the existing Predictions module's forecasts for the
room). Simulation compares plans (comfort vs. energy) without commanding anything; results are shown, never acted on.

### 5.3 A7.3 — Automatic control

* Activation per room, reviewed (like Lighting's room activation), with `mode` gaining `"automatic"` only through
  that review; the default stays Observing.
* Commands only through a new fenced companion path (`climate` owner, `climate.set_temperature` /
  `climate.set_hvac_mode` of configured thermostats, values clamped to the room's `min_target_c`..`max_target_c` and
  the device's own range from its capability snapshot).
* Manual override: a target set by a person (context not the App's) pauses control of that device for a set time.
* Open-window protection from the configured openings; minimum on/off cycle times; device reconnect and failure
  recovery (verify reported setpoints, bounded retries, then hand back with a notice).
* Every decision logged with its reason, like the lighting and curtain decision logs.

## 6. Tests

* `smart_lighting/tests/test_climate.py` (19): model rules, units, quality, reported vs inferred activity, change
  lists, renames, the observation store (records, gaps, coverage, cut-off lines), the service against the fake Home
  Assistant (fresh install without devices, discovery, reviewed save, stale/invalid/non-admin refusals, live
  readings, history with Recorder backfill, restart with a gap fill, backups/restore/import/export, recovery of a
  damaged file, registry renames, a Fahrenheit home, no heating command).
* `tests/e2e/a7_checks.py` against a real Home Assistant 2026.11.1 (demo climate entities, a generic thermostat,
  template sensors), the companion 1.0.0 and the real App image: phases `all` (52 checks), `restart-check`,
  `app-outage` (a change made while the App was stopped arrives from the Recorder).

## 7. Known limitations of A7.0

* Not verified on Home Assistant OS hardware or on the production installation (never deployed by the developer).
* Discovery heuristics (TRV detection, outdoor and "not a room temperature" hints) are guesses from names, models
  and device classes; the person's assignment always wins.
* The observation store keeps one copy in `/data` (included in Supervisor backups); its backups are not separate.
* A history request re-reads the day files of its window (at most 31 + 2 days); with a large store a 30-day view
  takes noticeably longer than a 24-hour one. A7.1 may add a per-day index if learning needs faster reads.
* `sun.sun` and cover positions are observed only while the App runs or from the Recorder's history; no weather
  forecast is stored.
* The companion's name in HACS and its dashboard cards still say "Smart Occupancy & Lighting" (unchanged on purpose
  in A7.0).
