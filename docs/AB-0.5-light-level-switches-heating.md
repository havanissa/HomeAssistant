# Automation Builder 0.5: Switches & Remotes fix, Light Level Manager, Heating Designer

Three additions on the existing design - room → device → specialised designer → native Home Assistant automation -
with no new runtime engine and no companion integration. Everything goes through the shared rule model, compiler,
conflict check, deployment (plan hash, ownership, external-change detection, rollback) and helper lifecycle of 0.4
([AB-0.4-lighting-designer.md](AB-0.4-lighting-designer.md)); every generated automation keeps working while the App
is stopped.

| Part | Where |
|---|---|
| Switch interactions (model, compile, simulation) | `backend/ab/designers/lighting2.py` (`_switch`, `switches`, `mapping_conditions`), `designers/switches.py` |
| Conflict check: presses of one event entity | `backend/ab/conflicts.py` (`_together`) |
| Interaction list (UI) | `static/lighting.js` (`switchesTab`, handlers `iadd`, `x*`, `fmore`) |
| Light Level Manager (config, inheritance, room automation, twins, observations) | `backend/ab/designers/levels.py` |
| Light Level matrices in the Lighting Designer | `designers/lighting2.py` (`_matrix` with `level`, `columns_of`, `cell_of`, `resolved_views`) |
| Heating profiles and Heating Designer (config, schedule, plan, command, twin, preview) | `backend/ab/designers/heating.py` |
| API | `core.py` (Light Level Manager, heating profiles, `heating_quick`, previews), `web.py` |
| Helper allowlist per domain | `ha.py` (`HELPER_NAMES`, `HELPER_IDS`) |
| Pages | `static/levels.js`, `static/heating.js`, `static/app.js`, `static/index.html`, `static/designer.js` |
| Tests | `tests/test_phase05.py`; repository `tests/automation_builder/test_switches_runtime.py`, `test_levels_runtime.py`, `test_heating_runtime.py`; `tests/e2e/e2e_06_checks.py`, `browser_06_checks.py` |

## 1. Switches & Remotes: the defect and the fix

**Symptom.** A switch could control a light "only once": choosing OFF for a press replaced its ON, so a user had to
choose between ON and OFF.

**Root causes (two).**

1. *Interface.* The 0.4 Switches tab drew one row per interaction and looked up one mapping for it
   (`mappings.find(m => m.interaction === i.id)`); choosing another action **replaced** that mapping's action, and
   removing an interaction removed all of its mappings. Adding the same device again created a second switch card.
2. *Conflict check.* Different presses of one **event entity** (single, double, long … - how ZHA, Zigbee2MQTT and Hue
   remotes appear in Home Assistant 2026) compile to one native state trigger, told apart by the event type. The
   conflict check compared only the native trigger, so *single → ON* and *double → OFF* were reported as a blocking
   **contradiction** (and two toggles on different presses as a "double toggle"): the device could not be deployed.
   Confirmed with the 0.4 code itself (`git 1be713c`): `[('contradiction', '“Lighting · Wall: Single press → on” and
   “Lighting · Wall: Double press → off” can run at the same moment and contradict each other on light.l1 …')]`;
   with 0.5: `[]`.

