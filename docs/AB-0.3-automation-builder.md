# Automation Builder 0.3 (Phase 0.3): specialised designers

Graphical designers for lights and covers, shared presence sources (rooms, named zones, position zones,
persons), templates, a simulation, and blueprint export of designer configurations. Built on 0.2.1
([AB-0.2-automation-builder.md](AB-0.2-automation-builder.md)); the compiler, the deployment workflow, the
diagnostics and the import were reused, not rewritten.

| Part | Where |
|---|---|
| Designer framework, registry, expansion, capabilities | `backend/ab/designers/__init__.py` |
| Cells, defaults, canonical rule building blocks | `backend/ab/designers/common.py` |
| Lighting Designer | `backend/ab/designers/lighting.py` |
| Cover Designer | `backend/ab/designers/cover.py` |
| Sun geometry (0° = South), templates and their Python twins | `backend/ab/designers/solar.py` |
| Presence sources (room, zone, position, person) | `backend/ab/designers/presence.py` |
| Templates (gallery) | `backend/ab/designers/gallery.py` |
| Simulation | `backend/ab/simulate.py` |
| Interface | `static/designer.js` (designers, matrix editor, gallery, simulation), `static/zones.js` (presence sources) |
| Tests | `tests/test_designers.py` (+ routes in `test_web.py`), `tests/automation_builder/test_designers_runtime.py`, `tests/e2e/e2e_04_checks.py`, `tests/e2e/browser_04_checks.py` |

Smart Comfort, Smart Occupancy Detection and Smart Adaptive & Predictive Climate are not changed (no file of
theirs is touched). Still no companion integration, no service calls, no new websocket command, no `map`.

## 1. Architecture: one model, several interfaces

```
device["designers"] = [ {id: g_xxxxxxxx, kind, version, ...} ]      the source of truth of a designer
config["presence"]  = {sources: {z_xxxxxxxx: {...}}}                 shared by every designer
          │
          ▼  designers.expand(device, presence)        (every analysis, plan, deployment, preview, blueprint)
generated rules: ordinary rules of the rule model (model.validate_rule accepts each one, checked every time)
          + a "generated" entry the compiler ignores (designer, layer, target, column, exclusivity keys)
          │
          ▼  effective device = own rules + generated rules
compiler.compile_device  →  ONE shared automation per device (generated rules never wait)
conflicts.analyse        →  the ordinary conflict check, plus designer conflicts (zone overlaps)
describe / diagnostics / blueprint / simulate  → all on the same effective device
```

* **No second source of truth.** Generated rules are derived each time, never stored or editable as rules. The
  YAML preview is the compiler's output; editing happens in the designer.
* **Stable identities.** A generated rule's id is a hash of (designer id, layer, target, column, mood value);
  bands, entries and sources have their own stable ids. Renaming a light in Home Assistant changes nothing (rule
  names use entity ids and the user's own names).
* **Versioned documents.** Each designer carries `version: 1`. A designer of an unknown kind or a newer version
  (written by a later App) is kept byte for byte, reported, and blocks deployment of its device: never dropped,
  never half-compiled. The configuration document keeps schema 1; `designers` and `presence` are absent unless
  configured, so a 0.2.1 document loads unchanged and compiles byte-identically (golden hashes of 0.1.0 pinned
  in `test_phase02.py` still hold). Downgrading to 0.2.1 would drop the designers at the next save (0.2.1 does
  not know them); the automatic backups keep them.
* **Extensible.** A designer = a module with `KIND`, `VERSION`, `ROWS`, `validate`, `expand`, `resolved_views`,
  `capability_problems` (optionally `index_problems`, `sources_of`), an entry in `REGISTRY`/`APPLICABLE` and an
  editor. Climate, ventilation, buttons/remotes, fans and switches are listed as planned (gallery text only; no
  decorative controls).
* **Limits.** Generated rules are validated with larger structural limits (`GENERATED_LIMITS`: 60 triggers,
  120 conditions, nesting 6) because they combine several sources by nature; a device's designers generate at
  most 300 rules.

### Blueprint inputs of designers

Every value a designer puts into a rule passes the expansion's `v()`/`e()`: normally that registers it as a
*candidate* input (key, group, selector, default) and returns it unchanged; for an exposed key it returns a
`compiler.Input`, which the compiler passes through. Templates the designers generate refer to such inputs
through automation variables, like the compiler's own. So one input stands for one setting wherever it is used
(a band limit, a delay, an entity), and the export's restore check (with every input at its default the
blueprint equals the deployed automation) covers designers too. That check found a key collision during
development (two long keys truncated to the same prefix); keys are now hashed, never truncated.