The rule model, rule ids (designer · switch · mapping), the compiler (one `if` block per mapping, triggers
deduplicated by content, the event type as the block's extra condition) and consolidation were already correct;
the runtime tests confirm it.

**Fix.** The switch card is an **interaction list** (event → action → lights), one line per mapping with its own id:
*+ Add Interaction* (event and action first, nothing saved half-made), enabled/disabled, ↑/↓ (the list order is the
run order), ✕ (that line only), optional *only when …* conditions (state, number, time; all/any). *Find more events
of this device* adds discovered events to an existing switch (already added ones are marked); manual events can be
added to a chosen switch. Discovery records a remote's button/channel (`subtype`). Removing an event that lines use
asks first. `conflicts._together` knows that two event types of one event entity never fire together (an empty list
still means "any"). Latched switches can **ignore changes made by an automation** (`trigger.to_state.context.parent_id
is none`), so a switch that follows its light is not a press (new latched switches get it on; old ones stay as they
were). Duplicate lines on the same event are still reported by the existing conflict check (`duplicate_rule`,
`double_toggle`, `contradiction`), never silently dropped.

**Migration.** None needed: the document only gains keys when used (`conditions`, `button`, `ignore_automations`);
a 0.4 designer validates to the same document and compiles **byte-identically** (golden hashes computed with the
0.4 code in `test_phase05.py`).

## 2. Light Level Manager

`config["light_levels"]` (absent unless configured): `templates`, `matrices` (default Truth/Brightness Matrices over
a template's bands), `default_template`, `default_matrices`, `rooms` (keyed by area id).

* **Bands**: stable ids (`dark`; never changed by a rename; reserved words refused), ordered dark → bright, an
  upper limit for each but the last (`below`, exclusive), strictly increasing: no gaps, no overlaps. 2–8 bands.
* **Settings**: hysteresis (lx), darker/brighter stabilisation (s), `unavailable_seconds` (then `no_reading`; unset =
  keep the last band). Unset = not applied, never a hidden default.
* **Inheritance** (`levels.effective`): template → room override (per band limit and per setting, kept when the
  template changes; overrides of removed bands are kept, reported, unused) → for matrices, the light's own cell.
  Every value carries its source (`global` / `room` / `unset`). A room adopts a template and matrices explicitly
  (new rooms start from the configured standard ones - a deliberate default the user set).
* **Versions**: a template, matrix or room gets `version + 1` only when its content changes; the room automation
  names the template version, so a change shows as "changes not deployed"; the deployment review is the preview.
* **Vehicle device**: each room is deployed as a managed device "Light Level · <room>" with a `light_level`
  designer; its target is the band helper `input_text.ab_<device>_<room>_light_level` created at deployment (and
  `input_number.ab_<device>_<room>_ambient_lux`, created at −1 = "no sample yet", for *off_sample*). It is listed under
  its room on the Overview, cannot be deleted from the Devices page, and its removal removes automation and helpers.
* **The room automation** (one, `queued`): template triggers "brighter than the band" `for: brighter_seconds`,
  "darker" `for: darker_seconds`, "no band yet", Home Assistant start, `automation_reloaded`; a 5-minute check (only
  when the reading has been steady for the delay, or no band is set); "no reading" `for: unavailable_seconds`;
  for *off_sample* the sample rule. The band calculation (`band_jinja`, Python twin `band_index`): with the current
  band *c*, `up` = limits passed by `+h`, `down` = by `−h`; move up when `c < up`, down when `c > down`, else stay.
* **Lights**: a Lighting Designer matrix with `level: {room}` has the room's bands as columns (state columns of the
  helper) and inherited cells (`cell_of`: light → room override → global default matrix); its views name the source
  ("room light level", "global default matrix"). Truth gating, Brightness, Mood, Advanced Rules and overrides work
  as in 0.4; all lights of a room read one band.
* **Ambient estimate**: *reference* (another sensor × factor), *off_sample* (sample after the chosen lights were off
  for the settle time; valid for `max_age_minutes`; `last_reported` keeps a repeated sample fresh), *calibrated*
  (reading − Σ contribution × brightness, ≥ 0). Contributions are typed or taken over from **observations**
  (`levels.Observations`: when a chosen light changes by ≥ 20 % and the room's sensor reports 3–90 s later with no
  other chosen light changed, the step per full brightness is kept, median and spread shown). Active calibration
  (switching lights on purpose) is **not offered**: the App never sends commands. The page shows raw, estimated
  lamp light, the value used, method, measured/estimated, freshness or calibration status.
* **Feedback loops**: hysteresis, stabilisation, the estimate, and a warning when a chosen light's observed
  contribution is at least the hysteresis while the raw reading is used.

## 3. Heating Designer

`config["heating"]`: `templates` (modes `{id, name, temp}`, `frost`), `default_template`, `rooms` (area → template,
overrides). Designer `heating` (rows `thermostats`): `profile.room`, `cells` (thermostat overrides: temperature,
NO ACTION, preset), `modes` (schedule `all` or `weekdays`/`weekend`, entries with a mode or a temperature; or an
entity whose states map to modes), `presence`, `window`, `away`, `override`, `priority`, `when_off`, `hvac_mode`,
`sensor`, `reconcile_minutes`. Built-in templates A–F are quick starts (`heating.quick_start`; illustrative times,
no source chosen); G is the room-profile matrix on the Heating page.

**Compilation**: one automation per thermostat (`queued`): triggers for every moment an input can change (schedule
times with weekday gates, presence/away/window with their delays, the mode helper, the override helper, the thermostat
coming back, a re-evaluation event, the manual-change detector); a `choose` over the layers in priority order (the
first that decides wins; schedule windows are split at midnight and the time before a day's first entry is the
previous day's last); a comparing template (`COMMAND_TEMPLATE`, Python twin `heating.command`) that clamps to
`min_temp`/`max_temp`, rounds to `target_temp_step`, handles single setpoint or a range, presets, OFF, and a thermostat
that is off (left off, or switched on to the chosen heating HVAC mode in the same `set_temperature`), and sends
nothing when within half a step. Manual change = the `temperature` attribute changed **without an automation
context** while the computed setpoint differs (so the thermostat echoing a command is not manual); it turns on the
override helper (and its timer). Overrides end at the next schedule entry, a duration, *resume* (a chosen
button/helper, or the helper turned off) or an event; the evaluation then applies what holds now.

**Safety**: OFF while a window is open requires a way back (`when_off: heat` and an HVAC mode); capabilities are
checked (HVAC modes and presets the device offers, a settable setpoint); a mode unset at every level warns and lets
the next layer decide; labels are sanitised before they reach a template variable.

**Blueprint**: the thermostat, sensors, temperatures, schedule times (shared by triggers and time windows), delays
are inputs. Fixed in 0.5: a controlled entity's input now also covers the references a designer makes to it.

## 4. Test results (2026-11-23, Home Assistant Core 2026.11.1)

"Stack" = the real App image behind the stand-in Supervisor against a real, freshly onboarded Home Assistant Core
2026.11.1 (repository test package + demo). Run C = every suite in order on a fresh instance and a fresh App data
directory, with the final code.

| Suite | Environment | Result |
|---|---|---|
| `automation_builder`: `pytest` (incl. `test_phase05.py`, 49 new; `test_web.py` +1) | unit, fake Home Assistant | 282 passed |
| `scripts/run_tests.py tests/automation_builder` (+ `test_switches_runtime.py` 12, `test_levels_runtime.py` 11, `test_heating_runtime.py` 38) | Home Assistant's parser and engine, real `input_text`/`input_number`/`input_boolean`/`timer`, MQTT | 201 passed (one pre-existing time-of-day failure, below) |
| `e2e_checks.py` / `browser_checks.py` (0.1) | stack | 87 of 87 / 103 of 103 |
| `e2e_02_checks.py` / `browser_02_checks.py` (0.2) | stack | 87 of 87 / 121 of 121 |
| `e2e_03_checks.py` / `browser_03_checks.py` (0.2.1) | stack | 38 of 38 + 1 skipped / 88 of 88 + 5 skipped (below) |
| `e2e_04_checks.py` / `browser_04_checks.py` (0.3) | stack | 43 of 43 / 150 of 150 |
| `e2e_05_checks.py` / `browser_05_checks.py` (0.4; switch steps moved to the interaction list) | stack | 41 of 41 / 195 of 195 |
| `e2e_06_checks.py` (0.5) | stack | 42 of 42 |
| `browser_06_checks.py` (0.5; desktop light and dark, tablet landscape and portrait, phone) | stack | 141 of 141 |
| Smart Occupancy Detection: App / companion | regression | 678 passed / 38 passed |
| Smart Adaptive & Predictive Climate: App / companion | regression | 154 passed / 71 passed |
| Smart Comfort: App / companion / engine | regression | 326 passed, 1 xfailed, 1 known failure / 43 passed / 1261 passed |
| Automation Import/Export Manager: App | regression | 79 passed, 1 environmental |

**Switch defect in the real runtime** (`e2e_06` 101–104): one remote (ZHA events) with four interactions on the same
light - single → ON, double → OFF, triple → toggle, a disabled single → 60 % - saved, no conflict, deployed; Home
Assistant switched the light ON, OFF, ON, OFF (one toggle per press), the disabled line did nothing, and ON/OFF kept
working with the App stopped. In the engine (`test_switches_runtime.py`): ON/OFF, ON/OFF/toggle, five interactions
(on, off, dim while held, release, resume), one event → several compatible actions, one switch → many lights,
several switches → one light, separate buttons, short and long presses, release, Zigbee2MQTT, override and resume
back to the current matrix baseline, *only when* conditions, a latched switch ignoring automation changes, and the
conflict check (ON+OFF on one press blocks, different presses never do). The editor flows (add twice, reorder,
disable, delete one line) are in `browser_06`; migration (byte-identical 0.4 output) and save/reload in
`test_phase05.py`.

**Found and fixed by the tests**: Home Assistant renders automation templates strictly (missing thermostat
attributes are read with `.get`); a new `input_number` reads 0 with a fresh timestamp (the sample helper is created
at −1 = no sample); a blueprint export left a designer's references to a controlled entity fixed (now the entity's
input); the passive observer was called with the wrong arguments (every state event raised an error while a light
level existed); a pre-existing body-reading bug: one stream read can return part of a request, so a larger import
was refused as "not JSON" (now read to the end; test with a body sent in parts).

**Not regressions**: the 0.2.1 suites expect the demo's "Living Room" (Sonos Play:5) device with 9 entities; a freshly
onboarded Home Assistant 2026.11.1 has none (its demo media players have no device; the 0.4 runs used an older
instance whose registry still had it). Those steps now report SKIP with the reason instead of failing, never as
passed. `test_runtime.py::test_or_conditions_and_weekdays` (0.1, unchanged) fails between 07:00 and about 07:30
US/Pacific on weekdays with the 0.4 code too (it moves the frozen clock back to a Saturday 07:00); it passed in the
other runs of the day. Smart Comfort `test_package::test_only_one_app_config_in_repository` (known since the fifth
App) and the Import/Export Manager's `test_the_other_apps_are_untouched` (uncommitted changes of another App: passes
once merged) are known.

## 5. Limitations

* Light levels: band limits are per room, not per light (one band per room keeps all lights consistent; a light's
  own values are its matrix cells). A band changes only on a reading change, the start, a reload or the 5-minute
  check; at deployment the band is set by the reload trigger or the next check. Lights are evaluated when an input
  (the band) changes, as in 0.4.
* The 5-minute check needs the reading's `last_changed` older than the stabilisation delay; a sensor reporting new
  values all the time relies on the template triggers.
* *off_sample* cannot follow daylight while a chosen light stays on (the sample ages out → no reading);
  *calibrated* assumes contributions add linearly with brightness; observations need the sensor to report within
  90 s of a light change.
* Heating: the away delay uses `last_changed` (a change between two "not home" states restarts it); a return mode
  also runs after a Home Assistant restart (the state is new). Manual-change detection sees the `temperature` and
  `target_temp_low` attributes only. Preset cells are offered per thermostat only. No warm-up prediction.
* Not run on Home Assistant OS in this phase.

## 6. Recommended next steps

1. A Home Assistant OS run with real remotes (ZHA event entities, Zigbee2MQTT) and a real TRV.
2. "Press now" capture of a remote's next event in the interaction editor.
3. Per-light band limits (a light with its own threshold) on the same helper mechanism.
4. Cooling profiles (heat/cool ranges) for heat pumps.