Stay fixed in a blueprint (named in its warnings): shapes of position zones and the sun curves (inside
templates), thresholds that include a hysteresis (an input cannot be added to another).

## 2. Strict unset and default semantics

| Cell | Stored | Meaning |
|---|---|---|
| Unset | absent | nothing configured; an explicit default applies (row default, then matrix default), else nothing |
| Use default | `{"kind": "default"}` | the default on purpose; without a default: blocking error |
| No action | `{"kind": "none"}` | explicitly nothing; stops default inheritance |
| Off / Close / values | `{"kind": "off"}` … | commands |

`common.resolve` implements the order (cell → row default → matrix default → unset); the backend computes every
label shown (`40% · 2700K`, `OFF`, `DEFAULT: 30%` with its source, `NO ACTION`, `UNSET`). A missing source (an
entity not chosen) blocks deployment with the reason; it is never confused with an unavailable state, which only
makes conditions fail. Nothing in the App chooses an entity, a value or a default: templates turn controls on
and leave everything unset; new designers start without rows; attribute fields are empty (= unchanged) until the
user types a value. The same document goes through save, export/import, backup/restore and compilation.

## 3. Lighting Designer

Layers: mood (lights × mood values), presence (lights × situations), light level (lights × bands), schedule
(windows + timed entries), overrides. The **combination** is explicit:

* exactly one layer **decides** (`primary`); with several layers on and none chosen, deployment is blocked;
* the **mood** is the palette for *Use mood* cells (expanded per mood value, exclusive by construction) and can
  re-apply on change;
* **light level / presence** permit (gates): `allow ON in bands …`, `allow OFF in bands …`, `allow ON while
  situations …`. Gates are conditions only, unless *also act when it changes* adds their entry triggers.

Generated per light and cell: triggers = the situation entering (with its entry/exit delay) + the end of every
situation that dominates it + mood/gate triggers where ticked; conditions = the situation holding (state with
`for`, `fresh != false` where requested), the dominating situations not holding, the mood value, the gates, the
time windows, the override signal and the manual-change pause; action = the cell's command (only attributes that
are set).

Overlapping situations (any two except opposite states of one source) need a policy:

| Policy | Generated |
|---|---|
| priority | a column's rule requires every column to its left with a decided cell (set, defaulted or NO ACTION) not to hold, and re-evaluates when one ends |
| exclusive | the same against every other decided column |
| compatible | no extra conditions; two acting columns with different commands = a blocking (confirmable) conflict |

Light-level bands: a band holds when the value is below its limit and not below the previous one (the brightest
band also requires the sensor to be available); entering a band from below needs the limit plus the
hysteresis, held for the delay. Checks at other moments use the plain limits.

Manual-change pause: a template `states[light].context.parent_id is not none or (now - last_updated) >= N
min` - a change not caused by an automation or script counts as manual (verified in Home Assistant: an
automation's own change carries the parent context; a change with a user context pauses).

Occupancy rechecks (opt-in) use the existing compiler rechecks for situations from binary sensors without
delays: the generated rule then has an `occupancy` trigger and a single basic action, so every safety rule of
0.1 applies.

## 4. Presence sources

`room` (on/off entity; optional "possibly occupied" entity + state), `zone` (own on/off entity), `position`
(x/y of up to three targets, as states or attributes, a rectangle or polygon, optional "(0, 0) = no target",
optional maximum age, units and coordinate notes), `person` (person/device tracker at a geographic zone given by
the user). Sources are shared and referenced by id; a source in use cannot be deleted; references are checked
when the configuration is validated. A shape being drawn is stored and reported as incomplete.

Position zones compile into a native template (ray casting over the targets; the Python twin is
`presence.evaluate_position`, compared with Home Assistant's rendering in the runtime tests). "Inside" (a target
is in the shape), "outside" (readings exist and none is in it) and "no readings" are three different states, so
unavailable data is neither occupied nor vacant. Templates are re-rendered by Home Assistant on state changes (and
each minute when a maximum age uses `now()`). Positions are anonymous; persons come only from person/tracker
entities. Position sensors report x and y as two state changes, so a half-updated reading can count for a
moment: the designer warns when a position zone has no entry/exit delay (found by the runtime test).

## 5. Cover Designer

Conversion (`solar.py`, tested both ways): user orientation 0° = South, clockwise (+90° = West); Home Assistant
azimuth 0° = North, clockwise. `ha = (user + 180) mod 360`, `user = ((ha − 180) mod 360)` in (−180, 180]. The
sector is `[centre − left, centre + right]` in user angles, converted to an azimuth range; a range containing
North becomes an OR of two conditions. Profile angle = `atan2(tan(elevation), cos(sun azimuth − window
azimuth))`.

Per cover, generated rules (each exclusive of the others by construction):

* **sun** — triggers: the sun entering the sector or the elevation range, thresholds entering (light level,
  temperature, weather condition), presence situations starting, time windows opening, the mode changing (when a
  sun policy exists), and for a following response every elevation/azimuth change. Conditions: in sector, in
  elevation range, thresholds, presence, windows, *not* in a mode that wins, wind below the limit / no rain (for
  weather-sensitive covers), override, manual pause, minimum interval, minimum change (`|current_position −
  target| ≥ min`). Action: the fixed position, *close*, or the curve template; tilt likewise.
* **leave** (when configured) — the sun clearly gone (outside the sector/range widened by the hysteresis, below
  the thresholds minus their hysteresis, weather left, presence ended, window closed), not while a mode with a
  policy is active (the mode returns there).
* **fallback** (when configured) — sun or a required sensor unavailable.
* **safety** — weather-sensitive covers with a wind sensor: wind above the limit or rain → close (retract).
* **modes** — per cover and mode with an acting cell: the mode becoming active (selector, or a mapped household
  mood). With *sun wins*, the mode's rule requires "not shading" and also runs when the sun leaves.

Explicit decisions that block until made: the sun policy per mode with a cell for a sun-shaded cover; weather
protection or the user's statement for awnings (device class `awning` is recognised and must be marked);
a minimum change for following positions (plus a minimum interval when the cover does not report its position).
Capabilities (supported features: open, close, set position, set tilt; `current_position`) are checked against
Home Assistant; unsupported controls are not offered.

## 6. Conflicts and consolidation

All generated rules finish at once, so a device stays one automation (`queued`), however many matrices and
modes it has; waiting ordinary rules keep their own automations as in 0.2. The conflict check now also knows
rules that exclude each other by construction: two values of one exclusivity key (two moods of one selector, two
bands of one sensor, the sun rule versus its leave/fallback rule), and `X` versus `none of (X)` / `not (all of
Y)` at the top level. Designer conflicts (*zone_overlap*) are listed with the ordinary ones, can be confirmed like
them, and their confirmations survive saves. A designer and ordinary rules acting on the same light are named
(warning); when they can act at the same moment, the ordinary conflict check requires a decision. Nothing gives a
designer priority over ordinary rules.

## 7. Simulation

`simulate.run(effective device, scenario, evaluators)`: one chosen trigger of the automation (by its native
content), the states (live, or typed and marked *test*), time and weekday. Three-valued conditions (unknown for
user templates, sun and device conditions), Home Assistant's semantics for `for` (strictly longer), numeric
`above`/`below` (strict), time ranges across midnight. Designer templates are evaluated by their Python twins.
The result lists the started rules (runs / blocked with the failing conditions / unknown), each target's final
command, conflicts and missing inputs. No request is made to Home Assistant. The runtime test
`test_simulation_predicts_what_home_assistant_does` compares simulation and engine for every combination of
zones, mood and light level; `e2e_04_checks.py` checks one prediction against the real Home Assistant.

## 8. Test results (2026-11-22, Home Assistant Core 2026.11.1)

"Stack" is the real App image behind the stand-in Supervisor against a real, disposable Home Assistant Core (the
test package `automation_builder/tests/e2e/ha/` now also has zone, radar, wind, rain and awning drivers). All
eight end-to-end and browser suites ran in order on a fresh stack.

| Suite | Environment | Result |
|---|---|---|
| `automation_builder`: `pytest` (incl. `test_designers.py`, 40 new, and the new routes in `test_web.py`) | unit, fake Home Assistant | 201 passed |
| `scripts/run_tests.py tests/automation_builder` (incl. `test_designers_runtime.py`, 13 new) | Home Assistant's own parser and engine | 58 passed |
| `e2e_checks.py` / `browser_checks.py` (0.1 scenario) | stack, fresh installation | 87 of 87 / 103 of 103 |
| `e2e_02_checks.py` / `browser_02_checks.py` | stack | 87 of 87 / 121 of 121 |
| `e2e_03_checks.py` / `browser_03_checks.py` | stack | 39 of 39 / 93 of 93 |
| `e2e_04_checks.py` (designers: lighting with mood × zones × light level, a position zone, covers against the real sun, an awning's protection, simulation vs. reality, App stopped, blueprint) | stack | 43 of 43 |
| `browser_04_checks.py` (gallery, matrix select/edit-many/copy/paste/clear/defaults, simulation, YAML, drawn position zone, compass; desktop light and dark, tablet landscape and portrait, phone) | stack | 150 of 150 |
| Smart Occupancy Detection: App / companion | regression | 678 passed / 38 passed |
| Smart Comfort: App / companion / engine | regression | 327 passed, 1 xfailed (as before) / 43 passed / 1261 passed |
| Smart Adaptive & Predictive Climate: App / companion | regression | 154 passed / 71 passed |

Runtime tests in Home Assistant's engine: the combined mood × presence × light-level scenario (confirmation delay,
re-apply on mood change, priority between zones, a zone vacant while the room is occupied, the light-level
permission, stale information ignored, delayed vacancy); unset cells, row and matrix defaults, *No Action*;
"nothing while ambiguous"; a position zone template compared with its Python twin for six reading sets, and
lights driven by it; the manual-change pause (an automation's own change carries a parent context, a change by a
user pauses); the sun-curve templates against Python for 4 orientations × 36 sun positions × 2 bases; the
orientation convention; the previews' sun path within 0.5° of astral (which `sun.sun` uses); cover tracking with
the minimum change, Privacy winning over the sun, Daylight losing to it and returning after the sun left, a west
window entering its sector; an awning retracted by wind and rain and not extended while windy; a designer
blueprint instantiated with another lamp and mood selector; and the simulation compared with the engine for every
combination of two zones, two moods and three light levels.

Found and fixed by the tests during the phase: two blueprint input keys could collide (truncated instead of
hashed); a polygon being drawn could not be saved before its third point; on the Presence page a source's kind
changed right after its name created a second source (the snapshot of a queued save was taken too early); a
half-updated position reading counts for a moment (now a warning recommends an entry/exit delay). The tests also
showed two properties worth knowing, now explained in the interface: a matrix default fills every unset cell
(also in zone columns), and with priority a set or defaulted cell (or *No Action*) in a left column keeps the
columns to its right from acting on that light.

Changed behaviour (intended): adding a device with lights or covers opens the template gallery; its card *Write
a rule* leads to the rule editor as before (the 0.1-0.2.1 browser suites now go through it).

## 9. Known limitations

* Weather *forecasts* are not used: they need a service call with a response; the current condition of a
  weather entity is. Hysteresis-derived thresholds, shapes and sun curves stay fixed in blueprints.
* Hysteresis applies to the moments a light-level band or the sun is entered or left; checks at other moments
  (someone enters a room) use the plain limits.
* Person sources: the exit delay is applied to the trigger only (Home Assistant has no "not X for N" condition).
* Position zones: up to three targets per source; shapes are drawn in the sensor's coordinates (no floor plan,
  no rotation/calibration transform); ESPHome-style radars that report (0, 0) for "no target" need the option.
* Timed entries act at their time regardless of the gates (only the override stops them).
* The manual-change pause treats every state after a Home Assistant restart as changed by hand.
* Not run on Home Assistant OS in this phase (the changes use no new interface of Home Assistant; deployment and
  blueprint installation are the paths validated on Home Assistant OS in 0.2.0).
* Climate, ventilation, button/remote, fan and switch designers are not built (planned; the rule editor covers
  those devices).
* A 0.2.1 App would drop the designers of a 0.3 configuration at its next save (restore from the backups).

## 10. Recommended next phase

1. Climate and ventilation designers on the same framework (temperature schedules per room, humidity/CO₂ fan
   control), and a button/remote mapping designer with "press now" capture.
2. Floor-plan view for position zones with rotation/offset calibration of each sensor, and zone presets for
   common radars.
3. Mood transitions (fade between mood settings) and per-mood scenes as an optional output.
4. Blueprint designer: groups and order of inputs, and blueprint import into designers.
5. A Home Assistant OS run of 0.3 with real covers and a real radar.
